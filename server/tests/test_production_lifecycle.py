import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

@pytest.mark.asyncio
async def test_production_owner_is_unique_per_data_directory(tmp_path):
    from app.core.instance import production_instance

    async with production_instance(tmp_path):
        with pytest.raises(RuntimeError, match="后台服务"):
            async with production_instance(tmp_path):
                pytest.fail("duplicate production owner")
        async with production_instance(tmp_path / "other"):
            pass
    async with production_instance(tmp_path):
        pass


@pytest.mark.parametrize("tray_available", [True, False])
def test_background_close_preserves_tray_or_taskbar_access(monkeypatch, tray_available):
    import main as desktop

    calls = []
    tray = object() if tray_available else None
    window = SimpleNamespace(hide=lambda: calls.append("hide"), minimize=lambda: calls.append("minimize"))
    monkeypatch.setattr(desktop, "_background_mode", True, raising=False)
    monkeypatch.setitem(desktop._tray_state, "quit", False)
    monkeypatch.setitem(desktop._tray_state, "tray", tray)
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda *args: pytest.fail("configured close asks again"))
    assert desktop._make_closing_guard(window)() is False
    assert calls == (["hide"] if tray_available else ["minimize"])
    assert desktop._tray_state["tray"] is tray


def test_background_tray_exit_allows_real_window_close(monkeypatch):
    import main as desktop

    monkeypatch.setattr(desktop, "_background_mode", True)
    monkeypatch.setitem(desktop._tray_state, "quit", True)
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda *args: pytest.fail("tray exit asks again"))
    assert desktop._make_closing_guard(SimpleNamespace())() is True


def test_activation_reuses_background_loop_after_window_closes(monkeypatch):
    import threading
    import main as desktop

    stopped = threading.Event()
    activation = threading.Event()
    calls = []

    def window(url):
        calls.append(url)
        if len(calls) == 1:
            activation.set()
        else:
            stopped.set()

    monkeypatch.setattr(desktop, "_activation", activation)
    monkeypatch.setattr(desktop, "_open_window", window)
    desktop._background_loop(stopped, "http://127.0.0.1", show=True)
    assert calls == ["http://127.0.0.1"] * 2


@pytest.mark.asyncio
async def test_startup_resumes_debt_before_starting_another_full_cycle(monkeypatch):
    from app.core import scheduler
    from app.domains.crawl import service

    modified = []

    async def yes(*args, **kwargs):
        return True

    monkeypatch.setattr(scheduler, "price_auto_enabled", yes)
    monkeypatch.setattr(service, "has_pending_missing", yes)
    monkeypatch.setattr(scheduler.scheduler, "get_job", lambda key: SimpleNamespace(next_run_time=None))
    monkeypatch.setattr(scheduler.scheduler, "modify_job", lambda key, **kwargs: modified.append(key))
    await scheduler._kick_stale_price_refresh()
    assert modified == ["price_repair"]


def test_activation_listener_survives_idle_client(monkeypatch):
    import threading
    import main as desktop

    activation = threading.Event()

    class Connection:
        def __init__(self, command):
            self.command = command

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def recv(self, size):
            if self.command is None:
                raise TimeoutError()
            return self.command

    class Listener:
        pending = [Connection(None), Connection(b"show")]

        def accept(self):
            if not self.pending:
                raise OSError()
            return self.pending.pop(0), None

    monkeypatch.setattr(desktop, "_activation", activation)
    desktop._listen_activation(Listener())
    assert activation.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", [(10, 1.0), None])
async def test_missed_grid_runs_once_even_when_recent_prices_are_fresh(monkeypatch, ratio):
    from datetime import datetime, timedelta
    from app.core import scheduler
    from app.core.database import write_session
    from app.domains.crawl import service
    from app.domains.crawl.cycle import PriceCycle

    async def value(result):
        return result

    modified = []
    async with write_session() as session:
        session.add(PriceCycle(kind="scheduled", status="completed", started_at=datetime.now() - timedelta(days=1)))
        await session.commit()
    monkeypatch.setattr(scheduler, "price_auto_enabled", lambda: value(True))
    monkeypatch.setattr(service, "has_pending_missing", lambda **kwargs: value(False))
    monkeypatch.setattr(scheduler, "_pool_fresh_ratio", lambda: value(ratio))
    monkeypatch.setattr(scheduler.scheduler, "get_job", lambda key: SimpleNamespace(next_run_time=None))
    monkeypatch.setattr(scheduler.scheduler, "modify_job", lambda key, **kwargs: modified.append(key))
    await scheduler._kick_stale_price_refresh()
    assert modified == ["price_refresh"]


def test_background_update_wait_does_not_look_like_user_exit(monkeypatch, tmp_path):
    import main as desktop

    def exit_code(code):
        raise SystemExit(code)

    monkeypatch.setattr(sys, "argv", ["main.py", "--server", "--data-dir", str(tmp_path)])
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HOLDEXAR_UPDATE_TOKEN", raising=False)
    monkeypatch.setattr(desktop, "_apply_webview2_static_args", lambda: None)
    monkeypatch.setattr(desktop, "_negotiate_ports", lambda: None)
    monkeypatch.setattr(desktop, "_acquire_lock", object)
    monkeypatch.setattr(desktop, "_swap_mutex_held", lambda: True)
    monkeypatch.setattr(desktop, "_log_update", lambda message: None)
    monkeypatch.setattr(desktop.os, "_exit", exit_code)
    with pytest.raises(SystemExit) as result:
        desktop.main()
    assert result.value.code == 75


def test_tray_loop_and_resources_end_when_window_closes(monkeypatch):
    from types import ModuleType
    import main as desktop

    calls = []

    class Closed:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

        def is_set(self):
            return False

    class Context:
        def ExitThread(self):
            calls.append("exit")

        def Dispose(self):
            calls.append("context")

    closed = Closed()
    tray = SimpleNamespace(Visible=True, Dispose=lambda: calls.append("tray"))
    forms = ModuleType("System.Windows.Forms")
    forms.ApplicationContext = Context
    forms.Timer = lambda: SimpleNamespace(Interval=0, Tick=Closed(), Start=lambda: None,
                                         Dispose=lambda: calls.append("timer"))
    forms.Application = SimpleNamespace(Run=lambda context: [handler() for handler in closed.handlers[:]])
    windows = ModuleType("System.Windows")
    windows.Forms = forms
    system = ModuleType("System")
    system.Windows = windows
    for key, value in [("System", system), ("System.Windows", windows), ("System.Windows.Forms", forms)]:
        monkeypatch.setitem(sys.modules, key, value)
    monkeypatch.setattr(desktop, "_create_tray", lambda window: tray)
    monkeypatch.setitem(desktop._tray_state, "tray", tray)
    desktop._run_tray(SimpleNamespace(events=SimpleNamespace(closed=closed)))
    assert calls == ["exit", "timer", "context", "tray"]
    assert not closed.handlers and desktop._tray_state["tray"] is None
