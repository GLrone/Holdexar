"""历史差量门禁行为验收（db_writer.upsert_game_and_prices）。

全池 6h 监控的存储代价护栏：价格未变的行不写 game_price_history 新快照
（否则 ~27 万冗余行/天）；价格/原价/折扣/版本后缀任一变化才产生新点。
现价表（game_current_prices）不受门禁影响，每轮照常刷新。

隔离：tmp 库 + get_session_factory 打桩（test_price_repair_schedule 模式）。
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
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

APPID = 996_001


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
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _prices(price: int, original: int, discount: int, suffix: str | None = None):
    return [
        {
            "appid": APPID, "region_code": "CN", "currency": "CNY",
            "price": price, "original_price": original, "discount_percent": discount,
            "sub_id": 111, "is_gold": False, "version_suffix": suffix,
            "is_bundle": False, "price_status": "ok", "crawled_at": datetime.now(),
        }
    ]


async def _game_row(db):
    async with db() as session:
        return await session.get(Game, APPID)


async def _gcp_row(db):
    async with db() as session:
        return await session.get(GameCurrentPrice, (APPID, "CN"))


async def _history_count(db, sub_id: int | None = None) -> int:
    async with db() as session:
        stmt = select(func.count()).select_from(GamePriceHistory).where(
            GamePriceHistory.appid == APPID
        )
        if sub_id is not None:
            stmt = stmt.where(GamePriceHistory.sub_id == sub_id)
        return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_delta_gate_skips_unchanged_snapshot(db):
    """同价三连写：history 只落 1 行；current 行照常存在（每轮刷新语义不损）。"""
    w = DbWriter()
    for _ in range(3):
        game = {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()}
        assert await w.upsert_game_and_prices(game, _prices(10000, 20000, 50)) is True

    assert await _history_count(db) == 1, "价格未变不得累积冗余快照"
    gcp = await _gcp_row(db)
    assert gcp is not None and gcp.price == 10000 and gcp.price_status == "ok"
    assert (await _game_row(db)).name == "门禁测试"


@pytest.mark.asyncio
async def test_delta_gate_writes_on_price_change(db):
    """现价/原价/折扣/版本后缀任一变化 → 产生新快照（走势图语义保留）。"""
    w = DbWriter()
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(10000, 20000, 50),
    )
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(8000, 20000, 60),   # 现价变
    )
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(8000, 20000, 60),   # 未变
    )
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(8000, 18000, 60),   # 原价变
    )
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(8000, 18000, 56),   # 折扣变
    )
    await w.upsert_game_and_prices(
        {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()},
        _prices(8000, 18000, 56, suffix="Gold Edition"),
    )

    assert await _history_count(db) == 5, "每次实质变化恰好一行"
    gcp = await _gcp_row(db)
    assert gcp.price == 8000 and gcp.discount_percent == 56


@pytest.mark.asyncio
async def test_delta_gate_treats_new_sub_as_change(db):
    """同区出现新 sub（新版本选项）：无基线可比 → 照写，不误跳。"""
    w = DbWriter()
    game = {"appid": APPID, "name": "门禁测试", "updated_at": datetime.now()}
    await w.upsert_game_and_prices(game, _prices(10000, 20000, 50))
    second = _prices(15600, 15600, 0, suffix="Digital Deluxe Edition")
    second[0]["sub_id"] = 222
    await w.upsert_game_and_prices(game, second)

    assert await _history_count(db, sub_id=222) == 1
    assert await _history_count(db, sub_id=111) == 1
