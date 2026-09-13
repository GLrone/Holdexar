"""games 域永降标记测试：refresh_pp_flags 的原价台阶检测。

判定语义：当前国区标准版原价 vs 历史上最近一次不同的原价 → 1=永降 2=永涨。
打折只动折后价不动原价 → 不触发；标准版多 sub 择优时换 sub 不误报。
库隔离到临时 sqlite，不触真实网络。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

NOW = datetime.now()


def _current(appid: int, original: int, *, sub_id: int = 0, discount: int = 0, price: int | None = None):
    return GameCurrentPrice(
        appid=appid, region_code="CN", currency="CNY",
        price=price if price is not None else original,
        original_price=original, discount_percent=discount,
        sub_id=sub_id, price_status="ok", updated_at=NOW,
    )


def _history(appid: int, original: int, *, days_ago: float, sub_id: int = 0, price: int | None = None):
    return GamePriceHistory(
        appid=appid, region_code="CN", currency="CNY",
        price=price if price is not None else original,
        original_price=original, discount_percent=0,
        sub_id=sub_id, is_gold=False, version_suffix=None,
        price_status="ok", snapshot_at=NOW - timedelta(days=days_ago),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(games_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.games.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _flag(db, appid: int) -> int:
    async with db() as session:
        row = await session.get(Game, appid)
        return row.pp_flag


async def _changed_at(db, appid: int):
    async with db() as session:
        row = await session.get(Game, appid)
        return row.pp_changed_at


@pytest.mark.asyncio
async def test_pp_flag_drop_and_raise(db):
    """100: 10000→8000 永降；200: 6000→9000 永涨；300: 原价恒定 → 0。"""
    async with db() as session:
        session.add_all([Game(appid=a, name=str(a)) for a in (100, 200, 300)])
        session.add_all([
            _current(100, 8000),
            _history(100, 10000, days_ago=3),
            _history(100, 8000, days_ago=1),
            _current(200, 9000),
            _history(200, 6000, days_ago=3),
            _history(200, 9000, days_ago=1),
            _current(300, 5000),
            _history(300, 5000, days_ago=1),
        ])
        await session.commit()

    refreshed = await games_service.refresh_pp_flags([100, 200, 300])
    assert refreshed == 3
    assert await _flag(db, 100) == 1
    assert await _flag(db, 200) == 2
    assert await _flag(db, 300) == 0


@pytest.mark.asyncio
async def test_pp_flag_ignores_discount_price_moves(db):
    """打折期间折后价怎么变都不算调价：原价恒 7000 → 0。"""
    async with db() as session:
        session.add_all([Game(appid=400, name="400")])
        session.add_all([
            _current(400, 7000, discount=30, price=4900),
            _history(400, 7000, days_ago=3, price=7000),
            _history(400, 7000, days_ago=1, price=4900),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([400])
    assert await _flag(db, 400) == 0


@pytest.mark.asyncio
async def test_pp_flag_sub_id_isolation(db):
    """当前挂真实 sub(77)：只比同 sub 历史——异 sub(99) 的 3000 不算台阶。

    若不做 sub 隔离，最近的不同原价会取到 sub99 的 3000 → 误判永涨。
    """
    async with db() as session:
        session.add_all([Game(appid=500, name="500")])
        session.add_all([
            _current(500, 8000, sub_id=77),
            _history(500, 10000, days_ago=2, sub_id=77),
            _history(500, 8000, days_ago=1, sub_id=77),
            _history(500, 3000, days_ago=1, sub_id=99),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([500])
    assert await _flag(db, 500) == 1


@pytest.mark.asyncio
async def test_pp_flag_incremental_only_touches_targets(db):
    """增量只动目标集合：600 有永降但不在 appids → 保持 0。"""
    async with db() as session:
        session.add_all([Game(appid=a, name=str(a)) for a in (600, 700)])
        session.add_all([
            _current(600, 8000),
            _history(600, 10000, days_ago=1),
            _current(700, 9000),
            _history(700, 6000, days_ago=1),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([700])
    assert await _flag(db, 600) == 0
    assert await _flag(db, 700) == 2


@pytest.mark.asyncio
async def test_pp_changed_at_is_latest_jump_snapshot(db):
    """pp_changed_at = 最近一次原价跳变的快照时刻：10000(5天前)→8000(2天前)
    →8000(1天前) 的跳变发生在 2 天前那条，与最后一条快照(1天前)区分开。"""
    jump_at = NOW - timedelta(days=2)
    async with db() as session:
        session.add_all([Game(appid=800, name="800")])
        session.add_all([
            _current(800, 8000),
            _history(800, 10000, days_ago=5),
            _history(800, 8000, days_ago=2),
            _history(800, 8000, days_ago=1),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([800])
    assert await _flag(db, 800) == 1
    changed = await _changed_at(db, 800)
    assert changed is not None and abs((changed - jump_at).total_seconds()) < 1


@pytest.mark.asyncio
async def test_pp_changed_at_none_when_price_never_changed(db):
    """原价恒定 → flag=0 且无跳变时刻（前端据此不展示徽章）。"""
    async with db() as session:
        session.add_all([Game(appid=900, name="900")])
        session.add_all([
            _current(900, 5000),
            _history(900, 5000, days_ago=3),
            _history(900, 5000, days_ago=1),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([900])
    assert await _flag(db, 900) == 0
    assert await _changed_at(db, 900) is None


@pytest.mark.asyncio
async def test_pp_changed_at_ignores_discount_price_moves(db):
    """打折只动折后价：折后价连续变化不产生原价跳变时刻。"""
    async with db() as session:
        session.add_all([Game(appid=1000, name="1000")])
        session.add_all([
            _current(1000, 7000, discount=30, price=4900),
            _history(1000, 7000, days_ago=3, price=7000),
            _history(1000, 7000, days_ago=2, price=5600),
            _history(1000, 7000, days_ago=1, price=4900),
        ])
        await session.commit()

    await games_service.refresh_pp_flags([1000])
    assert await _flag(db, 1000) == 0
    assert await _changed_at(db, 1000) is None
