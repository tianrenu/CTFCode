"""离线便携性回归；从 portable 目录运行 python -m unittest -v。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


class Portable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = list(sys.path)
        cls.modules = {name: sys.modules.pop(name, None) for name in
                       ("opencode_runtime", "opencode_engine", "opencode_safety")}
        cls.temp = tempfile.TemporaryDirectory(prefix="CTFCode 中文 空格 ")
        cls.root = Path(cls.temp.name) / "迁移后的程序"
        cls.model = Path(cls.temp.name) / "另一个位置的模型"
        cls.model.mkdir()
        src = Path(__file__).resolve().parent
        shutil.copytree(src, cls.root, ignore=shutil.ignore_patterns(
            "__pycache__", "local.json", "runtime", "components", "models", "独立环境", "程序",
            "state", "会话记录", "当前运行.json", "settings.json", "当前启动参数.json", "64K摘要输出约束_分词器.json"))
        cls.env = {key: os.environ.get(key) for key in ("CTFCODE_PYTHON", "CTFCODE_MODEL_DIR", "SHELL", "PYTHONUTF8")}
        spec = importlib.util.spec_from_file_location("portable_case", cls.root / "bootstrap.py")
        cls.boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.boot)
        cls.boot.configure(cls.model)
        sys.path.insert(0, str(cls.boot.HERE))
        import opencode_runtime
        cls.runtime = opencode_runtime

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.path
        for name, module in cls.modules.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module
        for key, value in cls.env.items():
            if value is None:
                os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        cls.temp.cleanup()

    def test_paths(self):
        self.assertTrue(self.runtime.HERE.is_relative_to(self.root))
        self.assertTrue(self.runtime.BASE.is_relative_to(self.root))
        self.assertEqual(Path(os.environ["CTFCODE_MODEL_DIR"]), self.model)

    def test_plugins(self):
        cfg = self.runtime.config()
        self.assertEqual(cfg["plugin"], [(self.runtime.HERE / "ctf-context.mjs").as_uri()])
        self.assertNotIn("${PYTHON}", json.dumps(cfg))
        self.assertIn(Path(sys.executable).as_posix(), cfg["agent"]["ctf-local"]["prompt"])

    def test_large(self):
        cfg = self.runtime.config_for_context(self.runtime.config(), "64")
        self.assertEqual(len(cfg["plugin"]), 2)
        self.assertEqual(self.runtime.context_profile(cfg)["physical_context"], 65536)
        self.assertEqual(cfg["agent"]["ctf-local"]["steps"], 240)

    def test_small(self):
        cfg = self.runtime.config_for_context(self.runtime.config(), "32")
        self.assertEqual(self.runtime.context_profile(cfg)["soft_context"], 24576)
        self.assertEqual(cfg["agent"]["ctf-local"]["steps"], 60)

    def test_window(self):
        with self.assertRaises(ValueError):
            self.runtime.config_for_context(self.runtime.config(), "128")

    def test_model(self):
        with self.assertRaises(ValueError):
            self.runtime.validate_workspace(str(self.model))
        self.assertFalse(self.boot.ready(self.model))

    def test_shards(self):
        with self.assertRaises(ValueError):
            self.boot.verify(self.model)

    def test_spec(self):
        argv = self.boot.configure(self.model)
        self.assertEqual(argv[argv.index("--port") + 1], "18791")
        self.assertEqual(argv[argv.index("--data-dir") + 1], str(self.model))
        self.assertIn("vram-reserve-mib=1792", argv)
        self.assertIn("mmap-experts=on", argv)
        self.assertIn("prefill=128", argv)

    def test_environment(self):
        cfg = self.runtime.config()
        env = self.runtime.child_env(cfg)
        self.assertEqual(env["CTFCODE_PYTHON"], str(Path(sys.executable).resolve()))
        self.assertEqual(env["OPENCODE_DISABLE_AUTOUPDATE"], "true")
        self.assertEqual(env["OPENCODE_DISABLE_DEFAULT_PLUGINS"], "true")
        self.assertTrue(Path(env["XDG_DATA_HOME"]).is_relative_to(self.root))

    def test_vocabulary(self):
        cfg = json.loads((self.runtime.HERE / "64K摘要输出约束_分词器.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(cfg["vocabulary_path"]), self.model / "packs/coder-iq1_m/tokenizer/vocab.json")
        self.assertEqual(cfg["blocked_token_id"], 248058)

    def test_roundtrip(self):
        self.boot.write(self.root / "local.json", {"model": str(self.model)})
        self.assertEqual(self.boot.model(), self.model)

    def test_controller(self):
        self.assertIn("ROOT/'bootstrap.py'", (self.root / "toolbox.py").read_text(encoding="utf-8"))
        self.assertNotIn("Remove-Item", (self.root / "toolbox.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
