import subprocess
import sys

import pytest

from app.domains.proxies import clash_manager


class PendingProcess:
    def __init__(self, *, never_exits=False, missing=False):
        self.returncode = None
        self.never_exits = never_exits
        self.missing = missing
        self.waits = 0
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.missing:
            self.returncode = 0
            raise ProcessLookupError("process exited")

    def wait(self, timeout):
        self.waits += 1
        if not self.killed or self.never_exits:
            raise subprocess.TimeoutExpired("owned-process", timeout)
        self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True


def test_stop_confirms_exit_after_kill():
    runtime = clash_manager.ClashRuntime()
    process = PendingProcess()
    runtime.process = process

    state = runtime.stop()

    assert process.poll() == 0
    assert process.waits == 2
    assert runtime.process is None
    assert state["running"] is False


def test_stop_keeps_live_process_when_kill_does_not_finish(monkeypatch):
    runtime = clash_manager.ClashRuntime()
    process = PendingProcess(never_exits=True)
    runtime.process = process
    runtime.controller_url = "http://127.0.0.1:12345"
    messages = []
    monkeypatch.setattr(
        clash_manager, "log_event", lambda _logger, message: messages.append(message)
    )

    with pytest.raises(subprocess.TimeoutExpired):
        runtime.stop()

    assert runtime.process is process
    assert runtime.status()["running"] is True
    assert runtime.controller_url == "http://127.0.0.1:12345"
    assert messages == []


def test_stop_clears_process_that_disappears_during_termination():
    runtime = clash_manager.ClashRuntime()
    runtime.process = PendingProcess(missing=True)

    assert runtime.stop()["running"] is False
    assert runtime.process is None


def test_stop_clears_already_exited_process():
    runtime = clash_manager.ClashRuntime()
    process = PendingProcess()
    process.returncode = 0
    runtime.process = process

    assert runtime.stop()["running"] is False
    assert runtime.process is None
    assert process.waits == 0


def test_stop_reaps_owned_child_process():
    runtime = clash_manager.ClashRuntime()
    process = subprocess.Popen(
        [
            sys._base_executable,
            "-c",
            "import os,time; print(os.getpid(),flush=True); time.sleep(60)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    runtime.process = process
    try:
        assert int(process.stdout.readline()) == process.pid
        state = runtime.stop()
        assert process.poll() is not None
        assert runtime.process is None
        assert state["running"] is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
