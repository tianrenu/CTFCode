// 64K隔离候选：只新增传输适配插件，沿用正式CTF插件/提示/权限；不启用PC3或pin。
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { createCompactionTransport } from "./compaction-cache.mjs";

const MODEL = "qwen3.8-flash-next-coder-iq1_m";
const SESSION = "x-ctf-cache-session", PURPOSE = "x-ctf-cache-purpose";
const hash = text => crypto.createHash("sha256").update(text).digest("hex");
const ORIGINAL_CONTINUE = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.";
const RESUME_EXECUTION = "[CTF_RESUME_AFTER_COMPACTION]交接摘要步骤已经结束。现在回到用户原任务，不要续写或复述摘要。按实际工具回执定位尚未完成的步骤，使用真实工具补齐缺失证据并执行；不要重复只查文件大小、目录或原附件哈希，不能只给计划或再次总结。摘要可能未完整收尾，不完整的路径不能用作工具参数；工具默认工作目录以本次运行时元数据为准，不猜目录或补全路径。如已完成，仅依据证据报告结果；如缺少输入、需要审批或遇到错误，明确说明并等待，不绕过权限。不伪造工具调用、阶段门禁或执行回执。";

function resumeExecution(directory) {
  if (typeof directory !== "string" || !path.isAbsolute(directory) || directory.length > 4096 || /[\r\n\0]/.test(directory))
    throw new Error("CTF_RESUME_INVALID_RUNTIME_DIRECTORY");
  // directory只来自插件初始化时fs.realpath的实际根目录；不是从模型摘要抽取或自动修复工具参数。
  return RESUME_EXECUTION + "\n[CTF_RUNTIME_CONTEXT]" + JSON.stringify({ working_directory: directory,
    source: "OpenCode plugin runtime directory", notice: "仅工作目录元数据，不是完成证据或授权扩展；文件操作仍按原工具权限。" });
}

function resumeAfterCompaction(messages, directory) {
  const replacements = [];
  for (let i = 0; i + 1 < messages.length; i++) {
    const summary = messages[i], next = messages[i + 1];
    if (summary.info?.role !== "assistant" || summary.info.summary !== true || summary.info.error || summary.info.finish !== "stop" ||
        typeof summary.info.sessionID !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(summary.info.sessionID) ||
        next.info?.role !== "user" || next.info.sessionID !== summary.info.sessionID) continue;
    const part = next.parts?.find(p => p.type === "text" && p.synthetic === true && p.text === ORIGINAL_CONTINUE);
    if (!part) continue;
    part.text = resumeExecution(directory);
    replacements.push({ session: next.info.sessionID, summary_id: summary.info.id, synthetic_message_id: next.info.id });
  }
  if (!replacements.length) return null;
  return { ...replacements[0], state: "resume_execution_after_compaction", replacements,
    original_sha256: hash(ORIGINAL_CONTINUE), replacement_sha256: hash(resumeExecution(directory)),
    runtime_directory_sha256: hash(directory),
    scope: "仅澄清原生synthetic恢复消息，不改用户消息/摘要/任务/工具权限，不增加算法或答案" };
}

async function contextOverflow(response) {
  if (response.status !== 400 || !/application\/json/i.test(response.headers.get("content-type") || "")) return false;
  let reader;
  try { reader = response.clone().body?.getReader(); } catch { return false; }
  if (!reader) return false;
  const pieces = []; let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 8192) { void reader.cancel().catch(() => {}); return false; }
      pieces.push(value);
    }
    const text = Buffer.concat(pieces).toString("utf8"), error = JSON.parse(text).error;
    return error?.type === "invalid_request_error" && typeof error.message === "string" &&
      error.message.includes("exceeds the context window") && error.message.includes("requests are never truncated");
  } catch { return false; }
  finally { reader.releaseLock(); }
}

async function loadCompactionLogitBias() {
  const manifest = JSON.parse(await fs.readFile(new URL("./64K摘要输出约束_分词器.json", import.meta.url), "utf8"));
  if (manifest.model !== MODEL || manifest.blocked_token !== "<tool_call>" || manifest.bias !== -100 ||
      !Number.isSafeInteger(manifest.blocked_token_id) || manifest.blocked_token_id < 0 ||
      !/^[a-f0-9]{64}$/.test(manifest.vocabulary_sha256) || !path.isAbsolute(manifest.vocabulary_path))
    throw new Error("CTF_COMPACTION_INVALID_TOKEN_MANIFEST");
  const stat = await fs.lstat(manifest.vocabulary_path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 32 * 1024 * 1024)
    throw new Error("CTF_COMPACTION_INVALID_VOCABULARY_FILE");
  const raw = await fs.readFile(manifest.vocabulary_path);
  if (hash(raw) !== manifest.vocabulary_sha256 || JSON.parse(raw.toString("utf8"))[manifest.blocked_token] !== manifest.blocked_token_id)
    throw new Error("CTF_COMPACTION_TOKENIZER_MISMATCH");
  return { [String(manifest.blocked_token_id)]: -100 };
}

function create64kTransport({ fetchImpl = globalThis.fetch, audit = async () => {}, compactionLogitBias, resumeText } = {}) {
  const transport = createCompactionTransport({ fetchImpl, audit, pinPrefix: false, compactionLogitBias,
    preserveMechanicalHistory: true, resumeText });
  return {
    configure(config) {
      const provider = config.provider?.["strata-local"];
      if (!provider || provider.npm !== "@ai-sdk/openai-compatible" || !provider.models?.[MODEL]) return;
      const base = new URL(provider.options?.baseURL);
      if (base.port === "8080") throw new Error("CTF_COMPACTION_BURP_PORT_RESERVED");
      transport.configure(config);
      const adaptedFetch = provider.options.fetch;
      const endpoint = base.href.replace(/\/$/, "") + "/chat/completions";
      provider.options.fetch = async (input, init) => {
        const response = await adaptedFetch(input, init);
        const headers = new Headers(init?.headers), session = headers.get(SESSION);
        if (String(input) !== endpoint || init?.method?.toUpperCase() !== "POST" ||
            headers.get(PURPOSE) !== "compaction" || !/^[A-Za-z0-9_-]{1,128}$/.test(session || "") ||
            typeof init?.body !== "string" || Buffer.byteLength(init.body, "utf8") > 2 * 1024 * 1024)
          return response;
        let body;
        try { body = JSON.parse(init.body); } catch { return response; }
        if (body.model !== MODEL || body.tools?.length || !await contextOverflow(response)) return response;
        // 只对服务明确“输入超窗，未生成/未裁剪”的400做一次原生重试；不重试超时/429/500或模型长度截断。
        // 原始系统/历史/压缩目标原文不变，保留原AbortSignal，不追加时间预算、不减少上下文或输出上限。
        headers.delete(SESSION); headers.delete(PURPOSE);
        await audit({ session, state: "native_fallback_on_context_overflow", retry_limit: 1,
          original_request_sha256: hash(init.body), reason: "adapted_prompt_does_not_fit", history_truncated: false });
        const native = await fetchImpl(input, { ...init, headers });
        // 复用已验证的逐帧工具调用拒绝器；原生compaction执行器始终tools:{}。
        return createCompactionTransport.testing.textOnlyCompaction(native);
      };
    },
    headers: transport.headers,
  };
}

async function Ctf64kCompaction({ directory }) {
  const root = await fs.realpath(directory), evidence = path.join(root, ".ctf-evidence");
  const audit = async record => {
    await fs.mkdir(evidence, { recursive: true });
    const stat = await fs.lstat(evidence), real = await fs.realpath(evidence);
    if (stat.isSymbolicLink() || !stat.isDirectory() || real !== evidence) throw new Error("CTF_COMPACTION_AUDIT_DIRECTORY_UNSAFE");
    const text = JSON.stringify({ utc: new Date().toISOString(), ...record }, null, 2);
    const name = `cache64-${Date.now()}-${crypto.randomBytes(4).toString("hex")}-${hash(text).slice(0, 12)}.json`;
    await fs.writeFile(path.join(evidence, name), text, { flag: "wx" });
  };
  const compactionLogitBias = await loadCompactionLogitBias();
  const transport = create64kTransport({ audit, compactionLogitBias, resumeText: resumeExecution(root) });
  return { config: async config => transport.configure(config), "chat.headers": async (input, output) => transport.headers(input, output),
    "experimental.chat.messages.transform": async (_, output) => {
      const record = resumeAfterCompaction(output.messages, root);
      if (record) await audit(record);
      else if (process.env.CTF_RESUME_DEBUG === "1" && output.messages.some(m => m.info?.summary === true))
        await audit({ state: "resume_skip_shape", messages: output.messages.slice(-3).map(m => ({
          role: m.info?.role, session: m.info?.sessionID, summary: m.info?.summary, finish: m.info?.finish,
          parts: (m.parts || []).map(p => ({ type: p.type, synthetic: p.synthetic, text_prefix: typeof p.text === "string" ? p.text.slice(0, 120) : null }))
        })) });
    } };
}

Ctf64kCompaction.testing = Object.freeze({ create64kTransport, contextOverflow, resumeAfterCompaction, resumeExecution, loadCompactionLogitBias, RESUME_EXECUTION });
export default Ctf64kCompaction;
