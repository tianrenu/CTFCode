// 使用真正的OpenCode V2服务和文件系统；不调用大模型，不模拟会话API。
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import net from "node:net";
import { spawn } from "node:child_process";
import { once } from "node:events";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const cli = process.env.CTFCODE_V2_CLI;
const python = process.env.CTFCODE_PYTHON;
const opts = { python, validator: path.join(root, "成熟Agent接入/OpenCode/opencode_runtime.py") };

test("真正V2 API：切目录/重开同目录产生空白新会话，旧记录归档原目录", {
  skip: !cli || !python ? "需显式指定CTFCODE_V2_CLI和CTFCODE_PYTHON；不使用全局/桌面CLI替代" : false,
  timeout: 120000,
}, async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "CTFCode V2 真服务 "));
  const a = path.join(dir, "题目 A");
  const b = path.join(dir, "题目 B");
  await Promise.all([fs.mkdir(a), fs.mkdir(b), fs.writeFile(path.join(dir, "fixture.marker"), "synthetic API test")]);
  const socket = net.createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const port = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  const token = crypto.randomBytes(24).toString("base64url");
  const headers = { Authorization: "Basic " + Buffer.from("opencode:" + token).toString("base64") };
  const child = spawn(python, ["-I", "-X", "utf8", "-u", path.join(root, "v2_fixture.py"), "--cli", cli,
    "--root", dir, "--port", String(port)], { windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, CTFCODE_FIXTURE_KEY: token } });
  let err = "";
  child.stderr.on("data", (chunk) => { err += chunk; });
  t.after(async () => {
    child.stdin.end();
    if (child.exitCode === null) await Promise.race([once(child, "exit"),
      new Promise((resolve) => setTimeout(resolve, 10000).unref())]);
    if (child.exitCode === null) child.kill();
    // 错误时保留服务日志；成功后的临时目录留给系统临时文件管理，不删除不明进程的文件。
  });
  const url = "http://127.0.0.1:" + port;
  const calls = [];
  async function api(route, body) {
    calls.push(route);
    const res = await fetch(url + route, { method: body === undefined ? "GET" : "POST",
      headers: { ...headers, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(10000) });
    const json = await res.json();
    if (!res.ok) throw new Error(JSON.stringify({ status: res.status, route, json }));
    return json.data ?? json;
  }
  const deadline = Date.now() + 45000;
  while (true) {
    if (child.exitCode !== null) throw new Error("服务已退出：" + err);
    const ready = await fetch(url + "/api/session/active", { headers, signal: AbortSignal.timeout(1000) }).then((res) => res.ok, () => false);
    if (ready) break;
    if (Date.now() > deadline) throw new Error("服务启动超时：" + err + "；日志：" + dir);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  const client = { session: {
    create: (value) => api("/api/session", value),
    get: ({ sessionID }) => api("/api/session/" + sessionID),
    active: () => api("/api/session/active"),
  }, experimental: { session: { export: ({ sessionID }) => api("/api/experimental/session/" + sessionID + "/export") } } };
  const old = await client.session.create({ location: { directory: a }, title: "合成旧会话" });
  const id = "ses_" + crypto.randomUUID().replaceAll("-", "");
  const text = "OLD_CONTEXT_" + crypto.randomUUID();
  const imported = await api("/api/experimental/session/import", { info: { ...old, id }, location: { directory: a }, messages: [
    { id: "msg_" + crypto.randomUUID().replaceAll("-", ""), type: "user", time: { created: Date.now() }, text },
  ] });
  let route = { type: "session", sessionID: imported.id };
  const ctx = { client, location: { directory: a }, ui: { router: {
    current: () => route, navigate: (next) => { route = next; },
  } }, data: { session: { sync: (id) => client.session.get({ sessionID: id }) } } };
  const state = policy(ctx, opts);
  const first = await state.fresh(b);
  assert.notEqual(first.session.id, imported.id);
  assert.equal(first.session.location.directory, b);
  assert.equal((await client.experimental.session.export({ sessionID: first.session.id })).messages.length, 0);
  assert.equal(path.relative(a, first.archive.directory).startsWith(".."), false);
  assert.match(await fs.readFile(first.archive.json, "utf8"), new RegExp(text));
  assert.match(await fs.readFile(first.archive.text, "utf8"), new RegExp(text));
  const second = await state.fresh(b);
  assert.notEqual(second.session.id, first.session.id);
  assert.equal((await client.experimental.session.export({ sessionID: second.session.id })).messages.length, 0);
  assert.equal(path.relative(b, second.archive.directory).startsWith(".."), false);
  const third = await state.fresh(a);
  assert.notEqual(third.session.id, imported.id);
  assert.equal((await client.experimental.session.export({ sessionID: third.session.id })).messages.length, 0);
  const current = route.sessionID;
  await assert.rejects(state.fresh(path.join(dir, "不存在")), /请选择独立/);
  assert.equal(route.sessionID, current);
  await fs.writeFile(path.join(b, "AGENTS.md"), "untrusted attachment instructions");
  await assert.rejects(state.fresh(b), /需要先审查/);
  assert.equal(route.sessionID, current);
  const folder = path.join(a, ".ctfcode", "会话归档", "会话_" +
    crypto.createHash("sha256").update(current).digest("hex").slice(0, 32));
  await fs.mkdir(folder, { recursive: true });
  await fs.writeFile(path.join(folder, "会话.json"), JSON.stringify({ session: { id: "wrong-identity" } }));
  const count = calls.filter((route) => route === "/api/session").length;
  await assert.rejects(state.fresh(a), /身份不符/);
  assert.equal(route.sessionID, current);
  assert.equal(calls.filter((route) => route === "/api/session").length, count);
  assert.equal((await client.session.get({ sessionID: imported.id })).location.directory, a);
  assert.equal(calls.some((route) => /prompt|generate|compact|move/.test(route)), false);
  t.diagnostic("真实API验证通过；仅合成历史记录，无模型推理。日志：" + dir);
});
