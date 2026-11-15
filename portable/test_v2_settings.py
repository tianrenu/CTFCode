"""V2配置映射的离线测试，不启动CLI/模型。"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent / "成熟Agent接入/OpenCode"
sys.path.insert(0, str(HERE))
import opencode_runtime as runtime

spec = importlib.util.spec_from_file_location("v2_settings", HERE / "v2/settings.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


class Settings(unittest.TestCase):
    def config(self, context="32", **kwargs):
        cfg = runtime.config_for_context(runtime.config(), context)
        return settings.make(cfg, HERE, sys.executable, **kwargs)

    def test_profiles(self):
        for context, window, steps, timeout in (("32", 24576, 60, 1800000), ("64", 65536, 240, 4500000)):
            cfg = self.config(context)
            model = cfg["providers"]["strata-local"]["models"][settings.MODEL]
            self.assertEqual(model["limit"], {"context": window, "output": 4096})
            self.assertEqual(cfg["agents"]["ctf-local"]["steps"], steps)
            self.assertEqual(cfg["providers"]["strata-local"]["settings"]["timeout"], timeout)
            self.assertTrue(cfg["compaction"]["auto"])
            self.assertFalse(cfg["experimental"]["background_compaction"])

    def test_no_legacy_fields(self):
        cfg = self.config()
        self.assertFalse(set(cfg) & {"small_model", "provider", "agent", "permission", "plugin", "enabled_providers"})
        self.assertEqual(list(cfg["providers"]), ["strata-local"])

    def test_shell_rules(self):
        cfg = self.config()
        shell = [rule for rule in cfg["permissions"] if rule["action"] == "shell"]
        self.assertTrue(any(rule["resource"] == "*" and rule["effect"] == "allow" for rule in shell))
        self.assertTrue(any("Remove-Item" in rule["resource"] and rule["effect"] == "ask" for rule in shell))
        self.assertFalse(any(rule["action"] == "bash" for rule in cfg["permissions"]))
        self.assertEqual(cfg["permissions"][-1], {"action": "external_directory", "resource": "*", "effect": "deny"})

    def test_archive_protected(self):
        cfg = self.config()
        for pattern in (".ctfcode/**", "**/.ctfcode/**"):
            self.assertIn({"action": "edit", "resource": pattern, "effect": "deny"}, cfg["permissions"])
        self.assertIn("不自动读取旧CTF_STATE.md", cfg["agents"]["ctf-local"]["system"])

    def test_endpoint(self):
        for endpoint in ("http://127.0.0.1:8080/v1", "https://127.0.0.1:18791/v1", "http://example.com:18791/v1",
                         "http://user:pass@127.0.0.1:18791/v1", "http://127.0.0.1:18791/v1?x=1", "http://127.0.0.1:18792/v1"):
            with self.assertRaises(ValueError):
                self.config(endpoint=endpoint)
        cfg = self.config(endpoint="http://127.0.0.1:18792/v1", fixture=True)
        self.assertEqual(cfg["providers"]["strata-local"]["settings"]["baseURL"], "http://127.0.0.1:18792/v1")

    def test_shell_and_paths(self):
        cfg = self.config()
        self.assertEqual(cfg["shell"], (Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe").as_posix())
        self.assertEqual(cfg["plugins"][0]["package"], (HERE / "v2").as_uri())
        self.assertEqual(cfg["plugins"][0]["options"]["python"], str(Path(sys.executable).resolve()))
        self.assertNotIn("${PYTHON}", json.dumps(cfg))


if __name__ == "__main__":
    unittest.main()
