"""非周期执行的价格事件检测（detect_window）。

手动单发 / 补抓拍的 job 不挂 Cycle，此前写价不产事件——事件流成了主轮专属
副产物。本文件验证：无 Cycle 的写入按窗口检出事件、归属键取 -job_id、
重复调用不新增行（幂等）。

隔离：tmp 库 + get_session_factory 逐模块打桩，不触真实库。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import events as events_mod
from app.domains.crawl.events import PriceEvent
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

APP = 990001
JOB_ID = 4242


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (cycle_mod, events_mod):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _seed(db, *, baseline_price: int, current_price: int, status: str = "ok"):
    """窗口前基线（history）+ 窗口内观察（current price 行）。"""
    window_end = datetime.now()
    window_start = window_end - timedelta(hours=1)
    async with db() as session:
        session.add(Game(appid=APP, name="测试游戏", type="game"))
        session.add(
            GamePriceHistory(
                appid=APP, region_code="cn", currency="CNY",
                price=baseline_price, original_price=baseline_price,
                discount_percent=0, sub_id=0, is_gold=False,
                version_suffix=None, price_status="ok",
                cny_fen=baseline_price, snapshot_at=window_start - timedelta(minutes=5),
                is_bundle=False,
            )
        )
        session.add(
            GameCurrentPrice(
                appid=APP, region_code="cn", price=current_price,
                original_price=baseline_price, discount_percent=0,
                sub_id=0, price_status=status, cny_fen=current_price,
                updated_at=window_end - timedelta(minutes=1),
            )
        )
        await session.commit()
    return window_start, window_end


async def _events(db) -> list[PriceEvent]:
    async with db() as session:
        return list(
            (await session.execute(select(PriceEvent).order_by(PriceEvent.id))).scalars().all()
        )


@pytest.mark.asyncio
async def test_uncycled_write_produces_drop_event(db):
    window_start, window_end = await _seed(db, baseline_price=10000, current_price=9000)
    written = await events_mod.detect_window(
        [APP], ["cn"], window_start, window_end, cycle_id=-JOB_ID
    )
    types = {e["event_type"] for e in written}
    assert "PRICE_DROP" in types
    rows = await _events(db)
    assert all(r.cycle_id == -JOB_ID for r in rows)


@pytest.mark.asyncio
async def test_detect_window_is_idempotent(db):
    window_start, window_end = await _seed(db, baseline_price=10000, current_price=9000)
    await events_mod.detect_window([APP], ["cn"], window_start, window_end, cycle_id=-JOB_ID)
    await events_mod.detect_window([APP], ["cn"], window_start, window_end, cycle_id=-JOB_ID)
    rows = await _events(db)
    assert len([r for r in rows if r.event_type == "PRICE_DROP"]) == 1


@pytest.mark.asyncio
async def test_status_transition_emits_restored(db):
    # 基线：窗口前有过 ok 价（history），上一事件记 missing → 窗口内恢复 ok
    window_end = datetime.now()
    window_start = window_end - timedelta(hours=1)
    async with db() as session:
        session.add(Game(appid=APP, name="测试游戏", type="game"))
        session.add(
            GamePriceHistory(
                appid=APP, region_code="cn", currency="CNY",
                price=10000, original_price=10000, discount_percent=0,
                sub_id=0, is_gold=False, version_suffix=None,
                price_status="ok", cny_fen=10000,
                snapshot_at=window_start - timedelta(minutes=5), is_bundle=False,
            )
        )
        session.add(
            PriceEvent(
                cycle_id=1, appid=APP, region_code="cn",
                event_type="PRICE_UNAVAILABLE",
                previous_json={"status": "ok"},
                current_json={"status": "missing"},
                occurred_at=window_start - timedelta(minutes=10),
                created_at=window_start - timedelta(minutes=10),
            )
        )
        session.add(
            GameCurrentPrice(
                appid=APP, region_code="cn", price=10000,
                original_price=10000, discount_percent=0, sub_id=0,
                price_status="ok", cny_fen=10000,
                updated_at=window_end - timedelta(minutes=1),
            )
        )
        await session.commit()
    written = await events_mod.detect_window(
        [APP], ["cn"], window_start, window_end, cycle_id=-JOB_ID
    )
    assert "PRICE_RESTORED" in {e["event_type"] for e in written}


@pytest.mark.asyncio
async def test_empty_scope_is_noop(db):
    written = await events_mod.detect_window([], [], datetime.now(), datetime.now(), -1)
    assert written == []
