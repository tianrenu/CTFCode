"""工具箱专用CLI/TUI与模型生命周期；不改桌面端、全局npm或用户环境变量。"""
from __future__ import annotations
import datetime as dt
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

HERE = Path(__file__).resolve().parent
TOOLBOX = HERE.parents[1]
BASE = (TOOLBOX / "本地模型配置与日志/Strata验证").resolve()
EXE = HERE / "程序/opencode.exe"
MODEL = "strata-local/qwen3.8-flash-next-coder-iq1_m"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BASE))
import opencode_engine as engine
import opencode_safety as safety


def config():
    text = (HERE / "opencode.json").read_text(encoding="utf-8")
    cfg = json.loads(text.replace("${PYTHON}", Path(sys.executable).as_posix()))
    large = cfg["provider"]["strata-local"]["models"][MODEL.split("/", 1)[1]]["limit"]["context"] == 65536
    cfg["plugin"] = [(HERE / name).as_uri() for name in (("ctf-context-64k.mjs", "ctf-compaction.mjs") if large else ("ctf-context.mjs",))]
    return cfg


def config_for_context(source, context_k=None):
    """本次启动选择，不改持久配置、桌面端、提示词、工具或权限。"""
    context_profile(source)
    cfg = copy.deepcopy(source)
    if context_k is not None and context_k not in ("32", "64"):
        raise ValueError("仅支持32或64两个显式档位，不自动扩大窗口")
    declaration = cfg["provider"]["strata-local"]["models"][MODEL.split("/", 1)[1]]
    large = context_k == "64" if context_k is not None else declaration["limit"]["context"] == 65536
    expected_plugins = [(HERE / "ctf-context.mjs").as_uri()]
    large_plugins = [(HERE / name).as_uri() for name in ("ctf-context-64k.mjs", "ctf-compaction.mjs")]
    source_large = declaration["limit"]["context"] == 65536
    if cfg.get("plugin") != (large_plugins if source_large else expected_plugins):
        raise ValueError("插件列表不是本工具箱已审查的32K或64K组合；未替换用户插件或加载模型")
    if not large and context_k is None:
        return cfg
    declaration["limit"]["context"] = 65536 if large else 24576
    declaration["name"] = "GSQ-RCO Coder（64K / HC Q8量化候选）" if large else "GSQ-RCO Coder（32K / 默认）"
    cfg["agent"]["ctf-local"]["steps"] = 240 if large else 60
    cfg["provider"]["strata-local"]["options"]["timeout"] = 4500000 if large else 1800000
    cfg["plugin"] = large_plugins if large else expected_plugins
    context_profile(cfg)
    return cfg


def owned_job(ledger):
    """新建CLI专用Windows作业；引擎不在此作业中，不附着其他应用。"""
    import importlib.util
    spec=importlib.util.spec_from_file_location('ctf_owned_process_job',HERE/'owned_process_job.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.OwnedProcessJob(process_mib=2048,total_mib=3072,max_processes=16,ledger=ledger)


def context_profile(cfg):
    """CLI声明决定唯一运行档；不把可配置上限当作真实引擎已验收。"""
    if cfg.get("model") != MODEL or cfg.get("small_model") != MODEL:
        raise ValueError("只允许同一个本地模型；不自动加载第二模型")
    declaration = cfg["provider"]["strata-local"]["models"][MODEL.split("/", 1)[1]]
    limits = declaration["limit"]
    soft, output = limits.get("context"), limits.get("output")
    if type(soft) is not int or type(output) is not int or output != 4096 or soft not in (24576, 65536):
        raise ValueError("仅允许已定义的32K/软24K或64K/软64K档，输出4096；不是任意大窗口")
    if limits.get("input") is not None:
        raise ValueError("不允许另设limit.input改变已验证压缩阈值")
    if cfg.get("compaction", {}).get("auto") is not True:
        raise ValueError("必须保留自动压缩，不能用无限历史绕过上下文上限")
    physical = 65536 if soft == 65536 else 32768
    steps=cfg.get('agent',{}).get('ctf-local',{}).get('steps')
    if type(steps) is not int or steps != (240 if physical==65536 else 60):
        raise ValueError('Agent步数与运行档不符：64K为240步，32K保持60步；仍有整轮时间/工具资源上限')
    timeout = cfg["provider"]["strata-local"].get("options", {}).get("timeout")
    expected_timeout = 4500000 if physical == 65536 else 1800000
    if type(timeout) is not int or timeout != expected_timeout:
        raise ValueError("请求总超时与运行档不符：32K为30分钟、64K为75分钟；仍受取消和资源守护约束")
    hc = {"STRATA_HC_Q8": "1", "STRATA_HC_Q8_INJECT": "0", "STRATA_HC_REQ8": "0"} if physical == 65536 else {}
    return {"physical_context": physical, "soft_context": soft, "output_tokens": output, "agent_steps": steps,
            "automatic_compaction_at_approx": soft-output, "request_timeout_ms":timeout,"hc_environment": hc,
            "hc_projection": "Q8候选（可能影响数值）" if hc else "原默认投影",
            "engine_version": "0.1.59" if hc else "0.1.52",
            "scope": "下次启动配置，不代表当前引擎已加载或CTF任务验收"}


def engine_release(profile):
    """新版放在独立命名空间；原32K的components/engine不安装或选择新版。"""
    large = profile["physical_context"] == 65536
    root = BASE / "components" / "ctf-64k-0.1.59" if large else BASE / "components"
    folder = root / "engine" / ("cuda13-0.1.59" if large else "0.1.52-windows-x86_64-cuda-cuda13")
    return {"components_root": str(root), "component_directory": str(folder),
            "native_executable": str(folder / "strata.exe"), "version": profile["engine_version"],
            "manifest_sha256": "102238fc50b9941a990cafcb0ead565eaf34ae235685d8fbdd4471774f066f88" if large else None}


def verify_engine_release(profile):
    """64K启动前再次用内置信任键验证清单和全部组件字节，不联网、不安装。"""
    release = engine_release(profile)
    if profile["physical_context"] != 65536:
        return {**release, "full_component_verified": False, "scope": "原32K仍由原Control验证；未修改其组件或选择状态"}
    root = Path(release["component_directory"])
    if root.resolve() != root or not root.is_relative_to(BASE / "components"):
        raise ValueError("64K组件目录包含链接或越界；未启动模型")
    manifest = root / "engine-component.json"
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 1048576:
        raise FileNotFoundError("64K已签名组件尚未完整安装；不会回退旧引擎或联网下载")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != release["manifest_sha256"]:
        raise ValueError("64K组件清单与已验证的0.1.59发布不符")
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(BASE / "runtime/control-0.1.52"))
        from strata_control import engine_component, signing
        trusted = signing.load_trusted_keys(environ={})
        component = engine_component.read_manifest(root)
        if component.version != release["version"] or Path(component.exe).resolve() != Path(release["native_executable"]):
            raise ValueError("64K清单版本或程序路径不符")
        previous = engine_component._verifier
        try:
            signing.install_component_verifier(trusted)
            engine_component.verify_signature(component, {engine_component.ENV_REQUIRE_SIGNED: "1"})
            engine_component.verify(component, full=True)
        finally:
            engine_component.register_signature_verifier(previous)
    finally:
        sys.path[:] = old_path
    return {**release, "full_component_verified": True, "scope": "内置信任键及全部字节通过，不代表本次已加载或解题通过"}


def context_argv(cfg, profile_name):
    profile = context_profile(cfg)
    return safety.launch_argv(profile["physical_context"]//1024, profile_name, kv="q4_0",
                              reserve_mib=1792, prefill=128)+["--set", "pool-workers=13"]


def context_environment(profile, source):
    """只整理模型子进程；阻止旧环境把32K默认档静默改成HC量化候选。"""
    env = dict(source)
    for key in ("STRATA_HC_Q8", "STRATA_HC_Q8_INJECT", "STRATA_HC_REQ8", "STRATA_PCIE_START", "STRATA_LOOKUP_MAX_T"):
        env.pop(key, None)
    env.update(profile["hc_environment"])
    env["STRATA_COMPONENTS_DIR"] = engine_release(profile)["components_root"]
    if profile["physical_context"] == 65536:
        env.update(STRATA_PCIE_START="old", STRATA_LOOKUP_MAX_T="0",
                   STRATA_WS_GUARD_POLL_MS="100", STRATA_TEST_WS_MIN_MIB="4")
    return env


def verify_context_runtime(profile, argv, env, metrics):
    """启动前后声明一致性；必须核验真实argv/env/metrics，不能只改JSON。"""
    for key, value in (("--max-context", str(profile["physical_context"])), ("--kv", "q4_0"),
                       ("--prefill", "128"), ("--pool-workers", "13"), ("--expert-cache", "auto"),
                       ("--vram-reserve-mib", "1792"), ("--prompt-cache", "1"), ("--conversation-cache-mib", "0")):
        if argv.count(key) != 1 or argv[argv.index(key)+1:argv.index(key)+2] != [value]:
            raise ValueError("真实引擎参数与运行档不符："+key)
    if metrics.get("engine", {}).get("max_context") != profile["physical_context"]:
        raise ValueError("真实服务窗口与启动配置不符；未进入TUI")
    if metrics.get("engine", {}).get("version") != profile["engine_version"]:
        raise ValueError("真实引擎版本与32K/64K档位不符；未进入TUI")
    for key in ("STRATA_HC_Q8", "STRATA_HC_Q8_INJECT", "STRATA_HC_REQ8"):
        expected = profile["hc_environment"].get(key)
        if env.get(key) != expected:
            raise ValueError("真实HC环境与运行档不符："+key)
    release = engine_release(profile)
    if env.get("STRATA_COMPONENTS_DIR") != release["components_root"]:
        raise ValueError("真实组件命名空间与运行档不符；未进入TUI")
    for key, value in (("STRATA_PCIE_START", "old"), ("STRATA_LOOKUP_MAX_T", "0")):
        if env.get(key) != (value if profile["physical_context"] == 65536 else None):
            raise ValueError("真实兼容策略与运行档不符："+key)
    if profile["physical_context"] == 65536 and metrics.get("engine", {}).get("spec") != 6:
        raise ValueError("真实64K投机窗口不是已测的6；未进入TUI")
    if profile["physical_context"] == 65536 and any(env.get(k) != v for k,v in (("STRATA_WS_GUARD_POLL_MS","100"),("STRATA_TEST_WS_MIN_MIB","4"))):
        raise ValueError("真实64K工作集守护参数与已测配置不符；未进入TUI")
    return {"physical_context": profile["physical_context"], "hc_projection": profile["hc_projection"],
            "engine_version": profile["engine_version"], "components_root": release["components_root"],
            "verified": True, "scope": "真实启动参数和服务上限；不是长任务或解题质量保证"}


def observed_context(state, metrics):
    """自检只公布身份与原生参数都匹配的运行窗口，不信任旧状态里的常量。"""
    proc = psutil.Process(state["native_pid"])
    if proc.create_time() != state["native_created"] or not Path(proc.exe()).resolve().is_relative_to(BASE.resolve()/"components"):
        raise ValueError("当前模型身份不符，不能报告运行窗口")
    argv = proc.cmdline()
    if argv.count("--max-context") != 1:
        raise ValueError("原生窗口参数不唯一")
    value = int(argv[argv.index("--max-context")+1])
    observed = metrics.get("engine", {}).get("max_context")
    if type(observed) is not int or observed != value or value not in (32768, 65536, 131072, 262144):
        raise ValueError("原生argv与服务窗口不符，不能确认当前窗口")
    if not proc.is_running() or proc.create_time() != state["native_created"]:
        raise ValueError("读取窗口期间引擎已变化")
    release=state.get("engine_release")
    version=metrics.get("engine",{}).get("version")
    if release and (Path(proc.exe()).resolve()!=Path(release["native_executable"]).resolve() or version!=release["version"]):
        raise ValueError("当前引擎版本或程序路径与本轮已验证发布不符")
    return {"physical_context": value, "engine_version":version, "verified": True,
            "source": "native argv + service metrics + PID/create_time；新启动另核对版本和发布程序路径"}


def normalize_native_powershell_env(source):
    """仅整理已明确选择本机WinPS5.1的子环境；不改系统变量、不干预pwsh或未知shell。"""
    env = dict(source)
    values = {key.upper(): value for key, value in env.items()}
    root, shell = values.get("SYSTEMROOT"), values.get("SHELL")
    if os.name != "nt" or not root or not shell:
        return env
    native = Path(root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    modules = native.parent / "Modules"
    norm = lambda value: os.path.normcase(os.path.normpath(str(value)))
    if norm(shell) != norm(native) or not modules.is_dir():
        return env
    keys = [key for key in env if key.upper() == "PSMODULEPATH"]
    entries = [part for key in keys for part in env[key].split(";") if part]
    for key in keys:
        del env[key]
    env["PSModulePath"] = ";".join([str(modules)] + [part for part in entries if norm(part) != norm(modules)])
    return env


def child_env(cfg):
    env = normalize_native_powershell_env(engine.ENV)
    for key in list(env):
        if key.startswith("OPENCODE_") or key in {"CTF_ASSISTED_POLICY", "CTF_TUI_STATUS_FILE"}:
            del env[key]
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps(cfg,ensure_ascii=False),
               OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
               OPENCODE_DISABLE_DEFAULT_PLUGINS="true", OPENCODE_DISABLE_LSP_DOWNLOAD="true",
               OPENCODE_DISABLE_CLAUDE_CODE="true", NO_PROXY="*", PYTHONUTF8="1", CTFCODE_PYTHON=str(Path(sys.executable).resolve()))
    for label in ("config", "data", "cache", "state"):
        env[f"XDG_{label.upper()}_HOME"] = str(HERE / "独立环境" / label)
    return env


def validate_workspace(target):
    p = Path(target.strip().strip('"')).resolve()
    if not p.is_dir() or p == Path(p.anchor) or p == Path.home() or p == TOOLBOX or p.is_relative_to(HERE) or p.is_relative_to(BASE):
        raise ValueError("请选择独立的题目副本目录，不要选择盘根、用户目录、工具箱或模型/程序目录")
    model = Path(os.environ.get("CTFCODE_MODEL_DIR", str(BASE / "models/GSQ-RCO-Coder"))).resolve()
    if p == model or p.is_relative_to(model):
        raise ValueError("模型目录不能作为题目工作区")
    # 防止CTF附件自带插件/配置在任何模型调用之前获得主机代码执行。
    for folder in (p, *p.parents):
        for name in ("opencode.json", "opencode.jsonc", ".opencode", "AGENTS.md", "CLAUDE.md"):
            if (folder / name).exists():
                raise ValueError(f"检测到需要先审查的项目配置/插件：{folder/name}；没有自动加载")
    return p


def preflight(target, cfg=None):
    workspace = validate_workspace(target)
    if not EXE.is_file():
        raise FileNotFoundError("工具箱独立CLI缺失，不使用桌面端或全局CLI替代")
    active = context_profile(cfg if cfg is not None else config())
    required = ["ctf-context.mjs", "ctf-tools.mjs", "owned_process_job.py"]
    if active["physical_context"] == 65536:
        required += ["ctf-context-64k.mjs", "ctf-compaction.mjs", "compaction-cache.mjs", "64K摘要输出约束_分词器.json"]
    for name in required:
        if not (HERE / name).is_file():
            raise FileNotFoundError(f"工具箱插件依赖缺失：{HERE/name}；未加载模型")
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() in {"llama-server.exe", "koboldcpp.exe"}:
            raise RuntimeError("已有其他本地推理服务，先自行关闭；不会同时加载或自动杀掉它")
    version = subprocess.run([str(EXE),"--version"],env=child_env(cfg if cfg is not None else config()),capture_output=True,text=True,
                             timeout=15,creationflags=engine.FLAGS)
    if version.returncode or version.stdout.strip() != "1.2.26":
        raise RuntimeError("独立CLI版本变化，插件/压缩机制需重新验证；未自动升级或回退桌面版")
    return workspace


def finalize_activity(out, state):
    """CLI退出后标记旁路记录已终止，避免把旧busy误读为仍在工作。"""
    p = out / "Agent状态.json"
    if not p.exists():
        return
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 65536:
        raise ValueError("活动状态不是有界普通文件")
    data = json.loads(p.read_text(encoding="utf-8"))
    if Path(data.get("workspace", "")).resolve() != Path(state["workspace"]).resolve():
        raise ValueError("活动状态工作目录不符")
    data.update(state="stopped", controller_state=state["state"],
                updated_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                notice="控制器/CLI已退出；此状态不代表题目已解出。")
    safety.write(p, data)


def headroom_low_since(previous, now, available, floor):
    """持续不足10秒才触发额外提交内存保护；0秒起点不会被当成未初始化。"""
    if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
        raise ValueError("资源采样时钟无效")
    if type(available) is not int or available < 0 or type(floor) is not int or floor <= 0:
        raise ValueError("资源采样字节数无效")
    if available >= floor:return None,False
    if previous is None:previous=now
    if type(previous) not in (int,float) or not math.isfinite(previous) or not 0 <= previous <= now:
        raise ValueError("资源保护时间不连续")
    return previous,now-previous>=10


class Runtime64Monitor(safety.Monitor):
    """沿用已实测64K额外保护；1GiB显存仅提示，不替代原256MiB取消线。"""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.commit_low=None;self.minimum_commit_free=None;self.minimum_gpu_free=None;self.gpu_warning_active=False

    def sample_diagnostics(self):
        super().sample_diagnostics()
        now=time.monotonic();free=self.gpu.sample()["gpu_free_bytes"]
        commit=safety.windows_commit_info()["available_bytes"]
        self.minimum_gpu_free=free if self.minimum_gpu_free is None else min(self.minimum_gpu_free,free)
        self.minimum_commit_free=commit if self.minimum_commit_free is None else min(self.minimum_commit_free,commit)
        warning=free < 1024**3
        if warning and not self.gpu_warning_active:
            text="整卡空闲显存不足1GiB，仅提示；仍保留256MiB持续10秒、RAM/提交内存/工作集保护，不擅自关闭其他应用。"
            safety.write(self.out/"显存余量提示.json",{"utc":time.time(),"level":"warning","cancel":False,"gpu_free_mib":round(free/1024**2,1),"notice":text})
            print(text,flush=True)
        elif not warning and self.gpu_warning_active:
            safety.write(self.out/"显存余量提示.json",{"utc":time.time(),"level":"cleared","cancel":False,"gpu_free_mib":round(free/1024**2,1)})
        self.gpu_warning_active=warning
        self.commit_low,stop=headroom_low_since(self.commit_low,now,commit,2*1024**3)
        if stop:self.cancel("resource_protection","Windows可用提交内存低于2GiB持续10秒；不降低其他保护强行续跑")

    def summary(self):
        result=super().summary()
        result.update(gpu_warning_floor_mib=1024,inherited_gpu_stop_floor_mib=256,
                      minimum_gpu_free_mib=None if self.minimum_gpu_free is None else round(self.minimum_gpu_free/1024**2,1),
                      minimum_commit_free_gib=None if self.minimum_commit_free is None else round(self.minimum_commit_free/1024**3,3))
        return result


def launch(target, *, context_k=None):
    cfg = config_for_context(config(), context_k)
    workspace = preflight(target, cfg)
    context = context_profile(cfg)
    now = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = BASE / "会话记录" / (now + "_OpenCode_TUI")
    out.mkdir(parents=True)
    state = {"state":"preparing","workspace":str(workspace),"out":str(out),"owner_pid":os.getpid(),
             "owner_created":psutil.Process().create_time(),"cli":str(EXE),**context}
    pointer = HERE / "当前运行.json"
    def save():
        safety.write(out / "会话状态.json",state)
        safety.write(pointer,state)
    with engine.RunLock():
        engine.OUT = out
        engine.status = {"opencode_tui":True}
        engine.abort.clear(); engine.abort_reason.clear(); engine.engine_lost.clear(); engine.watch_stop.clear()
        engine.native = engine.guard = engine.launch = None
        child=None;cli_job=None;monitor=None
        base_env=dict(engine.ENV)
        profile="oc-ui-"+str(int(time.time()))
        try:
            # 配置复制、NVML/监控和审计初始化也属于启动事务；任何早期错误都须留下终态。
            engine.ENV = context_environment(context, base_env)
            state["engine_release"] = verify_engine_release(context)
            for key, suffix in (("STRATA_RUNTIME_DIR","state/runtime"),("STRATA_PROFILE_DIR","state/profiles"),
                                ("STRATA_LAUNCH_PRESET_DIR","state/launch-presets")):
                engine.ENV[key]=str(out/suffix)
            settings=out/"settings.json";settings.write_bytes((BASE/"settings.json").read_bytes())
            engine.ENV.update(STRATA_SETTINGS_FILE=str(settings),STRATA_WS_GUARD_DIR=str(out),
                              STRATA_WS_GUARD_SECONDS="14400",STRATA_OWNER_PID=str(os.getpid()),
                              STRATA_OWNER_CREATED=str(state["owner_created"]))
            deadline=time.monotonic()+14400
            engine.deadline=deadline
            monitor_type=Runtime64Monitor if context["physical_context"] == 65536 else safety.Monitor
            monitor=monitor_type(out,out,safety.GPU(),deadline,diagnostics=True)
            safety.write(out/"所有者清理.json",{"owner_pid":os.getpid(),"owner_created":state["owner_created"],
                                             "control_argv":context_argv(cfg,profile)[:2],"profile":profile})
            save();monitor.thread.start()
            cli_job=owned_job(out/'CLI进程作业.jsonl')
            state['cli_resource_limits']={'per_process_private_mib':2048,'whole_tree_private_mib':3072,'max_processes':16,'kill_on_owner_or_cli_exit':True};save()
            available_gib = psutil.virtual_memory().available / 1024**3
            if available_gib < 20:
                print(f"提示：当前可用RAM约{available_gib:.1f}GiB，16GiB仅是加载门禁。多释放内存有利于文件缓存，但20GiB不是速度保证，高于它也曾明显变慢。未关闭其他应用。",flush=True)
            print(f"使用工具箱独立OpenCode CLI/TUI；引擎{context['physical_context']//1024}K，软窗口{context['soft_context']//1024}K，约{context['automatic_compaction_at_approx']//1024}K自动摘要。\n正在加载单个本地模型，不会启动旧Qwen27B。",flush=True)
            if context["hc_environment"]:
                print("注意：64K使用HC Q8投影量化候选，可能影响数值；不保证每道题的效果。",flush=True)
            engine.start_engine(context_argv(cfg,profile),profile)
            if Path(engine.native.exe()).resolve() != Path(engine_release(context)["native_executable"]):
                raise ValueError("实际引擎程序不属于所选版本；清理本轮，不启动TUI")
            response=engine.SESSION.get(engine.URL+"/metrics",timeout=4);response.raise_for_status()
            state["runtime_context"]=verify_context_runtime(context,engine.native.cmdline(),engine.native.environ(),response.json())
            state.update(state="running",native_pid=engine.native.pid,native_created=engine.native.create_time(),guard_pid=engine.guard.pid)
            book=workspace/"CTF_STATE.md"
            try:
                with book.open("x",encoding="utf-8") as f:
                    f.write("# CTF工作记忆\n\n## 目标与授权范围\n\n## 已验证事实与原始证据\n\n## 未验证假设\n\n## 失败方法及原因\n\n## 当前脚本与下一步\n")
            except FileExistsError:
                if book.is_symlink(): raise ValueError("CTF_STATE.md是链接，未启动Agent")
            env=child_env(cfg)
            env["CTF_TUI_STATUS_FILE"] = str(out / "Agent状态.json")
            safety.write(out/"启动配置.json",cfg)
            child=cli_job.spawn([str(EXE),str(workspace),"--agent","ctf-local","--model",MODEL],env=env,cwd=workspace)
            state.update(opencode_pid=child.pid,opencode_created=psutil.Process(child.pid).create_time());save()
            print("在TUI中连续两次Esc取消当前请求；用7号核对后端是否idle，取消不等于卸载模型。退出TUI后自动释放本轮模型；7号也可停止整轮。",flush=True)
            while child.poll() is None:
                if engine.abort.is_set() or (out/"停止测试").exists() or time.monotonic()>=deadline:
                    child.terminate()
                    state.update(state="cancelled",reason=monitor.reason or "停止请求/四小时时限")
                    break
                time.sleep(1)
            state["exit_code"]=child.wait(timeout=10)
            if state["state"] != "cancelled":
                state["state"]="stopped" if state["exit_code"] == 0 else "failed"
                if state["exit_code"]:
                    state["error"]=f"OpenCode TUI异常退出：{state['exit_code']}"
        except KeyboardInterrupt:
            state.update(state="cancelled",reason="控制器收到Ctrl+C，关闭本轮TUI和模型")
        except Exception as exc:
            state.update(state="failed",error=f"{type(exc).__name__}: {exc}");raise
        finally:
            if cli_job:
                try:
                    state['cli_job_cleanup']=cli_job.close()
                    if state['cli_job_cleanup']['errors']:state.update(state='failed',cli_job_cleanup_error=True)
                except Exception as exc:
                    state.update(state='failed',cli_job_cleanup_error=str(exc))
            if child and child.poll() is None:
                try:
                    child.terminate()
                    try:child.wait(10)
                    except subprocess.TimeoutExpired:child.kill();child.wait(5)
                except Exception as exc:
                    state.update(state='failed',cli_cleanup_error=str(exc))
            if monitor:
                try:
                    monitor.stop.set()
                    if monitor.thread.is_alive():monitor.thread.join(10)
                    state['resources']=monitor.summary()
                except Exception as exc:
                    state.update(state='failed',monitor_cleanup_error=str(exc))
            try:state['cleanup']=engine.stop_engine()
            except Exception as exc:state.update(state='failed',cleanup_error=str(exc))
            finally:engine.ENV=base_env
            state['finished']=dt.datetime.now().isoformat()
            try: finalize_activity(out, state)
            except Exception as exc: state.update(state='failed',activity_finalize_error=str(exc))
            save()
    return 0 if state["state"]=="stopped" else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--workspace-check"]:
        try:
            if len(sys.argv) != 3:
                raise ValueError("工作目录校验只接受一个路径")
            if not sys.argv[2] or any(char in sys.argv[2] for char in "\x00\r\n"):
                raise ValueError("工作目录为空或包含控制字符")
            workspace = validate_workspace(sys.argv[2])
            print(json.dumps({"ok": True, "directory": str(workspace)}, ensure_ascii=False))
        except (ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            raise SystemExit(1)
        raise SystemExit(0)
    raise SystemExit(launch(sys.argv[1]))
