"""Windows CLI进程树资源边界。只接管本模块新建的挂起进程，不附着用户已有进程。"""
import ctypes as C,json,os,subprocess,threading,time
from ctypes import wintypes as W
from pathlib import Path

SIZE=C.c_size_t
class BasicLimits(C.Structure):
    _fields_=[('PerProcessUserTimeLimit',C.c_int64),('PerJobUserTimeLimit',C.c_int64),('LimitFlags',W.DWORD),
        ('MinimumWorkingSetSize',SIZE),('MaximumWorkingSetSize',SIZE),('ActiveProcessLimit',W.DWORD),
        ('Affinity',SIZE),('PriorityClass',W.DWORD),('SchedulingClass',W.DWORD)]
class IOCounters(C.Structure):
    _fields_=[(k,C.c_uint64) for k in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
class ExtendedLimits(C.Structure):
    _fields_=[('BasicLimitInformation',BasicLimits),('IoInfo',IOCounters),('ProcessMemoryLimit',SIZE),('JobMemoryLimit',SIZE),('PeakProcessMemoryUsed',SIZE),('PeakJobMemoryUsed',SIZE)]
class Accounting(C.Structure):
    _fields_=[(k,C.c_int64) for k in ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')]+[(k,W.DWORD) for k in ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]


class OwnedProcessJob:
    """每进程private commit默认2GiB、CLI整树3GiB、最多16进程；不改变工具权限。"""
    def __init__(self,*,process_mib=2048,total_mib=3072,max_processes=16,ledger=None):
        if os.name!='nt':raise OSError('OwnedProcessJob只支持Windows；不允许静默无保护回退')
        if type(process_mib) is not int or type(total_mib) is not int or not 64<=process_mib<=total_mib<=16384 or type(max_processes) is not int or not 2<=max_processes<=64:
            raise ValueError('无效作业资源预算')
        self.api=C.WinDLL('kernel32',use_last_error=True)
        for name,argtypes,restype in [
            ('CreateJobObjectW',[C.c_void_p,W.LPCWSTR],W.HANDLE),
            ('SetInformationJobObject',[W.HANDLE,C.c_int,C.c_void_p,W.DWORD],W.BOOL),
            ('QueryInformationJobObject',[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.c_void_p],W.BOOL),
            ('AssignProcessToJobObject',[W.HANDLE,W.HANDLE],W.BOOL),('TerminateJobObject',[W.HANDLE,W.UINT],W.BOOL),
            ('CloseHandle',[W.HANDLE],W.BOOL),('OpenThread',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE),('ResumeThread',[W.HANDLE],W.DWORD)]:
            f=getattr(self.api,name);f.argtypes=argtypes;f.restype=restype
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle:raise C.WinError(C.get_last_error())
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None;self.process=None;self.terminated=False;self.cleanup_errors=[];self.handle_closed=False
        self.ledger=Path(ledger) if ledger else None
        self.limits=ExtendedLimits();b=self.limits.BasicLimitInformation
        b.LimitFlags=0x2000|0x100|0x200|0x8 # KILL_ON_JOB_CLOSE, PROCESS_MEMORY, JOB_MEMORY, ACTIVE_PROCESS
        b.ActiveProcessLimit=max_processes;self.limits.ProcessMemoryLimit=process_mib*2**20;self.limits.JobMemoryLimit=total_mib*2**20
        if not self.api.SetInformationJobObject(self.handle,9,C.byref(self.limits),C.sizeof(self.limits)):
            error=C.WinError(C.get_last_error());self.api.CloseHandle(self.handle);self.handle=None;raise error
        try:self._record('created',process_mib=process_mib,total_mib=total_mib,max_processes=max_processes,kill_on_job_close=True)
        except BaseException:self.close();raise

    def _record(self,event,**fields):
        if self.ledger:
            self.ledger.parent.mkdir(parents=True,exist_ok=True)
            with self.ledger.open('a',encoding='utf-8') as f:
                f.write(json.dumps({'epoch':time.time(),'event':event,**fields},ensure_ascii=False)+'\n')

    def spawn(self,args,**kwargs):
        """先挂起CreateProcess，再Assign作业，最后恢复唯一主线程，避免子进程逃逸窗口。"""
        import psutil
        with self.lock:
            if not self.handle or self.process:raise RuntimeError('一个作业仅允许一个新建根进程')
            if kwargs.get('shell') or kwargs.get('creationflags',0)&0x01000000:raise ValueError('不允许shell或BREAKAWAY_FROM_JOB')
            kwargs['creationflags']=kwargs.get('creationflags',0)|0x4 # CREATE_SUSPENDED
            process=subprocess.Popen(args,**kwargs);self.process=process
            try:
                if not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):raise C.WinError(C.get_last_error())
                actual=ExtendedLimits()
                if not self.api.QueryInformationJobObject(self.handle,9,C.byref(actual),C.sizeof(actual),None):raise C.WinError(C.get_last_error())
                if actual.BasicLimitInformation.LimitFlags!=self.limits.BasicLimitInformation.LimitFlags or actual.ProcessMemoryLimit!=self.limits.ProcessMemoryLimit or actual.JobMemoryLimit!=self.limits.JobMemoryLimit:
                    raise RuntimeError('Windows作业限额未按预期生效')
                root=psutil.Process(process.pid);created=root.create_time();threads=root.threads()
                if len(threads)!=1:raise RuntimeError('挂起新进程的线程数异常，不允许无保护恢复')
                thread=self.api.OpenThread(0x2,False,threads[0].id)
                if not thread:raise C.WinError(C.get_last_error())
                try:
                    if self.api.ResumeThread(thread)!=1:raise RuntimeError('主线程挂起计数异常')
                finally:self.api.CloseHandle(thread)
                self._record('root_resumed',pid=process.pid,created=created,limits_verified=True)
                self.thread=threading.Thread(target=self._watch,name='owned-cli-job',daemon=True);self.thread.start()
                return process
            except BaseException:
                # 若Assign失败，根进程仍挂起，从未执行用户/模型代码；仅终止这个新建Popen句柄。
                if process.poll() is None:process.kill()
                process.wait(timeout=10);self.close();raise

    def _watch(self):
        while not self.stop.wait(.1):
            code=self.process.poll()
            if code is not None:
                try:self._terminate('root_exited',root_exit_code=code)
                except Exception as exc:
                    self.cleanup_errors.append('root_exit: '+str(exc));self.close()
                return

    def _terminate(self,reason,**fields):
        with self.lock:
            if self.handle and not self.terminated:
                if not self.api.TerminateJobObject(self.handle,1):raise C.WinError(C.get_last_error())
                self.terminated=True
                self._record('terminated',reason=reason,**fields)

    def close(self):
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(2)
        with self.lock:
            if self.handle:
                try:self._terminate('owner_closed')
                except Exception as exc:self.cleanup_errors.append('terminate: '+str(exc))
                finally:
                    self.handle_closed=bool(self.api.CloseHandle(self.handle));self.handle=None
                    if not self.handle_closed:self.cleanup_errors.append('CloseHandle: '+str(C.WinError(C.get_last_error())))
                    try:self._record('closed')
                    except Exception as exc:self.cleanup_errors.append('audit_close: '+str(exc))
        # 审计磁盘失败不能跳过外层引擎/守护清理；返回错误供控制器报告，不静默成功。
        return {'handle_closed':self.handle_closed,'tree_termination_requested':self.terminated,'errors':list(self.cleanup_errors)}

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
