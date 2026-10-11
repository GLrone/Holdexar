"""价格周期台账保留（crawl/retention.prune_price_cycles）行为验收：

- 90 天窗外整行删除，窗内新态行不动；
- 存量 JSON 明细除最近 8 轮外置 NULL（保留轮回滚缓冲）；
- 二跑幂等零清理；块上限截断时 truncated 为真。

隔离：tmp 库（conftest autouse 数据目录），不触生产库。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.crawl import retention as crawl_retention
from app.domains.crawl.cycle import COMPLETED, PriceCycle


async def _cycle(
    *, days_ago: float, with_json: bool = False
) -> PriceCycle:
    started = datetime.now() - timedelta(days=days_ago)
    async with get_session_factory()() as session:
        row = PriceCycle(
            kind="scheduled", status=COMPLETED, scope="pool",
            started_at=started, finished_at=started,
        )
        if with_json:
            row.coverage_json = {"780001": {"ok": 1, "locked": 0}}
        session.add(row)
        await session.commit()
        return row.id


async def _json_count() -> int:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PriceCycle).where(PriceCycle.coverage_json.is_not(None))
            )
        ).scalars().all()
        return len(rows)


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
async def test_prune_trims_details_keeping_recent_rounds():
    # 10 轮带明细的窗内存量行（id 递增 = 时间新→旧）+ 1 轮无明细
    ids = [await _cycle(days_ago=1.0 + i, with_json=True) for i in range(10)]
    plain_id = await _cycle(days_ago=0.5)

    result = await crawl_retention.prune_price_cycles()

    assert result["rows_deleted"] == 0, "窗内行不整行删除"
    assert result["details_trimmed"] == 2, "只置空最旧 2 轮"
    async with get_session_factory()() as session:
        kept = {
            row.id: row.coverage_json is not None
            for row in (
                await session.execute(select(PriceCycle))
            ).scalars().all()
        }
    assert kept[plain_id] is False, "无明细行不受影响"
    assert all(kept[cid] for cid in ids[-8:]), "最近 8 轮明细保留"
    assert not any(kept[cid] for cid in ids[:2]), "更旧明细被置空"


@pytest.mark.asyncio
async def test_prune_idempotent_second_run():
    await _cycle(days_ago=91)
    for i in range(10):
        await _cycle(days_ago=1.0 + i, with_json=True)

    first = await crawl_retention.prune_price_cycles()
    assert first["rows_deleted"] == 1 or first["details_trimmed"] == 2
    second = await crawl_retention.prune_price_cycles()

    assert second["rows_deleted"] == 0
    assert second["details_trimmed"] == 0
    assert second["truncated"] is False


@pytest.mark.asyncio
async def test_prune_truncated_flag_on_chunk_cap():
    await _cycle(days_ago=91)
    await _cycle(days_ago=92)

    result = await crawl_retention.prune_price_cycles(chunk=1, max_chunks=1)

    assert result["rows_deleted"] == 1
    assert result["truncated"] is True, "块上限截断必须如实上报"
