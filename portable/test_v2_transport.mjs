// 真正V2 CLI通过本机合成OpenAI SSE接口发请求；不是模型解题或速度测试。
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { fixture, root, python, skip } from "./test_fixture.mjs";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";

const model = "qwen3.8-flash-next-coder-iq1_m";
const opts = { python, validator: path.join(root, "成熟Agent接入/OpenCode/opencode_runtime.py"),
  agent: "ctf-local", model: { providerID: "strata-local", id: model } };

for (const context of ["32", "64"]) test("V2 " + context + "K 实际请求：单模型、压缩缓存前缀、工具调用拒绝、换题不重放旧上下文", { skip, timeout: 120000 }, async (t) => {
  const wires = [];
  let poison = false;
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString("utf8");
    wires.push({ path: req.url, body: JSON.parse(body), session: req.headers["x-opencode-session-id"] });
    if (req.url !== "/v1/chat/completions") { res.writeHead(404); res.end("fixture endpoint only"); return; }
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    const base = { id: "chatcmpl-fixture", object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model };
    const compact = Boolean(wires.at(-1).body.logit_bias);
    const delta = compact && poison ? { tool_calls: [{ index: 0, id: "call_poison", type: "function", function: {
      name: "write", arguments: JSON.stringify({ path: "never-write.txt", content: "must never be written" }) } }] } :
      { role: "assistant", content: compact ? "## Objective\n合成摘要，仅测试传输，不是解题结果。\n## Next Move\n继续原任务。" : "SYNTHETIC_REPLY_" + wires.length };
    res.write("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta, finish_reason: null }] }) + "\n\n");
    res.end("data: " + JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: compact && poison ? "tool_calls" : "stop" }],
      usage: { prompt_tokens: 100, completion_tokens: 12, total_tokens: 112 } }) + "\n\ndata: [DONE]\n\n");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const value = await fixture(t, { model: "http://127.0.0.1:" + server.address().port + "/v1", context });
  const { api, client, a, b, dir } = value;
  const query = "?location[directory]=" + encodeURIComponent(a);
  const deadline = Date.now() + 15000;
  while (true) {
    const plugins = await api("/api/plugin" + query);
    const runtime = plugins.find((item) => item.id === "ctfcode.runtime");
    if (runtime?.state?.status === "active") break;
    if (runtime?.state?.status === "failed" || Date.now() > deadline)
      assert.fail("V2插件未激活：" + JSON.stringify(plugins) + "；日志：" + dir);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const models = await api("/api/model" + query);
  assert.equal(models.length, 1, JSON.stringify(models.map((item) => [item.providerID, item.id])));
  assert.equal(models[0].providerID, "strata-local");
  assert.equal(models[0].id, model);
  assert.equal(models[0].limit.context, context === "64" ? 65536 : 24576);
  const original = await client.session.create({ location: { directory: a }, agent: "ctf-local", model: opts.model });
  let route = { type: "session", sessionID: original.id };
  const state = policy({ client, location: { directory: a }, ui: { router: {
    current: () => route, navigate: (value) => { route = value; },
  } }, data: { session: { sync: (id) => client.session.get({ sessionID: id }) } } }, opts);
  const token = "OLD_USER_" + crypto.randomUUID();
  async function prompt(id, text) {
    await api("/api/session/" + id + "/prompt", { text });
    await api("/api/experimental/session/" + id + "/wait", {});
    const data = await client.experimental.session.export({ sessionID: id });
    const reply = data.messages.findLast((item) => item.type === "assistant");
    assert.equal(reply?.finish, "stop", JSON.stringify(data.messages));
    return data;
  }
  await prompt(original.id, token + "\nToday's date: KEEP_USER_DATE");
  assert.equal(wires.length, 1, JSON.stringify(wires.map((wire) => wire.path)));
  assert.equal(wires[0].body.model, model);
  const tools = wires[0].body.tools.map((tool) => tool.function.name).sort();
  assert.deepEqual(tools, ["edit", "glob", "grep", "read", "shell", "write"]);
  assert.match(wires[0].body.tools.find((tool) => tool.function.name === "shell").function.description, /PowerShell/);
  const systems = wires[0].body.messages.filter((item) => item.role === "system").map((item) => item.content).join("\n");
  assert.doesNotMatch(systems, /Today's date:/);
  assert.match(JSON.stringify(wires[0].body.messages), /Today's date: KEEP_USER_DATE/);
  assert.equal(wires[0].body.max_tokens ?? wires[0].body.max_completion_tokens, 4096);
  assert.equal(wires[0].body.temperature, 0.1);
  assert.equal(wires[0].body.logit_bias, undefined);
  await api("/api/session/" + original.id + "/compact", {});
  await api("/api/experimental/session/" + original.id + "/wait", {});
  const compressed = await client.experimental.session.export({ sessionID: original.id });
  assert.equal(compressed.messages.findLast((item) => item.type === "compaction")?.status, "completed", JSON.stringify(compressed.messages));
  assert.equal(wires.length, 2, "摘要应只有一次请求，不盲目重试");
  assert.deepEqual(wires[1].body.tools, wires[0].body.tools, "V2原生摘要保留相同工具schema，而不是重新改模板");
  assert.deepEqual(wires[1].body.messages.slice(0, wires[0].body.messages.length), wires[0].body.messages,
    "实际wire中的system和已有对话前缀必须字节内容一致");
  assert.deepEqual(wires[1].body.logit_bias, { "248058": -100 });
  await prompt(original.id, "CONTINUE_WITHIN_SAME_SESSION");
  assert.equal(wires.length, 3);
  assert.match(JSON.stringify(wires[2].body.messages), /conversation-checkpoint/);
  assert.equal(wires[2].body.logit_bias, undefined);
  poison = true;
  await api("/api/session/" + original.id + "/compact", {});
  await api("/api/experimental/session/" + original.id + "/wait", {});
  const rejected = await client.experimental.session.export({ sessionID: original.id });
  const failed = rejected.messages.findLast((item) => item.type === "compaction");
  assert.equal(failed?.status, "failed", JSON.stringify(failed));
  assert.match(JSON.stringify(failed.error), /CTF_COMPACTION_TOOL_CALL_DENIED/);
  assert.equal(wires.length, 4, "违规工具调用立即拒绝，不自动追加请求");
  assert.equal(await fs.stat(path.join(a, "never-write.txt")).then(() => true, () => false), false);
  poison = false;
  const next = await state.fresh(b);
  await prompt(next.session.id, "NEW_USER_B_ONLY");
  assert.equal(wires.length, 5);
  assert.doesNotMatch(JSON.stringify(wires[4].body), new RegExp(token));
  assert.doesNotMatch(JSON.stringify(wires[4].body), /SYNTHETIC_REPLY_1/);
  assert.match(await fs.readFile(next.archive.text, "utf8"), new RegExp(token));
  const again = await state.fresh(a);
  await prompt(again.session.id, "NEW_USER_A_ONLY");
  assert.equal(wires.length, 6);
  assert.doesNotMatch(JSON.stringify(wires[5].body), new RegExp(token));
  assert.doesNotMatch(JSON.stringify(wires[5].body), /NEW_USER_B_ONLY|SYNTHETIC_REPLY_5/);
  await fs.writeFile(path.join(dir, "wire-validation.json"), JSON.stringify({ context, requests: wires }, null, 2));
  t.diagnostic("真实V2与合成SSE通过；没有加载模型。证据：" + dir);
});
