// 使用公开OpenTUI节点构造器，不接触私有Prompt/Renderer状态，不向模型添加schema。
import { TextRenderable } from "@opentui/core";
import { frame } from "./control.mjs";

export function display(ctx) {
  const nodes = new Set();
  let state;
  const landing = () => ["CTFCode · 本地 CTF 工作台", frame(state), "/ctf 开始与配置  ·  /cd 打开题目  ·  /exit 归档退出"].join("\n");
  const close = ctx.ui.slot({ before: "session.composer.top", render: (input) => {
    const node = new TextRenderable(ctx.renderer, { content: frame(state, input.sessionID), fg: ctx.theme.text.muted,
      height: 1, width: "100%", paddingLeft: 2 });
    const entry = { node, session: input.sessionID };
    nodes.add(entry);
    node.on("destroyed", () => nodes.delete(entry));
    return node;
  } });
  const home = ctx.ui.slot({ after: "home.footer", render: () => {
    const node = new TextRenderable(ctx.renderer, { content: landing(),
      fg: ctx.theme.text.muted, height: 4, width: "100%", paddingLeft: 2 });
    const entry = { node, home: true };
    nodes.add(entry);
    node.on("destroyed", () => nodes.delete(entry));
    return node;
  } });
  return { update: (next) => { state = next; for (const entry of nodes) entry.node.content = entry.home ? landing() : frame(state, entry.session); },
    close: () => { close(); home(); } };
}
