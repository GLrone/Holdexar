from __future__ import annotations

import ctypes
import os
import subprocess
import time
from contextlib import contextmanager

from app.core.app_info import APP_NAME


@contextmanager
def update_mutex():
    if os.name != "nt":
        yield
        return
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, f"{APP_NAME}-update-swap")
    if not handle:
        raise OSError("无法取得更新独占权限")
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
        if not acquired:
            raise OSError("已有更新正在进行或无法核对更新权限")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime",
    )] + [(name, ctypes.c_uint32) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses",
    )]


class ProbeTree:
    """验收进程树在宿主退出时一同停止，接受后解除此限制。"""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.windll.kernel32
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.TerminateJobObject.restype = ctypes.c_int
        kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("无法创建验收进程保护")
        try:
            self._set_kill_on_close(True)
        except BaseException:
            self.close()
            raise

    def _set_kill_on_close(self, enabled: bool):
        if self.handle:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000 if enabled else 0
            if not ctypes.windll.kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError("无法设置验收进程保护")

    def assign(self, process):
        if self.handle and not ctypes.windll.kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("无法保护验收进程树")

    def stop(self, process):
        if self.handle:
            if not ctypes.windll.kernel32.TerminateJobObject(self.handle, 1):
                raise OSError("无法停止验收进程树")
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if self.handle:
            deadline = time.monotonic() + 5
            while True:
                accounting = _Accounting()
                if not ctypes.windll.kernel32.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                    raise OSError("无法核对验收进程树是否退出")
                if accounting.ActiveProcesses == 0:
                    break
                if time.monotonic() >= deadline:
                    raise OSError("验收进程树尚未完整退出")
                time.sleep(0.05)

    def detach(self):
        self._set_kill_on_close(False)
        self.close()

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
