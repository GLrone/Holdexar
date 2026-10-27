import importlib.util
import threading
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


def test_apply_native_styling_handles_missing_or_valid_window():
    # Should not raise even with fake window uid
    fake_window = SimpleNamespace(uid="fake_uid")
    desktop._apply_native_styling(fake_window)


def test_wait_and_navigate_waits_for_splash_rendered(monkeypatch):
    calls = []
    splash_rendered = threading.Event()
    fake_window = SimpleNamespace(load_url=lambda url: calls.append(url), events=SimpleNamespace(loaded=None))

    monkeypatch.setattr(desktop, "_wait_health", lambda timeout: True)
    monkeypatch.setattr(desktop, "_install_navigation_guard", lambda win, url: None)
    monkeypatch.setattr(desktop, "_session_entry_url", lambda url: f"{url}/bootstrap")

    def run_navigate():
        desktop._wait_and_navigate(fake_window, "http://127.0.0.1:28765", splash_rendered)

    t = threading.Thread(target=run_navigate)
    t.start()

    # Before splash_rendered is set, load_url should not have been called yet
    threading.Event().wait(0.05)
    assert len(calls) == 0

    # Set splash_rendered
    splash_rendered.set()
    t.join(timeout=1.0)

    assert len(calls) == 1
    assert calls[0] == "http://127.0.0.1:28765/bootstrap"
