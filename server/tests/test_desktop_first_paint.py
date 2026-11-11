import importlib.util
import socket
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_main():
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main_paint", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()


@pytest.mark.parametrize("go_dev, port, url", [
    (False, "28765", "http://localhost:8080"),
    (True, "32123", "http://127.0.0.1:49152"),
])
def test_launcher_routes_each_dev_window_to_its_frontend(monkeypatch, tmp_path, go_dev, port, url):
    spec = importlib.util.spec_from_file_location("holdexar_run_dev", ROOT / "run.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    for name in ("ensure_deps", "ensure_kernel", "ensure_npm", "ensure_web_deps"):
        monkeypatch.setattr(launcher, name, lambda *args: None)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "ensure_venv", lambda: Path(sys.executable))
    index = tmp_path / "index.html"
    index.touch()
    monkeypatch.setattr(launcher, "DIST_INDEX", index)
    monkeypatch.setattr(sys, "argv", ["run.py", "--dev", "--port", port])
    monkeypatch.setenv("HOLDEXAR_SERVER_EXE", "existing.exe")
    monkeypatch.delenv("HOLDEXAR_DEV_WATCH", raising=False)
    if go_dev:
        monkeypatch.setenv("HOLDEXAR_DEV_WATCH", "go-source")
    calls = []

    def frontend(backend_port):
        calls.append(("frontend", backend_port))
        return nullcontext(url)

    monkeypatch.setattr(launcher, "go_dev_frontend", frontend, raising=False)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, env, **kwargs: calls.append(
        ("desktop", env["HOLDEXAR_PORT"], env["HOLDEXAR_DEV_URL"])
    ))
    launcher.main()
    assert calls == ([("frontend", port)] if go_dev else []) + [("desktop", port, url)]


@pytest.mark.parametrize("build_result", [0, 7])
def test_go_launcher_builds_a_private_executable_before_opening(monkeypatch, tmp_path, build_result):
    spec = importlib.util.spec_from_file_location("holdexar_run_build", ROOT / "run.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "ensure_venv", lambda: Path(sys.executable))
    for name in ("ensure_deps", "ensure_kernel"):
        monkeypatch.setattr(launcher, name, lambda *args: None)
    index = tmp_path / "index.html"
    index.touch()
    monkeypatch.setattr(launcher, "DIST_INDEX", index)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.delenv("HOLDEXAR_SERVER_EXE", raising=False)
    source = str(tmp_path / "server-go")
    monkeypatch.setenv("HOLDEXAR_DEV_WATCH", source)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: name)
    built, opened = [], []

    def build(cmd, *, cwd):
        assert cmd[:3] == ["go", "build", "-o"]
        assert cwd == source
        built.append(Path(cmd[3]))
        built[-1].touch()
        return build_result

    def launch(cmd, *, cwd, env, check):
        exe = Path(env["HOLDEXAR_SERVER_EXE"])
        assert exe.is_file()
        opened.append(exe)

    monkeypatch.setattr(launcher, "_run", build)
    monkeypatch.setattr(launcher.subprocess, "run", launch)
    if build_result:
        with pytest.raises(SystemExit, match="Go 后端编译失败"):
            launcher.main()
        assert opened == []
    else:
        launcher.main()
        assert opened == built
    assert built and not built[0].parent.exists()


def test_health_timeout_bounds_a_listening_unresponsive_server(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(desktop, "SERVER_PORT", listener.getsockname()[1])
        started = time.monotonic()
        assert desktop._wait_health(.05) is False
        assert time.monotonic() - started < .25


def test_wait_and_navigate_does_not_wait_for_the_placeholder(monkeypatch):
    calls = []
    fake_window = SimpleNamespace(load_url=lambda url: calls.append(url), events=SimpleNamespace(loaded=None))

    monkeypatch.setattr(desktop, "_wait_health", lambda timeout: True)
    monkeypatch.setattr(desktop, "_install_navigation_guard", lambda win, url: None)
    monkeypatch.setattr(desktop, "_session_entry_url", lambda url: f"{url}/bootstrap")

    desktop._wait_and_navigate(fake_window, "http://127.0.0.1:28765")
    assert calls == ["http://127.0.0.1:28765/bootstrap"]


def test_window_creation_does_not_probe_the_backend_or_use_a_reveal_timer(monkeypatch):
    calls = []

    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def is_set(self):
            return False

    window = SimpleNamespace(
        show=lambda: calls.append("show"),
        events=SimpleNamespace(**{key: Event() for key in ("loaded", "closing", "closed")}),
    )

    def create(*args, **kwargs):
        calls.append("create")
        assert kwargs["html"] == desktop._SPLASH_HTML
        return window

    def start(*args, **kwargs):
        calls.append("start")
        for _ in range(2):
            for handler in window.events.loaded.handlers:
                handler()

    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected synchronous probe or reveal timer")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create, start=start))
    monkeypatch.setattr(desktop, "_webview2_available", lambda: True)
    monkeypatch.setattr(desktop, "_wait_health", unexpected)
    monkeypatch.setattr(desktop, "_server_port_listening", unexpected)
    monkeypatch.setattr(desktop.threading, "Timer", unexpected)
    monkeypatch.setattr(desktop, "_make_closing_guard", lambda _: lambda: True)
    monkeypatch.setattr(desktop, "_app_icon", lambda: None)
    monkeypatch.setattr(desktop, "_browser_fallback", lambda *args: calls.append("fallback"))
    monkeypatch.setattr(desktop.os, "_exit", lambda code: None)
    desktop._open_window("http://127.0.0.1:28765")
    assert calls == ["create", "start", "show"]


def test_controller_background_is_configured_before_creation(monkeypatch):
    monkeypatch.delenv("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", raising=False)
    desktop._apply_webview2_static_args()
    assert desktop.os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] == "FF1B2838"


@pytest.mark.parametrize("current, suffix", [
    ("http://127.0.0.1:28767/fetch?section=price#interval", "&next=%2Ffetch%3Fsection%3Dprice%23interval"),
    ("https://example.com/elsewhere", ""),
])
def test_reload_uses_only_new_ticket_and_retries_until_health(monkeypatch, current, suffix):
    calls = []
    checks = iter([None, None, None, None, None, 0])
    tickets = iter(["initial", "", "replacement", "replacement", "replacement"])
    health = iter([False, True])
    monkeypatch.setattr(desktop, "_external_server", {"proc": SimpleNamespace(poll=lambda: next(checks))})
    monkeypatch.setattr(desktop, "_wait_bootstrap_nonce", lambda timeout: next(tickets))
    monkeypatch.setattr(desktop, "_wait_health", lambda timeout: next(health))
    monkeypatch.setattr(desktop, "_session_entry_url", lambda url: url + "/api/v1/session/bootstrap?nonce=new")
    monkeypatch.setattr(desktop.time, "sleep", lambda duration: None)
    desktop._watch_server_reload(SimpleNamespace(load_url=calls.append, get_current_url=lambda: current), "http://127.0.0.1:28767", "initial")
    assert calls == ["http://127.0.0.1:28767/api/v1/session/bootstrap?nonce=new" + suffix]


def test_watch_navigation_starts_reload_monitor_with_consumed_ticket(monkeypatch):
    started = []
    monkeypatch.setenv("HOLDEXAR_DEV_WATCH", "source-root")
    monkeypatch.setattr(desktop, "_external_server", {"proc": object()})
    monkeypatch.setattr(desktop, "_wait_health", lambda timeout: True)
    monkeypatch.setattr(desktop, "_install_navigation_guard", lambda *args: None)
    monkeypatch.setattr(desktop, "_session_entry_url", lambda url: url + "/api/v1/session/bootstrap?nonce=initial")
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)))
    window = SimpleNamespace(load_url=lambda url: None)
    desktop._wait_and_navigate(window, "http://127.0.0.1:28767")
    assert len(started) == 1
    assert started[0]["args"] == (window, "http://127.0.0.1:28767", "initial")


def test_watch_stop_closes_parent_pipe_and_waits_before_termination(monkeypatch):
    calls = []
    proc = SimpleNamespace(
        poll=lambda: None,
        stdin=SimpleNamespace(close=lambda: calls.append("eof")),
        wait=lambda timeout: calls.append("wait"),
        terminate=lambda: calls.append("terminate"),
    )
    monkeypatch.setattr(desktop, "_external_server", {"proc": proc})
    desktop._stop_server_process()
    assert calls == ["eof", "wait"]
