// 真正CLI/API、原生工具与解释器；LLM响应为合成数据，不是模型自主解题证明。
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import fs from "node:fs/promises";
import path from "node:path";
import { fixture, python, skip, root } from "./test_fixture.mjs";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";
import { hash, Journal } from "./成熟Agent接入/OpenCode/v2/journal.mjs";

async function files(dir, session) {
  const folder = path.join(dir, ".ctfcode", "会话证据", "会话_" + hash(session).slice(0, 32));
  return Promise.all((await fs.readdir(folder)).map(async (name) => JSON.parse(await fs.readFile(path.join(folder, name), "utf8"))));
}
async function terminal(dir, session) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const rows = await files(dir, session);
    if (rows.some((row) => row.kind === "状态" && ["succeeded", "interrupted", "needs_attention"].includes(row.data.phase))) return rows;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail("旁路没有观察到原生终态");
}

for (const failing of [false, true]) test(failing ? "三次真实Python失败停止Agent，不自动重跑" : "真实工具证据、连续两次压缩、读回凭据与新目录不混用", {
  skip, timeout: 120000,
}, async (t) => {
  const wires = [];
  let step = 0;
  const source = failing ? "raise RuntimeError('EXPECTED_FAILURE')\n" :
    "from pathlib import Path\nPath('result.txt').write_text('alpha\\nbeta\\n',encoding='utf-8')\nprint('produced')\n";
  const chain = [["write", { path: "solve.py", content: source }],
    ...Array.from({ length: failing ? 3 : 1 }, () => ["shell", { command: '& "' + python + '" solve.py' }]),
    ...failing ? [] : [["read", { path: "result.txt", offset: 1, limit: 1 }], ["read", { path: "result.txt", offset: 2, limit: 1 }]]];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    wires.push(body);
    const compact = Boolean(body.logit_bias);
    const call = compact ? undefined : chain[step++];
    const base = { id: "synthetic-activity", object: "chat.completion.chunk", created: 1, model: body.model };
    const delta = call ? { tool_calls: [{ index: 0, id: "call_native_" + step, type: "function", function: {
      name: call[0], arguments: JSON.stringify(call[1]) } }] } : { content: compact ? "## Objective\n合成摘要：故意丢弃原始任务和产物字段。" : "SYNTHETIC_DONE" };
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta, finish_reason: null }] }) + "\n\n");
    res.end("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: call ? "tool_calls" : "stop" }],
      usage: { prompt_tokens: 100, completion_tokens: 12, total_tokens: 112 } }) + "\n\ndata: [DONE]\n\n");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const value = await fixture(t, { model: "http://127.0.0.1:" + server.address().port + "/v1" });
  const session = await value.client.session.create({ location: { directory: value.a }, agent: "ctf-local",
    model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } });
  const prompt = async (message) => {
    await value.api("/api/session/" + session.id + "/prompt", { text: message });
    await value.api("/api/experimental/session/" + session.id + "/wait", {});
  };
  const compact = async () => {
    await value.api("/api/session/" + session.id + "/compact", {});
    await value.api("/api/experimental/session/" + session.id + "/wait", {});
    const data = await value.client.experimental.session.export({ sessionID: session.id });
    const message = data.messages.findLast((item) => item.type === "compaction");
    await fs.writeFile(path.join(value.dir, "compaction-debug.json"), JSON.stringify(data, null, 2));
    assert.equal(message?.status, "completed", JSON.stringify(message) + "；证据：" + value.dir);
  };
  await prompt("请自己写Python脚本解析合成输入，输出到 result.txt 并用read完整读回；这是运行链验收。原始标记 ORIGINAL_TASK_9182");
  const rows = await terminal(value.a, session.id);
  const data = await value.client.experimental.session.export({ sessionID: session.id });
  const tools = rows.filter((row) => row.kind === "工具" && row.data.status === "completed");
  assert.equal(tools.length, 4);
  assert.deepEqual(wires[0].tools.map((tool) => tool.function.name).sort(), ["edit", "glob", "grep", "read", "shell", "write"]);
  const journal = new Journal(value.a, session.id);
  for (const part of data.messages.flatMap((item) => item.content ?? []).filter((part) => part.type === "tool" && part.state.metadata?.ctf_evidence?.path))
    assert.equal((await journal.verify(part.state.metadata.ctf_evidence)).data.id, part.id);
  if (failing) {
    assert.equal(data.messages.findLast((item) => item.type === "idle").outcome, "interrupted");
    assert.equal(wires.length, 4, "第三次失败后不得再自动发第五轮请求");
    assert.equal(tools.filter((row) => row.data.tool === "shell" && row.data.result.metadata?.exit !== undefined && row.data.result.metadata.exit !== 0).length, 3);
    assert.match(await fs.readFile(path.join(value.a, "solve.py"), "utf8"), /EXPECTED_FAILURE/);
    return;
  }
  assert.equal((await fs.readFile(path.join(value.a, "result.txt"), "utf8")).replaceAll("\r\n", "\n"), "alpha\nbeta\n");
  assert.ok(rows.some((row) => row.kind === "进度" && row.data.phase === "readback_observed" && row.data.semantic_result_verified === false));
  await compact();
  await prompt("只用read读取，禁止运行脚本，保留最新只读限制 LATEST_READ_ONLY_3317");
  const first = wires.at(-1);
  assert.match(JSON.stringify(first.messages), /CTF_CONTINUITY.*ORIGINAL_TASK_9182/);
  assert.match(JSON.stringify(first.messages), /execution_call_id.*call_native_2/);
  assert.match(JSON.stringify(first.messages), /result.txt/);
  await compact();
  await prompt("继续");
  const second = wires.at(-1);
  assert.match(JSON.stringify(second.messages), /CTF_CONTINUITY.*LATEST_READ_ONLY_3317/);
  assert.match(JSON.stringify(second.messages), /strict_read_only\\?":true/);
  assert.deepEqual(first.tools, second.tools, "旁路不得新增模型工具schema");
  assert.equal(first.messages.filter((item) => item.role === "system").map((item) => item.content).join(""),
    second.messages.filter((item) => item.role === "system").map((item) => item.content).join(""), "同任务系统前缀不能随观察状态变化");
  let route = { type: "session", sessionID: session.id };
  const state = policy({ client: value.client, location: { directory: value.a }, ui: { router: {
    current: () => route, navigate: (next) => { route = next; } } }, data: { session: { sync: (id) => value.client.session.get({ sessionID: id }) } } },
    { python, validator: path.join(root, "成熟Agent接入/OpenCode/opencode_runtime.py") });
  const next = await state.fresh(value.b);
  await value.api("/api/session/" + next.session.id + "/prompt", { text: "NEW_DIRECTORY_ONLY" });
  await value.api("/api/experimental/session/" + next.session.id + "/wait", {});
  assert.doesNotMatch(JSON.stringify(wires.at(-1)), /ORIGINAL_TASK_9182|LATEST_READ_ONLY_3317|CTF_CONTINUITY/);
  await fs.writeFile(path.join(value.dir, "activity-evidence.json"), JSON.stringify({ rows, data, wires }, null, 2));
  t.diagnostic("原生证据/执行/读回与两次压缩连续性通过；合成LLM，未加载GPU。证据：" + value.dir);
});
