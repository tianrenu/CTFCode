"""V1已审查设置的显式V2映射；不写全局配置，不启动模型。"""
from __future__ import annotations

from pathlib import Path
import os
from urllib.parse import urlsplit

MODEL = "qwen3.8-flash-next-coder-iq1_m"
PROVIDER = "strata-local"


def make(source, here, python, *, endpoint=None, fixture=False):
    here = Path(here).resolve()
    python = Path(python).resolve()
    if source.get("model") != f"{PROVIDER}/{MODEL}" or source.get("small_model") != source["model"]:
        raise ValueError("V2仍只允许同一个本地模型")
    model = source["provider"][PROVIDER]["models"][MODEL]
    window = model["limit"]["context"]
    if window not in (24576, 65536) or model["limit"].get("output") != 4096:
        raise ValueError("V2仅迁移已审查的32K/软24K或64K档")
    if source.get("compaction", {}).get("auto") is not True:
        raise ValueError("不能关闭自动压缩")
    large = window == 65536
    steps = 240 if large else 60
    timeout = 4500000 if large else 1800000
    if source["agent"]["ctf-local"].get("steps") != steps or source["provider"][PROVIDER]["options"].get("timeout") != timeout:
        raise ValueError("V2步数/请求超时必须沿用所选档位的限制")
    address = endpoint or "http://127.0.0.1:18791/v1"
    url = urlsplit(address)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password or url.query or url.fragment or url.path != "/v1" or not url.port or url.port == 8080:
        raise ValueError("只允许显式本地模型端点；8080保留给Burp")
    if not fixture and address != "http://127.0.0.1:18791/v1":
        raise ValueError("正式配置仅使用18791；其他本地端点仅供显式合成测试")
    rules = []
    for action, values in source["permission"].items():
        for resource, effect in (values.items() if isinstance(values, dict) else [("*", values)]):
            if effect not in ("allow", "deny", "ask"):
                raise ValueError("未识别的V1权限规则")
            rules.append({"action": "shell" if action == "bash" else action, "resource": resource, "effect": effect})
    rules += [{"action": "edit", "resource": ".ctfcode/**", "effect": "deny"},
              {"action": "edit", "resource": "**/.ctfcode/**", "effect": "deny"},
              {"action": "read", "resource": ".ctfcode/会话归档/**", "effect": "ask"},
              {"action": "read", "resource": "**/.ctfcode/会话归档/**", "effect": "ask"},
              {"action": "external_directory", "resource": "*", "effect": "deny"}]
    prompt = source["agent"]["ctf-local"]["prompt"].replace("命令工具实际使用PowerShell，不按名称猜成Bash", "shell工具实际使用PowerShell")
    prompt = prompt.replace("发现和失败及时更新CTF_STATE.md（目标、已验证事实/证据、假设、失败方法、下一步）", "按需在当前会话的独立笔记中记录目标、证据、假设、失败方法与下一步")
    prompt += " 每次新建/切换目录都是新任务，不自动读取旧CTF_STATE.md、.ctf-evidence或.ctfcode会话归档；只有用户主动提供或明确要求查看时才按权限读取。新会话不因旧目录存在文件就继承旧结论。"
    options = {"model": {"providerID": PROVIDER, "id": MODEL}, "agent": "ctf-local",
               "python": str(python), "validator": str(here / "opencode_runtime.py"),
               "window": window, "physical": 65536 if large else 32768, "output": 4096,
               "endpoint": address, "tokenizer": str(here / "64K摘要输出约束_分词器.json")}
    return {
        "$schema": "https://opencode.ai/v2/opencode.json", "model": source["model"],
        "default_agent": "ctf-local", "update": "disable", "share": "disabled", "warming": False,
        "snapshots": False, "shell": (Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe").as_posix(),
        "compaction": {"auto": True, "buffer": 4096, "keep": {"tokens": 4096}},
        "experimental": {"background_compaction": False, "subagent_depth": 0, "schedules": False,
                         "todo": False, "mcp_events": False, "shell_timeout_background": False},
        "permissions": rules,
        "policies": [{"action": "provider.use", "resource": "*", "effect": "deny"},
                     {"action": "provider.use", "resource": PROVIDER, "effect": "allow"},
                     {"action": "integration.connect", "resource": "*", "effect": "deny"},
                     {"action": "integration.use", "resource": "*", "effect": "deny"},
                     {"action": "integration.use", "resource": PROVIDER, "effect": "allow"},
                     {"action": "mcp.use", "resource": "*", "effect": "deny"},
                     {"action": "skill.use", "resource": "*", "effect": "deny"},
                     {"action": "websearch.use", "resource": "*", "effect": "deny"}],
        "agents": {"ctf-local": {"description": "本地授权CTF助手", "mode": "primary", "steps": steps,
                                 "model": source["model"], "system": prompt},
                   "build": {"disabled": True}, "plan": {"disabled": True}},
        "providers": {PROVIDER: {"name": "Strata 本地模型（18791）", "package": "@ai-sdk/openai-compatible",
                                 "settings": {"baseURL": address, "apiKey": "local-only", "timeout": timeout},
                                 "models": {MODEL: {"name": model["name"], "limit": {"context": window, "output": 4096},
                                                   "capabilities": {"tools": True, "input": ["text"], "output": ["text"],
                                                                    "temperature": True, "reasoning": {"supported": False}}}}}},
        "plugins": [{"package": (here / "v2").as_uri(), "options": options}],
    }
