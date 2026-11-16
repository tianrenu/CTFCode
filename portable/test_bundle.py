"""真实文件系统的便携构建边界；不下载组件、不加载模型。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("bundle", Path(__file__).resolve().parents[1] / "script/portable.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class Bundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="CTFCode 打包 边界 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_native(self):
        file = self.root / "untrusted.exe"
        file.write_bytes(b"must never execute")
        with self.assertRaisesRegex(ValueError, "已核验"):
            bundle.native(file)

    @unittest.skipUnless(os.environ.get("CTFCODE_V2_CLI"), "需指定已核验的定制CLI")
    def test_pinned(self):
        cfg = bundle.native(os.environ["CTFCODE_V2_CLI"])
        self.assertTrue(cfg["version"].startswith("2.3.0-ctfcode."))
        self.assertIn("compaction-provenance", cfg["features"])

    def test_sources(self):
        self.assertNotIn("dependencies.json", bundle.SOURCES)
        self.assertNotIn("dependencies.bun.lock", bundle.SOURCES)
        for name in ("activity.mjs", "journal.mjs", "status.mjs", "cli.json", "welcome.mjs", "provision.py"):
            self.assertIn("成熟Agent接入/OpenCode/v2/" + name, bundle.SOURCES)
        self.assertEqual([name for name in bundle.SOURCES if name.endswith(".bat")], ["3_CTF做题界面.bat", "7_自检.bat"])

    @unittest.skipUnless(os.environ.get("CTFCODE_LAUNCHER"), "需指定已编译入口")
    def test_launcher(self):
        cfg = bundle.launcher(os.environ["CTFCODE_LAUNCHER"])
        self.assertEqual(cfg["version"], "0.1.0-preview.4")
        self.assertIn("launcher/Program.cs", cfg["sources"])

    def test_paths(self):
        for name in ("../outside", "/outside", "C:/outside", "dir\\outside"):
            with self.assertRaises(ValueError):
                bundle.safe(name)
        self.assertEqual(str(bundle.safe("中文/有效.txt")), "中文/有效.txt")

    def recipe(self):
        file = self.root / "included.txt"
        file.write_text("known fixture", encoding="utf-8")
        data = {"format": 1, "contains_models": False, "contains_sessions": False, "files": [
            {"path": file.name, "bytes": file.stat().st_size, "sha256": bundle.digest(file)}]}
        (self.root / "bundle-files.json").write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(input=str(self.root), output=str(self.root / "bundle.zip"))

    def test_whitelist(self):
        opts = self.recipe()
        (self.root / "private.gguf").write_bytes(b"not a model; must not ship")
        (self.root / "local.json").write_text('{"private":"setting"}', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            bundle.pack(opts)
        with zipfile.ZipFile(opts.output) as archive:
            self.assertEqual(archive.namelist(), ["CTFCode/included.txt", "CTFCode/bundle-files.json"])

    def test_changed(self):
        opts = self.recipe()
        (self.root / "included.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            bundle.pack(opts)
        self.assertFalse(Path(opts.output).exists(), "校验失败不能留下看似可用的ZIP")

    def test_link(self):
        opts = self.recipe()
        file = self.root / "included.txt"
        target = self.root / "outside.txt"
        file.rename(target)
        try:
            file.symlink_to(target)
        except OSError as err:
            self.skipTest(str(err))
        with self.assertRaisesRegex(ValueError, "发生变化"):
            bundle.pack(opts)
        self.assertFalse(Path(opts.output).exists())


if __name__ == "__main__":
    unittest.main()
