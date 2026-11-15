import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { save, render } from "./成熟Agent接入/OpenCode/session-archive.mjs";

const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "CTFCode 归档 中文 "));
  // 这里只清理由本测试新建的临时目录，不触及题目或程序目录。
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const root = path.join(dir, "原题目");
  await fs.mkdir(root);
  const data = { session: { id: "ses_test", title: "测试题", directory: root }, messages: [
    { info: { role: "user", id: "msg_a" }, parts: [{ type: "text", text: "请分析编码" }] },
    { info: { role: "assistant", id: "msg_b" }, parts: [
      { type: "reasoning", text: "先看格式" }, { type: "text", text: "还未解出" },
      { type: "tool", tool: "shell", state: { input: { command: "echo test" }, output: "test", status: "completed" } },
    ] },
  ] };
  return { dir, root, data };
}

test("中文路径保存完整JSON、人工阅读版和真实哈希", async (t) => {
  const { root, data } = await fixture(t);
  const out = await save(root, data);
  const json = await fs.readFile(out.json, "utf8");
  assert.deepEqual(JSON.parse(json).messages, data.messages);
  assert.equal(JSON.parse(json).session.directory, root);
  const text = await fs.readFile(out.text, "utf8");
  assert.match(text, /用户[\s\S]*请分析编码[\s\S]*助手[\s\S]*思考记录[\s\S]*工具记录/);
  const checks = JSON.parse(await fs.readFile(path.join(out.directory, "校验.json"), "utf8"));
  assert.equal(checks.files["会话.json"], hash(json));
  assert.equal(checks.files["阅读.md"], hash(text));
});

test("同一题目的不同会话互不覆盖", async (t) => {
  const { root, data } = await fixture(t);
  const first = await save(root, data);
  const second = await save(root, { ...data, session: { ...data.session, id: "ses_next" }, messages: [] });
  assert.notEqual(first.directory, second.directory);
  assert.equal(JSON.parse(await fs.readFile(first.json, "utf8")).messages.length, 2);
  assert.equal(JSON.parse(await fs.readFile(second.json, "utf8")).messages.length, 0);
});

test("不能把原目录会话写进新目录", async (t) => {
  const { dir, root, data } = await fixture(t);
  const target = path.join(dir, "新题目");
  await fs.mkdir(target);
  await assert.rejects(save(target, data), /原目录与归档目录不一致/);
  assert.deepEqual(await fs.readdir(target), []);
  assert.deepEqual(await fs.readdir(root), []);
});

test("恶意会话ID不会作为文件路径", async (t) => {
  const { root, data } = await fixture(t);
  data.session.id = "../../escape\\file\0name";
  const out = await save(root, data);
  assert.equal(path.dirname(path.dirname(out.directory)), path.join(root, ".ctfcode"));
});

test("拒绝归档目录junction，不写入外部目录", async (t) => {
  const { dir, root, data } = await fixture(t);
  const target = path.join(dir, "不能写入");
  await fs.mkdir(target);
  await fs.symlink(target, path.join(root, ".ctfcode"), process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(save(root, data), /不是普通目录/);
  assert.deepEqual(await fs.readdir(target), []);
});

test("拒绝归档文件硬链接", async (t) => {
  const { dir, root, data } = await fixture(t);
  const out = await save(root, data);
  const target = path.join(dir, "原始证据.txt");
  await fs.writeFile(target, "original");
  await fs.unlink(out.json);
  await fs.link(target, out.json);
  await assert.rejects(save(root, data), /有界普通文件/);
  assert.equal(await fs.readFile(target, "utf8"), "original");
});

test("已有归档身份不符时拒绝覆盖", async (t) => {
  const { root, data } = await fixture(t);
  const out = await save(root, data);
  await fs.writeFile(out.json, JSON.stringify({ session: { id: "other" } }));
  await assert.rejects(save(root, data), /身份不符/);
});

test("并发更新同一会话按顺序落盘，不发生JSON交叉写入", async (t) => {
  const { root, data } = await fixture(t);
  const out = await Promise.all([1, 2, 3].map((n) => save(root,
    { ...data, messages: [{ info: { role: "user" }, parts: [{ type: "text", text: "round " + n }] }] })));
  assert.equal(new Set(out.map((item) => item.directory)).size, 1);
  assert.equal(JSON.parse(await fs.readFile(out[2].json, "utf8")).messages[0].parts[0].text, "round 3");
});

test("内容中的Markdown围栏不会吞掉后面的会话", async (t) => {
  const { data } = await fixture(t);
  data.messages[0].parts[0].text = "```\n原始内容\n````";
  assert.match(render(data), /`````\n```\n原始内容\n````\n`````/);
});

test("缺少会话标识或完整消息列表时明确失败", async (t) => {
  const { root, data } = await fixture(t);
  await assert.rejects(save(root, {}), /缺少有效/);
  await assert.rejects(save(root, { ...data, messages: null }), /缺少有效/);
});

test("新版V2消息的用户文字、助手内容、工具记录与其他事件均可查阅", async (t) => {
  const { root, data } = await fixture(t);
  data.messages = [
    { id: "msg_user", type: "user", text: "新的任务", files: [{ name: "附件.bin" }] },
    { id: "msg_model", type: "assistant", content: [
      { type: "reasoning", text: "观察数据" }, { type: "text", text: "未解出的部分" },
      { type: "tool", name: "shell", state: { status: "completed", input: { command: "echo ok" },
        content: [{ type: "text", text: "ok" }] } },
    ] },
    { id: "msg_idle", type: "idle", outcome: "interrupted" },
  ];
  const out = await save(root, data);
  const text = await fs.readFile(out.text, "utf8");
  assert.match(text, /用户[\s\S]*新的任务[\s\S]*助手[\s\S]*观察数据[\s\S]*未解出的部分[\s\S]*工具记录[\s\S]*interrupted/);
  assert.deepEqual(JSON.parse(await fs.readFile(out.json, "utf8")).messages, data.messages);
});
