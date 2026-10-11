"""维护预约位行为验收：体检/重建全程计入 crawler_busy，爬取启动被拒时
409 文案按维护类型区分；L0 绝不预约。

预约位与爬取占用共用 occupancy 一把锁（一事实一主账本）：无表结构改动、
纯进程内状态。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory, init_db
from app.crawler.occupancy import (
    MAINTENANCE_HEALTH,
    MAINTENANCE_REBUILD,
    CrawlerBusyError,
    begin_crawl,
    begin_maintenance,
    crawler_busy,
    end_crawl,
    end_maintenance,
    maintenance_kind,
    maintenance_reason,
)
from app.domains.crawl import service as crawl_service
from app.domains.proxypool import scheduling as sched

NOW = datetime(2026, 10, 11, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    from app.core.config import get_settings
    from app.core.database import get_engine, get_session_factory

    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    import os

    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _reset_reservation():
    end_maintenance()
    if crawler_busy():
        end_crawl()
    yield
    end_maintenance()
    if crawler_busy():
        end_crawl()


def test_reservation_blocks_begin_crawl_with_user_reason():
    begin_maintenance(MAINTENANCE_HEALTH)
    assert crawler_busy() is True
    with pytest.raises(CrawlerBusyError) as exc:
        begin_crawl("test-crawl")
    assert str(exc.value) == "代理体检进行中"
    end_maintenance()
    assert crawler_busy() is False


def test_begin_maintenance_rejected_while_crawl_holding():
    begin_crawl("test-crawl")
    with pytest.raises(CrawlerBusyError) as exc:
        begin_maintenance(MAINTENANCE_HEALTH)
    assert "已有爬取任务在执行" in str(exc.value)
    end_crawl()
    begin_maintenance(MAINTENANCE_HEALTH)
    assert maintenance_kind() == MAINTENANCE_HEALTH


@pytest.mark.asyncio
async def test_start_job_409_carries_maintenance_reason():
    begin_maintenance(MAINTENANCE_HEALTH)
    with pytest.raises(RuntimeError) as exc:
        await crawl_service.start_job(appids=[780001])
    assert str(exc.value) == "代理体检进行中"
    end_maintenance()

    begin_maintenance(MAINTENANCE_REBUILD)
    with pytest.raises(RuntimeError) as exc:
        await crawl_service.start_job(appids=[780001])
    assert str(exc.value) == "代理通道正在重建"


@pytest.mark.asyncio
async def test_start_full_queue_409_carries_maintenance_reason():
    begin_maintenance(MAINTENANCE_HEALTH)
    with pytest.raises(RuntimeError) as exc:
        await crawl_service.start_full_queue()
    assert str(exc.value) == "代理体检进行中"
    end_maintenance()

    begin_maintenance(MAINTENANCE_REBUILD)
    with pytest.raises(RuntimeError) as exc:
        await crawl_service.start_full_queue()
    assert str(exc.value) == "代理通道正在重建"


@pytest.mark.asyncio
async def test_run_maintenance_cycle_reserves_and_finally_releases(tmp_data_dir):
    await init_db()

    async def boom(*a, **kw):
        raise RuntimeError("probe boom")

    from app.domains.proxypool import scheduling as sched_mod
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(sched_mod, "_maintenance_probe", boom)
        async with get_session_factory()() as s:
            with pytest.raises(RuntimeError, match="probe boom"):
                await sched_mod.run_maintenance_cycle(
                    s, data_dir=tmp_data_dir,
                    controller_url="http://127.0.0.1:1", secret="x", now=NOW,
                )
        assert maintenance_kind() is None, "异常路径预约也必须在 finally 复位"
        assert sched_mod.crawler_busy() is False
        assert sched_mod.maintenance_running() is False

        async def ok_probe(*a, **kw):
            assert maintenance_kind() == MAINTENANCE_HEALTH, "探测体内预约必须在位"
            return "SENTINEL"

        monkey.setattr(sched_mod, "_maintenance_probe", ok_probe)
        async with get_session_factory()() as s:
            result = await sched_mod.run_maintenance_cycle(
                s, data_dir=tmp_data_dir,
                controller_url="http://127.0.0.1:1", secret="x", now=NOW,
            )
        assert result == "SENTINEL"
        assert maintenance_kind() is None
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_run_pending_rebuild_preserves_pending_when_busy():
    """体检预约在位：rebuild 让路且 pending 原样保留（预约失败不吞信号）。"""
    from app.domains.proxypool import scheduling as sched_mod

    begin_maintenance(MAINTENANCE_HEALTH)
    sched_mod.request_rebuild()
    assert sched_mod.rebuild_pending() is True

    result = await sched_mod.run_pending_rebuild(
        None, data_dir=None, controller_url="http://127.0.0.1:1",
        secret="x", runtime=None, exe_path="",
    )

    assert result is None
    assert sched_mod.rebuild_pending() is True, "让路不得消费 pending"
    end_maintenance()


@pytest.mark.asyncio
async def test_run_pending_rebuild_holds_rebuild_reservation(tmp_data_dir):
    await init_db()
    from app.domains.proxypool import scheduling as sched_mod

    async def fake_rebuild(session, **kw):
        assert maintenance_kind() == MAINTENANCE_REBUILD, "重建体内预约必须在位"
        return "REBUILT"

    async def no_skip(session, **kw):
        return None

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(sched_mod, "rebuild_runtime", fake_rebuild)
        monkey.setattr(sched_mod, "_rebuild_skip_reason", no_skip)
        sched_mod.request_rebuild()
        async with get_session_factory()() as s:
            result = await sched_mod.run_pending_rebuild(
                s, data_dir=tmp_data_dir,
                controller_url="http://127.0.0.1:1", secret="x",
                runtime=None, exe_path="",
            )
        assert result == "REBUILT"
        assert maintenance_kind() is None, "重建结束预约清空"
        assert sched_mod.rebuild_pending() is False, "pending 已消费"
    finally:
        monkey.undo()


def test_maintenance_running_projection_kind_scoped():
    begin_maintenance(MAINTENANCE_REBUILD)
    assert sched.maintenance_running() is False, "重建期间不点亮体检标志"
    end_maintenance()

    begin_maintenance(MAINTENANCE_HEALTH)
    assert sched.maintenance_running() is True
    end_maintenance()


def test_maintenance_reason_wording():
    assert maintenance_reason() is None
    begin_maintenance(MAINTENANCE_REBUILD)
    assert maintenance_reason() == "代理通道正在重建"
    end_maintenance()


@pytest.mark.asyncio
async def test_run_l0_cycle_never_reserves(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as s:
        out = await sched.run_l0_cycle(
            s, data_dir=tmp_data_dir,
            controller_url="http://127.0.0.1:1", secret="x", now=NOW,
        )
    assert out == ()
    assert maintenance_kind() is None, "L0 与爬取并行，绝不预约"

@pytest.mark.asyncio
async def test_maintenance_envelope_publishes_start_end_events(tmp_data_dir):
    """体检包络开始/结束 → SSE 事件 + 编排事件留痕（账本打通的实时面）。"""
    from app.core import orchestration
    from app.core.database import WritePriority, write_gate
    from app.core.events import bus

    await init_db()

    async def ok_probe(*a, **kw):
        return "SENTINEL"

    monkey = pytest.MonkeyPatch()
    sub = bus.subscribe()
    try:
        monkey.setattr(sched, "_maintenance_probe", ok_probe)
        async with get_session_factory()() as s:
            await sched.run_maintenance_cycle(
                s, data_dir=tmp_data_dir,
                controller_url="http://127.0.0.1:1", secret="x", now=NOW,
            )
        types = []
        while not sub.critical.empty():
            types.append(sub.critical.get_nowait().type)
        assert types[0] == "maintenance.started"
        assert types[-1] == "maintenance.ended"
        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as s:
            rows = await s.execute(
                select(orchestration.OrchestrationEvent.kind).where(
                    orchestration.OrchestrationEvent.kind.in_([
                        orchestration.KIND_MAINTENANCE_STARTED,
                        orchestration.KIND_MAINTENANCE_ENDED,
                    ])
                )
            )
            kinds = {k for (k,) in rows}
        assert kinds == {
            orchestration.KIND_MAINTENANCE_STARTED,
            orchestration.KIND_MAINTENANCE_ENDED,
        }
    finally:
        monkey.undo()
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_probe_progress_scoped_to_health_reservation():
    """探测进度只从体检包络内发布：无预约 / 重建预约一律静默。"""
    from app.core.events import bus
    from app.domains.proxypool.health import _publish_probe_progress

    sub = bus.subscribe()
    try:
        _publish_probe_progress("L1", 5)
        assert sub.progress.empty(), "无维护预约不得发布体检进度"

        begin_maintenance(MAINTENANCE_REBUILD)
        _publish_probe_progress("L1", 5)
        assert sub.progress.empty(), "重建包络不是体检进度"
        end_maintenance()

        begin_maintenance(MAINTENANCE_HEALTH)
        _publish_probe_progress("L1", 5)
        ev = await sub.get()
        assert ev.type == "maintenance.progress"
        assert ev.payload == {"kind": MAINTENANCE_HEALTH, "level": "L1", "probed": 5}
    finally:
        end_maintenance()
        bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_crawl_active_maintenance_info_from_ledger(tmp_data_dir):
    """/crawl/active.maintenanceInfo 从体检账本取实时情况（不另设进度状态）。"""
    from app.domains.crawl import router as crawl_router
    from app.domains.proxypool.models import HealthObservation, HealthRun

    await init_db()
    begin_maintenance(MAINTENANCE_HEALTH)
    try:
        async with get_session_factory()() as s:
            run = HealthRun(channel="scheduled", started_at=NOW)
            s.add(run)
            await s.flush()
            for i in range(3):
                s.add(HealthObservation(
                    node_id=f"n{i}", level="L1", ok=True,
                    run_id=run.id, channel="scheduled", observed_at=NOW,
                ))
            for i in range(2):
                s.add(HealthObservation(
                    node_id=f"n{i}", level="L2", ok=True,
                    run_id=run.id, channel="scheduled", observed_at=NOW,
                ))
            await s.commit()

        payload = await crawl_router.active()
        assert payload["maintenance"] is True
        info = payload["maintenanceInfo"]
        assert info is not None
        assert info["kind"] == MAINTENANCE_HEALTH
        assert info["reason"] == "代理体检进行中"
        assert info["probed"] == {"l1": 3, "l2": 2}
        assert info["startedAt"] is not None

        end_maintenance()
        payload = await crawl_router.active()
        assert payload["maintenance"] is False
        assert payload["maintenanceInfo"] is None
    finally:
        end_maintenance()
