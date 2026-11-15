"""仅用于离线回归：在Windows作业中启动隔离的V2 API，不加载推理模型。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--tui", action="store_true")
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
    if args.model_endpoint:
        here = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode"
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
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
        env["CTFCODE_PYTHON"] = str(Path(sys.executable).resolve())
    if args.tui:
        source = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode"
        env["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps({"tabs": {"mode": "off"}, "highlights": False,
            "plugins": [{"package": (source / "v2").as_uri(), "options": {
                "python": str(Path(sys.executable).resolve()), "validator": str(source / "opencode_runtime.py"),
                "startup": str(root / "题目 A")}}]})
    with module.OwnedProcessJob(process_mib=1536, total_mib=2048, max_processes=12,
                               ledger=root / "service-job.jsonl") as job:
        if args.tui:
            child = job.spawn([str(Path(args.cli).resolve()), str(root / "题目 A"), "--standalone"],
                              cwd=root, env=env)
            return child.wait()
        with (root / "service.stdout.log").open("wb") as out, (root / "service.stderr.log").open("wb") as err:
            child = job.spawn([str(Path(args.cli).resolve()), "serve", "--hostname", "127.0.0.1",
                               "--port", str(args.port)], cwd=root, env=env, stdout=out, stderr=err)
            print(json.dumps({"pid": child.pid, "port": args.port, "model_loaded": False}), flush=True)
            sys.stdin.buffer.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
