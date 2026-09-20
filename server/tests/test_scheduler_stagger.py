"""30 分钟重活的 scheduler 错峰：两个重活不落在 proxypool 周期的 5 分钟网格整点上。

同秒起跑会让它们与周期的写入一起抢 SQLite 写锁（真实生产已见 `database is locked`）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import scheduler as sched
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_refresh_and_bills_are_staggered_off_the_cycle_grid(monkeypatch, tmp_data_dir):
    recorded: dict[str, dict] = {}

    def fake_add_job(func, trigger=None, **kw):  # noqa: ANN001
        recorded[kw.get("id") or getattr(func, "__name__", "?")] = kw

    monkeypatch.setattr(sched.scheduler, "add_job", fake_add_job)
    monkeypatch.setattr(sched.scheduler, "start", lambda *a, **kw: None)
    monkeypatch.setattr(sched.scheduler, "shutdown", lambda *a, **kw: None)
    # `running` 是只读 property：在类上换掉它，绕过 start_scheduler 的重复启动守卫
    monkeypatch.setattr(type(sched.scheduler), "running", property(lambda self: False))

    sched.start_scheduler()

    refresh = recorded["subscription_refresh"]
    bills = recorded["bills_sync"]
    cycle = recorded["proxypool_cycle"]

    assert "next_run_time" in refresh, "订阅刷新必须错开周期网格"
    assert "next_run_time" in bills, "账单同步必须错开周期网格"
    assert "next_run_time" not in cycle, "周期仍应占 5 分钟网格整点"
    # 两次 datetime.now() 取值有微秒差，按容差比位移之差
    delta = refresh["next_run_time"] - bills["next_run_time"]
    expected = sched._REFRESH_STAGGER - sched._BILLS_STAGGER
    assert abs(delta - expected) < timedelta(seconds=1), f"{delta} 与 {expected} 不符"
    assert sched._BILLS_STAGGER > timedelta(0)
    assert sched._REFRESH_STAGGER > sched._BILLS_STAGGER
    assert sched._REFRESH_STAGGER < timedelta(minutes=30)