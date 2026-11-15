// 真正CLI的未配置启动门禁：可打开设置，但未核验词表时不能发送模型请求。
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import http from "node:http";
import crypto from "node:crypto";
import { once } from "node:events";
import { fixture, skip } from "./test_fixture.mjs";

test("V2缺少词表时插件仍可启动，实际请求被阻止，补齐并核验后才发送", { skip, timeout: 120000 }, async (t) => {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const base = { id: "startup-fixture", object: "chat.completion.chunk", created: 1,
      model: "qwen3.8-flash-next-coder-iq1_m" };
    res.end("data: " + JSON.stringify({ ...base, choices: [{ index: 0,
      delta: { role: "assistant", content: "SYNTHETIC_READY" }, finish_reason: "stop" }] }) + "\n\ndata: [DONE]\n\n");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }));
  const value = await fixture(t, { model: "http://127.0.0.1:" + server.address().port + "/v1", tokenizer: false });
  const deadline = Date.now() + 15000;
  while (true) {
    const plugins = await value.api("/api/plugin?location[directory]=" + encodeURIComponent(value.a));
    const plugin = plugins.find((item) => item.id === "ctfcode.runtime");
    if (plugin?.state?.status === "active") break;
    if (plugin?.state?.status === "failed" || Date.now() > deadline) assert.fail(JSON.stringify(plugins));
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.equal(requests.length, 0);
  async function prompt() {
    const info = await value.client.session.create({ location: { directory: value.a }, agent: "ctf-local",
      model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } });
    await value.api("/api/session/" + info.id + "/prompt", { text: "合成启动验证，不是模型解题。" });
    await value.api("/api/experimental/session/" + info.id + "/wait", {});
    return value.client.experimental.session.export({ sessionID: info.id });
  }
  const missing = await prompt();
  assert.match(JSON.stringify(missing.messages), /词表未就绪或核验失败/);
  assert.equal(requests.length, 0, "词表缺失不能先发送普通模型请求");
  const file = path.join(value.dir, "synthetic-tokenizer.json");
  const manifest = { model: "qwen3.8-flash-next-coder-iq1_m", blocked_token: "<tool_call>", bias: -100,
    blocked_token_id: 248058, vocabulary_path: path.join(value.dir, "synthetic-vocab.json"),
    vocabulary_sha256: "0".repeat(64) };
  await fs.writeFile(file, JSON.stringify(manifest));
  const invalid = await prompt();
  assert.match(JSON.stringify(invalid.messages), /词表未就绪或核验失败/);
  assert.equal(requests.length, 0, "错误哈希不能绕过门禁");
  manifest.vocabulary_sha256 = crypto.createHash("sha256").update(await fs.readFile(manifest.vocabulary_path)).digest("hex");
  await fs.writeFile(file, JSON.stringify(manifest));
  const ready = await prompt();
  assert.match(JSON.stringify(ready.messages), /SYNTHETIC_READY/);
  assert.equal(ready.messages.findLast((item) => item.type === "assistant")?.finish, "stop", JSON.stringify(ready.messages));
  assert.equal(requests.length, 1, "文件补齐后才进行唯一一次请求，未启动第二模型");
  await fs.writeFile(path.join(value.dir, "startup-validation.json"), JSON.stringify({ missing, invalid, ready, requests }, null, 2));
  t.diagnostic("无模型启动门禁通过；CLI与HTTP真实，词表和回复是合成夹具。证据：" + value.dir);
});
