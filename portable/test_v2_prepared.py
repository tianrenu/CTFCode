"""通过真实Strata配置代码复用prepared pack；合成权重只测试元数据，不启动引擎。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = os.environ.get("CTFCODE_STRATA_CONTROL")
SCRIPT = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode/v2/prepared.py"


@unittest.skipUnless(SOURCE, "须显式提供已核验的Strata Control目录；不自动下载")
class Prepared(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="CTFCode prepared 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "合成模型（非真实权重）"
        for name in ("IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf",
                     "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf",
                     "packs/coder-iq1_m/experts.bin", "packs/coder-iq1_m/dense.bin",
                     "packs/coder-iq1_m/tokenizer/vocab.json"):
            path = self.model / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic-not-a-model", encoding="utf-8")

    def runprep(self, context, *, code=0):
        result = subprocess.run([str(Path(SOURCE) / "python/python.exe"), "-I", "-X", "utf8", str(SCRIPT),
            "--source", SOURCE, "--model", str(self.model), "--out", str(self.root / ("运行_" + str(context))),
            "--context", str(context)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def test_profiles(self):
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.model.rglob("*") if p.is_file()}
        for context in (32768, 65536):
            report = json.loads(self.runprep(context).stdout)
            self.assertTrue(report["installed_config_matches"])
            self.assertFalse(report["model_loaded"])
            self.assertFalse(report["model_bytes_verified"])
            self.assertFalse(report["downloads"])
            cfg = json.loads(Path(report["config"]).read_text(encoding="utf-8"))
            self.assertEqual(cfg["port"], 18791)
            self.assertEqual(cfg["host"], "127.0.0.1")
            self.assertEqual(cfg["exe"], "component:strata-engine")
            self.assertEqual(cfg["args"][cfg["args"].index("--max-context") + 1], str(context))
            self.assertEqual(cfg["args"][cfg["args"].index("--spec") + 1], "4")
            self.assertNotIn("--mtp", cfg["args"])
        after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.model.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "生成运行配置不得改动模型文件")

    def test_missing(self):
        (self.model / "packs/coder-iq1_m/experts.bin").unlink()
        self.assertIn("模型文件不完整", self.runprep(32768, code=1).stderr)

    def test_wrong_model(self):
        for index in (1, 2):
            (self.model / ("IQ1_M/AAA-Other-%05d-of-00002.gguf" % index)).write_text("synthetic", encoding="utf-8")
        self.assertIn("未指向选定Coder", self.runprep(32768, code=1).stderr)

    def test_existing(self):
        self.runprep(32768)
        self.assertIn("已存在", self.runprep(32768, code=1).stderr)

    def test_overlap(self):
        for out in (Path(SOURCE), self.model, self.model / "packs", self.root):
            with self.subTest(out=out):
                result = subprocess.run([str(Path(SOURCE) / "python/python.exe"), "-I", "-X", "utf8", str(SCRIPT),
                    "--source", SOURCE, "--model", str(self.model), "--out", str(out), "--context", "32768"],
                    capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("必须独立", result.stderr)


if __name__ == "__main__":
    unittest.main()
