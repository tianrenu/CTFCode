"""仅用于离线回归：在Windows作业中启动隔离的V2 API，不加载推理模型。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--controller", action="store_true")
    parser.add_argument("--model-endpoint")
    parser.add_argument("--missing-tokenizer", action="store_true")
    parser.add_argument("--context", choices=("32", "64"), default="32")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir() or not (root / "fixture.marker").is_file():
        raise ValueError("只接受带有测试标识的独立临时目录")
    if not 1024 <= args.port <= 65535:
        raise ValueError("测试端口无效")
    source = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode/owned_process_job.py"
    spec = importlib.util.spec_from_file_location("fixture_job", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OPENCODE_", "CTF_", "CTFCODE_"))}
    for label in ("config", "data", "cache", "state"):
        env[f"XDG_{label.upper()}_HOME"] = str(root / label)
    env.update(OPENCODE_CONFIG_DIR=str(root / "config"), OPENCODE_DISABLE_AUTOUPDATE="true",
               OPENCODE_SERVER_PASSWORD=os.environ.get("CTFCODE_FIXTURE_KEY", secrets.token_urlsafe(32)),
               OPENCODE_DISABLE_MODELS_FETCH="true", OPENCODE_CONFIG_PROJECT_DISABLE="true",
               OPENCODE_DISABLE_CLAUDE_CODE="true", OPENCODE_DISABLE_FILEWATCHER="true",
               OPENCODE_CLI_CONFIG_CONTENT=json.dumps({"tabs": {"mode": "off"}, "highlights": False}),
               OPENCODE_CONFIG_CONTENT=json.dumps({"update": "disable", "share": "disabled", "warming": False,
                                                  "snapshots": False, "permissions": [
                                                      {"action": "*", "resource": "*", "effect": "deny"}]}))
    owner = None
    here = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode"
    if args.controller:
        package = root / "隔离程序"
        shutil.copytree(Path(__file__).resolve().parent, package, ignore=shutil.ignore_patterns(
            "__pycache__", "local.json", "runtime", "components", "models", "独立环境", "程序", "state",
            "会话记录", "当前运行.json", "settings.json", "当前启动参数.json", "64K摘要输出约束_分词器.json"))
        spec = importlib.util.spec_from_file_location("fixture_boot", package / "bootstrap.py")
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)
        model = root / "空模型目录"
        model.mkdir()
        boot.configure(model)
        boot.write(package / "local.json", {"model": str(model)})
        here = boot.HERE
    if args.model_endpoint or args.controller:
        sys.path.insert(0, str(here))
        import opencode_runtime as runtime
        spec = importlib.util.spec_from_file_location("v2_settings", here / "v2/settings.py")
        settings = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(settings)
        cfg = settings.make(runtime.config_for_context(runtime.config(), args.context), here, sys.executable,
                            endpoint=args.model_endpoint, fixture=True)
        # 明确的合成词表夹具，不代表真实模型词表/语义效果已验收。
        vocab = root / "synthetic-vocab.json"
        vocab.write_text(json.dumps({"<tool_call>": 248058, "fixture": 1}), encoding="utf-8")
        manifest = root / "synthetic-tokenizer.json"
        if not args.missing_tokenizer:
            manifest.write_text(json.dumps({"model": settings.MODEL, "blocked_token": "<tool_call>", "bias": -100,
                "blocked_token_id": 248058, "vocabulary_path": str(vocab),
                "vocabulary_sha256": hashlib.sha256(vocab.read_bytes()).hexdigest()}), encoding="utf-8")
        cfg["plugins"][0]["options"]["tokenizer"] = str(manifest)
        if args.controller:
            spec = importlib.util.spec_from_file_location("fixture_controller", here / "v2/controller.py")
            controller = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(controller)
            owner = controller.Controller(runtime, boot, settings, root / "控制器记录", context=args.context)
            cfg["plugins"][0]["options"]["controller"] = owner.connection()
            runtime.safety.write(owner.file, cfg)
            env.pop("OPENCODE_CONFIG_CONTENT", None)
            env["OPENCODE_CONFIG"] = str(owner.file)
        if not args.controller:
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
        env["CTFCODE_PYTHON"] = str(Path(sys.executable).resolve())
    if args.tui:
        options = cfg["plugins"][0]["options"] if args.controller or args.model_endpoint else {
            "python": str(Path(sys.executable).resolve()), "validator": str(here / "opencode_runtime.py")}
        options = {**options, "startup": str(root / "题目 A")}
        env["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps({"tabs": {"mode": "off"}, "highlights": False,
            "plugins": [{"package": (here / "v2").as_uri(), "options": options}]})
    try:
        with module.OwnedProcessJob(process_mib=1536, total_mib=2048, max_processes=12,
                               ledger=root / "service-job.jsonl") as job:
            if args.tui:
                child = job.spawn([str(Path(args.cli).resolve()), str(root / "题目 A"), "--standalone"],
                                  cwd=root, env=env)
                return child.wait()
            with (root / "service.stdout.log").open("wb") as out, (root / "service.stderr.log").open("wb") as err:
                child = job.spawn([str(Path(args.cli).resolve()), "serve", "--hostname", "127.0.0.1",
                                   "--port", str(args.port)], cwd=root, env=env, stdout=out, stderr=err)
                # 认证信息只写父测试进程的私有pipe，不写磁盘/测试报告。
                print(json.dumps({"pid": child.pid, "port": args.port, "model_loaded": False,
                                  **({"controller": owner.connection(), "validator": str(here / "opencode_runtime.py")} if owner else {})}), flush=True)
                sys.stdin.buffer.read()
    finally:
        if owner:
            owner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
