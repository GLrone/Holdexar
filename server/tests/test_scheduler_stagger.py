"""30 分钟重活的 scheduler 错峰：两个重活不落在 proxypool 周期的 5 分钟网格整点上。

同秒起跑会让它们与周期的写入一起抢 SQLite 写锁（真实生产已见 `database is locked`）。
"""
from __future__ import annotations

import os

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
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则 settings 缓存会把临时目录带进下一个测试文件
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
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

    assert refresh["next_run_time"] == cycle["next_run_time"]
    assert bills["next_run_time"] > refresh["next_run_time"]
    assert bills["next_run_time"] - refresh["next_run_time"] <= timedelta(minutes=2, seconds=1)


def test_skiptrace_listener_registered_once(monkeypatch, tmp_data_dir):
    """跳拍事件监听：mask 与回调正确，stop→start 重入不重复注册。"""
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

    monkeypatch.setattr(sched.scheduler, "add_job", lambda *a, **kw: None)
    monkeypatch.setattr(sched.scheduler, "start", lambda *a, **kw: None)
    monkeypatch.setattr(sched.scheduler, "shutdown", lambda *a, **kw: None)
    monkeypatch.setattr(type(sched.scheduler), "running", property(lambda self: False))

    registered: list[tuple] = []

    def fake_add_listener(callback, mask=None, **kw):
        registered.append((callback, mask))

    def fake_remove_listener(callback, **kw):
        registered[:] = [entry for entry in registered if entry[0] is not callback]

    monkeypatch.setattr(sched.scheduler, "add_listener", fake_add_listener)
    monkeypatch.setattr(sched.scheduler, "remove_listener", fake_remove_listener)

    sched.start_scheduler()
    sched.start_scheduler()

    entries = [e for e in registered if e[0] is sched._on_job_skipped]
    assert len(entries) == 1, "重入后注册仍须只有一份"
    assert entries[0][1] == EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED


def test_content_schedule_registration_matches_defaults(monkeypatch, tmp_data_dir):
    """内容源注册拍点与 CONTENT_SCHEDULE_DEFAULTS 同源（防两处漂移）；
    自定义值由启动链/设置保存重排覆盖，注册面永远持默认。"""
    recorded: dict[str, dict] = {}

    def fake_add_job(func, trigger=None, **kw):  # noqa: ANN001
        recorded[kw.get("id") or getattr(func, "__name__", "?")] = kw

    monkeypatch.setattr(sched.scheduler, "add_job", fake_add_job)
    monkeypatch.setattr(sched.scheduler, "start", lambda *a, **kw: None)
    monkeypatch.setattr(sched.scheduler, "shutdown", lambda *a, **kw: None)
    monkeypatch.setattr(type(sched.scheduler), "running", property(lambda self: False))

    sched.start_scheduler()

    hb = recorded["hb_choice"]
    epic = recorded["epic_free"]
    assert (hb["hour"], hb["minute"]) == sched.parse_schedule_time(
        sched.CONTENT_SCHEDULE_DEFAULTS["hb_choice"]["time"]
    )
    assert (epic["hour"], epic["minute"]) == sched.parse_schedule_time(
        sched.CONTENT_SCHEDULE_DEFAULTS["epic_free"]["time"]
    )
    assert "day" not in hb and "day" not in epic, "默认必须是每日（day 步进只来自自定义）"


@pytest.mark.asyncio
async def test_skiptrace_events_land_in_ledger_and_log(tmp_data_dir, monkeypatch, caplog):
    """max_instances 落在飞 run 流水；missed 出 WARNING 用户日志；无在飞 run 降级出同词形日志。"""
    import asyncio as _asyncio
    import logging
    import time as _time

    from datetime import datetime as _datetime

    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
    from apscheduler.events import JobExecutionEvent, JobSubmissionEvent

    from app.core.database import get_session_factory, init_db
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import scheduler_bridge

    await init_db()
    run_time = _datetime.now()

    async def _job():
        # 在飞 run 已登记（run_accounted 建账后），投让路事件并轮询等待落账
        sched._on_job_skipped(
            JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "evt_job", None, [run_time])
        )
        run_id = scheduler_bridge._RUNNING_JOB_RUNS.get("evt_job")
        deadline = _time.monotonic() + 5.0
        steps: list = []
        while _time.monotonic() < deadline:
            events = await agent_service.list_events(run_id) if run_id else []
            steps = [e for e in events if e["event_type"] == "step.completed"]
            if steps:
                break
            await _asyncio.sleep(0.01)
        assert steps, "让路流水必须投给在飞 run"
        assert steps[0]["payload"]["step"] == "evt_job"
        assert steps[0]["payload"]["summary"]["skipped"] == "上一轮还在进行，本拍让路"
        assert "原定时刻" in steps[0]["payload"]["summary"]
        return None

    await scheduler_bridge.run_accounted("evt_job", _job)

    # missed：无 run 上下文，走用户日志（WARNING + tag=跳过 + 中文任务名）
    with caplog.at_level(logging.WARNING, logger="app.core.scheduler"):
        sched._on_job_skipped(
            JobExecutionEvent(EVENT_JOB_MISSED, "wallet_sync", None, run_time)
        )
        await _asyncio.sleep(0.05)
        missed = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("钱包同步" in r.msg and "已跳过" in r.msg for r in missed)
        assert all(r.hl_tag == "跳过" for r in missed)

        # 无在飞 run 的 max_instances：降级出同词形日志，痕不丢
        caplog.clear()
        sched._on_job_skipped(
            JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "ghost_job", None, [run_time])
        )
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            degraded = [
                r for r in caplog.records
                if r.levelno == logging.WARNING and "ghost_job" in r.msg
            ]
            if degraded:
                break
            await _asyncio.sleep(0.01)
        assert degraded, "无在飞 run 时必须降级出日志"
        assert "上一轮还在进行，本拍让路" in degraded[0].msg
        assert degraded[0].hl_tag == "跳过"
