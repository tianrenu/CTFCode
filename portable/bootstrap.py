"""便携入口：只配置本包，复用用户模型，不修改旧工具箱或全局环境。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
BASE = ROOT / "本地模型配置与日志/Strata验证"
HERE = ROOT / "成熟Agent接入/OpenCode"
CONTROL = BASE / "runtime/control-0.1.52"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))


def write(path, value):
    if path.is_symlink():
        raise ValueError("拒绝覆盖链接：" + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    if temp.is_symlink():
        raise ValueError("临时文件不能是链接")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def model():
    path = ROOT / "local.json"
    if not path.exists():
        return (BASE / "models/GSQ-RCO-Coder").resolve()
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("本机设置不是有界普通文件")
    value = json.loads(path.read_text(encoding="utf-8"))["model"]
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("模型目录必须为绝对路径")
    return Path(value).resolve()


def configure(path):
    path = path.resolve()
    if path in (Path(path.anchor), Path.home(), ROOT):
        raise ValueError("请选择模型专用目录，不能使用盘根、主目录或程序根目录")
    os.environ.update(CTFCODE_PYTHON=str(Path(sys.executable).resolve()),
                      CTFCODE_MODEL_DIR=str(path), PYTHONUTF8="1")
    if os.name == "nt":
        os.environ["SHELL"] = str(Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    for key in ("CTF_ASSISTED_POLICY", "CTF_TUI_STATUS_FILE"):
        os.environ.pop(key, None)
    BASE.mkdir(parents=True, exist_ok=True)
    write(BASE / "settings.json", {"data_dir": str(path), "installs": [str(CONTROL)]})
    argv = [str(CONTROL / "python/python.exe"), str(CONTROL / "strata.py"), "launch",
            "--family", "coder", "--model", "IQ1_M", "--model-path",
            str(path / "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf"),
            "--data-dir", str(path), "--context", "32k", "--parallel", "1", "--vision", "off",
            "--mtp", "off", "--access", "local", "--port", "18791", "--tune", "skip",
            "--feed", str(BASE), "--yes", "--no-tui", "--set", "vram-reserve-mib=1792",
            "--set", "resident-experts=off", "--set", "mmap-experts=on", "--set", "prompt-cache=1",
            "--set", "conversation-cache-mib=0", "--set", "prefill=128", "--kv", "q4_0"]
    write(BASE / "当前启动参数.json", argv)
    cfg = json.loads((HERE / "64K摘要输出约束_分词器.template.json").read_text(encoding="utf-8"))
    cfg["vocabulary_path"] = str(path / "packs/coder-iq1_m/tokenizer/vocab.json")
    write(HERE / "64K摘要输出约束_分词器.json", cfg)
    return argv


def verify(path):
    cfg = json.loads((BASE / "官方模型哈希.json").read_text(encoding="utf-8"))
    for file in cfg["files"]:
        item = path / file["path"]
        if not item.is_file() or item.is_symlink() or item.stat().st_size != file["bytes"]:
            raise ValueError("缺少正确大小的模型分片：" + str(item))
        print("核验 SHA256：" + item.name, flush=True)
        with item.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != file["sha256"]:
                raise ValueError("模型 SHA256 不符，不继续准备或加载：" + str(item))


def ready(path):
    return all((path / rel).is_file() for rel in (
        "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf",
        "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf",
        "packs/coder-iq1_m/experts.bin", "packs/coder-iq1_m/dense.bin",
        "packs/coder-iq1_m/tokenizer/vocab.json"))


def prepare(path):
    verify(path)
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env.update(STRATA_INSTALL_DIR=str(CONTROL), STRATA_SETTINGS_FILE=str(BASE / "settings.json"),
               STRATA_COMPONENTS_DIR=str(BASE / "components"), STRATA_REQUIRE_SIGNED_COMPONENTS="1",
               NO_PROXY="*", PYTHONUTF8="1")
    print("正在从现有分片准备 pack；不迁移权重、不启动推理模型。", flush=True)
    argv = [str(CONTROL / "python/python.exe"), str(CONTROL / "strata.py"), "install", "--",
            "--family", "coder", "--model", "IQ1_M", "--gguf-dir", str(path / "IQ1_M"),
            "--data-dir", str(path), "--context", "32768", "--kv", "q4_0", "--vision", "no",
            "--mtp", "off", "--cuda", "13", "--low-ram", "mmap", "--vram-reserve-mib", "1792",
            "--parallel", "1", "--port", "18791", "--host", "127.0.0.1", "--no-browser", "--no-start"]
    subprocess.run(argv, env=env, cwd=CONTROL, check=True)
    if not ready(path):
        raise RuntimeError("模型准备未得到全部必要文件，请保留输出排查")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["setup"]:
        parser = argparse.ArgumentParser(description="选择现有模型目录；不搬动模型")
        parser.add_argument("--model", required=True)
        parser.add_argument("--prepare", action="store_true")
        parser.add_argument("--verify", action="store_true")
        opts = parser.parse_args(args[1:])
        path = Path(opts.model).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("模型目录必须是已存在的绝对目录")
        sys.path.insert(0, str(HERE))
        configure(model())
        import toolbox
        state = toolbox.read_status()
        if state and toolbox.owner_alive(state):
            raise RuntimeError("当前会话仍在运行，请先正常退出，不能热改模型目录")
        configure(path)
        if opts.prepare:
            prepare(path)
        if opts.verify and not opts.prepare:
            verify(path)
        write(ROOT / "local.json", {"model": str(path.resolve())})
        print("已保存本包模型位置；未修改旧工具箱、桌面版或全局配置。")
        return 0
    path = model()
    if args[:1] == ["ui"] and "--dry-run" not in args and not ready(path):
        if not sys.stdin.isatty():
            raise RuntimeError("尚未配置模型；先使用 setup --model <模型目录> --prepare")
        print("便携包已含 CLI、定制插件、Python、Strata 及引擎。模型权重须单独下载。")
        value = input("输入已有模型目录（内部应有 IQ1_M；回车取消）：").strip().strip('"')
        if not value:
            return 0
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("模型目录必须是已存在的绝对目录")
        configure(path)
        if not ready(path):
            prepare(path)
        write(ROOT / "local.json", {"model": str(path.resolve())})
    configure(path)
    import toolbox
    return toolbox.main(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as err:
        print(f"操作失败：{type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(1)
