"""本地AI精简入口：仅独立OpenCode CLI/TUI与GSQ-RCO Coder，不启动旧做题台。"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import psutil
sys.dont_write_bytecode=True

ROOT=Path(__file__).resolve().parent
OPEN_CODE=ROOT/'成熟Agent接入/OpenCode'
BASE=(ROOT/'本地模型配置与日志/Strata验证').resolve()
sys.path.insert(0,str(OPEN_CODE))
import opencode_runtime as runtime


def manual_cleanup():
    print("便携版不包含旧机器清理功能；不删除用户文件。")
    return 0


def read_status():
    p=OPEN_CODE/'当前运行.json'
    if not p.exists():return None
    if p.is_symlink() or p.stat().st_size>65536:raise ValueError('运行状态文件不安全')
    return json.loads(p.read_text(encoding='utf-8-sig'))


def owner_alive(state):
    try:
        p=psutil.Process(state['owner_pid'])
        return p.is_running() and p.create_time()==state['owner_created']
    except (psutil.Error,KeyError):return False


def agent_activity(state):
    """读取当前控制器的状态旁路；历史记录不能冒充正在运行。"""
    out=Path(state['out']).resolve()
    if not out.is_relative_to(BASE.resolve()/'会话记录'):
        raise ValueError('活动状态目录越界')
    p=out/'Agent状态.json'
    if not p.exists():return {'state':'unknown','note':'尚未收到TUI活动事件；不能据此判定停止或解出。'}
    if p.is_symlink() or not p.is_file() or p.stat().st_size>65536:
        raise ValueError('活动状态文件不是有界普通文件')
    data=json.loads(p.read_text(encoding='utf-8'))
    if Path(data.get('workspace','')).resolve()!=Path(state['workspace']).resolve():
        raise ValueError('活动状态工作目录不符')
    words={'submitted':'请求已提交','busy':'处理中（是否生成请看引擎live）',
           'running_tool':'正在执行工具','waiting_approval':'等待你的操作确认',
           'compacting':'正在压缩上下文','retry':'请求重试中','idle':'本轮已停止输出，等待输入；不代表题目已解出',
           'error':'本轮发生错误，请查看TUI','cancelled':'请求取消事件已收到；后端是否idle请看原生live，取消不等于卸载模型',
           'stopped':'控制器或CLI已退出；不是解题成功信号'}
    data['说明']=words.get(data.get('state'),'状态未知，请以TUI为准')
    try:
        updated=dt.datetime.fromisoformat(data['updated_utc'].replace('Z','+00:00'))
        data['seconds_since_last_event']=max(0,round((dt.datetime.now(dt.timezone.utc)-updated).total_seconds(),1))
    except (KeyError,ValueError,TypeError):data['seconds_since_last_event']=None
    data['event_age_note']='事件久未更新不能单独证明模型卡死；结合引擎live和TUI查看。'
    progress=data.get('progress')
    if isinstance(progress,dict):
        phases={'inspect':'读取格式/收集输入','needs_script':'需要写最小脚本，准备步骤已触发门禁',
                'needs_execution':'脚本已保存，尚需实际执行','execution_failed':'脚本执行失败，需要根据报错修复',
                'needs_outputs':'脚本已退出0，但尚未观察到本次声明产物；仍需继续分析/生成结果',
                'execution_observed':'已观察到脚本退出0，未声明文件产物；请核对实际输出，不代表解题完成',
                'needs_readback':'已观察到成功执行，尚需读取输出核验',
                'needs_output_revalidation':'执行后的输出发生变化或缺失，旧读回已撤销，需重新核验',
                'readback_observed':'已观察到输出读回；内容正确性仍未验收',
                'needs_attention':'准备步骤未推进到脚本，需检查或调整方向'}
        data['执行阶段']=phases.get(progress.get('phase'),'阶段未知，不据此认定完成')
        data['进度边界']='文件存在、退出0、读回或阶段变化均不等于CTF题目已解出。'
        pending=progress.get('pending_outputs')
        data['待生成输出']=[name[:160].replace('\r',' ').replace('\n',' ') for name in (pending[:8] if isinstance(pending,list) else []) if isinstance(name,str)]
        data['产物来源']=[]
        outputs=progress.get('outputs')
        for row in (outputs[:8] if isinstance(outputs,list) else []):
            if not isinstance(row,dict) or not isinstance(row.get('producer'),dict):continue
            producer=row['producer'];source_hash=producer.get('source_sha256')
            if type(producer.get('exit')) is not int or producer['exit']!=0 or not isinstance(source_hash,str) or len(source_hash)!=64 or any(c not in '0123456789abcdef' for c in source_hash):continue
            clean=lambda value: ''.join(c if c.isprintable() else '?' for c in str(value)[:160])
            data['产物来源'].append(f"{clean(row.get('path','未知输出'))} ← {clean(producer.get('script','未知脚本'))} / 源码SHA256={source_hash[:12]}…（执行溯源，不等于答案正确）")
        data['输出读回覆盖']=[]
        coverage=progress.get('read_coverage')
        for row in (coverage[:8] if isinstance(coverage,list) else []):
            if not isinstance(row,dict):continue
            covered,total=row.get('covered_lines'),row.get('total_lines')
            if type(covered) is not int or type(total) is not int or not 0<=covered<=total<=1048577:continue
            name=str(row.get('path','未知输出'))[:160].replace('\r',' ').replace('\n',' ')
            text=f"{name}: {covered}/{total}个完整行已送达（不等于内容正确）"
            next_line=row.get('next_missing_line')
            if row.get('complete') is not True and type(next_line) is int and 1<=next_line<=total:
                text+=f"；尚未完整读回，下页offset={next_line}，建议limit=20"
            data['输出读回覆盖'].append(text)

    return data


def stop_owned():
    state=read_status()
    if not state or not owner_alive(state):
        print('没有本工具箱仍在运行的控制器；不会关闭其他应用或模型。')
        return 0
    if state.get('readonly_development_probe') is True:
        print('当前为开发验证，7号仅显示实时进度；请在当前Codex任务提出停止，由本轮控制器清理。不停止未知控制器或其他应用。')
        return 2
    out=Path(state['out']).resolve()
    if not out.is_relative_to(BASE.resolve()/'会话记录') or out.is_symlink() or not out.is_dir():
        raise ValueError('本轮会话路径不符，未发送停止请求')
    process=psutil.Process(state['owner_pid'])
    permitted={ROOT/'bootstrap.py',ROOT/'toolbox.py',OPEN_CODE/'opencode_runtime.py'}
    args=process.cmdline()
    if not any(Path(a.strip('"')).resolve() in permitted for a in args[1:] if a.endswith('.py')):
        raise ValueError('控制器身份不符，未停止任何进程')
    (out/'停止测试').touch()
    print('已请求停止本工具箱当前会话，控制器将清理自己的CLI、模型和守护；其他应用不动。')
    return 0


def latest_completed_request(metrics):
    """不同采集层的数组顺序不同；只按原生完成请求时间选择，不凭下标猜最新。"""
    rows=metrics.get('requests') or []
    if not rows:return None
    if not isinstance(rows,list):raise ValueError('原生请求列表格式不符')
    for row in rows:
        timestamp=row.get('time') if isinstance(row,dict) else None
        if type(timestamp) not in (int,float) or not math.isfinite(timestamp) or timestamp<0:
            raise ValueError('原生请求缺少有效时间，不猜测数组顺序')
    newest=max(row['time'] for row in rows)
    matches=[row for row in rows if row['time']==newest]
    if len(matches)!=1:raise ValueError('最新原生请求时间不唯一，不猜测速率')
    return matches[0]


def check(as_json=False,menu=False):
    cfg=runtime.config()
    context=runtime.context_profile(cfg)
    model=Path(os.environ.get('CTFCODE_MODEL_DIR', str(BASE/'models/GSQ-RCO-Coder'))).resolve()
    required=[runtime.EXE,OPEN_CODE/'opencode_engine.py',OPEN_CODE/'opencode_safety.py',
              OPEN_CODE/'ctf-context.mjs',OPEN_CODE/'ctf-tools.mjs',
              BASE/'working_set_guard.py',BASE/'runtime/control-0.1.52/python/python.exe',
              BASE/'runtime/control-0.1.52/strata.py',BASE/'strata-feed.json',BASE/'strata-feed.json.sig',
              model/'IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf',
              model/'IQ1_M/Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf',
              model/'packs/coder-iq1_m/experts.bin',model/'packs/coder-iq1_m/dense.bin']
    missing=[str(p) for p in required if not p.is_file()]
    state=read_status()
    alive=bool(state and owner_alive(state))
    report={'runtime_files_ready':not missing,'missing':missing,'model':'GSQ-RCO Coder IQ1_M',
            'model_directory':str(model),'endpoint':'http://127.0.0.1:18791/v1',
            'physical_context':context['physical_context'],'soft_context':context['soft_context'],
            'configured_context':context,'running_context':None,'context_fields_scope':'配置值，不是当前实测运行窗口',
            'kv':'q4_0','prefill':128,'vram_reserve_mib':1792,
            'controller_running':alive,'session':state if alive else None}
    cfg64=runtime.config_for_context(cfg,'64')
    release64=runtime.engine_release(runtime.context_profile(cfg64))
    optional=[OPEN_CODE/name for name in ('ctf-context-64k.mjs','ctf-compaction.mjs','compaction-cache.mjs','64K摘要输出约束_分词器.json')]
    optional += [Path(release64['native_executable']),Path(release64['component_directory'])/'engine-component.json']
    report['context_64k_files']={'present':all(p.is_file() for p in optional),'missing':[str(p) for p in optional if not p.is_file()],
        'engine_version':release64['version'],'scope':'仅文件存在性；启动时另验证签名/完整字节/真实运行版本，不等于64K长任务已通过'}
    report['available_ram_gib']=round(psutil.virtual_memory().available/1024**3,2)
    cli_env={key.upper():value for key,value in runtime.child_env(cfg).items()}
    native_modules=Path(cli_env.get('SYSTEMROOT','C:/Windows'))/'System32/WindowsPowerShell/v1.0/Modules'
    first_modules=cli_env.get('PSMODULEPATH','').split(';')[0]
    report['powershell_environment']={'shell':cli_env.get('SHELL'),'native_module_directory':str(native_modules),
        'native_modules_first':bool(first_modules and os.path.normcase(os.path.normpath(first_modules))==os.path.normcase(os.path.normpath(str(native_modules)))),
        'scope':'仅本工具箱CLI/TUI子进程的路径检查；不是本次命令执行或解题验证'}
    try:report['windows_commit']=runtime.safety.windows_commit_info()
    except (OSError,ValueError) as ex:report['windows_commit_error']=str(ex)
    report['resource_note']='16GiB仅是加载门禁；20GiB不是性能分界线，高于20GiB也曾明显变慢。共享GPU分配不能单独证明显存溢出。'
    if alive and state.get('readonly_development_probe') is True:report['development_probe_note']=state.get('control_note')
    if alive:
        try:report['agent_activity']=agent_activity(state)
        except Exception as ex:report['agent_activity_error']=str(ex)
        try:
            metrics=runtime.engine.SESSION.get('http://127.0.0.1:18791/metrics',timeout=2).json()
            report['running_context']=runtime.observed_context(state,metrics)
            # 先核验引擎身份；真实/派生采样的顺序不同，禁止用下标选最新。
            report['live']=metrics.get('live');report['latest_request']=latest_completed_request(metrics)
            if report['running_context']['physical_context'] != context['physical_context']:
                report['context_difference_note']='当前运行窗口与下次启动配置不同；开发测试或配置改变不等于已更改当前引擎。'
        except Exception as ex:report['live_error']=str(ex)
        try:
            proc=psutil.Process(state['native_pid'])
            if proc.create_time()!=state['native_created'] or not Path(proc.exe()).resolve().is_relative_to(BASE.resolve()/'components'):
                raise ValueError('引擎身份不符，不读取其他进程的显存')
            counter=runtime.safety.WindowsGpuMemory()
            try:report['process_gpu_memory']=counter.sample(proc.pid)
            finally:counter.close()
            if proc.create_time()!=state['native_created'] or not proc.is_running():
                report.pop('process_gpu_memory',None)
                raise ValueError('采样期间引擎已变化')
        except Exception as ex:report['process_gpu_memory_error']=str(ex)
    else:report['live_note']='当前未运行本工具箱模型；文件检查不等于已加载或解题验证。'
    try:
        report['ollama_loaded_models']=runtime.engine.SESSION.get('http://127.0.0.1:11434/api/ps',timeout=2).json().get('models',[])
    except Exception:report['ollama_loaded_models']=None
    try:
        p=subprocess.run(['nvidia-smi','--query-gpu=name,memory.used,memory.total','--format=csv,noheader'],
                         capture_output=True,text=True,timeout=5,creationflags=runtime.engine.FLAGS)
        report['whole_gpu']=p.stdout.strip()
    except Exception as ex:report['gpu_error']=str(ex)
    if as_json:
        print(json.dumps(report,ensure_ascii=False,indent=2))
    else:
        print('=== 本地AI自检（只读） ===')
        print('运行文件：'+('齐全' if not missing else '缺失，不能启动'))
        print('PowerShell内置模块：'+('已优先使用本机版本，避免混入PowerShell7模块' if report['powershell_environment']['native_modules_first'] else '原生模块路径未置首，请核对所用shell；自检不修改系统环境'))
        for item in missing:print('  缺失：'+item)
        print(f"模型：GSQ-RCO Coder IQ1_M | 下次启动配置：物理{context['physical_context']//1024}K / 软{context['soft_context']//1024}K | 127.0.0.1:18791")
        print(f"配置自动摘要阈值约{context['automatic_compaction_at_approx']//1024}K；{context['hc_projection']}。不是实际解题质量保证。")
        print('3号启动可选32K或64K（仅本次）；64K仍是HC Q8候选，冷输入很慢，未认证长期稳定。')
        print(f"单次模型请求总超时{context['request_timeout_ms']//60000}分钟；冷读期间看下方输入进度，可随时取消，并非要固定等待这么久。")
        observed=report.get('running_context')
        print('当前实测运行窗口：'+(f"物理{observed['physical_context']//1024}K（身份/argv/服务已核对）" if observed else '尚未确认，不使用配置值代替'))
        if report.get('context_difference_note'):print(report['context_difference_note'])
        print(f"可用内存：{report['available_ram_gib']:.1f}GiB（16GiB仅是加载门禁，不保证流畅）")
        commit=report.get('windows_commit')
        if commit:
            print(f"Windows提交额度：已用 {commit['used_bytes']/1024**3:.1f} / 上限 {commit['limit_bytes']/1024**3:.1f}GiB；剩余 {commit['available_bytes']/1024**3:.1f}GiB")
            print('提交额度与空闲物理内存不同；剩余额度低也会挡住缓存/输入加速。自检不会更改分页文件。')
        elif report.get('windows_commit_error'):print('Windows提交额度：读取失败，不按0处理；'+report['windows_commit_error'])
        if not alive and report['available_ram_gib'] < 20:
            print('提示：多释放内存有利于文件缓存，但20GiB不是速度保证，高于它也可能卡顿。')
        print('控制器：'+('运行中' if alive else '未运行；旧记录不会算作正在工作'))
        if report.get('development_probe_note'):print(report['development_probe_note'])
        if alive:
            print('题目目录：'+str(state.get('workspace','未知')))
            activity=report.get('agent_activity',{})
            print('Agent：'+activity.get('说明',activity.get('note','尚无活动信息')))
            if activity.get('artifact_review_available'):print('产物链检查点已就绪：输出已读回，内容仍待核验；即使模型还在输出，也可以先查看产物。')
            if 'tool_calls' in activity:
                print(f"本轮工具 {activity['tool_calls']} 次 / 失败 {activity.get('tool_failures',0)} 次；最近工具：{activity.get('last_tool') or '无'}")
            if activity.get('执行阶段'):
                progress=activity['progress']
                print('执行阶段：'+activity['执行阶段'])
                print(f"本阶段只读 {progress.get('readonly_calls',0)} 次 / 防空转门禁 {progress.get('gate_hits',0)} 次；脚本 {len(progress.get('scripts') or [])} 个")
                if isinstance(progress.get('preparation_calls'),int):print(f"未产出脚本前的成功准备步骤：{progress['preparation_calls']} 次（变量和python -c也计数）")
                if progress.get('abort_requested'):print('已向本轮OpenCode会话发送停止空转请求；不自动重跑。')
                for line in activity.get('产物来源',[]):print('产物来源：'+line)
                for line in activity.get('输出读回覆盖',[]):print('输出读回覆盖：'+line)
                if activity.get('待生成输出'):print('尚未观察到本次输出：'+', '.join(activity['待生成输出']))
                print(activity['进度边界'])
                if progress.get('restore_notice'):print(progress['restore_notice'])
            live=report.get('live') or {}
            print('引擎：'+str(live.get('phase') or live.get('state') or '暂时无法读取；不据此判定卡死'))
            if live.get('prompt_total'):
                print(f"输入处理：{live.get('prompt_read',0)}/{live['prompt_total']} token")
            if live.get('generated') is not None:print(f"当前生成：{live['generated']} token")
            request=report.get('latest_request') or {}
            if request.get('decode_tok_s') is not None:print(f"最近已记录请求的生成速率：{request['decode_tok_s']} token/s（不是端到端速度）")
            memory=report.get('process_gpu_memory')
            if memory:
                print(f"本轮引擎GPU内存（WDDM分配）：专用 {memory['dedicated_bytes']/1024**2:.0f}MiB / 共享 {memory['shared_bytes']/1024**2:.0f}MiB")
                print('共享分配也可能是正常的主机内存映射；不等于全部在换页，不代表独占显卡。')
            for key in ('live_error','agent_activity_error','process_gpu_memory_error'):
                if report.get(key):print('状态读取异常：'+report[key])
        loaded=report.get('ollama_loaded_models')
        print('Ollama：'+('无法确认' if loaded is None else '未加载模型' if not loaded else ', '.join(m.get('name') or m.get('model') or '未知模型' for m in loaded)))
        print('整卡显存：'+str(report.get('whole_gpu') or report.get('gpu_error') or '未知'))
        print('注意：空闲/停止/正常退出均不代表题目已解出；以结果验证为准。')
    if menu and not as_json and sys.stdin.isatty() and not os.environ.get('TOOLBOX_NO_PAUSE'):
        hint='S：停止本工具箱模型；'
        choice=input('\n'+hint+'直接回车不更改：').strip().lower()
        if choice=='s':return stop_owned()
    return 1 if missing else 0


def main(argv=None):
    parser=argparse.ArgumentParser(description='独立OpenCode + GSQ-RCO Coder')
    sub=parser.add_subparsers(dest='command',required=True)
    ui=sub.add_parser('ui');ui.add_argument('--target');ui.add_argument('--dry-run',action='store_true')
    ui.add_argument('--context',choices=('32','64'),help='仅本次启动的上下文档位；不改持久配置')
    ui.add_argument('--native',action='store_true',help='显式试用原生V2 TUI；尚未替换默认入口')
    ui.add_argument('--cli',help='仅原生预览：已核验的独立2.3.0 CLI路径')
    status=sub.add_parser('check');status.add_argument('--json',action='store_true');status.add_argument('--menu',action='store_true');status.add_argument('--stop',action='store_true')
    sub.add_parser('stop')
    args=parser.parse_args(argv)
    if args.command=='stop' or args.command=='check' and args.stop:return stop_owned()
    if args.command=='check':return check(args.json,args.menu)
    if args.native:
        import importlib.util
        spec=importlib.util.spec_from_file_location('ctf_native_launch',OPEN_CODE/'v2/launch.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.main((["--target",args.target] if args.target else [])+
            (["--context",args.context] if args.context else [])+(["--cli",args.cli] if args.cli else [])+
            (["--dry-run"] if args.dry_run else []))
    if args.cli:raise ValueError('--cli仅用于显式--native预览，不影响原入口')
    target=args.target or input('请输入/拖入独立题目副本目录（回车取消，不加载模型）：').strip().strip('"')
    if not target:return 0
    if args.context is None and not args.dry_run and sys.stdin.isatty():
        default=runtime.context_profile(runtime.config())['physical_context']//1024
        choice=input(f'本次上下文32/64，回车沿用默认{default}K；64K仍是HC Q8候选、长冷输入很慢：').strip()
        if choice and choice not in ('32','64'):raise ValueError('上下文只接受32或64；未加载模型')
        args.context=choice or None
    if args.dry_run:
        cfg=runtime.config_for_context(runtime.config(),args.context)
        workspace=runtime.preflight(target,cfg)
        p=subprocess.run([str(runtime.EXE),'debug','config'],cwd=workspace,env=runtime.child_env(cfg),capture_output=True,timeout=30,creationflags=runtime.engine.FLAGS)
        if p.returncode:raise RuntimeError(p.stderr.decode('utf-8','replace'))
        actual=json.loads(p.stdout)
        if actual.get('enabled_providers')!=['strata-local'] or actual.get('model')!=runtime.MODEL:
            raise ValueError('独立CLI实际配置不符')
        context=runtime.context_profile(actual)
        if context != runtime.context_profile(cfg):raise ValueError('独立CLI实际上下文档与启动配置不符')
        print(json.dumps({'dry_run':True,'model_not_loaded':True,'workspace':str(workspace),
                          'cli_version':'1.2.26','actual_model':actual['model'],
                          'configured_context':context,'argv':runtime.context_argv(cfg,'oc-ui-dryrun'),
                          'hc_model_child_environment':context['hc_environment'],
                          'selected_engine':runtime.engine_release(context),
                          'selected_plugins':cfg['plugin']},ensure_ascii=False,indent=2))
        return 0
    return runtime.launch(target,context_k=args.context)


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(130)
    except Exception as exc:
        print(f'操作失败：{type(exc).__name__}: {exc}',file=sys.stderr)
        raise SystemExit(1)
