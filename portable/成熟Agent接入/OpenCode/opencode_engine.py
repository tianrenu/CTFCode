"""OpenCode专用单模型生命周期；不含赛题、旧Agent循环或测试入口。"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import psutil
import requests

BASE=(Path(__file__).resolve().parents[2]/"本地模型配置与日志/Strata验证").resolve()
OUT=BASE/"会话记录"
URL="http://127.0.0.1:18791"
FLAGS=subprocess.CREATE_NO_WINDOW
ENV={k:v for k,v in os.environ.items() if k.lower() not in {"http_proxy","https_proxy","all_proxy"}}
ENV.update(NO_PROXY="*",PYTHONUTF8="1",STRATA_COMPONENTS_DIR=str(BASE/"components"),
           STRATA_INSTALL_DIR=str(BASE/"runtime/control-0.1.52"),STRATA_SETTINGS_FILE=str(BASE/"settings.json"),
           STRATA_RUNTIME_DIR=str(BASE/"state/runtime"),STRATA_PROFILE_DIR=str(BASE/"state/profiles"),
           STRATA_TEST_WS_GIB="14")
SESSION=requests.Session();SESSION.trust_env=False
status={}
abort=threading.Event();abort_reason=[];engine_lost=threading.Event();watch_stop=threading.Event()
deadline=None
native=guard=launch=None
log_handles=[]
engine_phase="stopped"
engine_generation=0

def write(name, value):
    destination = OUT / name
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(destination)


def save():
    write("任务状态.json", status)


def same_native():
    try:
        return native and native.is_running() and native.create_time() == status["native_created"]
    except psutil.Error:
        return False


def native_startup_hint(profile, started):
    """仅从本轮已知位置日志取有界显存错误，不跟随stderr里的任意路径。"""
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", profile):
        return ""
    if not isinstance(started, (int, float)) or not os.environ.get("LOCALAPPDATA"):
        return ""
    root = Path(os.environ["LOCALAPPDATA"]) / "Strata/launch"
    path = root / f"{profile}.engine.log"
    try:
        # Windows MSIX可将单个文件重定向到应用LocalCache，而父目录resolve仍在原位。
        # 文件名已严格限定为本轮profile，位置只由可信LOCALAPPDATA构造；不接受日志里的路径。
        if path.is_symlink() or not path.is_file():
            return ""
        if path.stat().st_mtime < started - 2:
            return ""  # 同名旧日志不能冒充本轮诊断。
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 8192))
            lines = stream.read(8192).decode("utf-8", "replace").splitlines()
        prefix = "strata generate: no VRAM is left for the expert cache:"
        matches = [line for line in lines if line.startswith(prefix)]
        if matches:
            # 引擎后半段会建议降低预留；这里只展示实际缺口，不将其当作修复方案。
            return matches[-1].split("To make room:", 1)[0].strip()[:700]
        shrink = [line for line in lines if "free once the slots are written" in line and "shrinking the expert cache" in line]
        if shrink and any("pre-filled 0 of 0 slots" in line for line in lines):
            return "专家缓存首次分配后被缩减为0，无法开始验证；" + shrink[-1][:500]
    except OSError:
        pass
    return ""


def launch_is_ready():
    """--no-tui控制器完成预热后才算就绪；health=200不代表预热结束。"""
    if launch is None:
        return False
    code = launch.poll()
    if code is not None and code != 0:
        # 启动失败必须显示本轮真实原因，不能只给退出码，让显存不足看起来像无响应。
        path = OUT / f"模型启动-{engine_generation:02d}-stderr.log"
        detail = ""
        try:
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 4096))
                    detail = stream.read(4096).decode("utf-8", "replace").strip()[-1600:]
        except OSError:
            pass  # 日志不可读不妨碍快失败，也不伪造缺失的报错。
        hint = native_startup_hint(status.get("launch_profile"), status.get("launch_started_epoch"))
        raise RuntimeError(f"模型启动控制器异常退出（{code}）；不空等450秒。" +
                           (f"\n本轮启动错误：{detail}" if detail else f"请查看本轮启动日志：{path}") +
                           (f"\n本轮原生显存检查：{hint}" if hint else ""))
    return code == 0


def enforce_auto_cache(argv):
    """手动缓存会跳过当前安装版的WDDM写后空闲检查；不得把它当保护等价的优化。"""
    result = list(argv)
    choices = [v.split("=", 1)[1] for v in result if isinstance(v, str) and v.startswith("expert-cache=")]
    if "--expert-cache" in result or len(choices) > 1 or any(v != "auto" for v in choices):
        raise ValueError("只允许expert-cache=auto：当前引擎手动缓存跳过WDDM写后预留复查，已撤回固定缓存候选。")
    if not choices:
        result += ["--set", "expert-cache=auto"]
    return result


def start_engine(argv_override=None, profile_name="iq1_m-32k"):
    global native, guard, launch, engine_phase, engine_generation
    engine_phase = "loading"
    engine_generation += 1
    native, guard, launch = None, None, None
    (OUT / "停止工作集保护").unlink(missing_ok=True)
    (OUT / "工作集守护状态.json").unlink(missing_ok=True)
    if psutil.virtual_memory().available < 16 * 1024**3:
        raise RuntimeError("启动前可用RAM不足16GiB；不擅自关闭其他应用")
    try:
        loaded = SESSION.get("http://127.0.0.1:11434/api/ps", timeout=4).json().get("models", [])
    except requests.RequestException as exc:
        if any((p.info['name'] or '').lower().startswith('ollama') for p in psutil.process_iter(['name'])):
            raise RuntimeError('Ollama进程存在但接口不可达，无法确认是否加载模型；请自行检查后重试') from exc
        loaded=[]  # 没安装/没运行Ollama不应妨碍独立Strata；不把接口失败当作已有进程空闲。
    if loaded:
        raise RuntimeError("Ollama已有模型，避免同时加载；不自动卸载用户模型")
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info["name"] or "").lower() == "strata.exe":
            raise RuntimeError("已存在Strata引擎，避免绑定/停止不属于本轮的进程")
    if any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == 18791 for c in psutil.net_connections(kind="tcp")):
        raise RuntimeError("18791已被占用，不停止或复用未知服务")
    argv = enforce_auto_cache(list(argv_override) if argv_override is not None else json.loads((BASE / "当前启动参数.json").read_text(encoding="utf-8")))
    if argv[argv.index("--port") + 1] != "18791":
        raise ValueError("端口必须18791，不能使用Burp的8080")
    if not isinstance(profile_name,str) or not 1<=len(profile_name)<=64 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in profile_name):
        raise ValueError("本地运行profile名称非法")
    status.update(launch_argv=argv,launch_profile=profile_name)
    stdout = (OUT / f"模型启动-{engine_generation:02d}-stdout.log").open("wb")
    stderr = (OUT / f"模型启动-{engine_generation:02d}-stderr.log").open("wb")
    log_handles.extend([stdout, stderr])
    start = time.time()
    status["launch_started_epoch"] = start
    launch = subprocess.Popen(argv, env=ENV, cwd=BASE, stdout=stdout, stderr=stderr, creationflags=FLAGS)
    write(f"启动记录-{engine_generation:02d}.json", {"argv": argv, "launcher_pid": launch.pid, "utc": dt.datetime.now(dt.timezone.utc).isoformat()})
    for _ in range(450):
        if abort.is_set() or deadline and time.monotonic() >= deadline:
            raise TimeoutError("加载/恢复期间任务取消或预算已结束")
        control_ready = launch_is_ready()
        if native is None:
            for p in psutil.process_iter(["name", "exe", "create_time"]):
                if ((p.info["name"] or "").lower() == "strata.exe" and p.info["exe"]
                    and Path(p.info["exe"]).resolve().is_relative_to(BASE / "components")
                    and p.info["create_time"] >= start - 2):
                    native = p
                    status.update(native_pid=p.pid, native_created=p.create_time())
                    write("工作集限制.json", {"pid": p.pid, "max_bytes": 14 * 1024**3})
                    g = (OUT / f"工作集保护-{engine_generation:02d}.log").open("wb")
                    log_handles.append(g)
                    guard = subprocess.Popen([sys.executable, str(BASE / "working_set_guard.py")], env=ENV,
                                             stdout=g, stderr=subprocess.STDOUT, creationflags=FLAGS)
                    status["guard_pid"] = guard.pid
                    save()
                    break
        if native:
            if not native.is_running():
                launch_is_ready()  # 若控制器已报错，优先展示完整真实错误。
                hint = native_startup_hint(profile_name, start)
                raise RuntimeError("原生引擎在加载时退出；" + (hint or "请检查本轮启动日志，不把它误报为工作集守护故障。"))
            if guard.poll() is not None:
                raise RuntimeError(f"工作集守护在加载时退出（{guard.poll()}）；停止本轮，不取消保护继续运行。")
            try:
                h = SESSION.get(URL + "/health", timeout=1)
                heartbeat = OUT / "工作集守护状态.json"
                if h.status_code == 200 and heartbeat.exists() and control_ready:
                    actual = json.loads(heartbeat.read_text(encoding="utf-8"))
                    if actual["pid"] != native.pid or actual["max"] != 14 * 1024**3 or not actual["flags"] & 4:
                        raise RuntimeError("工作集硬上限核验失败")
                    write(f"加载与保护核验-{engine_generation:02d}.json", {"health": h.json(), "working_set": actual})
                    engine_lost.clear()
                    engine_phase = "ready"
                    return
            except requests.RequestException:
                pass
        if psutil.virtual_memory().available < 1.5 * 1024**3:
            raise RuntimeError("加载期可用RAM触底，立即停止本轮")
        time.sleep(1)
    raise TimeoutError("模型450秒内未就绪")


def stop_engine(final=True):
    global engine_phase
    engine_phase = "stopping" if final else "recovering"
    if final:
        watch_stop.set()
    result = {}
    other_native = any((p.info["name"] or "").lower() == "strata.exe"
                       and not (native and p.pid == native.pid and p.info["create_time"] == status.get("native_created"))
                       for p in psutil.process_iter(["name", "create_time"]))
    if native and (same_native() or not other_native):
        argv = status.get("launch_argv") or json.loads((BASE / "当前启动参数.json").read_text(encoding="utf-8"))
        try:
            stopped = subprocess.run(argv[:2] + ["launch", "--stop", "--preset", status.get("launch_profile","iq1_m-32k")], env=ENV, cwd=BASE,
                                     capture_output=True, timeout=25, creationflags=FLAGS)
            result["control_stop_exit"] = stopped.returncode
            result["control_stop_stdout"] = stopped.stdout.decode("utf-8", "replace")[-2000:]
        except Exception as exc:
            result["control_stop_error"] = str(exc)
        if same_native():
            native.terminate()
            try:
                native.wait(10)
            except psutil.TimeoutExpired:
                native.kill()
    if guard and guard.poll() is None:
        (OUT / "停止工作集保护").touch()
        try:
            guard.wait(5)
        except subprocess.TimeoutExpired:
            guard.terminate()
    if launch and launch.poll() is None:
        launch.terminate()
    result.update(native_stopped=not same_native(), guard_stopped=not guard or guard.poll() is not None,
                  other_apps_untouched=True, utc=dt.datetime.now(dt.timezone.utc).isoformat())
    write("清理结果.json" if final else f"恢复清理-{engine_generation:02d}.json", result)
    for f in log_handles:
        f.close()
    log_handles.clear()
    if final:
        engine_phase = "stopped"
    return result


class RunLock:
    """同一模型工作区只允许一个执行器；OS在进程退出后自动释放锁。"""
    def __enter__(self):
        import msvcrt
        path = BASE / "state" / "ctf_agent.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("已有本地执行器运行，不并发混用模型/日志") from exc
        return self

    def __exit__(self, *args):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
