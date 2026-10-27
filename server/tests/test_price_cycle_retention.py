"""周期、事件的年龄保留、未完成工作保护、截块和幂等性。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.crawl import retention as crawl_retention
from app.domains.crawl.cycle import COMPLETED, PriceCycle
from app.domains.crawl.events import PriceEvent


async def _cycle(
    *, days_ago: float
) -> PriceCycle:
    started = datetime.now() - timedelta(days=days_ago)
    async with get_session_factory()() as session:
        row = PriceCycle(
            kind="scheduled", status=COMPLETED, scope="pool",
            started_at=started, finished_at=started,
        )
        session.add(row)
        await session.commit()
        return row.id




@pytest.mark.asyncio
async def test_retention_keeps_unfinished_debt_until_a_later_stage_completes():
    from app.domains.crawl import cycle

    cid = await _cycle(days_ago=91)
    spec = {"scope": "catalog"}
    await cycle.record_stage(cid, spec, "deferred", "代理通道正在重建")
    await crawl_retention.prune_price_cycles()
    assert await cycle.get(cid) is not None
    assert await cycle.pending_specs() == [spec]
    latest = await _cycle(days_ago=1)
    await cycle.record_stage(latest, spec, "done")
    await crawl_retention.prune_price_cycles()
    assert await cycle.get(cid) is None
    assert await cycle.pending_specs() == []


@pytest.mark.asyncio
async def test_retained_cycle_does_not_resurrect_a_completed_sibling_stage():
    from app.domains.crawl import cycle

    first = await _cycle(days_ago=95)
    pool, catalog = {"scope": "pool"}, {"scope": "catalog"}
    await cycle.record_stage(first, pool, "deferred", "代理体检进行中")
    await cycle.record_stage(first, catalog, "deferred", "代理体检进行中")
    closed_pool = await _cycle(days_ago=94)
    await cycle.record_stage(closed_pool, pool, "done")
    assert await cycle.pending_specs() == [catalog]
    await crawl_retention.prune_price_cycles()
    assert await cycle.pending_specs() == [catalog]


@pytest.mark.asyncio
async def test_retention_preserves_fifo_when_completed_stage_later_becomes_deferred():
    from app.domains.crawl import cycle

    pool, catalog, specials = {"scope": "pool"}, {"scope": "catalog"}, {"scope": "specials"}
    first = await _cycle(days_ago=95)
    await cycle.record_stage(first, pool, "deferred", "让路")
    await cycle.record_stage(first, catalog, "deferred", "让路")
    second = await _cycle(days_ago=94)
    await cycle.record_stage(second, pool, "done")
    third = await _cycle(days_ago=2)
    await cycle.record_stage(third, specials, "deferred", "让路")
    fourth = await _cycle(days_ago=1)
    await cycle.record_stage(fourth, pool, "deferred", "让路")
    assert await cycle.pending_specs() == [catalog, specials, pool]
    await crawl_retention.prune_price_cycles()
    assert await cycle.pending_specs() == [catalog, specials, pool]


@pytest.mark.asyncio
async def test_prune_deletes_old_rows_keeps_window_rows():
    old_id = await _cycle(days_ago=91)
    fresh_id = await _cycle(days_ago=1)

    result = await crawl_retention.prune_price_cycles()

    assert result["rows_deleted"] == 1
    async with get_session_factory()() as session:
        assert await session.get(PriceCycle, old_id) is None
        assert await session.get(PriceCycle, fresh_id) is not None




@pytest.mark.asyncio
async def test_prune_idempotent_second_run():
    await _cycle(days_ago=91)
    for i in range(10):
        await _cycle(days_ago=1.0 + i)

    first = await crawl_retention.prune_price_cycles()
    assert first["rows_deleted"] == 1
    second = await crawl_retention.prune_price_cycles()

    assert second["rows_deleted"] == 0
    assert second["truncated"] is False


@pytest.mark.asyncio
async def test_prune_truncated_flag_on_chunk_cap():
    await _cycle(days_ago=91)
    await _cycle(days_ago=92)

    result = await crawl_retention.prune_price_cycles(chunk=1, max_chunks=1)

    assert result["rows_deleted"] == 1
    assert result["truncated"] is True, "块上限截断必须如实上报"


@pytest.mark.asyncio
async def test_prune_removes_price_events_outside_the_shared_90_day_window():
    now = datetime.now()
    async with get_session_factory()() as session:
        old = PriceEvent(
            cycle_id=-1, appid=1001, event_type="PRICE_DROP",
            occurred_at=now - timedelta(days=91), created_at=now - timedelta(days=91),
        )
        fresh = PriceEvent(
            cycle_id=-2, appid=1002, event_type="PRICE_DROP",
            occurred_at=now - timedelta(days=1), created_at=now - timedelta(days=1),
        )
        session.add_all([old, fresh])
        await session.commit()
        old_id, fresh_id = old.id, fresh.id

    result = await crawl_retention.prune_price_cycles(now=now)

    async with get_session_factory()() as session:
        assert await session.get(PriceEvent, old_id) is None
        assert await session.get(PriceEvent, fresh_id) is not None
    assert result["events_deleted"] == 1
