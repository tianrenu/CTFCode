"""独立V2 TUI先启动，模型按需加载；正式切换前仅由显式 --native 启用。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="CTFCode原生TUI（隔离预览）")
    parser.add_argument("--cli")
    parser.add_argument("--target")
    parser.add_argument("--context", choices=("32", "64"), default="32")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    boot = module("ctf_v2_boot", ROOT / "bootstrap.py")
    boot.configure(boot.model())
    import opencode_runtime as runtime
    import settings
    from controller import Controller
    cli = Path(args.cli).resolve() if args.cli else HERE / "程序/opencode.exe"
    if not cli.is_file() or cli.is_symlink():
        raise FileNotFoundError("独立V2 CLI缺失；未回退桌面版或全局CLI")
    release = json.loads(Path(__file__).with_name("cli.json").read_text(encoding="utf-8"))
    if release.get("format") != 1 or release.get("upstream") != "2.3.0" or not release.get("version", "").startswith("2.3.0-ctfcode.") or len(release.get("sha256", "")) != 64:
        raise ValueError("CTFCode独立CLI构建声明无效")
    with cli.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != release["sha256"]:
            raise ValueError("V2 CLI不是已核验的CTFCode定制构建；未启动或联网升级")
    cfg = runtime.config_for_context(runtime.config(), args.context)
    env = runtime.child_env(cfg)
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    for label in ("config", "data", "cache", "state"):
        env["XDG_" + label.upper() + "_HOME"] = str(HERE / "独立环境/v2" / label)
    env.update(OPENCODE_CONFIG_DIR=env["XDG_CONFIG_HOME"], OPENCODE_CONFIG_PROJECT_DISABLE="true",
               OPENCODE_DISABLE_FILEWATCHER="true")
    version = subprocess.run([str(cli), "--version"], env=env, capture_output=True, text=True,
                             timeout=15, creationflags=runtime.engine.FLAGS)
    if version.returncode or version.stdout.strip() != "opencode v" + release["version"]:
        raise RuntimeError("独立V2 CLI版本不符")
    startup = str(runtime.validate_workspace(args.target)) if args.target else None
    if args.dry_run:
        print(json.dumps({"dry_run": True, "model_not_loaded": True, "cli": str(cli), "version": release["version"],
            "startup": startup, "context": runtime.context_profile(cfg), "standalone": True,
            "configuration": settings.make(cfg, HERE, sys.executable)}, ensure_ascii=False, indent=2))
        return 0
    out = runtime.BASE / "会话记录" / (dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_Native_TUI")
    owner = Controller(runtime, boot, settings, out, context=args.context)
    job = None
    child = None
    try:
        options = settings.make(cfg, HERE, sys.executable)["plugins"][0]["options"]
        options["controller"] = owner.connection()
        if startup:
            options["startup"] = startup
        env.update(OPENCODE_CONFIG=str(owner.file), OPENCODE_CLI_CONFIG_CONTENT=json.dumps({
            "tabs": {"mode": "off"}, "highlights": False,
            "plugins": [{"package": (HERE / "v2").as_uri(), "options": options}]}))
        home = HERE / "独立环境/v2/首页"
        home.mkdir(parents=True, exist_ok=True)
        job = runtime.owned_job(out / "CLI进程作业.jsonl")
        child = job.spawn([str(cli), str(home), "--standalone"], cwd=home, env=env)
        with owner.mutex:
            owner.state.update(opencode_pid=child.pid, opencode_created=runtime.psutil.Process(child.pid).create_time(), cli=str(cli))
            owner.save()
        while child.poll() is None:
            if (out / "停止测试").exists():
                child.terminate()
                break
            if runtime.engine.abort.is_set() and owner.state["model_state"] == "ready":
                with owner.mutex:
                    owner.state["error"] = "；".join(runtime.engine.abort_reason) or "本轮资源保护触发，正在卸载模型"
                owner.stop()
            time.sleep(0.5)
        code = child.wait(timeout=15)
        with owner.mutex:
            owner.state["exit_code"] = code
        return code
    finally:
        # 正常退出先完成插件归档；意外退出由Windows作业收回本轮CLI进程树。
        # 无论CLI清理是否成功，均须继续尝试本控制器拥有的模型/守护清理。
        try:
            if job:
                result = job.close()
                with owner.mutex:
                    owner.state["cli_job_cleanup"] = result
            if child and child.poll() is None:
                child.wait(timeout=15)
        finally:
            owner.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as err:
        print(f"原生TUI启动失败：{type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(1)
