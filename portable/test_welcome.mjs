import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { overview, downloads } from "./成熟Agent接入/OpenCode/v2/welcome.mjs";
import { center } from "./成熟Agent接入/OpenCode/v2/ctf-center.mjs";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";
import { control } from "./成熟Agent接入/OpenCode/v2/control.mjs";
import { fixture, python, skip } from "./test_fixture.mjs";

test("开始页准确区分未配置、已准备和64K候选，不宣称校验或解题成功", () => {
  const state = { context: "32", model_directory: "D:\\Models", model_state: "stopped" };
  assert.match(overview(state).placeholder, /模型待配置.*题目待选择.*32K默认/);
  const ready = overview({ ...state, context: "64", workspace: "D:\\Task", model_files_present: true });
  assert.match(ready.placeholder, /模型已准备.*题目已打开.*64K候选/);
  assert.match(ready.options.find((item) => item.value === "prepare").description, /跳过/);
  assert.match(downloads(), /58.4GB/);
  assert.match(downloads(), /hf-mirror/);
  assert.equal(new Set(ready.options.map((item) => item.value)).size, ready.options.length);
});

test("向导连接真实控制器：读指南、选模型、换题和取消准备，全程不加载模型", { skip, timeout: 120000 }, async (t) => {
  const value = await fixture(t, { controller: true });
  const model = path.join(value.dir, "模型 空格 & 中文");
  await fs.mkdir(model);
  let route = { type: "home" };
  const actions = ["docs", "model", "cd", "prepare", "no", "done"];
  const prompts = [model, value.a];
  const alerts = [];
  const errors = [];
  const client = { ...value.client, location: { reload: () => value.api("/api/location/reload", {}) } };
  const ctx = { client, location: { directory: value.a }, data: { session: { sync: (id) => client.session.get({ sessionID: id }) } },
    ui: { router: { current: () => route, navigate: (next) => { route = next; } },
      dialog: { select: async () => actions.shift(), prompt: async () => prompts.shift(), alert: async (value) => { alerts.push(value); } },
      toast: { show: (value) => { if (value.variant === "error") errors.push(value); } } } };
  const opts = { controller: value.controller, python, validator: value.validator,
    agent: "ctf-local", model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } };
  const menu = center(ctx, opts, policy(ctx, opts), async () => {});
  t.after(menu.close);
  await menu.run("welcome");
  assert.deepEqual(errors, []);
  assert.equal(actions.length, 0);
  assert.match(alerts[0].message, /不必手改JSON/);
  const state = await control(opts, "status");
  assert.equal(state.model_directory, model);
  assert.equal(state.workspace, value.a);
  assert.equal(state.model_state, "stopped");
  assert.equal(state.native_pid, null);
  assert.equal(state.preparation_state, undefined);
  await assert.rejects(control(opts, "prepare", { confirm: true }), /分片缺失/);
  assert.equal((await fs.readdir(model)).length, 0);
});
