from __future__ import annotations

import ctypes
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("holdexar_update_processes", ROOT / "desktop" / "main.py")
desktop = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = desktop
spec.loader.exec_module(desktop)


@pytest.fixture(autouse=True)
def quiet_update_log(monkeypatch):
    monkeypatch.setattr(desktop, "_log_update", lambda _message: None)


class WinCall:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles")
@pytest.mark.parametrize("wait_result, expected", [(0, True), (258, False), (0xFFFFFFFF, False)])
def test_wait_pid_exit_reports_wait_result_and_closes_handle(monkeypatch, wait_result, expected):
    closed = []
    kernel = SimpleNamespace(
        OpenProcess=WinCall(lambda *_args: 91),
        WaitForSingleObject=WinCall(lambda *_args: wait_result),
        CloseHandle=WinCall(lambda handle: closed.append(handle) or 1),
        GetLastError=WinCall(lambda: 5),
    )
    monkeypatch.setattr(ctypes.windll, "kernel32", kernel)

    assert desktop._wait_pid_exit(12345, timeout=0) is expected
    assert closed == [91]


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles")
@pytest.mark.parametrize("error, expected", [(87, True), (5, False)])
def test_wait_pid_exit_distinguishes_missing_process_from_denied(monkeypatch, error, expected):
    kernel = SimpleNamespace(
        OpenProcess=WinCall(lambda *_args: None),
        WaitForSingleObject=WinCall(lambda *_args: 0),
        CloseHandle=WinCall(lambda *_args: 1),
        GetLastError=WinCall(lambda: error),
    )
    monkeypatch.setattr(ctypes.windll, "kernel32", kernel)
    assert desktop._wait_pid_exit(12345, timeout=0) is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows process snapshots")
def test_snapshot_failure_is_not_reported_as_idle(tmp_path, monkeypatch):
    kernel = SimpleNamespace(**{
        name: WinCall(lambda *_args: 0)
        for name in (
            "CreateToolhelp32Snapshot", "Process32FirstW", "Process32NextW",
            "OpenProcess", "QueryFullProcessImageNameW", "CloseHandle",
        )
    })
    kernel.CreateToolhelp32Snapshot = WinCall(lambda *_args: ctypes.c_void_p(-1).value)
    kernel.GetLastError = WinCall(lambda: 5)
    monkeypatch.setattr(ctypes.windll, "kernel32", kernel)

    with pytest.raises(OSError):
        desktop._pids_running_from(tmp_path)


def test_replace_retry_reuses_confirmed_process_cleanup(tmp_path, monkeypatch):
    src, dst = tmp_path / "source", tmp_path / "destination"
    src.write_text("payload", encoding="utf-8")
    replace = desktop.os.replace
    cleanup = []

    def locked_once(source, destination):
        if not cleanup:
            raise PermissionError("owned process still holds source")
        replace(source, destination)

    def stop_owned(target, **kwargs):
        cleanup.append((target, kwargs))
        return []

    monkeypatch.setattr(desktop.os, "replace", locked_once)
    monkeypatch.setattr(desktop, "_wait_target_idle", stop_owned)
    desktop._replace_with_retry(src, dst, attempts=2, delay=0, target_root=tmp_path)

    assert cleanup[0][0] == tmp_path
    assert dst.read_text(encoding="utf-8") == "payload"


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees")
def test_owned_process_tree_includes_external_image_and_is_stopped(tmp_path):
    cmd = tmp_path / "owned-cmd.exe"
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", cmd)
    python = getattr(sys, "_base_executable", sys.executable)
    internal = tmp_path / "_internal"
    internal.mkdir()
    script = (
        "import ctypes,os,sys,time; k=ctypes.windll.kernel32; "
        "k.CreateFileW.restype=ctypes.c_void_p; "
        "k.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,"
        "ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]; "
        "h=k.CreateFileW(sys.argv[1],0x80000000,3,None,3,0x02000000,None); "
        "assert h not in (None,ctypes.c_void_p(-1).value); "
        "print(os.getpid(),flush=True); time.sleep(60)"
    )
    command = (
        f'"{cmd}" /d /s /c ""{python}" -c "{script}" "{internal}""'
    )
    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    child = None
    try:
        child = int(proc.stdout.readline().strip())
        found = desktop._pids_running_from(tmp_path)
        assert proc.pid in found
        assert child in found
        with pytest.raises(PermissionError):
            os.replace(internal, tmp_path / "moved")
        assert desktop._wait_target_idle(tmp_path, timeout_s=8, grace_s=0) == []
        assert desktop._wait_pid_exit(child, timeout=1) is True
        proc.wait(timeout=2)
        os.replace(internal, tmp_path / "moved")
    finally:
        pids = [pid for pid in (proc.pid, child) if pid]
        subprocess.run(
            ["taskkill", "/T", "/F", *[part for pid in pids for part in ("/PID", str(pid))]],
            capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        proc.wait(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees")
def test_explicit_root_is_stopped_even_when_image_is_outside_target(tmp_path):
    python = getattr(sys, "_base_executable", sys.executable)
    proc = subprocess.Popen(
        [python, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert desktop._wait_target_idle(tmp_path, timeout_s=5, root_pid=proc.pid, grace_s=0) == []
        proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows process paths")
def test_helper_images_and_unrelated_processes_are_excluded(tmp_path, monkeypatch):
    helper = tmp_path / "data" / "staging" / "payload"
    helper.mkdir(parents=True)
    exe = tmp_path / "Holdexar.exe"
    exe.touch()
    me = os.getpid()
    rows = {
        me: (10001, "Holdexar.exe", str(helper / "Holdexar.exe")),
        10001: (1, "Holdexar.exe", str(exe)),
        10002: (me, "Holdexar.exe", str(helper / "Holdexar.exe")),
        10003: (10001, "external.exe", r"C:\tools\external.exe"),
        10004: (1, "external.exe", r"C:\tools\external.exe"),
    }
    monkeypatch.setattr(desktop, "_process_snapshot", lambda: rows)
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop.sys, "executable", str(helper / "Holdexar.exe"))

    assert desktop._pids_running_from(tmp_path) == [10001, 10003]


@pytest.mark.skipif(os.name != "nt", reason="Windows process paths")
def test_denied_install_image_is_not_assumed_absent(tmp_path, monkeypatch):
    (tmp_path / "Holdexar.exe").touch()
    monkeypatch.setattr(desktop, "_process_snapshot", lambda: {12345: (1, "Holdexar.exe", None)})
    monkeypatch.setattr(desktop, "_wait_pid_exit", lambda *_a, **_k: False)

    with pytest.raises(OSError):
        desktop._pids_running_from(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows process trees")
def test_ancestor_is_stopped_without_tree_killing_the_helper(tmp_path, monkeypatch):
    me = os.getpid()
    rows = {
        me: (10001, "helper.exe", r"C:\staging\helper.exe"),
        10001: (1, "Holdexar.exe", str(tmp_path / "Holdexar.exe")),
        10002: (10001, "child.exe", r"C:\tools\child.exe"),
    }
    commands, direct = [], []
    monkeypatch.setattr(desktop, "_process_snapshot", lambda: rows)
    monkeypatch.setattr(desktop, "_terminate_update_pid", direct.append)
    monkeypatch.setattr(desktop, "_wait_pid_exit", lambda *_a, **_k: True)

    def taskkill(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", taskkill)

    assert desktop._stop_update_processes([10001, 10002, me], tmp_path, 1) == []
    assert direct == [10001]
    assert commands[0][0] == ["taskkill", "/T", "/F", "/PID", "10002"]
    assert commands[0][1]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_successful_taskkill_still_requires_observed_exit(tmp_path, monkeypatch):
    now = [0.0]
    commands = []
    monkeypatch.setattr(desktop, "_pids_running_from", lambda _target: [12345])
    monkeypatch.setattr(desktop, "_process_snapshot", lambda: {12345: (1, "owned.exe", str(tmp_path / "owned.exe"))})
    monkeypatch.setattr(desktop, "_wait_pid_exit", lambda *_a, **_k: False)
    monkeypatch.setattr(desktop.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def taskkill(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", taskkill)

    assert desktop._wait_target_idle(tmp_path, timeout_s=1, grace_s=0) == [12345]
    assert commands


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handles")
def test_unrelated_external_lock_is_preserved_and_rename_fails_accurately(tmp_path):
    internal = tmp_path / "_internal"
    internal.mkdir()
    child_code = """import ctypes,sys
k=ctypes.windll.kernel32
k.CreateFileW.restype=ctypes.c_void_p
k.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
h=k.CreateFileW(sys.argv[1],0x80000000,3,None,3,0x02000000,None)
print('ready' if h not in (None,ctypes.c_void_p(-1).value) else 'failed',flush=True)
sys.stdin.readline()
k.CloseHandle.argtypes=[ctypes.c_void_p]
k.CloseHandle(h)
"""
    python = getattr(sys, "_base_executable", sys.executable)
    proc = subprocess.Popen(
        [python, "-c", child_code, str(internal)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert desktop._wait_target_idle(tmp_path, timeout_s=1, grace_s=0) == []
        with pytest.raises(PermissionError) as failure:
            desktop._replace_with_retry(
                internal, tmp_path / "moved", attempts=2, delay=0, target_root=tmp_path,
            )
        assert failure.value.winerror == 32
        assert proc.poll() is None
        proc.stdin.write("\n")
        proc.stdin.flush()
        proc.wait(timeout=2)
        os.replace(internal, tmp_path / "moved")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
