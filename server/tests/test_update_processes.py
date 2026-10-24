from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("holdexar_update_processes", ROOT / "desktop" / "main.py")
desktop = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = desktop
spec.loader.exec_module(desktop)


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
