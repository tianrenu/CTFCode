// 原生TUI控制中心；目录/模型/档位操作在这里执行，不进入模型上下文。
import { control, describe } from "./control.mjs";
import { overview, downloads } from "./welcome.mjs";

export function center(ctx, opts, policy, flush, render = () => {}) {
  let pending = false;
  let previous;
  let preparation;
  const fail = (err) => ctx.ui.toast.show({ title: "CTFCode", message: err.message, variant: "error" });
  const idle = async () => {
    if (Object.keys(await ctx.client.session.active()).length)
      throw new Error("仍有任务运行，请先停止任务，再切换模型或档位");
  };
  const wait = async () => {
    const deadline = Date.now() + 90000;
    while (true) {
      const state = await control(opts, "status");
      render(state);
      if (!["preparing", "loading", "stopping"].includes(state.model_state)) {
        if (state.cleanup_error) throw new Error(state.cleanup_error);
        return state;
      }
      if (Date.now() > deadline) throw new Error("卸载仍未结束；没有切换设置，请用 /ctf 查看真实状态");
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  };
  const halt = async () => {
    for (const id of Object.keys(await ctx.client.session.active()))
      await ctx.client.session.interrupt({ sessionID: id });
    const deadline = Date.now() + 15000;
    while (Object.keys(await ctx.client.session.active()).length) {
      if (Date.now() > deadline) throw new Error("任务仍在停止中，尚未卸载；请等待后重试");
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    await flush();
    await control(opts, "stop");
    return wait();
  };
  const execute = async (action, state) => {
    if (["status", "docs", "progress"].includes(action)) {
      const current = await control(opts, "status", { resources: action === "status" });
      await ctx.ui.dialog.alert({ title: "CTFCode · " + ({ status: "实时自检", docs: "模型准备指南", progress: "准备进度" })[action],
        message: action === "docs" ? downloads() : action === "status" ? describe(current) :
          [current.preparation_state ?? "尚未准备", current.preparation_tail ?? "暂无日志；从开始页确认准备后，日志会显示在这里。",
            ...(current.error ? ["原因：" + current.error] : []), "", "准备期间可用 /ctf stop 取消；保留部分pack，不删除权重。"].join("\n") });
      return;
    }
    if (action === "prepare") {
      await idle();
      if (state.model_files_present) {
        await ctx.ui.dialog.alert({ title: "模型已准备", message: "完整pack已存在，无需重复准备。启动时仍会进行必要核验。" });
        return;
      }
      const confirm = await ctx.ui.dialog.select({ title: "确认校验与准备模型？", placeholder: "不会下载或启动推理；现有权重不删除",
        options: [{ title: "返回，不更改文件", value: "no", description: "已有pack可跳过；缺少分片请先查看下载指南" },
          { title: "确认准备", value: "yes", description: "校验约58.4GB分片，写入所选目录的packs；需要SSD空间和至少16GiB可用RAM" }] });
      if (confirm !== "yes") return;
      await control(opts, "prepare", { confirm: true });
      ctx.ui.toast.show({ title: "CTFCode", message: "正在后台校验和准备；/ctf progress 查看进度，/ctf stop 可取消。", variant: "info" });
      return true;
    }
    if (action === "cd") {
      const dir = await ctx.ui.dialog.prompt({ title: "打开题目目录", description: "包括重开同目录，都会归档并新开空白会话。" });
      if (dir) await policy.fresh(dir);
      return;
    }
    if (action === "start") {
      const out = await control(opts, "start");
      ctx.ui.toast.show({ title: "CTFCode", message: out.model_state === "ready" ? "模型已经就绪，不重复加载。" : "开始加载；/ctf 可查看状态，完成或失败时会提示。", variant: "info" });
      return true;
    }
    if (action === "stop") {
      await halt();
      ctx.ui.toast.show({ title: "CTFCode", message: "本轮模型已卸载；TUI和会话记录保留。", variant: "success" });
      return true;
    }
    if (!["32", "64", "model"].includes(action)) throw new Error("用 /ctf 打开菜单，或输入 setup/status/start/stop/prepare/progress/32/64/model");
    await idle();
    const model = action === "model" ? await ctx.ui.dialog.prompt({ title: "选择已有模型目录",
      description: "必须是本机模型专用目录；不会复制权重或自动联网下载。", value: state.model_directory }) : undefined;
    if (action === "model" && !model) return;
    await halt();
    const out = await control(opts, "select", model ? { model: model.trim().replace(/^"(.*)"$/, "$1") } : { context: action });
    await ctx.client.location.reload();
    if (out.workspace) await policy.fresh(out.workspace);
    ctx.ui.toast.show({ title: "CTFCode", message: "设置已重载、上下文已清空；用 /ctf start 加载所选模型。", variant: "success" });
  };
  const run = async (input) => {
    if (pending) return fail(new Error("控制操作尚未结束，请等待"));
    pending = true;
    let guided = ["welcome", "setup"].includes(input?.trim());
    try {
      while (true) {
        const state = await control(opts, "status");
        render(state);
        const action = guided ? await ctx.ui.dialog.select(overview(state)) : input?.trim() ||
          await ctx.ui.dialog.select({ title: "CTFCode · 控制中心", placeholder: "仅影响本包，不修改桌面版", options: [
            { title: "开始一题 / 首次配置向导", value: "setup", description: "配置模型 → 准备pack → 打开题目 → 加载" },
            ...overview(state).options.filter((item) => item.value !== "done"),
            { title: "停止任务与模型准备 / 卸载模型", value: "stop", description: "只停止本控制器拥有的进程；保留权重与记录" },
            { title: "32K 默认档", value: "32", description: "先卸载，再重载配置和新建空白会话" },
            { title: "64K 候选档", value: "64", description: "冷输入较慢，不承诺近满窗口长期稳定" },
          ] });
        if (!action || action === "done") return;
        if (action === "setup") {
          guided = true;
          continue;
        }
        if (await execute(action, state) || !guided) return;
      }
    } catch (err) {
      fail(err);
    } finally {
      pending = false;
    }
  };
  let checking = false;
  const timer = opts.controller ? setInterval(async () => {
    if (checking) return;
    checking = true;
    try {
      const state = await control(opts, "status");
      render(state);
      if (state.exit_requested) {
        await halt();
        ctx.keymap.dispatch("app.exit");
        return;
      }
      if (previous && state.model_state !== previous && (["ready", "failed"].includes(state.model_state) || state.model_state === "stopped" && state.error))
        ctx.ui.toast.show({ title: "CTFCode · 模型状态", message: state.model_state === "ready" ?
          "模型已就绪，现在可以发送题目。" : (state.error ?? "加载失败；请用 /ctf 查看原因"),
          variant: state.model_state === "ready" ? "success" : "error", duration: 10000 });
      previous = state.model_state;
      if (preparation === "running" && state.preparation_state && state.preparation_state !== preparation)
        ctx.ui.toast.show({ title: "CTFCode · 模型准备", message: state.preparation_state === "completed" ?
          "模型准备完成；打开题目目录后即可启动。" : (state.error ?? "准备已停止；/ctf progress 查看日志"),
          variant: state.preparation_state === "completed" ? "success" : "warning", duration: 10000 });
      preparation = state.preparation_state;
    } catch (err) {
      // 持续显示“未知”，不把连接失败当成停止，也不每两秒弹一次错误。
      render({ connection_error: err.message });
    } finally {
      checking = false;
    }
  }, 2000) : undefined;
  return { run, close: () => clearInterval(timer) };
}
