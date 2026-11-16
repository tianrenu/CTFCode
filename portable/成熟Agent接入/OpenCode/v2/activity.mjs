// 原生Agent的旁路观察器：记录原始输入/工具回执，迁移已有进度门禁，不另建Agent循环。
import { Journal, Progress, input, text, continuity, pin } from "./journal.mjs";
import { control } from "./control.mjs";
import path from "node:path";

export async function activity(ctx, opts) {
  const sessions = new Map();
  const abort = new AbortController();
  const progress = new Progress(ctx.location.directory, async (id, reason) => {
    const state = get(id);
    state.reason = reason;
    state.phase = progress.view(id)?.phase === "needs_attention" ? "needs_attention" : "progress_gate";
    await publish(state);
  }, async (sessionID) => (await ctx.session.interrupt({ sessionID })).interrupted === true);
  function get(id) {
    if (!sessions.has(id)) {
      sessions.set(id, { session: id, journal: new Journal(ctx.location.directory, id), requests: [], pins: new Map(),
        phase: "idle", tools: 0, step: 0, failures: 0, pending: new Map(), finished: new Set(), semantic_result_verified: false });
      if (sessions.size > 64) sessions.delete(sessions.keys().next().value);
    }
    return sessions.get(id);
  }
  const view = (state) => ({ phase: state.phase, step: state.step, tools: state.tools, failures: state.failures,
    pending: state.pending.size, last_tool: state.last_tool, reason: state.reason, outcome: state.outcome,
    semantic_result_verified: false, ...(state.audit_error ? { audit_error: state.audit_error } : {}),
    progress: progress.view(state.session) ? { phase: progress.view(state.session).phase,
      scripts: progress.view(state.session).scripts.length, outputs: progress.view(state.session).outputs.length,
      readback: progress.view(state.session).readback.length, expected: progress.view(state.session).expected_outputs.length,
      gate_hits: progress.view(state.session).gate_hits } : undefined });
  async function publish(state) {
    const data = { ...view(state), observed_utc: new Date().toISOString() };
    // 先存本地证据；审计失败是可见状态，不把已发生的工具动作伪装成未执行。
    await state.journal.store("状态", data).catch((err) => { state.audit_error = err.message; data.audit_error = err.message; });
    if (opts.controller) await control(opts, "activity", { session: state.session, directory: ctx.location.directory, data })
      .catch((err) => { state.control_error = err.message; });
  }
  async function record(state, kind, data) {
    return state.journal.store(kind, data).catch((err) => {
      state.audit_error = err.message;
      return { saved: false, error: err.message };
    });
  }
  async function halt(state) {
    if (state.failures < 3 || state.stop_requested || progress.view(state.session)?.abort_requested) return;
    state.phase = "needs_attention";
    state.reason ??= "连续三次工具/执行失败；已请求停止本轮，不自动重跑。";
    state.stop_requested = (await ctx.session.interrupt({ sessionID: state.session })).interrupted === true;
  }
  await ctx.session.hook("prompt", async (event) => {
    const state = get(event.sessionID);
    const value = event.prompt.text ?? "";
    const receipt = await state.journal.store("输入", { id: event.messageID, text: value });
    const prior = progress.snapshot(event.sessionID)?.contract.request_sha256;
    await progress.begin(event.sessionID, value);
    if (prior !== progress.snapshot(event.sessionID)?.contract.request_sha256 || !/^(?:继续(?:执行|测试|做题)?吧?|接着|continue)[。.!！\s]*$/i.test(value.trim())) {
      state.requests.push({ text: value, receipt });
      if (state.requests.length > 2) state.requests.splice(1, state.requests.length - 2);
    }
    state.phase = "queued";
    state.reason = undefined;
    state.outcome = undefined;
    state.failures = 0;
    state.stop_requested = false;
    await publish(state);
  });
  const prepare = async (event, compact) => {
    const state = get(event.sessionID);
    pin(event, state.pins, state.next);
    if (compact) {
      state.phase = "compacting";
      state.next = await continuity(progress, state.journal, state.requests);
      await publish(state);
    }
  };
  await ctx.session.hook("context", (event) => prepare(event, false));
  await ctx.session.hook("compaction", (event) => prepare(event, true));
  await ctx.tool.hook("execute.before", async (event) => {
    const state = get(event.sessionID);
    state.last_tool = event.tool;
    const call = { ...input(event, ctx.location.directory) };
    // 原生参数和起点先落盘；没有通过存证和进度门禁就不执行动作。
    const receipt = await state.journal.store("调用", { id: event.id, tool: event.tool, input: event.input,
      message: event.messageID, utc: new Date().toISOString() });
    state.pending.set(event.messageID + ":" + event.id, { call, receipt });
    state.phase = "tool";
    await publish(state);
    await progress.before(call, call.args);
  });
  await ctx.tool.hook("execute.after", async (event) => {
    const state = get(event.sessionID);
    const key = event.messageID + ":" + event.id;
    const call = state.pending.get(key)?.call ?? input(event, ctx.location.directory);
    state.pending.delete(key);
    if (!state.finished.has(key)) state.tools++;
    state.finished.add(key);
    if (state.finished.size > 128) state.finished.delete(state.finished.values().next().value);
    const receipt = await record(state, "工具", { id: event.id, tool: event.tool, input: event.input,
      status: event.status, ...(event.status === "completed" ? { result: event.result } : { error: event.error }),
      utc: new Date().toISOString(), scope: "原生工具钩子实际回执；不是未截断子进程stdout或答案正确证明" });
    if (event.status === "completed") {
      const output = { output: text(event.result.content), metadata: event.result.metadata ?? {} };
      await progress.after({ ...call, args: call.args }, output).catch((err) => {
        progress.sessions.delete(event.sessionID);
        state.audit_error = "执行后进度取证失败：" + err.message;
      });
      event.result.metadata = { ...(event.result.metadata ?? {}), ctf_evidence: receipt };
    }
    if (event.status === "error") progress.observeToolError({ type: "tool", tool: call.tool,
      sessionID: event.sessionID, callID: event.id, state: { status: "error" } });
    const failed = event.status === "error" || typeof event.result?.metadata?.exit === "number" && event.result.metadata.exit !== 0;
    if (failed) state.failures++;
    if (!failed && event.tool === "shell") state.failures = 0;
    state.phase = state.pending.size ? "tool" : "running";
    await halt(state);
    await record(state, "进度", { ...progress.view(event.sessionID), evidence: receipt });
    await publish(state);
  });
  const types = ["session.execution.started", "session.execution.succeeded", "session.execution.failed", "session.execution.interrupted",
    "session.step.started", "session.step.first-token", "session.compaction.started", "session.compaction.ended", "session.compaction.failed",
    "permission.asked", "permission.replied", "session.tool.failed"];
  const stream = (async () => {
    for await (const event of ctx.event.subscribe({ types, signal: abort.signal })) {
      if (!sessions.has(event.data.sessionID)) continue;
      if (event.location?.directory && path.resolve(event.location.directory).toLowerCase() !== path.resolve(ctx.location.directory).toLowerCase()) continue;
      const state = get(event.data.sessionID);
      const type = event.type;
      if (type === "session.execution.started") state.phase = "running";
      if (type === "session.step.started") { state.phase = "request"; state.step++; }
      if (type === "session.step.first-token") state.phase = "generating";
      if (type === "session.compaction.started") state.phase = "compacting";
      if (type === "permission.asked") state.phase = "waiting_permission";
      if (type === "permission.replied") state.phase = "running";
      if (type === "session.compaction.ended") state.phase = "running";
      if (type === "session.compaction.failed") { state.phase = "failed"; state.reason = JSON.stringify(event.data.error).slice(0, 500); }
      if (type === "session.tool.failed") {
        const key = event.data.assistantMessageID + ":" + event.data.id;
        const call = state.pending.get(key);
        state.pending.delete(key);
        if (call) progress.observeToolError({ type: "tool", tool: call.call.tool,
          sessionID: state.session, callID: event.data.id, state: { status: "error" } });
        if (!state.finished.has(key)) {
          state.finished.add(key);
          state.tools++;
          state.failures++;
          await record(state, "工具", { native_event: event, input_ref: call?.receipt });
          await halt(state);
        }
      }
      if (/^session\.execution\.(?:succeeded|failed|interrupted)$/.test(type)) {
        state.outcome = type.split(".").at(-1);
        state.phase = state.reason && state.outcome === "interrupted" ? "needs_attention" : state.outcome;
        if (event.data.error) state.reason = JSON.stringify(event.data.error).slice(0, 500);
        for (const call of state.pending.values()) progress.observeToolError({ type: "tool", tool: call.call.tool,
          sessionID: state.session, callID: call.call.callID, state: { status: "error" } });
        state.pending.clear();
      }
      await publish(state);
    }
  })().catch(async (err) => {
    if (abort.signal.aborted) return;
    for (const state of sessions.values()) { state.audit_error = "原生事件订阅失败：" + err.message; await publish(state); }
  });
  return async () => {
    abort.abort();
    await stream;
    await Promise.all([...sessions.values()].map((state) => state.journal.queue.catch(() => {})));
  };
}
