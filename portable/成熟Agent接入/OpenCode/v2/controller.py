"""TUI专用模型控制器。只复用现有单模型加载器和保护，不向模型注册控制工具。"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

import psutil


class Controller:
    def __init__(self, runtime, boot, settings, out, *, context="32"):
        self.runtime = runtime
        self.boot = boot
        self.settings = settings
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.mutex = threading.RLock()
        self.thread = None
        self.monitor = None
        self.closed = False
        self.generation = 0
        self.env = dict(runtime.engine.ENV)
        self.state = {"state": "running", "model_state": "stopped", "out": str(self.out),
                      "owner_pid": os.getpid(), "owner_created": psutil.Process().create_time(),
                      "workspace": None, "session": None, "model_directory": str(boot.model()),
                      "context": context, "generation": 0, "native_pid": None, "native_created": None}
        self.cfg = runtime.config_for_context(runtime.config(), context)
        self.file = self.out / "opencode.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server.daemon_threads = True
        self.secret = secrets.token_urlsafe(32)
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)
        self.http = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.guard = runtime.engine.RunLock()
        try:
            self.guard.__enter__()
            self.configure()
        except BaseException:
            self.server.server_close()
            if hasattr(self.guard, "file") and not self.guard.file.closed:
                self.guard.__exit__()
            raise
        self.http.start()

    def save(self):
        self.state["updated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.runtime.safety.write(self.out / "会话状态.json", self.state)
        self.runtime.safety.write(self.runtime.HERE / "当前运行.json", self.state)

    def configure(self):
        cfg = self.settings.make(self.cfg, self.runtime.HERE, Path(sys.executable))
        cfg["plugins"][0]["options"]["controller"] = self.connection()
        self.runtime.safety.write(self.file, cfg)
        self.state.update(physical_context=self.runtime.context_profile(self.cfg)["physical_context"],
                          soft_context=self.runtime.context_profile(self.cfg)["soft_context"])
        self.save()

    def connection(self):
        # 只进入独立插件配置；不得加入状态、归档或模型工具参数。
        return {"endpoint": self.endpoint, "key": self.secret}

    def idle(self):
        if self.closed:
            raise RuntimeError("控制器已退出")
        if self.state.get("cleanup_error"):
            raise RuntimeError("上轮清理尚未成功，请先停止并核验；不能再加载或更改模型")
        if self.thread and self.thread.is_alive() or self.state["model_state"] in ("loading", "ready", "stopping"):
            raise RuntimeError("请先停止模型并等待卸载，再修改模型目录或上下文档位")

    def select(self, value):
        with self.mutex:
            self.idle()
            if set(value) - {"context", "model"} or not value:
                raise ValueError("设置只接受上下文或模型目录")
            context = value.get("context", self.state["context"])
            cfg = self.runtime.config_for_context(self.runtime.config(), context)
            if "model" in value:
                if not isinstance(value["model"], str) or any(char in value["model"] for char in "\x00\r\n"):
                    raise ValueError("模型目录无效")
                path = Path(value["model"]).expanduser()
                if not path.is_absolute() or not path.is_dir() or str(path).startswith("\\\\"):
                    raise ValueError("请选择已存在的本机模型目录；不复制、不下载权重")
                path = path.resolve()
                workspace = self.state["workspace"]
                if workspace and (path.is_relative_to(workspace) or Path(workspace).is_relative_to(path)):
                    raise ValueError("模型目录与题目目录不能互相包含")
            path = path if "model" in value else Path(self.state["model_directory"])
            try:
                self.boot.configure(path)
                self.boot.write(self.boot.ROOT / "local.json", {"model": str(path)})
                self.cfg = cfg
                self.state.update(context=context, model_directory=str(path), model_state="stopped", error=None)
                self.configure()
                self.state.pop("configuration_error", None)
                self.save()
            except Exception as err:
                # 多个配置文件不是一个文件系统事务；失败后必须禁用加载，不能沿用半更新状态。
                self.state.update(model_state="failed", configuration_error=str(err), error="设置未完整保存，请重新选择配置：" + str(err))
                self.save()
                raise RuntimeError(self.state["error"]) from err
            return copy.deepcopy(self.state)

    def workspace(self, value):
        with self.mutex:
            if self.closed or self.state["model_state"] in ("loading", "stopping"):
                raise RuntimeError("模型正在加载/卸载，请等待后再切换目录")
            if set(value) != {"directory", "session"} or not isinstance(value["directory"], str) or not isinstance(value["session"], str) or not value["session"].startswith("ses_") or len(value["session"]) > 128:
                raise ValueError("工作区交接缺少有效新会话")
            directory = self.runtime.validate_workspace(value["directory"])
            self.state.update(workspace=str(directory), session=value["session"])
            key = hashlib.sha256(value["session"].encode()).hexdigest()[:32]
            self.state["activity_directory"] = str(self.out / "sessions" / key)
            Path(self.state["activity_directory"]).mkdir(parents=True, exist_ok=True)
            self.save()
            return copy.deepcopy(self.state)

    def ready(self, value):
        with self.mutex:
            if self.closed or self.state["model_state"] != "ready" or not self.runtime.engine.same_native():
                raise RuntimeError("模型尚未就绪；请在 /ctf 中启动并查看真实加载状态")
            if value.get("session") != self.state["session"] or value.get("directory") != self.state["workspace"]:
                raise RuntimeError("这不是当前空白任务；历史会话只供查阅，请用 /cd 或 /new 开始新会话")
            if value.get("physical") != self.state["physical_context"]:
                raise RuntimeError("TUI与模型档位不一致；请重新加载配置并新建会话，未发送模型请求")
            return {"ready": True, "generation": self.generation}

    def status(self, *, resources=False):
        with self.mutex:
            state = copy.deepcopy(self.state)
        state["model_files_present"] = self.boot.ready(Path(state["model_directory"]))
        state["available_ram_gib"] = round(psutil.virtual_memory().available / 1024**3, 2)
        if resources:
            try:
                gpu = self.runtime.safety.GPU().sample()
                state["gpu"] = {key.replace("_bytes", "_mib"): round(value / 1024**2, 1) for key, value in gpu.items() if key.endswith("_bytes")}
                state["windows_commit"] = self.runtime.safety.windows_commit_info()
            except (OSError, RuntimeError, ValueError) as err:
                state["resource_error"] = str(err)
        if state["model_state"] == "ready" and self.runtime.engine.same_native():
            try:
                response = self.runtime.engine.SESSION.get(self.runtime.engine.URL + "/metrics", timeout=(0.5, 1))
                response.raise_for_status()
                metrics = response.json()
                state["running_context"] = self.runtime.observed_context(state, metrics)
                state["live"] = metrics.get("live")
            except Exception as err:
                state["metrics_error"] = str(err)
        return state

    def start(self):
        with self.mutex:
            if self.state["model_state"] in ("ready", "loading"):
                return copy.deepcopy(self.state)
            self.idle()
            if self.state.get("configuration_error"):
                raise RuntimeError("设置尚未完整保存，请先在 /ctf 中重新选择配置")
            if not self.state["workspace"]:
                raise RuntimeError("请先用 /cd 打开独立题目目录")
            self.runtime.validate_workspace(self.state["workspace"])
            if not self.boot.ready(Path(self.state["model_directory"])):
                raise RuntimeError("模型文件尚未准备好；请选择已准备的目录，未加载或下载模型")
            self.generation += 1
            self.state.update(model_state="loading", generation=self.generation, error=None, native_pid=None, native_created=None)
            self.runtime.engine.abort.clear()
            self.save()
            self.thread = threading.Thread(target=self.load, name="ctf-owned-model", daemon=True)
            self.thread.start()
            return copy.deepcopy(self.state)

    def load(self):
        engine = self.runtime.engine
        context = self.runtime.context_profile(self.cfg)
        out = self.out / ("模型_%02d" % self.generation)
        # 即使准备日志目录失败，也应进入可见终态，不留下永久loading。
        try:
            out.mkdir()
        except OSError as err:
            with self.mutex:
                self.state.update(model_state="failed", error=f"创建加载日志失败：{err}")
                self.save()
            return
        engine.OUT = out
        engine.status = {"opencode_tui": True}
        engine.abort_reason.clear()
        engine.engine_lost.clear()
        engine.watch_stop.clear()
        engine.native = engine.guard = engine.launch = None
        profile = "oc-v2-" + str(os.getpid()) + "-" + str(self.generation)
        failed = True
        try:
            release = self.runtime.verify_engine_release(context)
            if engine.abort.is_set():
                raise RuntimeError("加载前已收到停止请求")
            engine.ENV = self.runtime.context_environment(context, self.env)
            for key, suffix in (("STRATA_RUNTIME_DIR", "state/runtime"), ("STRATA_PROFILE_DIR", "state/profiles"),
                                ("STRATA_LAUNCH_PRESET_DIR", "state/launch-presets")):
                engine.ENV[key] = str(out / suffix)
            (out / "settings.json").write_bytes((self.runtime.BASE / "settings.json").read_bytes())
            engine.ENV.update(STRATA_SETTINGS_FILE=str(out / "settings.json"), STRATA_WS_GUARD_DIR=str(out),
                              STRATA_WS_GUARD_SECONDS="14400", STRATA_OWNER_PID=str(os.getpid()),
                              STRATA_OWNER_CREATED=str(self.state["owner_created"]))
            engine.deadline = time.monotonic() + 14400
            kind = self.runtime.Runtime64Monitor if context["physical_context"] == 65536 else self.runtime.safety.Monitor
            self.monitor = kind(out, out, self.runtime.safety.GPU(), engine.deadline, diagnostics=True)
            if context["physical_context"] == 65536:
                self.monitor.quiet = True
            self.runtime.safety.write(out / "所有者清理.json", {"owner_pid": os.getpid(),
                "owner_created": self.state["owner_created"], "control_argv": self.runtime.context_argv(self.cfg, profile)[:2], "profile": profile})
            self.monitor.thread.start()
            engine.start_engine(self.runtime.context_argv(self.cfg, profile), profile)
            if Path(engine.native.exe()).resolve() != Path(release["native_executable"]):
                raise ValueError("实际引擎不属于所选版本；停止本轮")
            response = engine.SESSION.get(engine.URL + "/metrics", timeout=4)
            response.raise_for_status()
            verified = self.runtime.verify_context_runtime(context, engine.native.cmdline(), engine.native.environ(), response.json())
            with self.mutex:
                if engine.abort.is_set():
                    raise RuntimeError("加载完成时收到停止请求；未开放模型调用")
                self.state.update(model_state="ready", native_pid=engine.native.pid, native_created=engine.native.create_time(),
                                  guard_pid=engine.guard.pid, engine_release=release, runtime_context=verified)
                self.save()
            failed = False
        except Exception as err:
            with self.mutex:
                cancelled = engine.abort.is_set() and not engine.abort_reason
                self.state.update(model_state="stopped" if cancelled else "failed",
                                  error=None if cancelled else f"{type(err).__name__}: {err}")
        finally:
            if failed:
                try:
                    self.cleanup()
                except Exception as err:
                    with self.mutex:
                        self.state.update(model_state="failed", cleanup_error=str(err))
                with self.mutex:
                    if engine.abort.is_set() and not self.closed and not self.state.get("cleanup_error"):
                        self.state["model_state"] = "stopped"
                    self.save()

    def cleanup(self):
        errors = []
        if self.monitor:
            try:
                self.monitor.stop.set()
                if self.monitor.thread.is_alive():
                    self.monitor.thread.join(10)
                if self.monitor.thread.is_alive():
                    raise RuntimeError("资源监控线程尚未退出")
                with self.mutex:
                    self.state["resources"] = self.monitor.summary()
                self.monitor = None
            except Exception as err:
                errors.append(str(err))
        try:
            result = self.runtime.engine.stop_engine()
            with self.mutex:
                self.state["cleanup"] = result
            if not result["native_stopped"] or not result["guard_stopped"]:
                raise RuntimeError("模型或工作集保护尚未退出")
            with self.mutex:
                self.state.update(native_pid=None, native_created=None, guard_pid=None)
        except Exception as err:
            errors.append(str(err))
        if errors:
            raise RuntimeError("；".join(errors))
        self.runtime.engine.ENV = self.env

    def stop(self):
        with self.mutex:
            self.runtime.engine.abort.set()
            if self.state["model_state"] == "stopping" and self.thread and self.thread.is_alive():
                return copy.deepcopy(self.state)
            if self.state["model_state"] not in ("ready", "loading", "stopping") and not self.state.get("cleanup_error") and not (self.thread and self.thread.is_alive()):
                return copy.deepcopy(self.state)
            previous = self.thread
            self.state["model_state"] = "stopping"
            self.save()
            def unload():
                try:
                    if previous and previous.is_alive():
                        previous.join(90)
                    if previous and previous.is_alive():
                        raise RuntimeError("加载线程尚未停止，未并发调用模型清理")
                    self.cleanup()
                    with self.mutex:
                        self.state["model_state"] = "stopped"
                        self.state.pop("cleanup_error", None)
                except Exception as err:
                    with self.mutex:
                        self.state.update(model_state="failed", error=f"卸载失败：{err}", cleanup_error=str(err))
                finally:
                    with self.mutex:
                        self.save()
            self.thread = threading.Thread(target=unload, name="ctf-unload-model", daemon=True)
            self.thread.start()
            return copy.deepcopy(self.state)

    def close(self):
        self.closed = True
        self.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(150)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("模型清理尚未完成；不能把退出报告为成功")
        self.server.shutdown()
        self.server.server_close()
        self.http.join(5)
        with self.mutex:
            self.state["state"] = "failed" if self.state.get("cleanup_error") else "stopped"
            self.save()
        if self.state.get("cleanup_error"):
            raise RuntimeError("清理失败；未释放模型独占锁：" + self.state["cleanup_error"])
        self.guard.__exit__()

    def handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                # 只接受认证的本机JSON；拒绝浏览器跨域/Host重绑定及无界输入。
                self.connection.settimeout(5)
                if self.headers.get("Origin") or self.headers.get("Host") != "127.0.0.1:" + str(owner.server.server_port) or not secrets.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + owner.secret).encode()):
                    self.reply(403, {"ok": False, "error": "控制器认证失败"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= 8192 or self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                        raise ValueError("控制器只接受有界JSON请求")
                    value = json.loads(self.rfile.read(size))
                    if not isinstance(value, dict):
                        raise ValueError("控制器请求必须是对象")
                    routes = {"/status": lambda: owner.status(resources=bool(value.get("resources"))),
                              "/select": lambda: owner.select(value), "/workspace": lambda: owner.workspace(value),
                              "/ready": lambda: owner.ready(value), "/start": owner.start, "/stop": owner.stop}
                    if self.path not in routes:
                        self.reply(404, {"ok": False, "error": "控制器操作不存在"})
                        return
                    self.reply(200, {"ok": True, "data": routes[self.path]()})
                except (ValueError, RuntimeError, OSError) as err:
                    self.reply(409, {"ok": False, "error": str(err)})

            def reply(self, code, value):
                data = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
        return Handler
