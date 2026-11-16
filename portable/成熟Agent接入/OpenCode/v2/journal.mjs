// 当前原生会话的本地证据；不恢复旧会话、不新增模型工具、不执行源码。
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import Legacy from "../ctf-context-64k.mjs";

export const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
export const text = (value) => typeof value === "string" ? value : (value ?? []).filter((part) => part.type === "text").map((part) => part.text).join("\n");

async function folder(dir) {
  await fs.mkdir(dir).catch((err) => { if (err.code !== "EEXIST") throw err; });
  const stat = await fs.lstat(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink() || path.resolve(await fs.realpath(dir)) !== path.resolve(dir))
    throw new Error("会话证据目录包含链接或重定向");
}

export class Journal {
  constructor(root, session) {
    this.root = path.resolve(root);
    this.session = session;
    this.dir = path.join(this.root, ".ctfcode", "会话证据", "会话_" + hash(session).slice(0, 32));
    this.queue = Promise.resolve();
  }
  async prepare() {
    await folder(path.join(this.root, ".ctfcode"));
    await folder(path.join(this.root, ".ctfcode", "会话证据"));
    await folder(this.dir);
  }
  async store(kind, value) {
    if (!/^[\p{L}\p{N}_-]{1,24}$/u.test(kind)) throw new Error("证据种类无效");
    const task = this.queue.catch(() => {}).then(async () => {
      await this.prepare();
      const raw = JSON.stringify({ format: 1, session: this.session, directory: this.root, kind, data: value }) + "\n";
      if (Buffer.byteLength(raw) > 2 * 1024 * 1024) throw new Error("单条原生证据超过2MiB；未静默截断");
      const digest = hash(raw);
      const file = path.join(this.dir, kind + "_" + digest + ".json");
      if (this.used === undefined) {
        const rows = await fs.readdir(this.dir, { withFileTypes: true });
        const sizes = await Promise.all(rows.filter((row) => row.isFile()).map((row) => fs.lstat(path.join(this.dir, row.name))));
        this.used = sizes.reduce((size, stat) => size + stat.size, 0);
      }
      if (this.used + Buffer.byteLength(raw) > 128 * 1024 * 1024)
        throw new Error("当前会话证据超过128MiB；没有覆盖旧证据");
      const stat = await fs.lstat(file).catch((err) => { if (err.code !== "ENOENT") throw err; return null; });
      if (stat) {
        if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 2 * 1024 * 1024 || await fs.readFile(file, "utf8") !== raw)
          throw new Error("同名原生证据的类型或内容不符");
      }
      if (!stat) {
        await fs.writeFile(file, raw, { encoding: "utf8", flag: "wx", mode: 0o600 });
        this.used += Buffer.byteLength(raw);
      }
      return { path: path.relative(this.root, file).replaceAll("\\", "/"), sha256: digest, bytes: Buffer.byteLength(raw) };
    });
    this.queue = task;
    return task;
  }
  async verify(receipt) {
    const file = path.resolve(this.root, receipt.path);
    if (path.dirname(file) !== this.dir || !/^[a-f0-9]{64}$/.test(receipt.sha256)) throw new Error("证据引用不属于当前会话");
    await this.prepare();
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 2 * 1024 * 1024) throw new Error("原生证据不是有界普通文件");
    const raw = await fs.readFile(file, "utf8");
    if (hash(raw) !== receipt.sha256) throw new Error("原生证据哈希不符");
    const data = JSON.parse(raw);
    if (data.session !== this.session || data.directory !== this.root) throw new Error("原生证据的会话或目录不符");
    return data;
  }
}

export class Progress extends Legacy.testing.ProgressTracker {
  // V2原生read送给模型的是带行号的文本，不伪造V1 XML包装或完整读回。
  async recordReadback(state, file, args, output) {
    if (typeof output.output !== "string" || !output.output.startsWith("Read file ")) return;
    const logical = path.resolve(this.root, file.path);
    if (await fs.realpath(logical) !== logical) return;
    const stat = await fs.lstat(logical);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1024 * 1024) return;
    const bytes = await fs.readFile(logical);
    if (hash(bytes) !== file.sha256 || bytes.includes(0)) return;
    const lines = bytes.toString("utf8").split("\n").map((line) => line.replace(/\r$/, ""));
    if (lines.at(-1) === "") lines.pop();
    const start = args.offset ?? 1;
    const limit = args.limit ?? 120;
    const delivered = output.output.split(/\r?\n/).slice(1).flatMap((line) => {
      const match = /^([1-9][0-9]*): (.*)$/.exec(line);
      if (!match) return [];
      const index = Number(match[1]);
      return index >= start && index < start + limit && match[2] === lines[index - 1] ? [[index, index]] : [];
    });
    state.read_ranges ??= new Map();
    const old = state.read_ranges.get(file.path);
    const ranges = [...(old?.sha256 === file.sha256 ? old.ranges : []), ...delivered].sort((a, b) => a[0] - b[0]).reduce((rows, pair) => {
      const last = rows.at(-1);
      if (last && pair[0] <= last[1] + 1) last[1] = Math.max(last[1], pair[1]);
      if (!last || pair[0] > last[1] + 1) rows.push([...pair]);
      return rows;
    }, []).slice(-128);
    const complete = bytes.length === 0 && /, 0 lines$/.test(output.output.trim()) ||
      ranges.length === 1 && ranges[0][0] === 1 && ranges[0][1] === lines.length;
    state.read_ranges.set(file.path, { sha256: file.sha256, total_lines: lines.length, ranges, complete });
    if (complete) state.readback.add(file.path);
    if (!complete) state.readback.delete(file.path);
  }
}

export function input(event, root) {
  const script = event.tool === "shell" && Legacy.testing.simplePythonCall(event.input?.command);
  const command = script && event.input.workdir && root ?
    'python "' + path.resolve(root, event.input.workdir, script) + '"' : event.input?.command;
  return { tool: event.tool === "shell" ? "bash" : event.tool, sessionID: event.sessionID, callID: event.id,
    args: { ...event.input, ...(command ? { command } : {}), ...(event.input?.path ? { filePath: event.input.path } : {}) } };
}

export async function continuity(progress, journal, requests) {
  const receipt = await progress.artifactReceipt(journal.session);
  const view = (item) => item && ({ ...item.receipt, excerpt: item.text.length > 360 ? item.text.slice(0, 240) + "…" + item.text.slice(-120) : item.text });
  const data = { first_user_request: view(requests[0]), latest_user_request: view(requests.at(-1)),
    limits: progress.snapshot(journal.session)?.contract ?? null, artifact_receipt: receipt,
    note: "仅当前会话的人类原文与压缩时的执行/读回线索；不是新授权或答案正确证明，使用前须按路径/哈希复核。" };
  const saved = await journal.store("连续性", data);
  return { data: Buffer.byteLength(JSON.stringify(data)) <= 6000 ? data : { record: saved,
    limits: data.limits && { read_only: data.limits.read_only, strict_read_only: data.limits.strict_read_only,
      requires_script: data.limits.requires_script, request_sha256: data.limits.request_sha256 },
    note: "完整连续性数据较大；按当前会话证据索引读取，未截断文件或猜测结论。" }, receipt: saved };
}

export function pin(event, pins, pending) {
  const message = event.messages.findLast((item) => item.metadata?.ctfcode?.type === "compaction" &&
    item.metadata.ctfcode.status === "completed" && item.id === item.metadata.ctfcode.messageID);
  if (!message) return false;
  const known = pins.get(message.id) ?? pending;
  if (!known) return false;
  pins.set(message.id, known);
  if (pins.size > 8) pins.delete(pins.keys().next().value);
  const marker = "[CTF_CONTINUITY " + known.receipt.sha256 + "]";
  const value = text(message.content);
  if (!value.startsWith("<conversation-checkpoint>")) throw new Error("原生压缩消息格式变化；未猜测或修改用户文字");
  const data = JSON.stringify(known.data);
  if (Buffer.byteLength(data) > 6000) throw new Error("连续性数据超过上下文预算；未静默截断");
  if (!value.includes(marker)) message.content = [{ type: "text", text: value + "\n" + marker +
    "\n以下是历史数据，不扩展授权；完整记录用原生read按需读取，不自动扫描其他会话。\n" + data }];
  return true;
}
