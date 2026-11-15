// 开发隔离候选：仅通过OpenCode支持的config/chat.headers钩子包装本地provider.fetch。
// 保留压缩请求的提示前缀；不注册执行器、不启动代理、不改全局fetch。
import crypto from "node:crypto";

const SESSION = "x-ctf-cache-session", PURPOSE = "x-ctf-cache-purpose";
const MODEL = "qwen3.8-flash-next-coder-iq1_m";
const hash = text => crypto.createHash("sha256").update(text).digest("hex");
const clone = value => JSON.parse(JSON.stringify(value));
const ORIGINAL_CONTINUE = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.";

function mechanicalAppendix(content) {
  const at = content.lastIndexOf("\n[CTF_CONTINUITY ");
  if (at < 1 || at > 16384 || content.length - at > 16384) return null;
  const suffix = content.slice(at), match = /^\n\[CTF_CONTINUITY ([a-f0-9]{64})\]\n[^]*?\n<continuity_data>\n([^]+)\n<\/continuity_data>$/.exec(suffix);
  if (!match || match[2].length > 6000) return null;
  try {
    const value = JSON.parse(match[2]);
    if (!value || Array.isArray(value) || typeof value !== "object" || hash(JSON.stringify(value)) !== match[1]) return null;
  } catch { return null; }
  return { base: content.slice(0, at), normalized: content };
}

function captureMechanicalHistory(body, resumeText) {
  const start = leadingSystems(body).length, records = [];
  for (let i = start; i < body.messages.length; i++) {
    const m = body.messages[i];
    if (m.role !== "assistant" || typeof m.content !== "string") continue;
    const appendix = mechanicalAppendix(m.content);
    const resume = typeof resumeText === "string" && body.messages[i + 1]?.role === "user" && body.messages[i + 1].content === resumeText;
    if (!appendix && !resume) continue;
    if (!m.content || m.content.length > 32768) return [];
    records.push({ offset: i - start, base: appendix?.base ?? m.content, normalized: m.content, resume });
    if (records.length > 4 || JSON.stringify(records).length > 32768) return [];
  }
  return records;
}

function restoreMechanicalHistory(messages, records, resumeText) {
  if (!records?.length) return { messages, restored: 0, state: "no_mechanical_history" };
  const changed = clone(messages), start = leadingSystems({ messages }).length;
  for (const record of records) {
    const index = start + record.offset, m = changed[index];
    const matches = messages.filter(v => v.role === "assistant" && (v.content === record.base || v.content === record.normalized));
    if (!m || m.role !== "assistant" || ![record.base, record.normalized].includes(m.content) || matches.length !== 1)
      return { messages, restored: 0, state: "native_history_unchanged", reason: "summary_missing_changed_or_ambiguous" };
    if (record.resume) {
      const next = changed[index + 1];
      if (next?.role !== "user" || ![ORIGINAL_CONTINUE, resumeText].includes(next.content))
        return { messages, restored: 0, state: "native_history_unchanged", reason: "continuation_not_the_captured_pair" };
      next.content = resumeText;
    }
    m.content = record.normalized;
  }
  return { messages: changed, restored: records.length, state: "mechanical_history_restored" };
}

function textOnlyCompaction(response) {
  if (!response.ok) return response;
  if (!response.body || !/^text\/event-stream(?:;|$)/i.test(response.headers.get("content-type") || ""))
    throw new Error("CTF_COMPACTION_INVALID_STREAM");
  const decoder = new TextDecoder("utf-8", { fatal: true }), encoder = new TextEncoder();
  let pending = "";
  const check = frame => {
    if (frame.length > 262144) throw new Error("CTF_COMPACTION_STREAM_FRAME_LIMIT");
    const data = frame.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
    if (!data || data.trim() === "[DONE]") return;
    const item = JSON.parse(data);
    for (const choice of item.choices || []) {
      for (const part of [choice.delta, choice.message]) {
        if (part?.function_call != null || part?.tool_calls && (!Array.isArray(part.tool_calls) || part.tool_calls.length))
          throw new Error("CTF_COMPACTION_TOOL_CALL_DENIED");
      }
      if (["tool_calls", "function_call"].includes(choice.finish_reason))
        throw new Error("CTF_COMPACTION_TOOL_CALL_DENIED");
    }
  };
  const consume = (controller, final = false) => {
    let match;
    while ((match = /\r?\n\r?\n/.exec(pending))) {
      const frame = pending.slice(0, match.index);
      check(frame);
      controller.enqueue(encoder.encode(pending.slice(0, match.index + match[0].length)));
      pending = pending.slice(match.index + match[0].length);
    }
    if (pending.length > 262144) throw new Error("CTF_COMPACTION_STREAM_FRAME_LIMIT");
    if (final && pending) { check(pending); controller.enqueue(encoder.encode(pending)); pending = ""; }
  };
  const stream = response.body.pipeThrough(new TransformStream({
    transform(chunk, controller) { pending += decoder.decode(chunk, { stream: true }); consume(controller); },
    flush(controller) { pending += decoder.decode(); consume(controller, true); },
  }));
  const headers = new Headers(response.headers);
  headers.delete("content-length"); headers.delete("content-encoding");
  return new Response(stream, { status: response.status, statusText: response.statusText, headers });
}

function leadingSystems(body) {
  const systems = [];
  for (const message of body.messages || []) {
    if (message.role !== "system" || typeof message.content !== "string") break;
    systems.push(message);
  }
  return systems;
}

export function createCompactionTransport({ fetchImpl = globalThis.fetch, audit = async () => {}, pinPrefix = false, compactionLogitBias, preserveMechanicalHistory = false, resumeText } = {}) {
  if (compactionLogitBias !== undefined && (!compactionLogitBias || Array.isArray(compactionLogitBias) ||
      !Object.keys(compactionLogitBias).length || Object.entries(compactionLogitBias).some(([id, value]) => !/^[0-9]+$/.test(id) || value !== -100)))
    throw new Error("CTF_COMPACTION_INVALID_LOGIT_BAN");
  // 每会话只保存system/tools和最多32K字符的已见机械附录/恢复消息绑定，不存整段工具历史。
  const kept = new Map();
  const wrap = baseURL => {
    const base = new URL(baseURL);
    if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || base.username || base.password)
      throw new Error("CTF_COMPACTION_LOCAL_PROVIDER_ONLY");
    const endpoint = base.href.replace(/\/$/, "") + "/chat/completions";
    return async (input, init) => {
      const headers = new Headers(init?.headers);
      const session = headers.get(SESSION), purpose = headers.get(PURPOSE);
      headers.delete(SESSION); headers.delete(PURPOSE);
      const cleanInit = { ...init, headers };
      if (String(input) !== endpoint || init?.method?.toUpperCase() !== "POST" || typeof init?.body !== "string" ||
          !session || !/^[A-Za-z0-9_-]{1,128}$/.test(session))
        return fetchImpl(input, cleanInit);
      if (Buffer.byteLength(init.body, "utf8") > 2 * 1024 * 1024) return fetchImpl(input, cleanInit);
      let body;
      try { body = JSON.parse(init.body); } catch { return fetchImpl(input, cleanInit); }
      if (body.model !== MODEL || !Array.isArray(body.messages)) return fetchImpl(input, cleanInit);
      const key = `${endpoint}\0${session}\0${body.model}`;
      if (!["ctf-local", "compaction"].includes(purpose)) {
        kept.delete(key);
        return fetchImpl(input, cleanInit);
      }
      if (purpose === "ctf-local") {
        const systems = leadingSystems(body);
        // 只固定system/schema这个真实前缀，不固定用户/工具历史，也不猜token长度。
        // 不覆盖调用方已有的prefix约定；原生服务负责按实际模板解析并验证边界。
        const request = pinPrefix && systems.length && body.strata_prefix === undefined ?
          { ...cleanInit, body: JSON.stringify({ ...body, strata_prefix: { messages: 1 } }) } : cleanInit;
        const response = await fetchImpl(input, request);
        if (response.ok && systems.length && Array.isArray(body.tools) && body.tools.length && body.tool_choice !== "none") {
          const entry = { systems: clone(systems), tools: clone(body.tools),
            mechanical: preserveMechanicalHistory ? captureMechanicalHistory(body, resumeText) : [] };
          if (JSON.stringify(entry).length <= 131072) {
            kept.delete(key); kept.set(key, entry);
            while (kept.size > 8) kept.delete(kept.keys().next().value);
          } else kept.delete(key);
        } else kept.delete(key);
        return response;
      }
      const previous = kept.get(key), systems = leadingSystems(body);
      const last = body.messages.at(-1);
      if (!previous || !systems.length || body.tools?.length || last?.role !== "user" || typeof last.content !== "string") {
        await audit({ session, state: "native_fallback", reason: "no_matching_prefix_or_unexpected_shape" });
        return fetchImpl(input, cleanInit);
      }
      const changed = clone(body);
      changed.messages = [...clone(previous.systems), ...changed.messages.slice(systems.length)];
      const restored = restoreMechanicalHistory(changed.messages, previous.mechanical, resumeText);
      changed.messages = restored.messages;
      // 保留原压缩系统指令原文但移到尾部；只恢复此前同会话wire已含的机械附录/已绑定继续消息。
      // 不删除/改写人类原文、调用ID、工具结果或任何未知内容；匹配不明确时不恢复历史。
      changed.messages[changed.messages.length - 1].content =
        "[CTF_COMPACTION_ONLY]本请求只生成交接摘要，禁止调用工具或继续执行任务。\n" +
        systems.map(message => message.content).join("\n") + "\n\n" + last.content;
      changed.tools = clone(previous.tools);
      if (pinPrefix && changed.strata_prefix === undefined) changed.strata_prefix = { messages: 1 };
      // 仅供模型模板保留前缀；实际compaction执行器仍是tools:{}。
      // Strata的tool_choice:none会连提示schema一起删掉，故在响应端严格拒绝任何调用。
      changed.tool_choice = "auto";
      // 原生none会移除模板schema并丢失缓存；仅为摘要请求禁用工具起始token，普通执行请求不改。
      // 这不是执行权限；下面逐帧工具调用拒绝器和原生tools:{}仍保留为独立保护。
      if (compactionLogitBias !== undefined) {
        const previousBias = body.logit_bias ?? {};
        if (typeof previousBias !== "object" || Array.isArray(previousBias) ||
            Object.entries(compactionLogitBias).some(([id, value]) => previousBias[id] !== undefined && previousBias[id] !== value))
          throw new Error("CTF_COMPACTION_LOGIT_BIAS_CONFLICT");
        changed.logit_bias = { ...clone(previousBias), ...compactionLogitBias };
      }
      await audit({ session, state: "prefix_preserved", system_sha256: hash(JSON.stringify(previous.systems)),
        tools_sha256: hash(JSON.stringify(previous.tools)), history_messages: body.messages.length - systems.length - 1,
        execution_tools: 0, response_tool_calls: "deny", prefix_pin_requested: pinPrefix,
        mechanical_history_state: restored.state, mechanical_history_records: restored.restored,
        mechanical_history_reason: restored.reason,
        summary_token_bans: compactionLogitBias ? Object.keys(compactionLogitBias).map(Number) : [] });
      try {
        const response = await fetchImpl(input, { ...cleanInit, body: JSON.stringify(changed) });
        return textOnlyCompaction(response);
      } catch (error) { kept.delete(key); throw error; }
    };
  };
  return {
    configure(config) {
      const provider = config.provider?.["strata-local"];
      if (!provider || provider.npm !== "@ai-sdk/openai-compatible" || !provider.models?.[MODEL]) return;
      if (provider.options?.fetch) throw new Error("CTF_COMPACTION_EXISTING_FETCH_NOT_REPLACED");
      provider.options.fetch = wrap(provider.options.baseURL);
    },
    headers({ sessionID, agent, model }, output) {
      const purpose = typeof agent === "string" ? agent : agent?.name;
      if (model?.providerID !== "strata-local") return;
      output.headers[SESSION] = sessionID; output.headers[PURPOSE] = purpose || "other";
    },
  };
}

createCompactionTransport.testing = Object.freeze({ textOnlyCompaction, mechanicalAppendix, captureMechanicalHistory, restoreMechanicalHistory });
