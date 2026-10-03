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


# ── 标准版选择（_plan_price_rows 纯函数直测） ────────────────────────────


def _opt(appid, sub, price, suffix=None, *, is_bundle=False, status="ok", currency="CNY"):
    return {
        "appid": appid, "region_code": "CN", "currency": currency,
        "price": price, "original_price": price, "discount_percent": 0,
        "sub_id": sub, "is_gold": False, "version_suffix": suffix,
        "is_bundle": is_bundle, "price_status": status,
    }


def _plan(w, rows):
    return w._plan_price_rows(rows, ({}, {}), datetime.now(), None)


def _cn_row(current):
    return next(r for r in current if r["region_code"] == "CN")


def test_plan_named_standard_edition_is_standard_candidate():
    """显式命名的 Standard Edition 是本体官方命名：归一化为标准版候选，
    与无名本体同台按 min(sub_id) 竞争——不再被当版本款排除。"""
    w = DbWriter()
    rows = [
        _opt(APPID_A, 200, 30000),
        _opt(APPID_A, 100, 10900, "Standard Edition"),
    ]
    current, _, ok, _ = _plan(w, rows)
    assert _cn_row(current)["sub_id"] == 100
    assert _cn_row(current)["price"] == 10900
    assert ok == {"CN"}


def test_plan_standard_edition_four_pack_stays_non_standard():
    """带附加词的礼包 SKU（Four Pack）不归一：仍是版本款，不进候选。"""
    w = DbWriter()
    rows = [
        _opt(APPID_A, 500, 12000),
        _opt(APPID_A, 100, 40000, "Standard Edition Four Pack"),
    ]
    current, _, _, _ = _plan(w, rows)
    assert _cn_row(current)["sub_id"] == 500


def test_plan_all_suffixed_fallback_is_order_independent():
    """该区全部选项带真版本后缀（无候选）：兜底取有价行里 sub_id 最小者，
    与响应顺序无关——曾经按响应序取末行，同一 appid 各区混装不同版本。"""
    w = DbWriter()
    deluxe = _opt(APPID_A, 2000000, 30000, "Deluxe Edition")
    starter = _opt(APPID_A, 1567580, 49000, "Starter Edition")
    for rows in ([starter, deluxe], [deluxe, starter]):
        current, _, _, _ = _plan(w, rows)
        assert _cn_row(current)["sub_id"] == 1567580
        assert _cn_row(current)["price"] == 49000


def test_plan_bundle_never_enters_fallback_pick():
    """捆绑包选项绝不落 current：有价非捆绑行缺席时落状态行（末行）。"""
    w = DbWriter()
    rows = [
        _opt(APPID_A, 300, 20000, is_bundle=True),
        _opt(APPID_A, 0, None, status="locked"),
    ]
    current, _, _, _ = _plan(w, rows)
    row = _cn_row(current)
    assert row["price_status"] == "locked" and row["price"] is None


def test_plan_status_only_region_picks_lowest_sub():
    """纯降级区（missing/locked，无价）走第一轮候选选择：取最小 sub 的
    状态行（候选判据不查状态，兜底分支只服务全后缀区域）。"""
    w = DbWriter()
    rows = [
        _opt(APPID_A, 100, None, status="locked"),
        _opt(APPID_A, 200, None, status="missing"),
    ]
    current, _, ok, _ = _plan(w, rows)
    row = _cn_row(current)
    assert row["sub_id"] == 100 and row["price_status"] == "locked"
    assert ok == set()
