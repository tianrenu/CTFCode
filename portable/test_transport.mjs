import test from "node:test";
import assert from "node:assert/strict";
import * as pluginModule from "./成熟Agent接入/OpenCode/ctf-compaction.mjs";
import crypto from "node:crypto";
import { createCompactionTransport } from "./成熟Agent接入/OpenCode/compaction-cache.mjs";
const { create64kTransport, contextOverflow, resumeAfterCompaction, resumeExecution, RESUME_EXECUTION } = pluginModule.default.testing;
const WORKSPACE = "C:\\CTF\\题目_231825";

const URL = "http://127.0.0.1:18791/v1/chat/completions", model = "qwen3.8-flash-next-coder-iq1_m";
const jsonResponse = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const overflow = () => jsonResponse(400, { error: { type: "invalid_request_error", message: "prompt exceeds the context window (65536); requests are never truncated" } });
const ok = (tool = false) => new Response('data: ' + JSON.stringify({ choices: [{ delta: tool ? { tool_calls: [{ function: { name: "read" } }] } : { content: "summary" }, finish_reason: tool ? "tool_calls" : "stop" }] }) + '\n\ndata: [DONE]\n\n', { headers: { "Content-Type": "text/event-stream" } });
function setup(fetchImpl, audit = async () => {}) {
  const p = create64kTransport({ fetchImpl, audit, compactionLogitBias: { "248058": -100 }, resumeText: resumeExecution(WORKSPACE) });
  const cfg = { provider: { "strata-local": { npm: "@ai-sdk/openai-compatible", options: { baseURL: "http://127.0.0.1:18791/v1" }, models: { [model]: {} } } } };
  p.configure(cfg);
  return { cfg, call: async (agent, body, extra = {}) => {
    const out = { headers: {} };p.headers({ sessionID: "ses_test64", agent, model: { providerID: "strata-local" } }, out);
    return cfg.provider["strata-local"].options.fetch(URL, { method: "POST", headers: out.headers, body: JSON.stringify(body), ...extra });
  } };
}
const main = { model, messages: [{ role: "system", content: "MAIN" }, { role: "user", content: "DATA" }], tools: [{ type: "function", function: { name: "read", parameters: {} } }], max_tokens: 4096 };
const compact = { model, messages: [{ role: "system", content: "COMPACT" }, { role: "user", content: "DATA" }, { role: "user", content: "summarize" }], max_tokens: 4096 };

test("only precise context errors qualify, not arbitrary400/500", async () => {
  assert.equal(await contextOverflow(overflow()), true);
  for (const response of [jsonResponse(400, { error: { type: "invalid_request_error", message: "bad parameter" } }), jsonResponse(500, { error: { message: "exceeds the context window" } }), new Response("invalid", { status: 400 })])
    assert.equal(await contextOverflow(response), false);
});
test("preserves history and schemas without PC3 or pin", async () => {
  const calls=[];const {call}=setup(async (u,i)=>{calls.push(JSON.parse(i.body));return ok();});
  await (await call("ctf-local",main)).text();await (await call("compaction",compact)).text();
  assert.equal(calls[1].messages[0].content,"MAIN");assert.deepEqual(calls[1].tools,main.tools);
  assert.equal(calls[1].messages[1].content,"DATA");assert.match(calls[1].messages.at(-1).content,/COMPACT/);
  assert.equal(calls[1].strata_prefix,undefined);assert.equal(calls[1].max_tokens,4096);
});
test("bounded error inspection cannot wait on the unconsumed original tee", async () => {
  const response=jsonResponse(400,{error:{message:"x".repeat(9000)}});
  const result=await Promise.race([contextOverflow(response),new Promise(resolve=>setTimeout(()=>resolve("timeout"),200))]);
  assert.equal(result,false);assert.match(await response.text(),/xxx/);
});
test("one original native retry preserves body and signal; strips private control headers", async () => {
  const calls=[],audit=[];const {call}=setup(async (u,i)=>{calls.push(i);return calls.length===2?overflow():ok();},async r=>audit.push(r));
  const signal=new AbortController().signal;
  await (await call("ctf-local",main)).text();assert.match(await (await call("compaction",compact,{signal})).text(),/summary/);
  assert.equal(calls.length,3);assert.deepEqual(JSON.parse(calls[2].body),compact);
  assert.equal(new Headers(calls[2].headers).has("x-ctf-cache-session"),false);
  assert.equal(calls[1].signal,signal);assert.equal(calls[2].signal,signal);
  assert.equal(audit.filter(r=>r.state==="native_fallback_on_context_overflow").length,1);
});
test("a second native400 is returned, never retried endlessly", async () => {
  let n=0;const {call}=setup(async()=>++n===1?ok():overflow());
  await (await call("ctf-local",main)).text();const response=await call("compaction",compact);
  assert.equal(response.status,400);assert.equal(n,3);
});
test("unrelated errors do not change provider behavior", async () => {
  let n=0;const {call}=setup(async()=>++n===1?ok():jsonResponse(400,{error:{type:"invalid_request_error",message:"bad parameter"}}));
  await (await call("ctf-local",main)).text();assert.equal((await call("compaction",compact)).status,400);assert.equal(n,2);
});
test("adapted and native-fallback summary streams both reject tool calls", async () => {
  for(const fallback of [false,true]){
    let n=0;const {call}=setup(async()=>{n++;return n===1?ok():n===2&&fallback?overflow():ok(true);});
    await (await call("ctf-local",main)).text();const response=await call("compaction",compact);
    await assert.rejects(response.text(),/CTF_COMPACTION_TOOL_CALL_DENIED/);
  }
});
test("Burp8080 is not accepted as a model endpoint", () => {
  const cfg={provider:{"strata-local":{npm:"@ai-sdk/openai-compatible",options:{baseURL:"http://127.0.0.1:8080/v1"},models:{[model]:{}}}}};
  assert.throws(()=>create64kTransport().configure(cfg),/BURP_PORT_RESERVED/);
});
const resumeMessages = () => [{info:{id:"summary",role:"assistant",summary:true,sessionID:"s",finish:"stop"},parts:[{type:"text",text:"原摘要不得改写"}]},
  {info:{id:"continue",role:"user",sessionID:"s"},parts:[{type:"text",synthetic:true,text:"Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."}]}];
test("only native synthetic continuation changes; summary stays byte-identical and change is idempotent", () => {
  const messages=resumeMessages(),before=JSON.stringify(messages[0]);
  assert.equal(resumeAfterCompaction(messages,WORKSPACE).state,"resume_execution_after_compaction");
  assert.equal(messages[1].parts[0].text,resumeExecution(WORKSPACE));assert.equal(JSON.stringify(messages[0]),before);
  assert.equal(resumeAfterCompaction(messages,WORKSPACE),null);
  assert.doesNotMatch(RESUME_EXECUTION,/base85|xor|challenge\.txt|flag\{/i);
});
test("does not change user-authored input, another session, failed or truncated summary", () => {
  for(const change of [m=>delete m[1].parts[0].synthetic,m=>m[1].info.sessionID="other",m=>m[0].info.error={name:"test"},m=>m[0].info.finish="length",m=>delete m[0].info.sessionID]){
    const messages=resumeMessages();change(messages);const before=JSON.stringify(messages);
    assert.equal(resumeAfterCompaction(messages,WORKSPACE),null);assert.equal(JSON.stringify(messages),before);
  }
});

test("same native synthetic continuation remains normalized after later tool turns without editing them",()=>{
  const messages=resumeMessages();messages.push({info:{role:"assistant",id:"tool_turn",sessionID:"s"},parts:[{type:"tool",tool:"read",state:{status:"completed",output:"REAL"}}]});
  const later=JSON.stringify(messages.at(-1));
  assert.equal(resumeAfterCompaction(messages,WORKSPACE).replacements.length,1);
  assert.equal(messages[1].parts[0].text,resumeExecution(WORKSPACE));assert.equal(JSON.stringify(messages.at(-1)),later);
  assert.equal(resumeAfterCompaction(messages,WORKSPACE),null);
});

function mechanicalFixture({normalizedResume=true,tampered=false}={}) {
  const value={first_user_request:{text:"original user text",full_text_sha256:"a".repeat(64)},notice:"data, not new authorization"};
  const json=JSON.stringify(value),digest=crypto.createHash("sha256").update(json).digest("hex");
  const base="真实原摘要，只确认附件存在，尚未读取内容。";
  const full=base+`\n[CTF_CONTINUITY ${tampered?"0".repeat(64):digest}]\n机械数据，非授权。\n<continuity_data>\n${json}\n</continuity_data>`;
  const normal={...main,messages:[{role:"system",content:"MAIN"},{role:"user",content:"What did we do so far?"},{role:"assistant",content:full},
    {role:"user",content:normalizedResume?resumeExecution(WORKSPACE):"Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."},
    {role:"assistant",content:"",tool_calls:[{id:"real_call",type:"function",function:{name:"read",arguments:'{"filePath":"original"}'}}]},
    {role:"tool",tool_call_id:"real_call",content:"REAL TOOL OUTPUT"}]};
  const history=structuredClone(normal.messages.slice(1));history[1].content=base;history[2].content="Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.";
  const comp={...compact,messages:[{role:"system",content:"COMPACT"},...history,{role:"user",content:"summarize"}]};
  return {normal,comp,base,full};
}

test("later compaction restores only previously seen verified mechanical history and exact continuation pair",async()=>{
  const fixture=mechanicalFixture(),calls=[],records=[];const {call}=setup(async(u,i)=>{calls.push(JSON.parse(i.body));return ok();},async r=>records.push(r));
  await(await call("ctf-local",fixture.normal)).text();await(await call("compaction",fixture.comp)).text();
  assert.deepEqual(calls[1].messages.slice(0,fixture.normal.messages.length),fixture.normal.messages);
  assert.deepEqual(calls[1].tools,fixture.normal.tools);assert.match(calls[1].messages.at(-1).content,/COMPACT/);
  assert.equal(records.find(r=>r.state==="prefix_preserved").mechanical_history_state,"mechanical_history_restored");
});

test("old normal histories with original Continue restore appendix without inventing a continuation rewrite",async()=>{
  const fixture=mechanicalFixture({normalizedResume:false}),calls=[];const {call}=setup(async(u,i)=>{calls.push(JSON.parse(i.body));return ok();});
  await(await call("ctf-local",fixture.normal)).text();await(await call("compaction",fixture.comp)).text();
  assert.deepEqual(calls[1].messages.slice(0,fixture.normal.messages.length),fixture.normal.messages);
});

test("tampered appendix is not restored, changed user text and ambiguous summaries are left untouched",async()=>{
  const funcs=createCompactionTransport.testing;
  const bad=mechanicalFixture({normalizedResume:false,tampered:true});assert.equal(funcs.captureMechanicalHistory(bad.normal).length,0);
  const fixture=mechanicalFixture(),records=funcs.captureMechanicalHistory(fixture.normal,resumeExecution(WORKSPACE));
  for(const change of [m=>m[3].content="real human new direction",m=>m.splice(4,0,{role:"assistant",content:fixture.base})]){
    const messages=structuredClone(fixture.comp.messages);change(messages);const before=structuredClone(messages);
    const result=funcs.restoreMechanicalHistory(messages,records,resumeExecution(WORKSPACE));
    assert.equal(result.restored,0);assert.deepEqual(result.messages,before);
  }
});

test("mechanical history cache is bounded and does not retain the whole tool transcript",()=>{
  const fixture=mechanicalFixture(),funcs=createCompactionTransport.testing;fixture.normal.messages.at(-1).content="X".repeat(100000);
  const records=funcs.captureMechanicalHistory(fixture.normal,resumeExecution(WORKSPACE));assert.equal(records.length,1);
  assert.ok(JSON.stringify(records).length<32768);assert.doesNotMatch(JSON.stringify(records),/XXXXXXXXXX/);
});

test("OpenCode plugin module exports only one factory, not test values/functions",()=>{ assert.deepEqual(Object.keys(pluginModule),["default"]); assert.equal(typeof pluginModule.default,"function"); });

test("runtime directory round-trips exactly and cannot inject recovery instructions",()=>{
  const directory='C:\\CTF\\题目_完整路径含空格_231825';
  const metadata=JSON.parse(resumeExecution(directory).split("[CTF_RUNTIME_CONTEXT]")[1]);
  assert.equal(metadata.working_directory,directory);
  assert.doesNotMatch(resumeExecution(directory),/base85|xor|challenge\.txt|flag\{/i);
  for(const bad of [undefined,"relative/path","C:\\CTF\nnew instruction","C:\\CTF\0bad"])
    assert.throws(()=>resumeExecution(bad),/CTF_RESUME_INVALID_RUNTIME_DIRECTORY/);
});

test("only adapted compaction bans tool-call token, preserves other bias and does not rewrite normal requests",async()=>{
  const calls=[];const {call}=setup(async(u,i)=>{calls.push(JSON.parse(i.body));return ok();});
  const normal={...main,logit_bias:{"42":1}};
  await(await call("ctf-local",normal)).text();await(await call("compaction",{...compact,logit_bias:{"42":1}})).text();
  assert.deepEqual(calls[0],normal);assert.deepEqual(calls[1].logit_bias,{"42":1,"248058":-100});
  assert.equal(calls[1].tool_choice,"auto");assert.deepEqual(calls[1].tools,main.tools);
  await(await call("ctf-local",normal)).text();assert.deepEqual(calls[2],normal);
});
test("conflicting caller tool-token bias is rejected rather than silently overwritten",async()=>{
  const {call}=setup(async()=>ok());await(await call("ctf-local",main)).text();
  await assert.rejects(call("compaction",{...compact,logit_bias:{"248058":1}}),/CTF_COMPACTION_LOGIT_BIAS_CONFLICT/);
});
test("summary token ban comes from hash-verified actual model vocabulary",{skip:!process.env.CTFCODE_VOCAB_TEST},async()=>{
  assert.deepEqual(await pluginModule.default.testing.loadCompactionLogitBias(),{"248058":-100});
});
