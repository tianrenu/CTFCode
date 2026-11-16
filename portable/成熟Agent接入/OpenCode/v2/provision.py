"""控制器拥有的首次pack准备进程；复用现有分片、不启动推理，输出由TUI显示。"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    args = parser.parse_args(argv)
    path = Path(args.model)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('必须选择已存在的本机模型目录')
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('ctf_provision_boot', root / 'bootstrap.py')
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    boot.configure(path)
    if boot.ready(path):
        print('完整pack已存在，跳过准备；启动时仍须核验模型。', flush=True)
        return 0
    boot.prepare(path)
    print('PACK_READY：准备完成；尚未加载推理模型。', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as err:
        print(f'准备失败：{type(err).__name__}: {err}', file=sys.stderr, flush=True)
        raise SystemExit(1)
