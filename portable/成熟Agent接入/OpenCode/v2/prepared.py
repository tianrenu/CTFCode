"""复用Strata的prepared-pack配置生成器；只写本轮运行配置，不安装、下载或加载模型。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--context", type=int, choices=(32768, 65536), required=True)
    args = parser.parse_args(argv)
    source = Path(args.source).resolve()
    model = Path(args.model).resolve()
    out = Path(args.out).resolve()
    if not source.is_dir() or not model.is_dir() or any(
        a == b or a.is_relative_to(b) or b.is_relative_to(a)
        for a, b in ((source, model), (source, out), (model, out))
    ):
        raise ValueError("程序、模型和本轮配置目录必须独立")
    out.mkdir(parents=True, exist_ok=True)
    os.environ["STRATA_INSTALL_DIR"] = str(out)
    sys.path.insert(0, str(source))
    from strata_cli.launch import adopt
    from strata_cli.launch.catalog import Catalog
    from strata_cli.launch.runner import desc_matches, files_present
    from strata_cli.launch.settings import LaunchSettings
    catalog = Catalog(src=source, root=out)
    cfg, errors = adopt.plan(family="coder", model="IQ1_M", model_dir=model / "IQ1_M", data_dir=model,
        src=source, context=args.context, kv="q4_0", model_name="qwen3.8-flash-next-coder-iq1_m",
        family_def=catalog.tables()[1]["coder"], log=out / "engine.log", backend="cuda", pack_name="coder-iq1_m")
    if errors or cfg is None:
        raise ValueError("准备后的模型文件不完整：" + "；".join(errors))
    first = model / "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf"
    last = model / "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf"
    for flag, expected in (("--native", first), ("--ple-gguf", last), ("--pack", model / "packs/coder-iq1_m")):
        if cfg["args"].count(flag) != 1 or Path(cfg["args"][cfg["args"].index(flag) + 1]).resolve() != expected:
            raise ValueError("Strata生成的运行配置未指向选定Coder文件：" + flag)
    if "--mtp" in cfg["args"]:
        index = cfg["args"].index("--mtp")
        del cfg["args"][index:index + 2]
    # --spec是草稿数量；引擎metrics的verify窗口还计入两格，4对应已测的窗口6。
    cfg["args"][cfg["args"].index("--spec") + 1] = "4"
    cfg.update(port=18791, host="127.0.0.1")
    file = out / "strata-coder-iq1_m.json"
    if file.is_symlink() or file.exists():
        raise ValueError("本轮安装配置已存在，不覆盖未知配置")
    with file.open("x", encoding="utf-8") as stream:
        json.dump(cfg, stream, ensure_ascii=False, indent=2)
    settings = LaunchSettings(family="coder", model="IQ1_M", context=args.context, kv="q4_0", vision="off", mtp="off")
    if catalog.installed_config("coder-iq1_m") != file or files_present(cfg) or not desc_matches(catalog.strata.describe_config(file), settings):
        raise ValueError("Strata未将本轮配置识别为可复用安装；不回退下载/准备")
    print(json.dumps({"config": str(file), "context": args.context, "installed_config_matches": True,
        "model_bytes_verified": False, "downloads": False, "model_loaded": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:
        print(f"准备运行配置失败：{type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(1)
