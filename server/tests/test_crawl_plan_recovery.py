import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory, write_session
from app.crawler.utils import get_beijing_time_obj
from app.domains.crawl import cycle, cycle_run, service
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice


def units(tasks):
    return {(aid, t["region"].upper()) for t in tasks for aid in t["appids"]}


async def interrupted_plan(*, cancelled=False):
    stamp = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=10)
    async with write_session() as session:
        row = cycle.PriceCycle(
            kind="manual", status=cycle.CANCELLED if cancelled else cycle.RUNNING,
            started_at=stamp, expected_json={"appids": [101, 102], "regions": ["CN", "US"]},
            specs_json=[{"scope": "catalog", "resolved_appids": [101, 102],
                         "resolved_regions": ["CN", "US"]}],
        )
        session.add(row)
        session.add_all([Game(appid=101, name="one"), Game(appid=102, name="two")])
        session.add(GameCurrentPrice(appid=101, region_code="CN", currency="CNY",
                                    price_status="ok", updated_at=stamp + timedelta(minutes=1)))
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_restart_recovers_only_unobserved_cells_in_unstarted_stage():
    cid = await interrupted_plan()
    await service.cleanup_orphan_jobs()
    expected = {(101, "US"), (102, "CN"), (102, "US")}
    assert units(await service._missing_tasks(cooldown_minutes=4)) == expected
    assert units(await service._missing_tasks(cooldown_minutes=4)) == expected
    async with get_session_factory()() as session:
        jobs = (await session.execute(select(CrawlJob).where(CrawlJob.cycle_id == cid))).scalars().all()
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_cancelled_plan_does_not_resume():
    await interrupted_plan(cancelled=True)
    await service.cleanup_orphan_jobs()
    assert units(await service._missing_tasks(cooldown_minutes=0)) == set()


@pytest.mark.asyncio
async def test_parallel_cycles_do_not_replace_the_active_stop_token(monkeypatch):
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        await leave.wait()
        return True

    monkeypatch.setattr(cycle_run, "_run_price_cycle", run)
    first = asyncio.create_task(cycle_run.run_price_cycle([]))
    await entered.wait()
    original = service._chain_stop
    try:
        assert await asyncio.wait_for(cycle_run.run_price_cycle([]), 0.2) is False
        assert service._chain_stop is original
    finally:
        leave.set()
        await first


@pytest.mark.asyncio
async def test_user_stop_marks_all_unstarted_stages_cancelled():
    cid = await interrupted_plan()
    specs = [{"scope": "pool"}, {"scope": "catalog"}]
    stop = asyncio.Event()
    stop.set()
    await service.run_sequential(specs, cycle_id=cid, stop_event=stop)
    async with get_session_factory()() as session:
        row = await session.get(cycle.PriceCycle, cid)
        assert all(s.get("execution", {}).get("state") == "cancelled" for s in row.specs_json)


@pytest.mark.asyncio
async def test_whole_chain_stop_survives_exit_before_stage_finishes(monkeypatch):
    cid = await interrupted_plan()
    monkeypatch.setattr(service, "_PROCESS_STARTED_AT", get_beijing_time_obj().replace(tzinfo=None) - timedelta(hours=1))
    token = asyncio.Event()
    service.register_chain_stop(token)
    try:
        assert await service.stop_job(whole_chain=True)
    finally:
        service.unregister_chain_stop(token)
    monkeypatch.setattr(service, "_PROCESS_STARTED_AT", get_beijing_time_obj().replace(tzinfo=None) + timedelta(seconds=1))
    await service.cleanup_orphan_jobs()
    assert units(await service._missing_tasks(cooldown_minutes=0)) == set()


@pytest.mark.asyncio
async def test_exclusion_discharges_original_recovery_targets():
    from app.domains.monitoring import service as monitoring

    cid = await interrupted_plan()
    await service.cleanup_orphan_jobs()
    await monitoring.set_exclusion("game", 101, True)
    await monitoring.set_exclusion("game", 102, True)
    assert units(await service._missing_tasks(cooldown_minutes=0)) == set()
    assert await service._unobserved_job_targets(0) == set()


@pytest.mark.asyncio
async def test_deleted_recovery_job_cannot_restore_settled_plan():
    from sqlalchemy import delete

    cid = await interrupted_plan()
    await service.cleanup_orphan_jobs()
    await service._missing_tasks(cooldown_minutes=0)
    async with write_session() as session:
        jobs = (await session.execute(select(CrawlJob).where(CrawlJob.cycle_id == cid))).scalars().all()
        for job in jobs:
            job.stats_json = {**job.stats_json, "skipUnits": ["101:CN", "101:US", "102:CN", "102:US"]}
        await session.commit()
    await service._unobserved_job_targets(0, settle=True)
    async with write_session() as session:
        await session.execute(delete(CrawlJob).where(CrawlJob.cycle_id == cid))
        await session.commit()
    assert units(await service._missing_tasks(cooldown_minutes=0)) == set()


@pytest.mark.asyncio
async def test_stop_during_planning_cannot_create_recoverable_cycle():
    token = asyncio.Event()
    token.set()
    cid = await cycle.create("manual", "all", stop_event=token)
    await cycle.freeze_expected(cid, [{"scope": "appids", "resolved_appids": [101], "resolved_regions": ["CN"]}])
    async with get_session_factory()() as session:
        assert (await session.get(cycle.PriceCycle, cid)).status == cycle.CANCELLED
    await cycle.recover_pending_stages()
    assert await service._unobserved_job_targets(0) == set()


@pytest.mark.asyncio
async def test_stop_keeps_original_handle_when_job_finishes_during_write(monkeypatch):
    from types import SimpleNamespace

    handle = SimpleNamespace(id=7, stop_event=asyncio.Event())
    monkeypatch.setattr(service, "_active", handle)

    async def finished(*args, **kwargs):
        service._active = None

    monkeypatch.setattr(service, "_finish_job", finished)
    assert await service.stop_job(whole_chain=False)
    assert handle.stop_event.is_set()


@pytest.mark.asyncio
async def test_cancelled_parent_neither_recovers_nor_prevents_retention():
    from sqlalchemy import text
    from app.domains.crawl.retention import unfinished_job_units_sql

    cid = await interrupted_plan(cancelled=True)
    async with write_session() as session:
        job = CrawlJob(kind="manual", status="interrupted", cycle_id=cid,
                       stats_json={"targets": {"appids": [101, 102], "regions": ["CN", "US"]}})
        session.add(job)
        await session.commit()
    assert await service._unobserved_job_targets(0) == set()
    async with get_session_factory()() as session:
        retained = (await session.execute(text("SELECT id FROM crawl_jobs WHERE " + unfinished_job_units_sql()))).all()
        assert retained == []


@pytest.mark.asyncio
async def test_excluded_missing_rows_do_not_consume_repair_budget():
    from app.domains.monitoring import service as monitoring

    stamp = get_beijing_time_obj().replace(tzinfo=None) - timedelta(days=1)
    async with write_session() as session:
        session.add_all([Game(appid=101, name="excluded"), Game(appid=102, name="valid")])
        session.add_all([GameCurrentPrice(appid=aid, region_code="CN", currency="CNY", price_status="missing",
                                         updated_at=stamp + timedelta(minutes=aid)) for aid in [101, 102]])
        await session.commit()
    await monitoring.set_exclusion("game", 101, True)
    assert units(await service._missing_tasks(cooldown_minutes=0, limit_rows=1)) == {(102, "CN")}
    await monitoring.set_exclusion("game", 102, True)
    assert not await service.has_pending_missing(cooldown_minutes=0)
