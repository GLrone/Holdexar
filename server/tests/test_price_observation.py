"""价格尝试观察（attempt observation）写侧语义测试。

每个 (appid, 区) 的最近一次抓取结果必须落在唯一写面（db_writer）：
- 成功观察（含 locked / 无选项）：attempt_outcome=success + last_success_at 推进
- 传输失败：attempt_outcome=failed，行上保留上一次成功价与 last_success_at
- 连续穷尽转 blocked：仍是失败态，价照旧保留
隔离：tmp 库 + get_session_factory 打桩，不出网不触生产库。
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
from app.crawler.db_writer import DbWriter
from app.domains.games.models import Game, GameCurrentPrice

APPID = 760100


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    import app.crawler.db_writer as db_writer_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(db_writer_module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    # 批量写路径要查 steam_events（活动日历快照标签）——模型须注册建表
    import app.domains.steam_events.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _price_row(region: str, status: str, *, price: int | None = 1000) -> dict:
    """browse 形状的现价行（steam_answer 由 browse 归一传入）。"""
    return {
        "appid": APPID,
        "region_code": region.upper(),
        "currency": "CNY" if region.lower() == "cn" else "USD",
        "price": price,
        "original_price": (price * 2) if price is not None else None,
        "discount_percent": 0,
        "sub_id": 0,
        "is_gold": False,
        "version_suffix": None,
        "is_bundle": False,
        "price_status": status,
        "steam_answer": {"ok": "ok", "locked": "locked"}.get(status, "no_options"),
        "crawled_at": datetime.now(),
    }


async def _get_row(db) -> GameCurrentPrice | None:
    async with db() as session:
        return (
            await session.execute(
                select(GameCurrentPrice).where(
                    GameCurrentPrice.appid == APPID,
                    GameCurrentPrice.region_code == "CN",
                )
            )
        ).scalars().first()


@pytest.mark.asyncio
async def test_success_upsert_stamps_observation(db):
    """成功观察：outcome=success + answer + last_success_at 落行。"""
    async with db() as session:
        session.add(Game(appid=APPID, name="g", created_at=datetime.now(),
                         updated_at=datetime.now()))
        await session.commit()

    writer = DbWriter()
    await writer.connect()
    await writer.upsert_task_batch([
        ({"appid": APPID, "name": "g", "type": "game"},
         [_price_row("cn", "ok")]),
    ])

    row = await _get_row(db)
    assert row is not None
    assert row.attempt_outcome == "success"
    assert row.steam_answer == "ok"
    assert row.last_success_at is not None
    assert row.price == 1000


@pytest.mark.asyncio
async def test_locked_is_success_observation_without_price(db):
    """locked = 成功观察（Steam 明确答复该区不售），answer=locked 无价。"""
    async with db() as session:
        session.add(Game(appid=APPID, name="g", created_at=datetime.now(),
                         updated_at=datetime.now()))
        await session.commit()

    writer = DbWriter()
    await writer.connect()
    await writer.upsert_task_batch([
        ({"appid": APPID, "name": "g", "type": "game"},
         [_price_row("cn", "locked", price=None)]),
    ])

    row = await _get_row(db)
    assert row is not None
    assert row.attempt_outcome == "success"
    assert row.steam_answer == "locked"
    assert row.price is None


@pytest.mark.asyncio
async def test_failed_attempt_keeps_price_and_last_success(db):
    """传输失败：outcome=failed，旧价与 last_success_at 原样保留。"""
    now = datetime.now()
    good_at = now - timedelta(hours=4)
    async with db() as session:
        session.add(Game(appid=APPID, name="g", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=APPID, region_code="CN", currency="CNY",
            price=1000, original_price=2000, cny_fen=1000,
            price_status="ok", attempt_outcome="success",
            steam_answer="ok", last_success_at=good_at,
            updated_at=now - timedelta(hours=4),
        ))
        await session.commit()

    writer = DbWriter()
    await writer.connect()
    await writer.mark_region_status(APPID, "CN", "missing")

    row = await _get_row(db)
    assert row is not None
    assert row.attempt_outcome == "failed"
    assert row.steam_answer is None
    assert row.price == 1000, "失败不抹上次成功价"
    assert row.original_price == 2000
    assert row.cny_fen == 1000
    assert row.last_success_at == good_at
    assert row.price_status == "missing"
    assert row.fail_count == 1


@pytest.mark.asyncio
async def test_exhausted_failures_turn_blocked_and_keep_price(db):
    """连败穷尽转 blocked 终态：仍是失败态，价照旧保留。"""
    now = datetime.now()
    good_at = now - timedelta(hours=8)
    async with db() as session:
        session.add(Game(appid=APPID, name="g", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=APPID, region_code="CN", currency="CNY",
            price=1000, cny_fen=1000, price_status="missing",
            attempt_outcome="failed", last_success_at=good_at,
            fail_count=4, updated_at=now - timedelta(hours=1),
        ))
        await session.commit()

    writer = DbWriter()
    await writer.connect()
    await writer.mark_region_status(APPID, "CN", "missing")

    row = await _get_row(db)
    assert row is not None
    assert row.price_status == "blocked"
    assert row.attempt_outcome == "failed"
    assert row.price == 1000
    assert row.last_success_at == good_at


@pytest.mark.asyncio
async def test_recovery_after_failure_flips_to_success(db):
    """失败后补抓成功：outcome 回 success，last_success_at 推进到本次。"""
    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=APPID, name="g", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=APPID, region_code="CN", currency="CNY",
            price=None, price_status="missing", attempt_outcome="failed",
            fail_count=2, updated_at=now - timedelta(hours=1),
        ))
        await session.commit()

    writer = DbWriter()
    await writer.connect()
    await writer.upsert_task_batch([
        ({"appid": APPID, "name": "g", "type": "game"},
         [_price_row("cn", "ok", price=1500)]),
    ])

    row = await _get_row(db)
    assert row is not None
    assert row.attempt_outcome == "success"
    assert row.last_success_at is not None
    assert row.price == 1500
    assert row.fail_count == 0


@pytest.mark.asyncio
async def test_latest_observation_follows_success_only(db, monkeypatch):
    """全库时钟（灵动岛数据年龄事实源）：随成功观察推进，传输失败不回退。

    任何触发方（自动轮 / 手动 / 补抓）写的是同一张活表同一枚观察章，
    时钟因此对所有触发方可见；空库返回 None（「没有数据」≠「数据很旧」）。
    """
    import app.domains.crawl.freshness as freshness_mod

    monkeypatch.setattr(freshness_mod, "get_session_factory", lambda: db)

    assert await freshness_mod.latest_price_observation() is None

    writer = DbWriter()
    await writer.connect()
    await writer.upsert_task_batch([
        ({"appid": APPID, "name": "g", "type": "game"},
         [_price_row("cn", "ok")]),
    ])
    first = await freshness_mod.latest_price_observation()
    assert first is not None

    await writer.mark_region_status(APPID, "CN", "missing")
    assert await freshness_mod.latest_price_observation() == first, "失败不回退时钟"
