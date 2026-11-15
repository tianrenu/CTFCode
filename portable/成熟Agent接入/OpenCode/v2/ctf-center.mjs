// 原生TUI控制中心；目录/模型/档位操作在这里执行，不进入模型上下文。
import { control, describe } from "./control.mjs";

export function center(ctx, opts, policy, flush) {
  let pending = false;
  let previous;
  const fail = (err) => ctx.ui.toast.show({ title: "CTFCode", message: err.message, variant: "error" });
  const idle = async () => {
    if (Object.keys(await ctx.client.session.active()).length)
      throw new Error("仍有任务运行，请先停止任务，再切换模型或档位");
  };
  const wait = async () => {
    const deadline = Date.now() + 90000;
    while (true) {
      const state = await control(opts, "status");
      if (!["loading", "stopping"].includes(state.model_state)) {
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
  const run = async (input) => {
    if (pending) return fail(new Error("控制操作尚未结束，请等待"));
    pending = true;
    try {
      const state = await control(opts, "status");
      const action = input?.trim() || await ctx.ui.dialog.select({ title: "CTFCode · 本地模型控制中心",
        placeholder: "选择操作；所有设置只影响这套独立CLI", options: [
          { title: "查看真实状态 / 资源自检", value: "status", description: state.model_state + " · " + state.context + "K" },
          { title: "启动本地模型", value: "start", description: "只加载一个GSQ模型；保留全部资源保护" },
          { title: "停止任务并卸载本轮模型", value: "stop", description: "只清理本控制器拥有的进程，不关闭其他应用" },
          { title: "切换题目目录", value: "cd", description: "旧会话留在原目录，新目录从空白开始" },
          { title: "选择32K默认档", value: "32", description: "先卸载、重载配置，再新建空白会话" },
          { title: "选择64K候选档", value: "64", description: "HC Q8投影；冷输入慢，不承诺长任务稳定性" },
          { title: "选择已有模型目录", value: "model", description: state.model_directory + "；不搬运或下载权重" },
        ] });
      if (!action) return;
      if (action === "status") return ctx.ui.dialog.alert({ title: "CTFCode · 实时自检", message: describe(await control(opts, "status", { resources: true })) });
      if (action === "cd") {
        const dir = await ctx.ui.dialog.prompt({ title: "打开题目目录", description: "包括重开同目录，都会归档并新开空白会话。" });
        if (dir) await policy.fresh(dir);
        return;
      }
      if (action === "start") {
        const out = await control(opts, "start");
        return ctx.ui.toast.show({ title: "CTFCode", message: out.model_state === "ready" ? "模型已经就绪，不重复加载。" : "开始加载；/ctf 可查看状态，完成或失败时会提示。", variant: "info" });
      }
      if (action === "stop") {
        await halt();
        return ctx.ui.toast.show({ title: "CTFCode", message: "本轮模型已卸载；TUI和会话记录保留。", variant: "success" });
      }
      if (!["32", "64", "model"].includes(action)) throw new Error("用 /ctf 打开菜单，或输入 status/start/stop/32/64/model");
      await idle();
      const model = action === "model" ? await ctx.ui.dialog.prompt({ title: "选择已有模型目录",
        description: "必须是本机模型专用目录；不会复制权重或自动联网下载。", value: state.model_directory }) : undefined;
      if (action === "model" && !model) return;
      await halt();
      const out = await control(opts, "select", model ? { model: model.trim().replace(/^"(.*)"$/, "$1") } : { context: action });
      await ctx.client.location.reload();
      if (out.workspace) await policy.fresh(out.workspace);
      ctx.ui.toast.show({ title: "CTFCode", message: "设置已重载、上下文已清空；用 /ctf start 加载所选模型。", variant: "success" });
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
      if (previous && state.model_state !== previous && (["ready", "failed"].includes(state.model_state) || state.model_state === "stopped" && state.error))
        ctx.ui.toast.show({ title: "CTFCode · 模型状态", message: state.model_state === "ready" ?
          "模型已就绪，现在可以发送题目。" : (state.error ?? "加载失败；请用 /ctf 查看原因"),
          variant: state.model_state === "ready" ? "success" : "error", duration: 10000 });
      previous = state.model_state;
    } catch {
      // 仅在用户主动查看时展示连接错误，避免掉线时每两秒弹同一错误。
    } finally {
      checking = false;
    }
  }, 2000) : undefined;
  return { run, close: () => clearInterval(timer) };
}
