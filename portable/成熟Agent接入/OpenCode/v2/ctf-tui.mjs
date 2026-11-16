// V2 TUI 插件：覆盖原生命令的策略，不向模型增加工具 schema 或提示词。
import { policy, archive } from "./session-policy.mjs";
import { center } from "./ctf-center.mjs";
import { display } from "./status.mjs";

export default {
  id: "ctfcode.tui",
  async setup(ctx) {
    const banner = display(ctx);
    const state = policy(ctx, ctx.options, banner.update);
    const [memory, update] = ctx.storage.memory("boot", { initial: { started: false } });
    const fail = (err) => ctx.ui.toast.show({ title: "CTFCode", message: err.message, variant: "error" });
    const flush = async () => {
      const route = ctx.ui.router.current();
      if (route.type === "session") await archive(ctx.client, route.sessionID);
    };
    const menu = center(ctx, ctx.options, state, flush, banner.update);
    let timer;
    const unwatch = ctx.data.listen(({ details }) => {
      const route = ctx.ui.router.current();
      if (route.type !== "session" || details.data?.sessionID !== route.sessionID) return;
      if (!/^session\.(?:execution\.(?:succeeded|failed|interrupted)|step\.(?:ended|failed)|inbox\.enqueued|synthetic)$/.test(details.type)) return;
      clearTimeout(timer);
      timer = setTimeout(() => flush().catch(fail), 500);
    });
    const enter = async (value) => {
      const dir = value?.trim() || await ctx.ui.dialog.prompt({ title: "切换题目目录并新开会话",
        description: "旧会话保存在原工作目录；新会话不会恢复旧上下文。", placeholder: "粘贴题目目录的完整路径" });
      if (!dir) return;
      await state.fresh(dir).then(() => ctx.ui.toast.show({ title: "CTFCode", message: "已切换：空白新会话，旧记录留在原目录。", variant: "info" }), fail);
    };
    ctx.keymap.layer(() => ({ mode: "global", priority: 1000, commands: [{
      id: "session.cd", title: "切换题目目录（归档并新开会话）", palette: true,
      slash: { name: "cd", arguments: true }, run: enter,
    }, {
      id: "session.new", title: "归档并新开空白会话", palette: true, slash: { name: "new" }, run: async () => {
        const route = ctx.ui.router.current();
        const info = route.type === "session" ? await ctx.client.session.get({ sessionID: route.sessionID }) : undefined;
        await enter(info?.location?.directory);
      },
    }, {
      id: "ctfcode.center", title: "CTFCode · 模型、上下文与资源自检", palette: true,
      slash: { name: "ctf", arguments: true }, run: menu.run,
    }, {
      id: "ctfcode.archive", title: "保存当前会话归档", palette: true,
      slash: { name: "archive" }, run: async () => {
        const route = ctx.ui.router.current();
        if (route.type !== "session") return;
        await archive(ctx.client, route.sessionID).then((out) => ctx.ui.toast.show({
          title: "归档已保存", message: out.directory, variant: "info" }), fail);
      },
    }] }));
    // 正常启动不接收 --continue/--session，且运行器须将 tabs.mode 固定为 off。
    // 这里只在显式配置了启动目录时新建；从不读取历史消息来恢复模型上下文。
    if (ctx.options.startup && !memory.started) {
      await state.fresh(ctx.options.startup, true);
      update((draft) => { draft.started = true; });
    }
    return async () => {
      menu.close();
      banner.close();
      unwatch();
      clearTimeout(timer);
      await flush();
    };
  },
};
