// 真正V2服务+真实Python控制器。模型目录为空，不能把此测试说成加载/推理通过。
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fixture, python, skip } from "./test_fixture.mjs";
import { policy } from "./成熟Agent接入/OpenCode/v2/session-policy.mjs";
import { control } from "./成熟Agent接入/OpenCode/v2/control.mjs";

test("原生V2控制器：空模型可打开、换目录同步、档位真重载、未就绪不发送推理", { skip, timeout: 120000 }, async (t) => {
  const value = await fixture(t, { controller: true });
  const opts = { controller: value.controller, python, validator: value.validator,
    agent: "ctf-local", model: { providerID: "strata-local", id: "qwen3.8-flash-next-coder-iq1_m" } };
  const initial = await control(opts, "status");
  assert.equal(initial.model_state, "stopped");
  assert.equal(initial.native_pid, null);
  assert.equal(initial.model_files_present, false);
  await assert.rejects(control(opts, "start"), /先用 \/cd/);
  let route = { type: "home" };
  const state = policy({ client: value.client, location: { directory: value.a }, ui: { router: {
    current: () => route, navigate: (next) => { route = next; },
  } }, data: { session: { sync: (id) => value.client.session.get({ sessionID: id }) } } }, opts);
  const a = await state.fresh(value.a);
  const first = await control(opts, "status");
  assert.equal(first.workspace, value.a);
  assert.equal(first.session, a.session.id);
  await assert.rejects(control(opts, "start"), /模型文件尚未准备好/);
  await value.api("/api/session/" + a.session.id + "/prompt", { text: "未加载模型时，不应发出模型请求。" });
  await value.api("/api/experimental/session/" + a.session.id + "/wait", {});
  const blocked = await value.client.experimental.session.export({ sessionID: a.session.id });
  assert.match(JSON.stringify(blocked.messages), /模型尚未就绪/);
  assert.equal(blocked.messages.findLast((item) => item.type === "idle")?.outcome, "failed");
  const b = await state.fresh(value.b);
  const second = await control(opts, "status");
  assert.equal(second.workspace, value.b);
  assert.equal(second.session, b.session.id);
  assert.notEqual(second.activity_directory, first.activity_directory);
  assert.match(await fs.readFile(b.archive.text, "utf8"), /未加载模型时/);
  for (const [context, window] of [["64", 65536], ["32", 24576]]) {
    await control(opts, "select", { context });
    await value.api("/api/location/reload", {});
    const models = await value.api("/api/model?location[directory]=" + encodeURIComponent(value.b));
    assert.equal(models.length, 1);
    assert.equal(models[0].limit.context, window, "必须是V2实际配置，不只是界面显示数字");
    const fresh = await state.fresh(value.b);
    assert.equal((await value.client.experimental.session.export({ sessionID: fresh.session.id })).messages.length, 0);
  }
  const final = await control(opts, "status");
  assert.equal(final.context, "32");
  assert.equal(final.model_state, "stopped");
  assert.equal(final.native_pid, null);
  assert.doesNotMatch(JSON.stringify({ initial, first, second, final }), new RegExp(value.controller.key));
  const file = path.join(value.dir, "controller-validation.json");
  await fs.writeFile(file, JSON.stringify({ initial, first, second, final, blocked }, null, 2));
  t.diagnostic("真实控制器与V2配置重载通过；没有启动GPU模型。证据：" + file);
});
