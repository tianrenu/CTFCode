"""运行模型自己编写的离线 Python 静态分析脚本，不包含题目解码算法。

保守 AST/能力白名单 + 独立子进程 + Windows Job 限额；不是通用 Python 沙箱。
没有 shell、网络、任意文件访问、pip、附件执行或用户指定输出路径。
"""
from __future__ import annotations

import ast
import builtins
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import types

MODULES = {
    "base64": "b64decode b64encode standard_b64decode standard_b64encode urlsafe_b64decode urlsafe_b64encode b32decode b32encode b16decode b16encode b85decode b85encode a85decode a85encode",
    "binascii": "hexlify unhexlify a2b_hex b2a_hex a2b_base64 b2a_base64 crc32 crc_hqx Error",
    "struct": "pack unpack unpack_from iter_unpack calcsize error",
    "hashlib": "md5 sha1 sha224 sha256 sha384 sha512 blake2b blake2s",
    "zlib": "compress decompress crc32 adler32 error",
    "gzip": "compress decompress",
    "json": "loads dumps JSONDecodeError",
    "re": "search findall finditer match fullmatch split sub escape compile IGNORECASE MULTILINE DOTALL ASCII I M S A",
    "itertools": "product permutations combinations combinations_with_replacement chain islice repeat cycle zip_longest accumulate pairwise",
    "collections": "Counter defaultdict deque",
    "math": "ceil floor sqrt log log2 log10 gcd lcm prod factorial isqrt comb perm pi e inf",
    "capstone": "cs_disasm_lite CS_ARCH_X86 CS_ARCH_ARM CS_ARCH_ARM64 CS_MODE_16 CS_MODE_32 CS_MODE_64 CS_MODE_ARM CS_MODE_THUMB CS_MODE_LITTLE_ENDIAN CS_MODE_BIG_ENDIAN",
}
METHODS = set("encode decode hex fromhex to_bytes from_bytes bit_length bit_count join split rsplit splitlines strip lstrip rstrip replace translate maketrans startswith endswith find rfind index rindex count lower upper casefold capitalize title swapcase isascii isalpha isdigit isalnum isspace isprintable ljust rjust center zfill partition rpartition removeprefix removesuffix append extend insert pop remove clear copy reverse sort get items keys values update setdefault fromkeys add discard union intersection difference symmetric_difference issubset issuperset isdisjoint group groups groupdict start end span findall finditer search match fullmatch sub split hexdigest digest update most_common elements total rotate appendleft popleft real imag numerator denominator".split())
ATTRIBUTES = METHODS | {x for names in MODULES.values() for x in names.split()}
SAFE_BUILTINS = "abs all any ascii bin bool bytearray bytes chr complex dict divmod enumerate filter float frozenset hex int isinstance issubclass iter len list map max min next oct ord pow range repr reversed round set slice sorted str sum tuple zip Exception ValueError TypeError KeyError IndexError RuntimeError StopIteration UnicodeDecodeError ZeroDivisionError AssertionError"
MAX_SOURCE, MAX_OUTPUT = 30000, 12000


def specifications(profile):
    specs = dict(MODULES)
    attrs = set(ATTRIBUTES)
    if profile == "reverse":
        specs.update({
            "pefile": "PE DIRECTORY_ENTRY MACHINE_TYPE",
            "capstone": "Cs CS_ARCH_X86 CS_MODE_16 CS_MODE_32 CS_MODE_64 CS_ARCH_ARM CS_ARCH_ARM64 CS_MODE_ARM CS_MODE_THUMB CS_MODE_LITTLE_ENDIAN CS_MODE_BIG_ENDIAN",
            "unicorn": "Uc UcError UC_ARCH_X86 UC_MODE_16 UC_MODE_32 UC_MODE_64 UC_PROT_ALL UC_PROT_READ UC_PROT_WRITE UC_PROT_EXEC UC_HOOK_CODE UC_HOOK_BLOCK UC_HOOK_MEM_READ UC_HOOK_MEM_WRITE UC_HOOK_MEM_UNMAPPED UC_HOOK_INTR UC_HOOK_INSN UC_HOOK_INSN_INVALID",
        })
        constants = importlib.import_module("unicorn.x86_const")
        specs["unicorn.x86_const"] = " ".join(x for x in dir(constants) if x.startswith(("UC_X86_REG_", "UC_X86_INS_")))
        attrs.update(x for names in specs.values() for x in names.split())
        attrs.update("x86_const sections PE_TYPE OPTIONAL_HEADER FILE_HEADER DOS_HEADER DATA_DIRECTORY DIRECTORY_ENTRY_IMPORT DIRECTORY_ENTRY_EXPORT AddressOfEntryPoint ImageBase SizeOfImage SizeOfHeaders SectionAlignment FileAlignment NumberOfRvaAndSizes DllCharacteristics Subsystem Magic Machine NumberOfSections TimeDateStamp e_lfanew e_magic VirtualAddress PointerToRawData SizeOfRawData Misc_VirtualSize Characteristics Name Size dll imports name ordinal symbols address size bytes mnemonic op_str detail skipdata operands type reg imm mem base index scale disp get_data get_memory_mapped_image get_offset_from_rva get_rva_from_offset get_section_by_rva get_section_by_offset get_string_at_rva parse_data_directories disasm disasm_lite mem_map mem_unmap mem_protect mem_read mem_write reg_read reg_write emu_start emu_stop hook_add hook_del mem_regions".split())
    elif profile != "standard":
        raise ValueError("未知执行能力配置")
    return specs, attrs


def reverse_libraries(specs):
    """只包裹通用库的危险入口；没有题目结构、算法、地址、密钥或解题脚本。"""
    pe = importlib.import_module("pefile")
    uc = importlib.import_module("unicorn")
    def parse_pe(*, data, fast_load=False):
        if not isinstance(data, (bytes, bytearray)) or len(data) > 2 * 1024**2:
            raise ValueError("PE解析只接受<=2MiB的数据，不能指定主机文件路径")
        return pe.PE(data=bytes(data), fast_load=bool(fast_load))
    class Emulator:
        def __init__(self, arch, mode):
            self._uc = uc.Uc(arch, mode)
            self._mapped = 0
        def mem_map(self, address, size, perms=7):
            if type(size) is not int or size <= 0 or self._mapped+size > 128*1024**2:
                raise ValueError("仿真映射总量最多128MiB")
            self._uc.mem_map(address, size, perms)
            self._mapped += size
        def mem_unmap(self, address, size):
            self._uc.mem_unmap(address, size)
            self._mapped = max(0, self._mapped-size)
        def mem_protect(self, *args):
            return self._uc.mem_protect(*args)
        def mem_read(self, address, size):
            if not 0 <= size <= 8*1024**2:
                raise ValueError("单次仿真读取最多8MiB")
            return self._uc.mem_read(address, size)
        def mem_write(self, address, data):
            if len(data) > 8*1024**2:
                raise ValueError("单次仿真写入最多8MiB")
            return self._uc.mem_write(address, bytes(data))
        def reg_read(self, *args):
            return self._uc.reg_read(*args)
        def reg_write(self, *args):
            return self._uc.reg_write(*args)
        def emu_start(self, begin, until, timeout=0, count=0):
            return self._uc.emu_start(begin, until, timeout=min(timeout or 5000000,5000000), count=min(count or 5000000,5000000))
        def emu_stop(self):
            return self._uc.emu_stop()
        def hook_add(self, hook_type, callback, user_data=None, begin=1, end=0, *extra):
            def bounded_callback(original, *rest):
                return callback(self, *rest)
            return self._uc.hook_add(hook_type, bounded_callback, user_data, begin, end, *extra)
        def hook_del(self, hook):
            return self._uc.hook_del(hook)
        def mem_regions(self):
            return self._uc.mem_regions()
    return parse_pe, Emulator


def validate(source, profile="standard"):
    if not isinstance(source, str) or not 1 <= len(source) <= MAX_SOURCE:
        raise ValueError("source须为1..30000字符的Python源码")
    specs, attributes = specifications(profile)
    tree = ast.parse(source, filename="model_script.py", mode="exec")
    if sum(1 for _ in ast.walk(tree)) > 10000:
        raise ValueError("脚本AST过大")
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor,
                             ast.AsyncWith, ast.With, ast.Global, ast.Nonlocal)):
            raise ValueError("受限脚本不支持class/async/with/global/nonlocal")
        if isinstance(node, ast.Name) and node.id != "_" and node.id.startswith("_"):
            raise ValueError("禁止私有名及解释器内部访问: " + node.id)
        if isinstance(node, (ast.FunctionDef, ast.arg)):
            name = node.name if isinstance(node, ast.FunctionDef) else node.arg
            if name != "_" and name.startswith("_"):
                raise ValueError("禁止私有函数名/参数名")
        if isinstance(node, ast.Attribute):
            if node.attr not in attributes or not (isinstance(node.ctx, ast.Load) or profile == "reverse" and node.attr in {"detail", "skipdata"} and isinstance(node.ctx, ast.Store)):
                raise ValueError("属性不在静态分析白名单: " + node.attr)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module]
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("不允许相对导入")
            if any(n not in specs for n in names):
                raise ValueError("仅允许已授权库: " + ", ".join(specs))
            for alias in node.names:
                if (alias.asname or alias.name).startswith("_") or alias.name == "*":
                    raise ValueError("禁止私有或星号导入")
                if isinstance(node, ast.ImportFrom) and alias.name not in specs[node.module].split():
                    raise ValueError("导入符号未授权")
    return tree


def child():
    payload = json.loads(sys.stdin.read(300000))
    root = Path(payload["root"]).resolve()
    source = payload["source"]
    profile = payload.get("profile", "standard")
    if profile in {"standard","reverse"}:
        for path in payload.get("library_paths", []):
            sys.path.append(path)
    tree = validate(source, profile)
    specs, _ = specifications(profile)
    artifacts = []
    derived = Path(payload.get("derived_root", str(root))).resolve()
    def save_bytes(label, data):
        if profile != "reverse" or not isinstance(label, str) or not 1 <= len(label) <= 100:
            raise ValueError("保存派生数据须使用<=100字符标签")
        if not isinstance(data, (bytes, bytearray)) or len(data) > 8*1024**2 or len(artifacts) >= 16:
            raise ValueError("派生数据单份最多8MiB/每次最多16份")
        derived.mkdir(parents=True, exist_ok=True)
        if sum(p.stat().st_size for p in derived.glob("*.bin")) + len(data) > 256*1024**2:
            raise ValueError("本题派生数据总量达到256MiB上限")
        digest = hashlib.sha256(data).hexdigest()
        target = derived / (digest + ".bin")
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError("已有派生数据哈希不匹配，拒绝覆盖")
        else:
            target.write_bytes(data)
        item = {"artifact_id": digest, "bytes": len(data), "label": label}
        artifacts.append(item)
        return item
    def read_artifact(artifact_id):
        if not isinstance(artifact_id, str) or len(artifact_id) != 64 or any(c not in "0123456789abcdef" for c in artifact_id):
            raise ValueError("派生数据ID必须为SHA256")
        target = (derived / (artifact_id + ".bin")).resolve()
        if not target.is_relative_to(derived) or not target.is_file() or target.stat().st_size > 8*1024**2:
            raise ValueError("派生数据不存在或越界")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_id:
            raise ValueError("派生数据哈希核验失败")
        return data
    outputs, size = [], 0
    def safe_print(*values, sep=" ", end="\n"):
        nonlocal size
        if not isinstance(sep, str) or not isinstance(end, str):
            raise ValueError("print仅支持str sep/end")
        text = sep.join(str(v) for v in values) + end
        left = MAX_OUTPUT - size
        if left > 0:
            outputs.append(text[:left])
            size += min(left, len(text))
        if len(text) > left:
            raise ValueError("stdout达到12000字符上限，请缩小输出")
    def read_bytes(path):
        if not isinstance(path, str) or len(path) > 512:
            raise ValueError("path须为题目根目录相对路径")
        rel = Path(path.replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or ":" in path:
            raise ValueError("拒绝越界路径")
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.stat().st_size > 2 * 1024**2:
            raise ValueError("只可读取干净输入内不超过2MiB的文件")
        return target.read_bytes()
    def read_text(path, encoding="utf-8"):
        if encoding not in {"utf-8", "ascii", "latin-1", "utf-16le"}:
            raise ValueError("不支持的文本编码")
        return read_bytes(path).decode(encoding)
    modules = {}
    for name, names in specs.items():
        original = importlib.import_module(name)
        modules[name] = types.SimpleNamespace(**{x: getattr(original, x) for x in names.split()})
    if profile == "standard":
        cs=importlib.import_module("capstone")
        def static_disasm(code, arch, mode, offset, count=0):
            if not isinstance(code,(bytes,bytearray)) or len(code)>65536:
                raise ValueError("静态反汇编只接受<=64KiB字节数据，不接受主机路径或内存指针")
            if type(arch) is not int or arch not in {cs.CS_ARCH_X86,cs.CS_ARCH_ARM,cs.CS_ARCH_ARM64}:
                raise ValueError("静态反汇编架构仅支持X86/ARM/ARM64")
            if type(mode) is not int or type(offset) is not int or not 0<=offset<2**64 or type(count) is not int or not 0<=count<=1024:
                raise ValueError("静态反汇编地址/模式/条数无效，单次最多1024条")
            return list(cs.cs_disasm_lite(arch,mode,bytes(code),offset,count=count or 1024))
        modules["capstone"].cs_disasm_lite=static_disasm
    if profile == "reverse":
        pe_factory, emulator = reverse_libraries(specs)
        modules["pefile"].PE = pe_factory
        modules["unicorn"].Uc = emulator
        modules["unicorn"].x86_const = modules["unicorn.x86_const"]
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in modules:
            raise ValueError("未授权导入")
        return modules[name] if fromlist or "." not in name else modules[name.split(".")[0]]
    safe = {x: getattr(builtins, x) for x in SAFE_BUILTINS.split()}
    def primitive_type(value):
        # 保留type(x)对普通数据的兼容性，但不开放type元类/动态类构造。
        actual = type(value)
        if actual not in (bool, int, float, complex, str, bytes, bytearray, list, tuple,
                           dict, set, frozenset, range, slice, type(None)):
            raise ValueError("type只支持普通数据，不支持函数/类/模块反射")
        return actual
    safe.update(print=safe_print, __import__=safe_import, type=primitive_type)
    environment = {"__builtins__": safe, "read_bytes": read_bytes, "read_text": read_text}
    if profile == "reverse":
        environment.update(save_bytes=save_bytes, read_artifact=read_artifact)
        def api_info(obj, prefix=""):
            # 仅给真实且已授权的公共API名称，不开放getattr/eval或私有解释器访问。
            module_name = obj if isinstance(obj,str) and obj in modules else None
            if module_name is not None:obj=modules[module_name]
            if not isinstance(prefix,str) or len(prefix)>80 or prefix.startswith("_"):
                raise ValueError("API查询前缀无效")
            _, allowed = specifications("reverse")
            names=sorted(n for n in dir(obj) if n in allowed and not n.startswith("_") and n.startswith(prefix))
            return {"type": type(obj).__name__, "module":module_name,
                    "available_allowed_attributes": names[:100],"total":len(names),"truncated":len(names)>100,
                    "notice":"只读SDK接口说明，不是算法或解题提示；不要臆造不存在的方法名。"}
        environment["api_info"] = api_info
        def readonly_dir(obj):
            return api_info(obj)["available_allowed_attributes"]
        def readonly_getattr(obj, name, *default):
            _, allowed = specifications("reverse")
            if not isinstance(name,str) or name.startswith("_") or name not in allowed or len(default)>1:
                raise ValueError("动态属性只允许已授权的公共字段/方法，禁止私有或解释器内部属性")
            return getattr(obj,name,*default)
        def readonly_hasattr(obj,name):
            _, allowed = specifications("reverse")
            return isinstance(name,str) and name in allowed and not name.startswith("_") and hasattr(obj,name)
        safe.update(dir=readonly_dir,getattr=readonly_getattr,hasattr=readonly_hasattr)
    result = {"ok": True}
    try:
        exec(compile(tree, "model_script.py", "exec"), environment, environment)
    except BaseException as exc:
        result.update(ok=False, error=type(exc).__name__ + ": " + str(exc)[:500])
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "model_script.py"]
        result["error_type"] = type(exc).__name__
        if frames:
            line = frames[-1].lineno
            result["line"] = line
            result["source_line"] = source.splitlines()[line - 1][:500]
    result["stdout"] = "".join(outputs)
    if profile == "reverse":
        result["artifacts"] = artifacts
    print(json.dumps(result, ensure_ascii=True))


def assign_job(process):
    """子进程开始读源码前绑定384MiB/单进程/关闭即杀 Job；失败则拒绝执行。"""
    if os.name != "nt":
        raise RuntimeError("本执行器当前仅验证Windows Job边界")
    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(x, ctypes.c_ulonglong) for x in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = EXTENDED()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.ProcessMemoryLimit = 384 * 1024**2
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not k.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        return k, handle
    except BaseException:
        k.CloseHandle(handle)
        raise


def run_script(source, root, out, timeout=12, profile="standard"):
    # 校验拒绝仍由调用方写入审计；绝不执行未通过白名单的源码。
    validate(source, profile)
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    script = out / ("model-" + digest[:16] + ".py")
    script.write_text(source, encoding="utf-8")
    start = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    p = subprocess.Popen([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--child"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=out, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    handle, kernel = None, None
    try:
        kernel, handle = assign_job(p)
        payload = {"source": source, "root": str(Path(root).resolve()), "profile": profile}
        if profile == "reverse":
            payload["derived_root"] = str(out.parent / "模型派生数据")
            payload["library_paths"] = sorted({str(Path(importlib.util.find_spec(name).origin).parent.parent if name in {"capstone", "unicorn"} else Path(importlib.util.find_spec(name).origin).parent) for name in ("pefile", "capstone", "unicorn")})
        else:
            payload["library_paths"] = [str(Path(importlib.util.find_spec("capstone").origin).parent.parent)]
        stdout, stderr = p.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout)
        if len(stdout) > 100000 or len(stderr) > 12000:
            result = {"ok": False, "error": "进程输出超过上限"}
        elif p.returncode:
            result = {"ok": False, "error": "子进程失败/触发内存限制", "stderr": stderr.decode("utf-8", "replace")[:2000]}
        else:
            result = json.loads(stdout.decode("utf-8"))
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        result = {"ok": False, "error": "脚本超过时间限制", "error_type": "ScriptTimeout", "timeout_s": timeout}
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
        if handle:
            kernel.CloseHandle(handle)
    result.update(source_sha256=digest, script_file=script.name, elapsed_s=round(time.monotonic() - start, 3), exit_code=p.returncode)
    if not result.get("ok"):
        result["repair_required"] = True
        result["next_action"] = "请自行检查错误及行号，修改源码后再调用run_python；执行器不代写算法。不要原样重试失败脚本。"
    (out / (script.stem + ".result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def validation_diagnostic(exc, source, profile):
    """拒绝执行时也给行号和接口自查方式；不代写/自动修复模型源码。"""
    result={"ok":False,"error_type":type(exc).__name__,"error":str(exc)[:1200],"repair_required":True}
    line=getattr(exc,"lineno",None)
    if isinstance(exc,SyntaxError):
        result.update(category="python_syntax",column=exc.offset,
                      next_action="先修正这一行的Python语法/缩进，再执行；不能把问号或伪代码作为源码。执行器未修改源码。")
    elif isinstance(exc,ValueError) and "属性不在静态分析白名单:" in str(exc):
        attr=str(exc).split(":",1)[1].strip()
        result.update(category="unsupported_api",rejected_attribute=attr,
                      next_action=("不要继续猜属性名。先用短脚本api_info(obj)查询实际对象；模块可api_info('pefile')或api_info('unicorn')。不开放inspect/私有反射。"
                                   if profile=="reverse" else "此属性不在普通配置的计算接口中；核对工具说明，不原样重试，不扩大权限。"))
        try:
            line=next((n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Attribute) and n.attr==attr),None)
        except (SyntaxError,TypeError):pass
    else:
        result.update(category="execution_policy",next_action="先核对工具说明和真实允许的接口；未执行未通过校验的源码，不扩大权限。")
    if line is not None and isinstance(source,str) and 1<=line<=len(source.splitlines()):
        result.update(line=line,source_line=source.splitlines()[line-1][:500])
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["--child"]:
        child()
    elif len(sys.argv) == 4 and sys.argv[1] in {"--tool", "--tool-reverse"}:
        # 仅由受信任插件提供root/out；源码来自模型，沿用原AST/Job/超时边界。
        request={}
        profile="reverse" if sys.argv[1]=="--tool-reverse" else "standard"
        try:
            request = json.loads(sys.stdin.read(200000))
            # 能力只由受信任启动参数选择；模型提交的JSON不能升级profile。
            result = run_script(request["source"], Path(sys.argv[2]), Path(sys.argv[3]), timeout=12,
                                profile=profile)
        except Exception as exc:
            result = validation_diagnostic(exc,request.get("source") if isinstance(request,dict) else None,profile)
        print(json.dumps(result, ensure_ascii=False))
