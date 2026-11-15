// OpenCode V2 原生 /cd 的 CTF 策略：先归档，再新建，不移动或恢复旧会话。
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { save } from "../session-archive.mjs";
import { control } from "./control.mjs";

const exec = promisify(execFile);

export async function check(opts, value, base) {
  if (typeof value !== "string" || !value.trim() || /[\x00\r\n]/.test(value))
    throw new Error("请输入有效的题目目录");
  const raw = value.trim().replace(/^"(.*)"$/s, "$1");
  const full = raw === "~" ? os.homedir() : /^~[/\\]/.test(raw) ? path.join(os.homedir(), raw.slice(2)) : raw;
  const dir = path.resolve(base, full);
  if (!path.isAbsolute(opts.python ?? "") || !path.isAbsolute(opts.validator ?? ""))
    throw new Error("缺少独立运行器的绝对路径，未绕过目录门禁");
  const out = await exec(opts.python, ["-I", "-X", "utf8", opts.validator, "--workspace-check", dir],
    { windowsHide: true, timeout: 20000, maxBuffer: 65536, encoding: "utf8" }).catch((err) => {
      const out = err.stdout && JSON.parse(err.stdout);
      throw new Error(out?.error ?? "工作目录校验进程失败；未切换");
    });
  const result = JSON.parse(out.stdout);
  if (result.ok !== true || typeof result.directory !== "string" || !path.isAbsolute(result.directory))
    throw new Error("工作目录校验没有返回有效结果");
  return result.directory;
}

export async function archive(client, id) {
  const data = await client.experimental.session.export({ sessionID: id, sanitize: false });
  const dir = data.info?.location?.directory;
  if (data.info?.id !== id || typeof dir !== "string" || !Array.isArray(data.messages))
    throw new Error("原生会话导出不完整；没有归档或切换目录");
  return save(dir, { session: { ...data.info, directory: dir }, messages: data.messages });
}

export function policy(ctx, opts) {
  let pending = false;
  async function fresh(value, initial = false) {
    if (pending) throw new Error("正在切换目录，请等待当前操作结束");
    pending = true;
    try {
      const route = ctx.ui.router.current();
      const previous = route.type === "session" ? route.sessionID : undefined;
      const source = previous ? await ctx.client.session.get({ sessionID: previous }) : undefined;
      const base = source?.location?.directory ?? ctx.location?.directory ?? process.cwd();
      const dir = await check(opts, value, base);
      const active = await ctx.client.session.active();
      if (Object.keys(active).length) throw new Error("当前仍有任务运行，请先停止任务，等它停止后再切换目录");
      // 启动只创建新会话，不导出/读取默认首页可能列出的旧记录。
      const saved = previous && !initial ? await archive(ctx.client, previous) : undefined;
      // 归档后重新校验，避免把目录检查期间出现的配置/插件带入新 Location。
      await check(opts, dir, base);
      if (Object.keys(await ctx.client.session.active()).length)
        throw new Error("归档期间有任务开始运行，请先停止任务；未切换目录");
      const info = await ctx.client.session.create({ location: { directory: dir },
        title: path.basename(dir) + " · 新会话", ...(opts.agent ? { agent: opts.agent } : {}),
        ...(opts.model ? { model: opts.model } : {}),
        metadata: { ctfcode: { fresh: true, automatic_restore: false } } });
      if (info.parentID || info.location?.directory !== dir || info.id === previous)
        throw new Error("原生新会话的身份或目录异常，未切换界面");
      await ctx.data.session.sync(info.id);
      if (opts.controller) await control(opts, "workspace", { directory: dir, session: info.id });
      ctx.ui.router.navigate({ type: "session", sessionID: info.id });
      return { session: info, archive: saved };
    } finally {
      pending = false;
    }
  }
  return { fresh };
}
