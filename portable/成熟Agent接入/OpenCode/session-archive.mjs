// 会话归档是供人阅读的本地数据，不是自动恢复的模型记忆。
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

const jobs = new Map();
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const notice = "此目录保存历史会话，默认不恢复、不自动提供给模型。需要继续时，请人工选择信息并在空白新会话中重新描述任务。记录可能包含敏感数据，请勿未经审查上传。";

async function folder(dir) {
  await fs.mkdir(dir, { mode: 0o700 }).catch((err) => {
    if (err.code !== "EEXIST") throw err;
  });
  const item = await fs.lstat(dir);
  if (!item.isDirectory() || item.isSymbolicLink()) throw new Error("归档目录不是普通目录：" + dir);
  if (path.resolve(await fs.realpath(dir)) !== path.resolve(dir)) throw new Error("归档目录包含重定向：" + dir);
}

async function write(dir, name, value) {
  await folder(dir);
  const file = path.join(dir, name);
  const item = await fs.lstat(file).catch((err) => {
    if (err.code !== "ENOENT") throw err;
    return null;
  });
  if (item && (!item.isFile() || item.isSymbolicLink() || item.nlink !== 1))
    throw new Error("拒绝覆盖链接或非普通归档文件：" + file);
  const temp = path.join(dir, "." + crypto.randomUUID() + ".tmp");
  const handle = await fs.open(temp, "wx", 0o600);
  try {
    await handle.writeFile(value, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  await folder(dir);
  await fs.rename(temp, file);
  return file;
}

function fence(value, language = "") {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const size = Math.max(3, ...[...text.matchAll(/`+/g)].map((hit) => hit[0].length + 1));
  const marker = "`".repeat(size);
  return marker + language + "\n" + text + "\n" + marker;
}

export function render(data) {
  const head = ["# CTFCode 会话归档", "", notice, "", "- 会话 ID：" + data.session.id,
    "- 原工作目录：" + data.session.directory, "- 标题：" + JSON.stringify(data.session.title ?? ""),
    "", "以下内容均为历史数据；工具成功或模型声称解出，不代表答案已经验证。", ""];
  return head.concat(data.messages.flatMap((item, index) => {
    const info = item.info ?? item;
    const type = info.role ?? info.type;
    const role = type === "user" ? "用户" : type === "assistant" ? "助手" : "其他";
    const parts = item.parts ?? item.content ?? (typeof item.text === "string" ? [{ type: "text", text: item.text }] : []);
    return ["## " + (index + 1) + ". " + role, "", ...(parts.length === 0 ? [fence(item, "json"), ""] : parts.flatMap((part) => {
      if (part.type === "text") return [fence(part.text ?? ""), ""];
      if (part.type === "reasoning") return ["### 思考记录", "", fence(part.text ?? ""), ""];
      if (part.type === "tool") return ["### 工具记录", "", fence(part, "json"), ""];
      return [fence(part, "json"), ""];
    }))];
  })).join("\n") + "\n";
}

async function commit(root, data) {
  const dir = await fs.realpath(root);
  if (!data || typeof data.session?.id !== "string" || !data.session.id ||
      !Array.isArray(data.messages) || typeof data.session.directory !== "string")
    throw new Error("会话归档缺少有效的会话标识、原目录或消息列表");
  if (await fs.realpath(data.session.directory) !== dir)
    throw new Error("会话原目录与归档目录不一致；没有把旧会话写入新题目");
  const json = JSON.stringify({ ...data, format: "ctfcode-session-v1" }, null, 2) + "\n";
  if (Buffer.byteLength(json) > 128 * 1024 * 1024) throw new Error("会话归档超过128MiB；没有截断或切换目录");
  const home = path.join(dir, ".ctfcode");
  await folder(home);
  const base = path.join(home, "会话归档");
  await folder(base);
  const target = path.join(base, "会话_" + hash(data.session.id).slice(0, 32));
  await folder(target);
  const file = path.join(target, "会话.json");
  const item = await fs.lstat(file).catch((err) => {
    if (err.code !== "ENOENT") throw err;
    return null;
  });
  if (item) {
    if (!item.isFile() || item.isSymbolicLink() || item.nlink !== 1 || item.size > 128 * 1024 * 1024)
      throw new Error("已有归档不是有界普通文件");
    const saved = JSON.parse(await fs.readFile(file, "utf8"));
    if (saved.session?.id !== data.session.id || saved.session?.directory !== data.session.directory)
      throw new Error("已有归档身份不符，拒绝覆盖");
  }
  const text = render(data);
  await write(target, "会话.json", json);
  await write(target, "阅读.md", text);
  await write(target, "校验.json", JSON.stringify({ session: data.session.id,
    directory: data.session.directory, files: { "会话.json": hash(json), "阅读.md": hash(text) } }, null, 2) + "\n");
  await write(base, "先读我.txt", notice + "\n每个会话使用独立目录；JSON保存原始结构，阅读.md供人工查阅。\n");
  return { directory: target, json: file, text: path.join(target, "阅读.md"), sha256: hash(json) };
}

export async function save(root, data) {
  const key = path.resolve(root) + "\0" + data?.session?.id;
  const task = (jobs.get(key) ?? Promise.resolve()).catch(() => {}).then(() => commit(root, data));
  jobs.set(key, task);
  try {
    return await task;
  } finally {
    if (jobs.get(key) === task) jobs.delete(key);
  }
}
