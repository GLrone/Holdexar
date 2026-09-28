"""批量写入口 upsert_task_batch 验收：一批一次事务，差量门禁与单写入口同语义。

隔离：tmp 库 + get_session_factory 打桩（test_history_delta_gate 同模式）。
观测时刻的活动标签打桩为 None，使本用例不依赖活动日历表。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.crawler.db_writer import DbWriter
from app.domains.games.models import GameCurrentPrice, GamePriceHistory

APPID_A = 996_101
APPID_B = 996_102


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    import app.crawler.db_writer as dw

    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    from app.domains.steam_events import service as steam_events_service

    async def _no_event(_ts):
        return None

    monkeypatch.setattr(steam_events_service, "active_event_key_at", _no_event)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _game(appid, name="批量测试"):
    return {"appid": appid, "name": name, "updated_at": datetime.now()}


def _prices(appid, price, original, discount, suffix=None):
    return [
        {
            "appid": appid, "region_code": "CN", "currency": "CNY",
            "price": price, "original_price": original, "discount_percent": discount,
            "sub_id": 111, "is_gold": False, "version_suffix": suffix,
            "is_bundle": False, "price_status": "ok", "crawled_at": datetime.now(),
        }
    ]


async def _history_count(db, appid):
    async with db() as session:
        stmt = select(func.count()).select_from(GamePriceHistory).where(
            GamePriceHistory.appid == appid
        )
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_batch_writes_every_entry(db):
    """一批多款一次写入：逐款都落 current，价格正确。"""
    w = DbWriter()
    results = await w.upsert_task_batch(
        [
            (_game(APPID_A, "甲"), _prices(APPID_A, 10000, 20000, 50)),
            (_game(APPID_B, "乙"), _prices(APPID_B, 3000, 5000, 40)),
        ]
    )
    assert results == [True, True]
    assert await _history_count(db, APPID_A) == 1
    assert await _history_count(db, APPID_B) == 1
    async with db() as session:
        a = await session.get(GameCurrentPrice, (APPID_A, "CN"))
        b = await session.get(GameCurrentPrice, (APPID_B, "CN"))
    assert a is not None and a.price == 10000
    assert b is not None and b.price == 3000


@pytest.mark.asyncio
async def test_batch_has_no_low_entry_cap(db):
    """单批条数只受调用方任务大小约束：300 款一批照写，没有 200 条上限。"""
    w = DbWriter()
    entries = [
        (_game(996_500 + i, f"批{i}"), _prices(996_500 + i, 1000 + i, 2000, 10))
        for i in range(300)
    ]
    results = await w.upsert_task_batch(entries)
    assert results == [True] * 300
    async with db() as session:
        row = await session.get(GameCurrentPrice, (996_500 + 299, "CN"))
    assert row is not None and row.price == 1299


@pytest.mark.asyncio
async def test_batch_delta_gate_skips_unchanged(db):
    """批量通道同样受差量门禁：同价三连写只落 1 行 history。"""
    w = DbWriter()
    for _ in range(3):
        results = await w.upsert_task_batch(
            [(_game(APPID_A), _prices(APPID_A, 10000, 20000, 50))]
        )
        assert results == [True]
    assert await _history_count(db, APPID_A) == 1


@pytest.mark.asyncio
async def test_batch_change_produces_new_snapshot(db):
    """批量通道的 prev 基线取自同批预载：变价照常产生新快照。"""
    w = DbWriter()
    await w.upsert_task_batch([(_game(APPID_A), _prices(APPID_A, 10000, 20000, 50))])
    results = await w.upsert_task_batch(
        [(_game(APPID_A), _prices(APPID_A, 8000, 20000, 60))]
    )
    assert results == [True]
    assert await _history_count(db, APPID_A) == 2
