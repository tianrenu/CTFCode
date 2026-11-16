// 使用真正的OpenCode V2服务和文件系统；不调用大模型，不模拟会话API。
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { policy, archive } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";
import { fixture, root, python, skip } from "./test_fixture.mjs";

const opts = { python, validator: path.join(root, "成熟Agent接入/OpenCode/opencode_runtime.py") };

test("真正V2 API：切目录/重开同目录产生空白新会话，旧记录归档原目录", {
  skip,
  timeout: 120000,
}, async (t) => {
  const { dir, a, b, api, client, calls } = await fixture(t);
  await fs.writeFile(path.join(a, "attachment.txt"), "original attachment");
  await fs.writeFile(path.join(a, "solve.py"), "# retained analysis\n");
  await fs.writeFile(path.join(a, "result.txt"), "retained result");
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
  await archive(client, third.session.id);
  const count = calls.filter((route) => route.endsWith("/export")).length;
  const boot = await policy(ctx, opts).fresh(a, true);
  assert.notEqual(boot.session.id, third.session.id);
  assert.equal(boot.archive, undefined, "启动不导出首页偶然选中的历史会话");
  assert.equal(calls.filter((route) => route.endsWith("/export")).length, count, "启动不读取旧消息");
  assert.equal((await client.experimental.session.export({ sessionID: boot.session.id })).messages.length, 0);
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
  const created = calls.filter((route) => route === "/api/session").length;
  await assert.rejects(state.fresh(a), /身份不符/);
  assert.equal(route.sessionID, current);
  assert.equal(calls.filter((route) => route === "/api/session").length, created);
  assert.equal(await fs.readFile(path.join(a, "attachment.txt"), "utf8"), "original attachment");
  assert.equal(await fs.readFile(path.join(a, "solve.py"), "utf8"), "# retained analysis\n");
  assert.equal(await fs.readFile(path.join(a, "result.txt"), "utf8"), "retained result");
  assert.equal((await client.session.get({ sessionID: imported.id })).location.directory, a);
  assert.equal(calls.some((route) => /prompt|generate|compact|move/.test(route)), false);
  t.diagnostic("真实API验证通过；仅合成历史记录，无模型推理。日志：" + dir);
});
