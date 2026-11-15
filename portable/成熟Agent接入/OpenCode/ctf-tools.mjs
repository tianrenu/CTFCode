// 通用只读取证与模型自写脚本接口；没有题目路径、答案、密钥或解码流程。
import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
const sha = s => crypto.createHash("sha256").update(s).digest("hex");

// 仅解析模型刚写的源码，绝不import/exec/eval该源码，也不运行附件或提供解码算法。
// 固定受信任解释器 + -I -S：不加载题目目录中的re.py、sitecustomize或用户site包。
const STATIC_PYTHON = process.env.CTFCODE_PYTHON;
const SOURCE_INSPECTOR = String.raw`
import ast,json,re,sys
MAX=65536
data=json.loads(sys.stdin.buffer.read(131073))
source=data.get('source')
if not isinstance(source,str) or len(source.encode('utf-8'))>MAX: raise ValueError('source_limit')
source=source.removeprefix('\ufeff')
result={'status':'checked','syntax_valid':True,'findings':[],'executes_source':False,'semantic_result_verified':False}
try:
    tree=ast.parse(source,filename='<model_source>')
except (SyntaxError,ValueError,RecursionError) as e:
    result['syntax_valid']=False
    result['findings']=[{'code':'PY_SYNTAX_ERROR','severity':'warning','line':getattr(e,'lineno',None),'message':str(getattr(e,'msg',type(e).__name__))[:180]}]
else:
    if sum(1 for _ in ast.walk(tree))>20000: raise ValueError('ast_limit')
    modules=set(); compilers=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name=='re')
        if isinstance(node,ast.ImportFrom) and node.module=='re':
            compilers.update(a.asname or a.name for a in node.names if a.name=='compile')
    def regex(expr,patterns):
        if isinstance(expr,ast.Name): return patterns.get(expr.id)
        if not isinstance(expr,ast.Call) or not expr.args: return None
        f=expr.func
        compile_call=(isinstance(f,ast.Name) and f.id in compilers or isinstance(f,ast.Attribute) and isinstance(f.value,ast.Name) and f.value.id in modules and f.attr=='compile')
        if not compile_call: return None
        pat=expr.args[0]; flags=0
        if not isinstance(pat,ast.Constant) or not isinstance(pat.value,str): return None
        if len(expr.args)>2 or any(k.arg!='flags' for k in expr.keywords): return None
        flag=expr.args[1] if len(expr.args)==2 else next((k.value for k in expr.keywords),None)
        if flag is not None:
            if not isinstance(flag,ast.Constant) or type(flag.value) is not int: return None
            flags=flag.value
        try: return re.compile(pat.value,flags)
        except (re.error,ValueError,OverflowError,RecursionError): return None
    def match(expr,patterns,iterator=False):
        if not isinstance(expr,ast.Call) or not isinstance(expr.func,ast.Attribute): return None
        if expr.func.attr not in (('finditer',) if iterator else ('match','search','fullmatch')): return None
        return regex(expr.func.value,patterns)
    seen=set()
    def scan(node,patterns,matches):
        if node is None: return
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)): return
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='group' and isinstance(node.func.value,ast.Name):
            obj=node.func.value.id; rx=matches.get(obj)
            if rx is not None:
                for arg in node.args:
                    if not isinstance(arg,ast.Constant) or type(arg.value) is not int or arg.value<=0: continue
                    index=arg.value; named=next((k for k,v in rx.groupindex.items() if v==index),None)
                    code='PY_GROUP_INDEX_OUT_OF_RANGE' if index>rx.groups else 'PY_NAMED_GROUP_POSITIONAL' if rx.groupindex else None
                    key=(node.lineno,node.col_offset,index)
                    if code and key not in seen and len(result['findings'])<12:
                        seen.add(key)
                        result['findings'].append({'code':code,'severity':'warning' if index>rx.groups else 'info','line':node.lineno,'match_variable':obj[:64],'index':index,'group_name':named,'group_count':rx.groups})
        for child in ast.iter_child_nodes(node): scan(child,patterns,matches)
    def targets(node):
        return {n.id for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
    def block(body,patterns,matches):
        patterns=dict(patterns); matches=dict(matches)
        for node in body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                local=dict(patterns)
                for child in node.body:
                    for name in targets(child): local.pop(name,None)
                args=[*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs]
                args.extend(a for a in (node.args.vararg,node.args.kwarg) if a is not None)
                for a in args: local.pop(a.arg,None)
                block(node.body,local,{})
                patterns.pop(node.name,None); matches.pop(node.name,None)
            elif isinstance(node,ast.ClassDef):
                patterns.pop(node.name,None); matches.pop(node.name,None)
            elif isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
                value=node.value; scan(value,patterns,matches)
                rx=regex(value,patterns); mx=match(value,patterns)
                dest=node.targets if isinstance(node,ast.Assign) else [node.target]
                for target in dest:
                    for name in targets(target): patterns.pop(name,None); matches.pop(name,None)
                    if isinstance(target,ast.Name):
                        if rx is not None: patterns[target.id]=rx
                        if mx is not None: matches[target.id]=mx
            elif isinstance(node,(ast.For,ast.AsyncFor)):
                scan(node.iter,patterns,matches); inner=dict(matches); pp=dict(patterns)
                for name in targets(node.target): inner.pop(name,None); pp.pop(name,None)
                rx=match(node.iter,patterns,True)
                if rx is not None and isinstance(node.target,ast.Name): inner[node.target.id]=rx
                block(node.body,pp,inner); block(node.orelse,patterns,matches)
                for child in node.body:
                    for name in targets(child): patterns.pop(name,None); matches.pop(name,None)
            elif isinstance(node,(ast.If,ast.While)):
                scan(node.test,patterns,matches); block(node.body,patterns,matches); block(node.orelse,patterns,matches)
                for child in [*node.body,*node.orelse]:
                    for name in targets(child): patterns.pop(name,None); matches.pop(name,None)
            elif isinstance(node,(ast.With,ast.AsyncWith)):
                for item in node.items: scan(item.context_expr,patterns,matches)
                block(node.body,patterns,matches)
                for child in node.body:
                    for name in targets(child): patterns.pop(name,None); matches.pop(name,None)
            elif isinstance(node,ast.Try):
                for body2 in [node.body,node.orelse,node.finalbody,*[h.body for h in node.handlers]]: block(body2,patterns,matches)
                for name in targets(node): patterns.pop(name,None); matches.pop(name,None)
            else: scan(node,patterns,matches)
    block(tree.body,{},{})
print(json.dumps(result,ensure_ascii=True))
`;

export async function inspectPythonSource(source) {
  if (typeof source !== "string" || Buffer.byteLength(source, "utf8") > 65536)
    return { status: "skipped", reason: "source_limit", executes_source: false, semantic_result_verified: false };
  return new Promise(resolve => {
    const child = spawn(STATIC_PYTHON, ["-I", "-S", "-c", SOURCE_INSPECTOR],
      { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    let settled = false, bytes = 0; const chunks = [];
    const finish = report => { if (settled) return; settled = true; clearTimeout(timer); resolve(report); };
    const unavailable = reason => finish({ status: "unavailable", reason, executes_source: false, semantic_result_verified: false });
    const timer = setTimeout(() => { child.kill(); unavailable("inspector_timeout"); }, 2000);
    child.on("error", () => unavailable("inspector_start_failed"));
    child.stdout.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > 16384) { child.kill(); unavailable("inspector_output_limit"); } else chunks.push(chunk);
    });
    child.stderr.resume();
    child.stdin.on("error", () => unavailable("inspector_input_failed"));
    child.on("close", code => {
      if (settled) return;
      if (code !== 0) return unavailable("inspector_failed");
      try {
        const result = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (result.status !== "checked" || !Array.isArray(result.findings) || result.findings.length > 12 ||
            result.executes_source !== false || result.semantic_result_verified !== false) throw new Error("schema");
        finish(result);
      } catch { unavailable("inspector_invalid_report"); }
    });
    child.stdin.end(JSON.stringify({ source }));
  });
}

export async function buildTools(root, policy) {
  if (await fs.realpath(policy.workspace) !== root) throw new Error("工具工作区与受信任策略不一致");
  const profile=policy.profile??"standard";
  if(!["standard","reverse"].includes(profile))throw new Error("未知执行能力配置");
  const { z } = await import(new URL("./独立环境/cache/.bun/install/cache/zod@4.1.8@@@1/index.js", import.meta.url));
  const manifest = JSON.parse(await fs.readFile(path.join(root, "INPUT_MANIFEST.json"), "utf8"));
  const allowed = new Set(manifest.map(x => x.path));
  for (const name of ["题面.txt", "INPUT_MANIFEST.json", "CTF_STATE.md", "solve.py"]) allowed.add(name);
  if(policy.model_handover)allowed.add("HANDOVER.json");
  const out = await fs.realpath(policy.out);
  let busy = false;
  const readArgs={action:z.enum(["catalog","text","strings","hex","evidence"]),path:z.string().max(512).optional(),start:z.union([z.number().int().min(0).max(2097152),z.string().regex(/^(?:0[xX][0-9a-fA-F]+|[0-9]+)$/)]).optional().describe("text/evidence为0起始行，strings/hex为字节偏移；可用整数或0x十六进制字符串"),limit:z.number().int().min(1).max(160).optional(),query:z.string().max(100).optional()};
  const sourceArgs={source:z.string().min(1).max(30000)};
  const noteArgs={content:z.string().min(1).max(6000)};
  const validators={ctf_read:z.object(readArgs),ctf_python:z.object(sourceArgs),ctf_note:z.object(noteArgs)};
  async function target(name) {
    if (typeof name !== "string" || name.length > 512 || name.includes("\\") || name.includes(":") || path.isAbsolute(name) || name.split("/").includes("..")) throw new Error("只接受工作区内的正斜杠相对路径");
    if (!allowed.has(name) && !/^\.ctf-evidence\/tool-[a-f0-9]{64}\.txt$/.test(name)) throw new Error("文件不在原始输入/工作记忆/工具证据允许列表");
    const p = path.join(root, name), actual = await fs.realpath(p), st = await fs.lstat(p);
    if (st.isSymbolicLink() || path.relative(root, actual).startsWith("..") || !st.isFile() || st.size > 2*1024**2) throw new Error("文件越界、链接或超过2MiB");
    return p;
  }
  async function writeOwned(name, content) {
    const p = path.join(root, name);
    try { const s = await fs.lstat(p); if (s.isSymbolicLink() || !s.isFile() || await fs.realpath(p) !== p) throw new Error("写入目标不是工作区普通文件"); }
    catch(e) { if (e.code !== "ENOENT") throw e; }
    await fs.writeFile(p, content, "utf8");
  }
  async function evidence(tool, args, result, ctx) {
    const record = { utc:new Date().toISOString(), sessionID:ctx.sessionID, messageID:ctx.messageID, tool, args, result };
    const data = JSON.stringify(record), id = sha(data);
    const saved=path.join(out,`tool-${id}.json`);
    try{await fs.writeFile(saved,data,{encoding:"utf8",flag:"wx"});}
    catch(e){if(e.code!=="EEXIST"||(await fs.lstat(saved)).isSymbolicLink()||await fs.readFile(saved,"utf8")!==data)throw e;}
    await fs.appendFile(path.join(out,"工具审计.jsonl"), JSON.stringify({utc:record.utc,tool,evidence_id:id,ok:result.ok})+"\n");
    return JSON.stringify({...result,evidence_id:id});
  }
  async function invoke(name,args,ctx,fn) {
    if (ctx.abort?.aborted) throw new Error("调用已取消，未执行工具");
    await ctx.ask({permission:name,patterns:[typeof args?.path==="string"?args.path:(name==="ctf_python"?"solve.py":"CTF_STATE.md")],always:["*"],metadata:{scope:"local static CTF"}});
    let result;
    try {
      const normalized={...args};
      if(name==="ctf_read"){
        if(typeof normalized.start==="string"&&/^(?:0[xX][0-9a-fA-F]+|[0-9]+)$/.test(normalized.start))normalized.start=Number(normalized.start);
        if(Number.isInteger(normalized.limit)&&normalized.limit>160)normalized.limit=160;
      }
      const parsed=validators[name].parse(normalized);
      result=await fn(parsed);
      if(JSON.stringify(parsed)!==JSON.stringify(args))result.input_normalized=true;
    } catch(e) { result={ok:false,error:String(e.message || e).slice(0,1200)}; }
    return evidence(name,args,result,ctx);
  }
  function python(source,ctx) {
    return new Promise((resolve,reject)=>{
      if(ctx.abort?.aborted){reject(new Error("用户已取消，未启动执行桥"));return;}
      const child=spawn(policy.python,[policy.runner,profile==="reverse"?"--tool-reverse":"--tool",root,out],{windowsHide:true,stdio:["pipe","pipe","pipe"],env:{...process.env,PYTHONUTF8:"1"}});
      const chunks=[];let stdoutBytes=0,stderr="",settled=false;
      const fail=e=>{clearTimeout(timer);ctx.abort?.removeEventListener("abort",abort);if(!settled){settled=true;reject(e);}};
      const timer=setTimeout(()=>{child.kill();fail(new Error("受限执行桥超过25秒"));},25000);
      const abort=()=>{child.kill();fail(new Error("用户取消脚本"));};
      ctx.abort?.addEventListener("abort",abort,{once:true});
      child.stdout.on("data",d=>{stdoutBytes+=d.length;if(stdoutBytes>100000){child.kill();fail(new Error("执行桥输出超限"));}else chunks.push(d);});
      child.stderr.on("data",d=>{stderr=(stderr+d.toString("utf8")).slice(-2000);});
      child.on("error",fail);
      child.on("close",code=>{clearTimeout(timer);ctx.abort?.removeEventListener("abort",abort);if(settled)return;settled=true;try{resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));}catch{reject(new Error(`执行桥未返回JSON，exit=${code}：${stderr}`));}});
      child.stdin.on("error",fail);child.stdin.end(JSON.stringify({source}));
    });
  }
  return {
    ctf_read: {
      description:"只读本题。catalog含隐藏目录；text按行分页；strings带偏移；hex每页最多256字节。evidence用path=64位证据ID分页读取同会话原始脚本及结果，自动验哈希。其他路径用相对路径；不解码、不执行附件。",
      args:readArgs,
      execute:async(args,ctx)=>invoke("ctf_read",args,ctx,async(args)=>{
        const start=args.start??0,limit=args.limit??40;
        if(args.action==="catalog"){
          const rows=manifest.filter(x=>!args.query||x.path.includes(args.query));
          return {ok:true,total:rows.length,files:rows.slice(start,start+limit).map(x=>`${x.path}\t${x.bytes} bytes`),next:start+limit<rows.length?start+limit:null};
        }
        if(args.action==="evidence"){
          if(!/^[a-f0-9]{64}$/.test(args.path??""))throw new Error("证据ID须为64位小写SHA256");
          const file=path.join(out,`tool-${args.path}.json`),stat=await fs.lstat(file);
          if(stat.isSymbolicLink()||!stat.isFile()||stat.size>262144)throw new Error("证据不是有界普通文件");
          const raw=await fs.readFile(file,"utf8");if(sha(raw)!==args.path)throw new Error("证据哈希不符");
          const record=JSON.parse(raw);if(record.sessionID!==ctx.sessionID)throw new Error("证据不属于当前会话");
          const lines=JSON.stringify(record,null,2).split("\n");
          return {ok:true,view:"original_model_evidence",source_evidence_id:args.path,total_lines:lines.length,start,
            lines:lines.slice(start,start+limit),next:start+limit<lines.length?start+limit:null};
        }
        const data=await fs.readFile(await target(args.path)), header=data.subarray(0,16).toString("hex");
        const storedHash=/^\.ctf-evidence\/tool-([a-f0-9]{64})\.txt$/.exec(args.path??"");
        if(storedHash&&sha(data)!==storedHash[1])throw new Error("工具/任务原文证据哈希不符");
        if(args.action==="text"){
          if(data.includes(0))return {ok:false,error:"文件含NUL，可能是二进制；请用strings/hex或自己写脚本read_bytes",bytes:data.length,header_hex:header};
          let text=new TextDecoder("utf-8",{fatal:true}).decode(data),view="raw_text",source_page;
          if(args.path==="HANDOVER.json"&&policy.model_handover){
            // 只投影模型的原文，不改写解法；避免JSON字符串反复转义占满上下文。
            const handover=JSON.parse(text);
            if(!Array.isArray(handover.executions))throw new Error("交接记录格式无效");
            const initial=handover.initial_working_memory_read;
            const initialLines=initial&&Array.isArray(initial.result?.lines)?["=== earliest working-memory read (historical; may be stale) ===",`original_evidence_id: ${initial.original_evidence_id}`,...initial.result.lines]:[];
            text=[handover.scope,...initialLines,...handover.executions.flatMap((item,i)=>{
              const {stdout,...metadata}=item.result??{};
              return [`=== execution ${i+1} ===`,`original_evidence_id: ${item.original_evidence_id}`,"source:",item.source,"result_metadata:",JSON.stringify(metadata,null,2),"stdout:",typeof stdout==="string"?stdout:""];
            })].join("\n");view="model_handover_verbatim";
          }
          if(/^\.ctf-evidence\/tool-[a-f0-9]{64}\.txt$/.test(args.path)){
            try{
              const envelope=JSON.parse(text);
              if(typeof envelope.stdout==="string"){text=envelope.stdout;view="tool_stdout";}
              else if(envelope.ok===true&&Array.isArray(envelope.lines)&&envelope.lines.every(x=>typeof x==="string")){
                text=envelope.lines.join("\n");view="tool_text_page";
                source_page={view:envelope.view,start:envelope.start,total_lines:envelope.total_lines,next:envelope.next};
              }else{text=JSON.stringify(envelope,null,2);view="tool_json_pretty";}
            }catch{}
          }
          const lines=text.split(/\r?\n/);
          return {ok:true,view,...(source_page?{source_page}:{}),total_lines:lines.length,start,lines:lines.slice(start,start+limit),next:start+limit<lines.length?start+limit:null};
        }
        if(args.action==="hex"){
          const n=Math.min(limit*16,256),end=Math.min(start+n,data.length),rows=[];
          for(let i=start;i<end;i+=16)rows.push({offset:i,hex:data.subarray(i,Math.min(i+16,end)).toString("hex"),ascii:Array.from(data.subarray(i,Math.min(i+16,end)),b=>b>=32&&b<=126?String.fromCharCode(b):".").join("")});
          return {ok:true,bytes:data.length,returned_bytes:Math.max(0,end-start),rows,next:end<data.length?end:null};
        }
        const rows=[];let next=null;
        for(let i=start;i<data.length;){if(data[i]<32||data[i]>126){i++;continue;}const begin=i;while(i<data.length&&data[i]>=32&&data[i]<=126)i++;if(i-begin<4)continue;const text=data.subarray(begin,i).toString("ascii");if(args.query&&!text.includes(args.query))continue;rows.push({offset:begin,text:text.slice(0,500),truncated:text.length>500});if(rows.length>=limit){next=i;break;}}
        return {ok:true,bytes:data.length,strings:rows,next};
      })
    },
    ctf_python: {
      description:"提交你自己编写的Python源码并立即受限执行，保存为solve.py并留存SHA256。可用read_bytes/read_text相对路径和纯计算标准库；不能open/os/sys/subprocess/联网/主机执行附件。结果ok=false时按真实报错修复，不原样重试。没有内置解码算法。"+(profile==="reverse"?" 逆向配置可import pefile、capstone、unicorn及unicorn.x86_const；PE只能PE(data=...)。api_info(obj)或api_info('unicorn',prefix)查询真实允许API，不可import inspect。Unicorn只做内存CPU仿真；save_bytes(label,data)保存派生数据，read_artifact(id)读取。脚本12秒/384MiB，映射128MiB，单次emu_start最多5秒/500万指令。不提供算法、地址、密钥或答案。":""),
      args:sourceArgs,
      execute:async(args,ctx)=>invoke("ctf_python",args,ctx,async(args)=>{
        if(busy)throw new Error("已有脚本运行，请串行执行");busy=true;
        try{await writeOwned("solve.py",args.source);return await python(args.source,ctx);}finally{busy=false;}
      })
    },
    ctf_note: {
      description:"保存有界工作记忆。只记已验证结论及证据、未解问题、失败方法、下一步；保留关键串/偏移，禁止把猜测认证为真。得到关键结论后立即保存，避免压缩后重头搜索。",
      args:noteArgs,
      execute:async(args,ctx)=>invoke("ctf_note",args,ctx,async(args)=>{await writeOwned("CTF_STATE.md",args.content);return {ok:true,state_sha256:sha(args.content),chars:args.content.length};})
    }
  };
}
