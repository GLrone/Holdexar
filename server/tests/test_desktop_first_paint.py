import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_main():
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main_paint", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()


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
