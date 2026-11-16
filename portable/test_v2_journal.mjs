// 真实文件系统/解释器的证据、产物归属和读回测试，不模拟成功结果。
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Journal, Progress, input, continuity, pin, hash } from "./成熟Agent接入/OpenCode/v2/journal.mjs";
import { frame } from "./成熟Agent接入/OpenCode/v2/control.mjs";

const exec = promisify(execFile);
async function root(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "CTFCode 原生证据 "));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  return dir;
}

test("同目录不同原生会话独立存证，真实SHA可校验，篡改后拒绝", async (t) => {
  const dir = await root(t);
  const a = new Journal(dir, "ses_a"), b = new Journal(dir, "ses_b");
  const one = await a.store("工具", { output: "native fixture" });
  const two = await b.store("工具", { output: "native fixture" });
  assert.notEqual(one.path, two.path);
  assert.equal((await a.verify(one)).data.output, "native fixture");
  await assert.rejects(a.verify(two), /不属于当前会话/);
  await fs.writeFile(path.join(dir, one.path), "tampered");
  await assert.rejects(a.verify(one), /哈希不符/);
  await assert.rejects(a.store("../outside", {}), /种类无效/);
});

test("证据目录junction和证据文件硬链接均拒绝，不写外部位置", async (t) => {
  const dir = await root(t), outside = await root(t);
  await fs.symlink(outside, path.join(dir, ".ctfcode"), process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(new Journal(dir, "ses_junction").store("工具", {}), /链接|重定向/);
  assert.deepEqual(await fs.readdir(outside), []);
  const b = new Journal(outside, "ses_hardlink");
  const receipt = await b.store("工具", { value: 1 });
  const file = path.join(outside, receipt.path), copy = path.join(outside, "must-keep.json");
  await fs.rename(file, copy);
  await fs.link(copy, file);
  await assert.rejects(b.verify(receipt), /普通文件/);
  await assert.rejects(b.store("工具", { value: 1 }), /类型或内容/);
  assert.match(await fs.readFile(copy, "utf8"), /"value":1/);
});

test("原生产物必须由已写脚本真实产生且分页完整读回，改源码撤销凭据", {
  skip: !process.env.CTFCODE_PYTHON && "需显式指定独立Python；不使用全局解释器替代",
}, async (t) => {
  const dir = await root(t), progress = new Progress(dir), session = "ses_chain";
  await progress.begin(session, "请自己写Python脚本解析输入，输出到 result.txt 并用read读回");
  const source = "from pathlib import Path\nPath('result.txt').write_text('alpha\\nbeta\\n',encoding='utf-8')\n";
  const write = input({ tool: "write", sessionID: session, id: "write_1", input: { path: "solve.py", content: source } }, dir);
  await progress.before(write, write.args);
  await fs.writeFile(path.join(dir, "solve.py"), source);
  await progress.after(write, { output: "Created file successfully: solve.py", metadata: {} });
  const call = input({ tool: "shell", sessionID: session, id: "run_1", input: { command: '& "' + process.env.CTFCODE_PYTHON + '" solve.py' } }, dir);
  await progress.before(call, call.args);
  const result = await exec(process.env.CTFCODE_PYTHON, ["solve.py"], { cwd: dir, windowsHide: true });
  await progress.after(call, { output: result.stdout, metadata: { exit: 0 } });
  assert.equal(progress.view(session).phase, "needs_readback");
  const read = input({ tool: "read", sessionID: session, id: "read_1", input: { path: "result.txt", offset: 1, limit: 1 } }, dir);
  await progress.before(read, read.args);
  await progress.after(read, { output: "Read file result.txt, lines 1-1\n1: alpha", metadata: {} });
  assert.equal(await progress.artifactReceipt(session), null, "只读第一页不能冒充完整读回");
  const rest = input({ tool: "read", sessionID: session, id: "read_2", input: { path: "result.txt", offset: 2, limit: 1 } }, dir);
  await progress.before(rest, rest.args);
  await progress.after(rest, { output: "Read file result.txt, lines 2-2\n2: beta", metadata: {} });
  const receipt = await progress.artifactReceipt(session);
  assert.equal(receipt.execution_call_id, "run_1");
  assert.equal(receipt.outputs[0].sha256, hash(await fs.readFile(path.join(dir, "result.txt"))));
  assert.equal(progress.view(session).semantic_result_verified, false);
  const bytes = await fs.readFile(path.join(dir, "result.txt"));
  await fs.appendFile(path.join(dir, "result.txt"), "changed\n");
  assert.equal(await progress.artifactReceipt(session), null, "输出变更不能沿用旧读回凭据");
  await fs.writeFile(path.join(dir, "result.txt"), bytes);
  await fs.appendFile(path.join(dir, "solve.py"), "# changed\n");
  assert.equal(await progress.artifactReceipt(session), null);
  const next = new Progress(dir);
  await next.begin("ses_next", "请自己写Python脚本解析输入，输出到 result.txt 并用read读回");
  assert.equal(await next.artifactReceipt("ses_next"), null, "同目录的新会话不能借用已有脚本/结果认证执行");
});

test("当前只读要求阻止写入和执行，继续消息不恢复旧写脚本合同", async (t) => {
  const dir = await root(t), progress = new Progress(dir), session = "ses_readonly";
  await progress.begin(session, "请自己写Python脚本解析输入，输出到 result.txt 并用read读回");
  await progress.begin(session, "只用read读取，禁止运行脚本 LATEST_READ_ONLY");
  await progress.begin(session, "继续");
  for (const tool of ["write", "edit", "shell"]) {
    const call = input({ tool, sessionID: session, id: tool, input: { path: "solve.py", command: "python solve.py" } }, dir);
    await assert.rejects(progress.before(call, call.args), /CTF_READ_ONLY_TASK_GATE/);
  }
  assert.deepEqual(await fs.readdir(dir), []);
  assert.equal(progress.view(session).strict_read_only, true);
});

test("连续性仅绑定原生压缩消息，原始输入和相同会话后续请求前缀稳定", async (t) => {
  const dir = await root(t), journal = new Journal(dir, "ses_compact"), progress = new Progress(dir);
  const prompt = "只用read读取，禁止运行脚本；Today's date: KEEP_USER_DATE";
  await progress.begin("ses_compact", prompt);
  const receipt = await journal.store("输入", { text: prompt });
  const saved = await continuity(progress, journal, [{ text: prompt, receipt }]);
  const pins = new Map();
  const user = { id: "msg_user", role: "user", content: [{ type: "text", text: "<conversation-checkpoint>user text" }] };
  const event = { messages: [user] };
  assert.equal(pin(event, pins, saved), false);
  assert.match(user.content[0].text, /user text$/);
  event.messages.push({ id: "msg_native", role: "user", metadata: { ctfcode: { type: "compaction", status: "completed", messageID: "msg_native" } },
    content: [{ type: "text", text: "<conversation-checkpoint>native summary</conversation-checkpoint>" }] });
  assert.equal(pin(event, pins, saved), true);
  const first = JSON.stringify(event.messages);
  assert.match(first, /KEEP_USER_DATE/);
  assert.equal(saved.data.limits.strict_read_only, true);
  pin(event, pins, saved);
  assert.equal(JSON.stringify(event.messages), first);
  assert.equal((await journal.verify(saved.receipt)).data.latest_user_request.sha256, receipt.sha256);
});

test("可见状态区分模型驻留与任务结束，旧会话不显示当前模型工作状态", () => {
  const state = { session: "ses_a", context: "32", model_state: "ready", task: { phase: "succeeded", tools: 2 } };
  assert.match(frame(state, "ses_a"), /已就绪.*本轮结束（答案未认证）/);
  assert.match(frame(state, "ses_b"), /历史会话只供查看/);
  assert.match(frame({ ...state, live: { state: "reading", prompt_read: 128, prompt_tokens: 2000 } }, "ses_a"), /128\/2000/);
});
