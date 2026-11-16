"""真实loopback控制器、Windows独占锁与迁移目录；不加载GPU模型。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import requests


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Controller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = list(sys.path)
        cls.modules = {name: sys.modules.pop(name, None) for name in
                       ("opencode_runtime", "opencode_engine", "opencode_safety")}
        cls.env = dict(os.environ)
        cls.temp = tempfile.TemporaryDirectory(prefix="CTFCode 控制器 中文 ")
        cls.root = Path(cls.temp.name)
        cls.package = cls.root / "独立程序"
        shutil.copytree(Path(__file__).resolve().parent, cls.package, ignore=shutil.ignore_patterns(
            "__pycache__", "local.json", "runtime", "components", "models", "独立环境", "程序", "state",
            "会话记录", "当前运行.json", "settings.json", "当前启动参数.json", "64K摘要输出约束_分词器.json"))
        cls.boot = module("controller_boot", cls.package / "bootstrap.py")
        cls.model = cls.root / "未准备的模型"
        cls.model.mkdir()
        cls.boot.configure(cls.model)
        cls.boot.write(cls.package / "local.json", {"model": str(cls.model)})
        cls.here = cls.boot.HERE
        sys.path.insert(0, str(cls.here))
        import opencode_runtime
        cls.runtime = opencode_runtime
        cls.settings = module("controller_settings", cls.here / "v2/settings.py")
        cls.control = module("controller_impl", cls.here / "v2/controller.py")

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.path
        for name, value in cls.modules.items():
            sys.modules.pop(name, None)
            if value is not None:
                sys.modules[name] = value
        os.environ.clear()
        os.environ.update(cls.env)
        cls.temp.cleanup()

    def setUp(self):
        self.boot.configure(self.model)
        self.boot.write(self.package / "local.json", {"model": str(self.model)})
        self.out = self.root / ("控制轮次_" + self._testMethodName)
        self.owner = self.control.Controller(self.runtime, self.boot, self.settings, self.out)
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers.update(Authorization="Bearer " + self.owner.connection()["key"])
        self.addCleanup(self.http.close)
        self.addCleanup(self.owner.close)

    def post(self, action, body=None, *, code=200, headers=None):
        response = self.http.post(self.owner.endpoint + "/" + action, json=body or {}, headers=headers, timeout=5)
        self.assertEqual(response.status_code, code, response.text)
        return response.json()

    def test_status(self):
        data = self.post("status")["data"]
        self.assertEqual(data["model_state"], "stopped")
        self.assertIsNone(data["native_pid"])
        self.assertFalse(data["model_files_present"])
        self.assertIsNone(self.runtime.engine.native)
        for path in (self.out / "会话状态.json", self.here / "当前运行.json"):
            self.assertNotIn(self.owner.secret, path.read_text(encoding="utf-8"))
        self.assertNotIn(self.owner.secret, json.dumps(data))

    def test_preparation(self):
        for body in ({}, {"confirm": False}, {"confirm": 1}, {"confirm": True, "command": "unexpected"}):
            self.post("prepare", body, code=409)
        self.assertIn("分片缺失", self.post("prepare", {"confirm": True}, code=409)["error"])
        self.assertFalse(self.post("status")["data"]["model_inventory"]["weights_present"])
        self.assertIsNone(self.owner.thread)
        self.assertIsNone(self.runtime.engine.native)

    def test_progress(self):
        file = self.out / '准备.log'
        file.write_text('x' * 5000 + '\n进度：50%\n', encoding='utf-8')
        self.owner.state.update(preparation_state='running', preparation_log=str(file), model_state='preparing')
        state = self.post('status')['data']
        self.assertLessEqual(len(state['preparation_tail']), 2000)
        self.assertIn('50%', state['preparation_tail'])
        self.assertNotIn(self.owner.secret, json.dumps(state))
        self.post('workspace', {'directory': str(self.root), 'session': 'ses_during_prepare'}, code=409)
        self.owner.state['model_state'] = 'stopped'

    def test_cancel_preparation(self):
        # Real process-tree fixture, not real weights: exercise the production owner/cancel path.
        file = self.here / 'v2/provision.py'
        before = file.read_bytes()
        marker = self.out / 'worker.json'
        file.write_text('import json,os,subprocess,sys,time\n'
            'from pathlib import Path\n'
            'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(90)"])\n'
            f'Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n'
            'print("SYNTHETIC_PREPARATION_RUNNING",flush=True)\ntime.sleep(90)\n', encoding='utf-8')
        try:
            self.runtime.engine.abort.clear()
            self.owner.state.update(model_state='preparing', preparation_state='running')
            self.owner.thread = threading.Thread(target=self.owner.provision, daemon=True)
            self.owner.thread.start()
            deadline = time.monotonic() + 15
            while not marker.exists():
                self.assertLess(time.monotonic(), deadline, self.owner.state)
                time.sleep(0.05)
            pids = json.loads(marker.read_text())
            self.assertIn('SYNTHETIC', self.post('status')['data']['preparation_tail'])
            self.post('stop')
            previous = self.owner.thread
            self.post('stop')
            self.assertIs(self.owner.thread, previous, '重复停止不应启动第二个并发清理线程')
            self.settle()
            self.assertEqual(self.owner.state['preparation_state'], 'cancelled')
            self.assertEqual(self.owner.state['model_state'], 'stopped')
            self.assertNotIn('cleanup_error', self.owner.state)
            self.assertTrue(all(not self.runtime.psutil.pid_exists(pid) for pid in pids))
            self.assertIsNone(self.runtime.engine.native)
            self.assertTrue(marker.exists(), '取消不应删除用户或准备进程的文件')
        finally:
            self.post('stop')
            self.settle()
            file.write_bytes(before)

    def test_provision_failure(self):
        self.owner.state.update(model_state='preparing', preparation_state='running')
        self.runtime.engine.abort.clear()
        self.owner.provision()
        self.assertEqual(self.owner.state['preparation_state'], 'failed')
        self.assertEqual(self.owner.state['model_state'], 'failed')
        self.assertIn('分片', self.post('status')['data']['preparation_tail'])
        self.assertIsNone(self.owner.state['preparation_pid'])
        self.assertIsNone(self.runtime.engine.native)

    @unittest.skipUnless(os.name == "nt", "Win32元数据长路径")
    def test_extended(self):
        a = Path(self.control.extended(self.out / ("a" * 90)))
        b = a / ("b" * 90)
        file = b / "state.json"
        self.assertGreater(len(str(file)), 260)
        try:
            b.mkdir(parents=True)
            file.write_text('{"owned":true}', encoding="utf-8")
            self.assertEqual(json.loads(file.read_text(encoding="utf-8")), {"owned": True})
            self.assertEqual(self.control.extended(file), str(file))
        finally:
            if file.exists():
                file.unlink()
            if b.exists():
                b.rmdir()
            if a.exists():
                a.rmdir()

    def test_auth(self):
        self.post("status", code=403, headers={"Authorization": "Bearer wrong"})
        self.post("status", code=403, headers={"Origin": "https://example.invalid"})
        self.post("status", code=403, headers={"Host": "localhost:" + str(self.owner.server.server_port)})
        self.post("not-a-route", code=404)
        self.assertEqual(self.owner.state["model_state"], "stopped")

    def test_journal_link(self):
        journal = self.out / "控制轨迹.jsonl"
        saved = journal.with_suffix(".saved")
        target = self.root / "不得修改的轨迹.txt"
        target.write_text("keep", encoding="utf-8")
        journal.rename(saved)
        try:
            os.link(target, journal)
            with self.assertRaisesRegex(ValueError, "普通独立文件"):
                self.owner.save()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        finally:
            journal.unlink(missing_ok=True)
            saved.rename(journal)

    def test_journal_symlink(self):
        journal = self.out / "控制轨迹.jsonl"
        saved = journal.with_suffix(".saved")
        target = self.root / "不存在的轨迹.txt"
        journal.rename(saved)
        try:
            try:
                journal.symlink_to(target)
            except OSError as err:
                self.skipTest("系统不允许创建测试符号链接：" + str(err))
            with self.assertRaisesRegex(ValueError, "普通独立文件"):
                self.owner.save()
            self.assertFalse(target.exists(), "不得沿断开的符号链接写入")
        finally:
            journal.unlink(missing_ok=True)
            saved.rename(journal)

    def test_workspace(self):
        a = self.root / "题目 A"
        b = self.root / "题目 B"
        a.mkdir(exist_ok=True)
        b.mkdir(exist_ok=True)
        one = self.post("workspace", {"directory": str(a), "session": "ses_test_a"})["data"]
        two = self.post("workspace", {"directory": str(b), "session": "ses_test_b"})["data"]
        self.assertNotEqual(one["activity_directory"], two["activity_directory"])
        self.assertTrue(Path(one["activity_directory"]).is_dir())
        self.assertEqual(two["workspace"], str(b))
        self.post("workspace", {"directory": str(self.model), "session": "ses_model"}, code=409)
        self.post("workspace", {"directory": 3, "session": "ses_invalid"}, code=409)
        self.assertEqual(self.owner.state["workspace"], str(b))

    def test_profiles(self):
        for value, window in (("64", 65536), ("32", 24576)):
            self.post("select", {"context": value})
            cfg = json.loads(self.owner.file.read_text(encoding="utf-8"))
            self.assertEqual(cfg["providers"]["strata-local"]["models"][self.settings.MODEL]["limit"]["context"], window)
            self.assertEqual(cfg["plugins"][0]["options"]["controller"], self.owner.connection())
        self.post("select", {"context": "128"}, code=409)
        self.assertEqual(self.owner.state["context"], "32")
        text = (self.out / "控制轨迹.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(any(row["context"] == "64" for row in rows))
        self.assertEqual(rows[-1]["context"], "32")
        self.assertNotIn(self.owner.secret, text)

    def test_activity(self):
        work = self.root / "状态测试题目"
        work.mkdir()
        self.post("workspace", {"directory": str(work), "session": "ses_activity_a"})
        data = {"phase": "succeeded", "step": 1, "tools": 2, "failures": 0, "pending": 0, "semantic_result_verified": False}
        body = {"session": "ses_activity_a", "directory": str(work), "data": data}
        self.post("activity", body)
        state = self.post("status")["data"]
        self.assertEqual(state["model_state"], "stopped", "任务结束不等于模型加载或卸载")
        self.assertEqual(state["task"], data)
        ui = module("controller_toolbox", self.package / "toolbox.py")
        self.assertEqual(ui.agent_activity(state)["说明"], "本轮结束，答案未经认证")
        file = Path(state["activity_directory"]) / "Agent状态.json"
        self.assertEqual(json.loads(file.read_text(encoding="utf-8")), data)
        self.post("activity", {**body, "data": {**data, "semantic_result_verified": True}}, code=409)
        self.post("activity", {**body, "data": {**data, "native_pid": 123}}, code=409)
        self.post("workspace", {"directory": str(work), "session": "ses_activity_b"})
        self.assertIsNone(self.post("status")["data"]["task"])
        self.post("activity", body, code=409)
        self.assertEqual(json.loads(file.read_text(encoding="utf-8")), data, "旧任务状态留在原会话，不能覆盖新会话")

    def test_exit(self):
        self.owner.state["exit_requested"] = True
        self.assertIn("归档并退出", self.post("start", code=409)["error"])
        self.assertIn("归档", self.post("ready", code=409)["error"])
        self.post("select", {"context": "64"}, code=409)
        self.post("workspace", {"directory": str(self.root), "session": "ses_exit"}, code=409)
        self.assertTrue(self.post("status")["data"]["exit_requested"])
        self.post("stop")

    def test_unconfigured(self):
        self.post("start", code=409)
        work = self.root / "空白题目"
        work.mkdir(exist_ok=True)
        self.post("workspace", {"directory": str(work), "session": "ses_empty"})
        data = self.post("start", code=409)
        self.assertIn("模型文件尚未准备好", data["error"])
        self.post("ready", {"directory": str(work), "session": "ses_empty", "physical": 32768}, code=409)
        self.post("stop")
        self.post("stop")
        self.assertIsNone(self.runtime.engine.native)
        self.assertIsNone(self.owner.thread)

    def test_model(self):
        path = self.root / "另一个模型目录"
        path.mkdir(exist_ok=True)
        self.owner.state.update(preparation_state="failed", preparation_log="old-model.log")
        data = self.post("select", {"model": str(path)})["data"]
        self.assertEqual(data["model_directory"], str(path))
        self.assertEqual(self.boot.model(), path)
        self.assertFalse(data.get("model_files_present", False))
        self.assertNotIn("preparation_log", data, "换模型不能混用旧模型的准备状态；日志文件仍保留")
        self.assertFalse(list(path.iterdir()), "选择目录不能下载或复制模型")
        self.post("select", {"model": str(self.root / "不存在")}, code=409)
        self.post("select", {"model": str(Path(path.anchor))}, code=409)

    def test_partial_config(self):
        temp = self.package / "local.json.tmp"
        temp.mkdir()
        try:
            self.post("select", {"context": "64"}, code=409)
            self.assertIn("configuration_error", self.owner.state)
            self.assertEqual(self.owner.state["model_state"], "failed")
            self.assertIn("设置尚未完整保存", self.post("start", code=409)["error"])
        finally:
            temp.rmdir()
        self.post("select", {"context": "32"})
        self.assertNotIn("configuration_error", self.owner.state)
        self.assertEqual(self.owner.state["model_state"], "stopped")

    def test_lock(self):
        with self.assertRaisesRegex(RuntimeError, "已有本地执行器"):
            self.control.Controller(self.runtime, self.boot, self.settings, self.root / "第二控制器")
        self.assertEqual(self.post("status")["data"]["owner_pid"], os.getpid())

    def prepared(self):
        # 合成空文件只用于通过存在性检查；64K签名组件缺失会在任何GPU初始化前拒绝。
        path = self.root / (self._testMethodName + "_合成模型")
        path.mkdir()
        for name in ("IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf",
                     "IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf",
                     "packs/coder-iq1_m/experts.bin", "packs/coder-iq1_m/dense.bin",
                     "packs/coder-iq1_m/tokenizer/vocab.json"):
            file = path / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("synthetic-not-a-model", encoding="utf-8")
        self.post("select", {"model": str(path), "context": "64"})
        work = self.root / (self._testMethodName + "_题目")
        work.mkdir()
        self.post("workspace", {"directory": str(work), "session": "ses_guard"})

    def settle(self):
        deadline = time.monotonic() + 10
        while self.owner.thread and self.owner.thread.is_alive():
            self.assertLess(time.monotonic(), deadline, "受控失败/取消没有及时结束")
            time.sleep(0.02)

    def test_start_failure(self):
        self.prepared()
        self.owner.state["runtime_context"] = {"verified": True, "physical_context": 32768}
        self.owner.state["engine_release"] = {"version": "old-generation"}
        self.post("start")
        self.settle()
        self.assertEqual(self.owner.state["model_state"], "failed")
        self.assertIn("64K已签名组件尚未完整安装", self.owner.state["error"])
        self.assertTrue(self.owner.state["cleanup"]["native_stopped"])
        self.assertIsNone(self.runtime.engine.native)
        self.assertNotIn("runtime_context", self.owner.state, "不得沿用上轮已核验状态")
        self.assertNotIn("engine_release", self.owner.state)
        self.assertIsNone(self.owner.monitor, "签名门禁之前不得初始化GPU监控/加载")
        self.post("select", {"context": "32"})

    def test_stop_loading(self):
        self.prepared()
        self.post("start")
        self.post("stop")
        self.settle()
        self.assertIn(self.owner.state["model_state"], ("stopped", "failed"))
        self.assertTrue(self.owner.state["cleanup"]["native_stopped"])
        self.assertIsNone(self.runtime.engine.native)
        self.post("stop")

    @unittest.skipUnless(os.environ.get("CTFCODE_V2_CLI"), "须显式提供隔离V2 CLI，不使用桌面/全局CLI")
    def test_launch(self):
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.package / "bootstrap.py"),
            "ui", "--cli", os.environ["CTFCODE_V2_CLI"], "--dry-run", "--context", "64"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["version"], json.loads((self.here / "v2/cli.json").read_text(encoding="utf-8"))["version"])
        self.assertTrue(data["model_not_loaded"])
        self.assertTrue(data["standalone"])
        self.assertIsNone(data["startup"], "无题目目录也能先打开TUI")
        self.assertEqual(data["context"]["physical_context"], 65536)
        self.assertIsNone(self.runtime.engine.native)

    def test_cli_hash(self):
        file = self.root / "invalid.exe"
        file.write_bytes(b"not an executable; must never run")
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.package / "bootstrap.py"),
            "ui", "--native", "--cli", str(file), "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("不是已核验的CTFCode定制构建", result.stderr)
        self.assertIsNone(self.runtime.engine.native)


if __name__ == "__main__":
    unittest.main()
