"""模型运行保护：仅对已确认路径的单个 Strata 引擎维持 Windows 工作集上限。

已观察到运行期间硬上限意外降至十几MiB，但重设来源尚未确认。
本保护复查并恢复设定值；缩短轮询只能减少错误限额存在时间，不能保证阻止页面驱逐或提升推理速率。
不修改全局系统设置；引擎PID变化即退出，不能保护自动重启的新进程。
"""
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
import time

import psutil


def guard_poll_seconds(value=None):
    """缩短错误上限存在的窗口，不改变工作集上限；拒绝热轮询及过慢守护。"""
    text = "100" if value is None else str(value)
    if not re.fullmatch(r"[0-9]{2,4}", text) or not 50 <= int(text) <= 1000:
        raise ValueError("工作集守护轮询只允许50..1000毫秒")
    return int(text) / 1000


def guard_minimum_bytes(raw="4"):
    if not isinstance(raw,str) or not re.fullmatch(r"[0-9]{1,4}",raw):
        raise ValueError("最低工作集仅允许4..3072MiB的十进制整数，默认仍4MiB")
    mib=int(raw)
    if not 4<=mib<=3072:raise ValueError("最低工作集仍只允许4..3072MiB")
    return mib*1024**2


def restore_working_set(kernel, handle, target, read_rss=None, minimum=4*1024**2):
    """只对调用者已经核验并打开的句柄恢复原限额；不枚举或重开其他进程。"""
    if not 8 * 1024**3 <= target <= 16 * 1024**3:
        raise ValueError("工作集目标仍只允许8..16GiB")
    if type(minimum) is not int or not 4*1024**2<=minimum<=3*1024**3:
        raise ValueError("最低工作集参数超出4..3072MiB")
    lo, hi, flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
    if not kernel.GetProcessWorkingSetSizeEx(handle, ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(flags)):
        raise ctypes.WinError(ctypes.get_last_error())
    if hi.value == target and flags.value & 4 and (minimum==4*1024**2 or lo.value>=minimum):
        return None
    before = {"min": lo.value, "max": hi.value, "flags": flags.value}
    rss_before = read_rss() if read_rss else None
    if not kernel.SetProcessWorkingSetSizeEx(handle, minimum, target, 4):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"before": before, "new_max": target, "rss_before_bytes": rss_before,
            "rss_after_bytes": read_rss() if read_rss else None}


POLL_SECONDS = guard_poll_seconds(os.environ.get("STRATA_WS_GUARD_POLL_MS"))
MINIMUM = guard_minimum_bytes(os.environ.get("STRATA_TEST_WS_MIN_MIB","4"))
BASE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("STRATA_WS_GUARD_DIR", str(BASE / "state" / "guard"))).resolve()
if not OUT.is_relative_to(BASE.resolve()):
    raise ValueError("保护记录目录必须位于本次Strata工作区内")
STOP = OUT / "停止工作集保护"
PID = json.loads((OUT / "工作集限制.json").read_text(encoding="utf-8"))["pid"]
TARGET = int(os.environ.get("STRATA_TEST_WS_GIB", "8")) * 1024**3
LIFETIME = int(os.environ.get("STRATA_WS_GUARD_SECONDS", "4500"))
if not 120 <= LIFETIME <= 14400:
    raise ValueError("工作集守护寿命须120..14400秒（仅覆盖用户授权的长测试及加载余量）")
if not 8 * 1024**3 <= TARGET <= 16 * 1024**3:
    raise ValueError("本机测试工作集上限只允许8..16GiB")


def owner_alive(pid, created):
    try:
        p=psutil.Process(pid)
        return p.is_running() and p.create_time()==created
    except psutil.Error:
        return False


def cleanup_owner_lease():
    """控制台意外退出时，只停止有本轮所有权证明的唯一启动profile。"""
    record=json.loads((OUT/'所有者清理.json').read_text(encoding='utf-8'))
    expected=[BASE/'runtime/control-0.1.52/python/python.exe', BASE/'runtime/control-0.1.52/strata.py']
    argv=record['control_argv']
    if len(argv)!=2 or [Path(x).resolve() for x in argv]!=[p.resolve() for p in expected] or not re.fullmatch(r'oc-ui-\d{10,}',record['profile']):
        raise ValueError('拒绝执行未验证的所有者清理命令')
    if record['owner_pid']!=int(os.environ['STRATA_OWNER_PID']) or record['owner_created']!=float(os.environ['STRATA_OWNER_CREATED']):
        raise ValueError('所有者证明不匹配')
    r=subprocess.run(argv+['launch','--stop','--preset',record['profile']],capture_output=True,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
    return {'control_exit':r.returncode,'stdout':r.stdout.decode('utf-8','replace')[-1000:]}


def publish_heartbeat(path, snapshot, retry_seconds=2):
    """Windows读者可能短暂拒绝FILE_SHARE_DELETE；只对占用类错误有界重试。"""
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(snapshot),encoding='utf-8')
    deadline=time.monotonic()+retry_seconds
    while True:
        try:
            temp.replace(path)
            return
        except OSError as exc:
            sharing=isinstance(exc,PermissionError) or getattr(exc,'winerror',None) in {5,32,33}
            if not sharing or time.monotonic()>=deadline:
                raise  # 持续失败仍退出守护，由控制器保护性取消；不发布虚假心跳。
            time.sleep(.05)


def main():
    process = psutil.Process(PID)
    if Path(process.exe()).name.lower() != "strata.exe" or not Path(process.exe()).resolve().is_relative_to((BASE / "components").resolve()):
        raise ValueError("拒绝对非本次Strata引擎操作")
    created = process.create_time()
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(wintypes.DWORD)]
    k.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    h = k.OpenProcess(0x100 | 0x400, False, PID)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        began = time.monotonic()
        heartbeat = 0
        while time.monotonic() - began < LIFETIME and not STOP.exists():
            if not process.is_running() or process.create_time() != created:
                break
            if os.environ.get('STRATA_OWNER_PID') and not owner_alive(int(os.environ['STRATA_OWNER_PID']),float(os.environ['STRATA_OWNER_CREATED'])):
                result={'reason':'本轮控制器已退出，释放自己的模型','pid':PID}
                try:
                    result.update(cleanup_owner_lease())
                except Exception as exc:
                    result['cleanup_error']=str(exc)
                if process.is_running() and process.create_time()==created:
                    process.terminate()
                (OUT/'所有者退出清理.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                break
            restored = restore_working_set(k, h, TARGET, lambda: process.memory_info().rss,minimum=MINIMUM)
            if restored:
                record = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), "pid": PID,
                          "poll_ms": round(POLL_SECONDS * 1000), **restored}
                with (OUT / "工作集修正.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                print(json.dumps(record), flush=True)
            if time.monotonic() - heartbeat >= 5:
                lo2, hi2, flags2 = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
                if not k.GetProcessWorkingSetSizeEx(h, ctypes.byref(lo2), ctypes.byref(hi2), ctypes.byref(flags2)):
                    raise ctypes.WinError(ctypes.get_last_error())
                snapshot = {"pid": PID, "min": lo2.value, "max": hi2.value, "flags": flags2.value,
                            "utc": dt.datetime.now(dt.timezone.utc).isoformat()}
                publish_heartbeat(OUT / "工作集守护状态.json",snapshot)
                heartbeat = time.monotonic()
            time.sleep(POLL_SECONDS)
    except psutil.NoSuchProcess:
        pass
    finally:
        k.CloseHandle(h)


if __name__ == "__main__":
    main()
