"""OpenCode运行参数与资源保护；不含上下文基准或赛题执行。"""
from __future__ import annotations
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time
import psutil
import requests
import opencode_engine as runtime
BASE=runtime.BASE
URL=runtime.URL
GIB=1024**3
MIB=1024**2

def _commit_bytes(total_pages, limit_pages, page_size):
    if not 0 <= total_pages <= limit_pages or limit_pages <= 0 or page_size <= 0:
        raise ValueError("Windows提交内存计数无效，不冒充0占用")
    return {"used_bytes":total_pages*page_size,"limit_bytes":limit_pages*page_size,
            "available_bytes":(limit_pages-total_pages)*page_size,
            "scope":"system_commit_not_free_physical_ram"}


def windows_commit_info():
    """只读Windows系统commit；物理RAM空闲不代表提交额度仍充足。"""
    if os.name != "nt":raise OSError("Windows提交内存计数仅支持Windows")
    class Info(ctypes.Structure):
        _fields_=[("cb",ctypes.c_ulong)]+[(name,ctypes.c_size_t) for name in (
            "CommitTotal","CommitLimit","CommitPeak","PhysicalTotal","PhysicalAvailable",
            "SystemCache","KernelTotal","KernelPaged","KernelNonpaged","PageSize")]+[
            (name,ctypes.c_ulong) for name in ("HandleCount","ProcessCount","ThreadCount")]
    data=Info();data.cb=ctypes.sizeof(data)
    dll=ctypes.WinDLL("psapi.dll",use_last_error=True)
    query=dll.GetPerformanceInfo;query.argtypes=[ctypes.POINTER(Info),ctypes.c_ulong];query.restype=ctypes.c_int
    if not query(ctypes.byref(data),data.cb):raise ctypes.WinError(ctypes.get_last_error())
    return _commit_bytes(data.CommitTotal,data.CommitLimit,data.PageSize)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def launch_argv(window, profile, kv="int8", reserve_mib=None, prefill=None):
    if window not in (32, 64, 128, 256):
        raise ValueError("上下文窗口只允许32/64/128/256Ki tokens")
    if kv not in ("int8", "q4_0", "k8v4"):
        raise ValueError("本轮KV格式不在已安装引擎支持的测试允许列表")
    argv = json.loads((BASE / "当前启动参数.json").read_text(encoding="utf-8"))
    if prefill is not None:
        if prefill not in (128,256,512):
            raise ValueError("复测只允许维持或缩小已用prefill512批量")
        positions=[i for i,v in enumerate(argv) if v.startswith("prefill=")]
        if len(positions)!=1:
            raise ValueError("原prefill项不唯一，拒绝猜测")
        argv[positions[0]]=f"prefill={prefill}"
    if reserve_mib is not None:
        if not 1344 <= reserve_mib <= 3072:
            raise ValueError("复测只允许维持或提高显存安全预留，不通过降低余量强行加载")
        positions = [i for i, v in enumerate(argv) if v.startswith("vram-reserve-mib=")]
        if len(positions) != 1:
            raise ValueError("原配置显存预留项不唯一，拒绝猜测")
        argv[positions[0]] = f"vram-reserve-mib={reserve_mib}"
    argv[argv.index("--context") + 1] = f"{window}k"
    if "--kv" in argv:
        argv[argv.index("--kv") + 1] = kv
    else:
        argv += ["--kv", kv]
    if "--save-preset" in argv:
        argv[argv.index("--save-preset") + 1] = profile
    else:
        argv += ["--save-preset", profile]
    return argv


class GPU:
    """使用驱动自带NVML，只读采样整卡；不能把整卡使用当作模型独占显存。"""
    class Memory(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    def __init__(self):
        self.dll = ctypes.CDLL(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/nvml.dll"))
        self.handle = ctypes.c_void_p()
        if self.dll.nvmlInit_v2() or self.dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.handle)):
            raise RuntimeError("无法初始化NVML；未启动无GPU资源观测的压力测试")
        self.dll.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.Memory)]
        self.dll.nvmlDeviceGetTemperature.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]

    def sample(self):
        memory = self.Memory()
        if self.dll.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(memory)):
            raise RuntimeError("NVML内存采样失败")
        temperature = ctypes.c_uint()
        code = self.dll.nvmlDeviceGetTemperature(self.handle, 0, ctypes.byref(temperature))
        return {"gpu_used_bytes": memory.used, "gpu_free_bytes": memory.free,
                "gpu_total_bytes": memory.total, "gpu_temperature_c": temperature.value if code == 0 else None}


class WindowsGpuMemory:
    """只读WDDM进程内存计数；共享分配包含合法映射，不等同于显存溢出。"""
    class Value(ctypes.Structure):
        _fields_ = [("status", ctypes.c_uint32), ("large", ctypes.c_longlong)]

    class Item(ctypes.Structure):
        pass

    Item._fields_ = [("name", ctypes.c_wchar_p), ("value", Value)]

    def __init__(self):
        if os.name != "nt":
            raise OSError("WDDM计数仅支持Windows")
        self.dll = ctypes.WinDLL("pdh")
        self.query = ctypes.c_void_p()
        self.counters = {}
        signatures = {
            "PdhOpenQueryW": ([ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]),
            "PdhAddEnglishCounterW": ([ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]),
            "PdhCollectQueryData": ([ctypes.c_void_p]),
            "PdhGetFormattedCounterArrayW": ([ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]),
            "PdhCloseQuery": ([ctypes.c_void_p]),
        }
        for name, args in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, ctypes.c_uint32
        self._check(self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query)))
        try:
            for label in ("Dedicated", "Shared"):
                handle = ctypes.c_void_p()
                self._check(self.dll.PdhAddEnglishCounterW(self.query,
                    rf"\GPU Process Memory(*)\{label} Usage", 0, ctypes.byref(handle)))
                self.counters[label.lower()] = handle
        except Exception:
            self.close()
            raise

    @staticmethod
    def _check(code):
        if code:
            raise OSError(f"PDH计数不可用: 0x{code:08x}")

    def _rows(self, handle):
        size, count = ctypes.c_uint32(), ctypes.c_uint32()
        code = self.dll.PdhGetFormattedCounterArrayW(handle, 0x400, ctypes.byref(size), ctypes.byref(count), None)
        if code not in (0, 0x800007D2):  # PDH_MORE_DATA
            self._check(code)
        for _ in range(3):  # 实例变化时有界扩容，不能无限重试或返回伪造的零。
            if not 0 < size.value <= 16 * MIB:
                raise OSError("WDDM计数数组为空或超过上限")
            buffer = ctypes.create_string_buffer(size.value)
            code = self.dll.PdhGetFormattedCounterArrayW(handle, 0x400, ctypes.byref(size), ctypes.byref(count), buffer)
            if code == 0x800007D2:
                continue
            self._check(code)
            if count.value > len(buffer) // ctypes.sizeof(self.Item):
                raise OSError("WDDM计数数组长度不符")
            items = ctypes.cast(buffer, ctypes.POINTER(self.Item))
            return [(items[i].name or "", items[i].value.status, items[i].value.large) for i in range(count.value)]
        raise OSError("WDDM计数实例持续变化")

    @staticmethod
    def for_pid(pid, counters):
        if type(pid) is not int or pid <= 0:
            raise ValueError("WDDM采样需要明确的进程PID")
        adapters = {}
        for label in ("dedicated", "shared"):
            for name, status, value in counters[label]:
                match = re.fullmatch(r"pid_(\d+)_(luid_0x[0-9a-f]+_0x[0-9a-f]+_phys_\d+)", name, re.I)
                if not match or int(match[1]) != pid or status not in (0, 1) or value < 0:
                    continue
                bucket = adapters.setdefault(match[2].lower(), {})
                if label in bucket:
                    raise ValueError("WDDM同一适配器计数重复，拒绝相加")
                bucket[label] = value
        if not adapters or any(set(v) != {"dedicated", "shared"} for v in adapters.values()):
            raise OSError("未取得该PID的完整WDDM计数；不能当作占用为零")
        return {"pid": pid, "dedicated_bytes": sum(v["dedicated"] for v in adapters.values()),
                "shared_bytes": sum(v["shared"] for v in adapters.values()), "adapters": adapters,
                "scope": "WDDM allocations across this PID's adapters, not physical residency or proof of GPU paging"}

    def sample(self, pid):
        self._check(self.dll.PdhCollectQueryData(self.query))
        return self.for_pid(pid, {k: self._rows(h) for k, h in self.counters.items()})

    def close(self):
        if self.query.value:
            self.dll.PdhCloseQuery(self.query)
            self.query = ctypes.c_void_p()


class Monitor:
    def __init__(self, out, root, gpu, overall_deadline, diagnostics=False):
        self.out, self.root, self.gpu, self.overall_deadline = out, root, gpu, overall_deadline
        self.stop = threading.Event()
        self.deadline = None
        self.reason = None
        self.minimum_ram = psutil.virtual_memory().available
        self.maximum_gpu = self.maximum_rss = 0
        self.low_ram = self.low_gpu = None
        self.diagnostics = diagnostics
        self.diagnostic_errors = 0
        self._diagnostic_last = 0
        self._diagnostic_session = requests.Session()
        self._diagnostic_session.trust_env = False
        self._wddm = None
        self._wddm_init_error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample_diagnostics(self):
        """10秒一次的只读诊断；系统磁盘I/O不冒充模型独占I/O，缺页计数不冒充硬缺页。"""
        now = time.monotonic()
        if not self.diagnostics or runtime.engine_phase != "ready" or now - self._diagnostic_last < 10:
            return
        self._diagnostic_last = now
        sample = {"utc": utc(), "scope": "system disk counters include other apps; process page faults include soft faults"}
        try:sample["windows_commit"]=windows_commit_info()
        except (OSError,ValueError) as exc:sample["windows_commit_error"]=str(exc)
        try:
            r = self._diagnostic_session.get(URL + "/metrics", timeout=(0.5, 1))
            r.raise_for_status()
            data = r.json()
            history=data.get("requests") or []
            if not isinstance(history,list):raise ValueError("请求计时窗口不是列表，跳过辅助诊断")
            sample.update(engine=data.get("engine"), live=data.get("live"),
                          latest_request=(history or [None])[0], hardware=data.get("hardware"))
            # /metrics只保留有限条近期请求；每次采样保留有界窗口，结束后可按ID合并，避免漏掉冷首轮。
            sample["recent_requests"]=history[:16]
            sample["disks"] = {k: v._asdict() for k, v in (psutil.disk_io_counters(perdisk=True) or {}).items()}
            sample["swap"] = psutil.swap_memory()._asdict()
            if runtime.same_native():
                sample["process_memory"] = runtime.native.memory_info()._asdict()
                sample["process_io"] = runtime.native.io_counters()._asdict()
                # 累计CPU时钟；结合相邻采样间隔区分实际计算与host wall等待。
                # (delta user + delta system) / delta wall 是核心等价占用，不是整机百分比。
                sample["process_cpu_times"] = runtime.native.cpu_times()._asdict()
        except (requests.RequestException, ValueError, psutil.Error, OSError) as exc:
            self.diagnostic_errors += 1
            sample["error"] = f"{type(exc).__name__}: {exc}"
        # 辅助计数失败不取消任务，也不替代NVML/RAM/所有权等原有保护。
        if runtime.same_native():
            try:
                if self._wddm is None:
                    if self._wddm_init_error:
                        raise OSError(self._wddm_init_error)
                    try:
                        self._wddm = WindowsGpuMemory()
                    except OSError as exc:
                        self._wddm_init_error = str(exc)
                        raise
                sample["process_gpu_memory"] = self._wddm.sample(runtime.native.pid)
            except (OSError, ValueError) as exc:
                sample["process_gpu_memory_error"] = f"{type(exc).__name__}: {exc}"
        with (self.out / "引擎与IO诊断.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(sample, ensure_ascii=False) + "\n")
        write(self.out / "最新引擎诊断.json", sample)

    def cancel(self, kind, reason):
        if self.reason:
            return
        self.reason = {"kind": kind, "reason": reason, "utc": utc()}
        write(self.out / "取消原因.json", self.reason)
        runtime.abort_reason.append(reason)
        runtime.abort.set()
        if runtime.same_native():
            runtime.native.terminate()

    def run(self):
        try:
            self._run()
        finally:
            if self._wddm is not None:
                self._wddm.close()
            self._diagnostic_session.close()

    def _run(self):
        with (self.out / "资源监控.jsonl").open("a", encoding="utf-8") as log:
            while not self.stop.is_set():
                now = time.monotonic()
                try:
                    available = psutil.virtual_memory().available
                    gpu = self.gpu.sample()
                    rss = runtime.native.memory_info().rss if runtime.same_native() else 0
                    self.minimum_ram = min(self.minimum_ram, available)
                    self.maximum_gpu = max(self.maximum_gpu, gpu["gpu_used_bytes"])
                    self.maximum_rss = max(self.maximum_rss, rss)
                    log.write(json.dumps({"utc": utc(), "ram_available_bytes": available,
                                          "native_rss_bytes": rss, **gpu}) + "\n")
                    log.flush()
                    self.sample_diagnostics()
                    self.low_ram = (self.low_ram or now) if available < 1.5 * GIB else None
                    self.low_gpu = (self.low_gpu or now) if gpu["gpu_free_bytes"] < 256 * MIB else None
                    if (self.root / "停止测试").exists():
                        self.cancel("user_cancelled", "检测到本轮停止测试标记")
                    elif now >= self.overall_deadline or self.deadline and now >= self.deadline:
                        self.cancel("time_budget", "本档请求或整轮测试到达时间上限，不继续强行等待")
                    elif self.low_ram and now - self.low_ram >= 15:
                        self.cancel("resource_protection", "可用RAM低于1.5GiB持续15秒")
                    elif self.low_gpu and now - self.low_gpu >= 10:
                        self.cancel("resource_protection", "整卡剩余显存低于256MiB持续10秒，避免严重共享内存压力")
                    elif runtime.engine_phase == "ready":
                        heartbeat = self.out / "工作集守护状态.json"
                        if not runtime.same_native():
                            self.cancel("engine_lost", "原生引擎退出，不绑定新PID或自动重试")
                        elif not runtime.guard or runtime.guard.poll() is not None or not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 20:
                            self.cancel("guard_lost", "工作集保护退出或心跳超过20秒")
                except psutil.NoSuchProcess:
                    if runtime.engine_phase == "ready":
                        self.cancel("engine_lost", "采样时原生引擎消失")
                except Exception as exc:
                    self.cancel("monitor_failed", f"资源监控失败: {type(exc).__name__}: {exc}")
                if self.reason:
                    return
                self.stop.wait(2)

    def summary(self):
        return {"minimum_available_ram_gib": round(self.minimum_ram / GIB, 3),
                "whole_gpu_peak_used_mib": round(self.maximum_gpu / MIB, 1),
                "native_peak_rss_gib": round(self.maximum_rss / GIB, 3),
                "cancelled": self.reason, "diagnostic_sample_errors": self.diagnostic_errors}
