// 仅用脚本化的合成模型响应验证原生工具与权限；不冒充模型独立解题。
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import path from "node:path";
import fs from "node:fs/promises";
import { fixture, root, python, skip } from "./test_fixture.mjs";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";

test("V2真实工具：写错脚本→静态提示→读源码→改脚本→执行→读回", { skip, timeout: 120000 }, async (t) => {
  const requests = [];
  let scenario = "chain";
  let issued = false;
  const commands = [
    ["write", { path: "solution.py", content: "print('broken'\n" }],
    ["read", { path: "solution.py" }],
    ["write", { path: "solution.py", content: "from pathlib import Path\nPath('answer.txt').write_text('CTF_NATIVE_OK', encoding='utf8')\nprint('done')\n" }],
    ["shell", { command: "& '" + python.replaceAll("\\", "/").replaceAll("'", "''") + "' 'solution.py'" }],
    ["read", { path: "answer.txt" }],
    ["read", { path: "page.txt" }],
  ];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const index = requests.length - 1;
    const guarded = scenario === "timeout" ? ["shell", { command: "Set-Content -LiteralPath 'never-timeout.txt' -Value 'forbidden'", timeout: 0 }] :
      scenario === "background" ? ["shell", { command: "Set-Content -LiteralPath 'never-background.txt' -Value 'forbidden'", background: true }] :
      ["shell", { command: "Remove-Item -LiteralPath 'protected.txt'" }];
    const tool = scenario === "chain" ? commands[index] : issued ? undefined : guarded;
    if (scenario !== "chain") issued = true;
    const delta = tool ? { tool_calls: [{ index: 0, id: "call_" + index, type: "function",
      function: { name: tool[0], arguments: JSON.stringify(tool[1]) } }] } : { content: "SYNTHETIC_TOOL_CHAIN_DONE" };
    const base = { id: "fixture-tools", object: "chat.completion.chunk", model: "qwen3.8-flash-next-coder-iq1_m", created: 1 };
    res.write("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta, finish_reason: null }] }) + "\n\n");
    res.end("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: tool ? "tool_calls" : "stop" }],
      usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } }) + "\n\ndata: [DONE]\n\n");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const value = await fixture(t, { model: "http://127.0.0.1:" + server.address().port + "/v1" });
  await fs.writeFile(path.join(value.a, "page.txt"), Array.from({ length: 200 }, (_, n) => "ROW_" + (n + 1)).join("\n"));
  const id = (await value.client.session.create({ location: { directory: value.a }, agent: "ctf-local",
    model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } })).id;
  await value.api("/api/session/" + id + "/prompt", { text: "合成工具链验收，按实际工具记录操作，不是比赛题。" });
  await value.api("/api/experimental/session/" + id + "/wait", {});
  const data = await value.client.experimental.session.export({ sessionID: id });
  const tools = data.messages.flatMap((item) => item.content ?? []).filter((part) => part.type === "tool");
  assert.deepEqual(tools.map((item) => [item.name, item.state.status]), commands.map(([name]) => [name, "completed"]), JSON.stringify(data.messages));
  assert.equal(requests.length, 7);
  assert.match(JSON.stringify(requests[1].messages), /PY_SYNTAX_ERROR/);
  assert.match(JSON.stringify(requests.at(-1).messages), /CTF_NATIVE_OK/);
  assert.equal(await fs.readFile(path.join(value.a, "answer.txt"), "utf8"), "CTF_NATIVE_OK");
  assert.equal(tools.find((item) => item.name === "shell").state.metadata.ctf_effective_limits.timeout_ms, 120000);
  assert.equal(tools.find((item) => item.name === "read").state.metadata.ctf_effective_limits.lines, 120);
  assert.match(JSON.stringify(requests.at(-1).messages), /120: ROW_120/);
  assert.doesNotMatch(JSON.stringify(requests.at(-1).messages), /121: ROW_121/);
  const permissions = await value.api("/api/session/" + id + "/permission");
  assert.equal(permissions.length, 0, "普通工具不应逐条等待审批");
  for (const kind of ["timeout", "background"]) {
    scenario = kind;
    issued = false;
    const next = await value.client.session.create({ location: { directory: value.a }, agent: "ctf-local",
      model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } });
    await value.api("/api/session/" + next.id + "/prompt", { text: "合成门禁测试：" + kind });
    await value.api("/api/experimental/session/" + next.id + "/wait", {});
    const result = await value.client.experimental.session.export({ sessionID: next.id });
    assert.match(JSON.stringify(result.messages), kind === "timeout" ? /shell超时必须/ : /不启用无界后台/);
    assert.equal(await fs.stat(path.join(value.a, "never-" + kind + ".txt")).then(() => true, () => false), false);
  }
  scenario = "approval";
  issued = false;
  await fs.writeFile(path.join(value.a, "protected.txt"), "must be kept");
  const pending = await value.client.session.create({ location: { directory: value.a }, agent: "ctf-local",
    model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } });
  await value.api("/api/session/" + pending.id + "/prompt", { text: "合成审批测试：不得未经确认删除测试文件。" });
  const deadline = Date.now() + 10000;
  const permission = await (async () => {
    while (Date.now() < deadline) {
      const list = await value.api("/api/session/" + pending.id + "/permission");
      if (list.length) return list[0];
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.fail("高风险命令没有出现审批；日志：" + value.dir);
  })();
  assert.equal(permission.action, "shell");
  assert.match(JSON.stringify(permission.resources), /Remove-Item/);
  const state = policy({ client: value.client, location: { directory: value.a }, ui: { router: {
    current: () => ({ type: "session", sessionID: pending.id }), navigate: () => assert.fail("忙碌任务不应切换"),
  } }, data: { session: { sync: (id) => value.client.session.get({ sessionID: id }) } } },
  { python, validator: path.join(root, "成熟Agent接入/OpenCode/opencode_runtime.py") });
  await assert.rejects(state.fresh(value.b), /仍有任务运行/);
  await value.api("/api/session/" + pending.id + "/permission/" + permission.id + "/reply", { decision: "reject" });
  await value.api("/api/experimental/session/" + pending.id + "/wait", {});
  assert.equal(await fs.readFile(path.join(value.a, "protected.txt"), "utf8"), "must be kept");
  await fs.writeFile(path.join(value.dir, "native-tool-validation.json"), JSON.stringify({ data, requests }, null, 2));
  t.diagnostic("原生工具和本机PowerShell/Python实际执行通过；响应是合成的。证据：" + value.dir);
});
