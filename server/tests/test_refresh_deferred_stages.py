from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.crawl import cycle, service


async def seed_cycle(specs):
    async with get_session_factory()() as session:
        row = cycle.PriceCycle(
            kind="scheduled", status="partial", started_at=datetime.now(),
            specs_json=specs,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_deferred_catalog_survives_new_round_and_clears_only_on_completion():
    catalog = {"scope": "catalog"}
    first = await seed_cycle([catalog])
    await cycle.record_stage(first, catalog, "deferred", "代理通道正在重建")
    assert await cycle.prioritize_specs([{"scope": "pool"}, catalog]) == [
        catalog, {"scope": "pool"},
    ]
    second = await seed_cycle([catalog])
    await cycle.record_stage(second, catalog, "deferred", "代理体检进行中")
    assert await cycle.pending_specs() == [catalog]
    await cycle.record_stage(second, catalog, "done")
    assert await cycle.pending_specs() == []


@pytest.mark.asyncio
async def test_busy_segment_is_saved_in_cycle_and_not_lost(monkeypatch):
    specs = [{"scope": "pool"}, {"scope": "catalog"}]
    cid = await seed_cycle(specs)
    monkeypatch.setattr(service, "_tun_mode_suspected", lambda: False)

    async def start(**kwargs):
        if kwargs["scope"] == "catalog":
            raise RuntimeError("代理通道正在重建")
        return {"id": 1, "stats": {"success": 1}}

    monkeypatch.setattr(service, "start_job", start)
    monkeypatch.setattr(service, "_active", None)
    assert len(await service.run_sequential(specs, cycle_id=cid)) == 1
    assert await cycle.pending_specs() == [{"scope": "catalog"}]
    async with get_session_factory()() as session:
        row = await session.scalar(select(cycle.PriceCycle).where(cycle.PriceCycle.id == cid))
        assert row.specs_json[1]["execution"]["reason"] == "代理通道正在重建"


@pytest.mark.asyncio
async def test_disabled_catalog_is_not_reintroduced_by_old_debt():
    catalog = {"scope": "catalog"}
    cid = await seed_cycle([catalog])
    await cycle.record_stage(cid, catalog, "deferred", "已有任务在跑")
    assert await cycle.prioritize_specs([{"scope": "pool"}]) == [{"scope": "pool"}]


@pytest.mark.asyncio
async def test_empty_stage_clears_debt_and_cancel_does_not_create_debt(monkeypatch):
    import asyncio

    spec = {"kind": "missing"}
    cid = await seed_cycle([spec])
    await cycle.record_stage(cid, spec, "deferred", "已有任务在跑")
    monkeypatch.setattr(service, "_tun_mode_suspected", lambda: False)

    async def empty(**kwargs):
        raise ValueError("没有待补抓的欠账（missing）数据")

    monkeypatch.setattr(service, "start_job", empty)
    await service.run_sequential([spec], cycle_id=cid)
    assert await cycle.pending_specs() == []
    stop = asyncio.Event()
    stop.set()
    cancelled = await seed_cycle([{"scope": "catalog"}])
    await service.run_sequential([{"scope": "catalog"}], cycle_id=cancelled, stop_event=stop)
    assert await cycle.pending_specs() == []


def test_cycle_with_a_deferred_stage_cannot_report_completed():
    assert cycle.decide_terminal(
        ["done"], expected_units=2, entered_repairing=False, deferred=True,
    ) == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["manual", "scheduled"])
async def test_manual_and_scheduled_rounds_share_the_same_catalog_debt(origin):
    old = {"scope": "catalog", "kind": origin}
    cid = await seed_cycle([old])
    await cycle.record_stage(cid, old, "deferred", "代理通道正在重建")
    assert await cycle.prioritize_specs([{"scope": "pool"}, {"scope": "catalog"}]) == [
        {"scope": "catalog"}, {"scope": "pool"},
    ]
    newer = await seed_cycle([{"scope": "catalog"}])
    await cycle.record_stage(newer, {"scope": "catalog"}, "done")
    assert await cycle.pending_specs() == []


@pytest.mark.asyncio
async def test_finished_job_with_zero_price_success_keeps_stage_pending(monkeypatch):
    from app.domains.crawl.models import CrawlJob

    spec = {"scope": "catalog"}
    cid = await seed_cycle([spec])
    async with get_session_factory()() as session:
        row = CrawlJob(kind="scheduled", status="done", cycle_id=cid,
                       stats_json={"success": 0, "failed": 3})
        session.add(row)
        await session.commit()
        job_id = row.id

    async def start(**kwargs):
        return {"id": job_id}

    monkeypatch.setattr(service, "start_job", start)
    monkeypatch.setattr(service, "_active", None)
    monkeypatch.setattr(service, "_tun_mode_suspected", lambda: False)
    await service.run_sequential([spec], cycle_id=cid)
    assert await cycle.pending_specs() == [spec]
