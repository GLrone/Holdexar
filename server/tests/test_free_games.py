"""免费游戏适配行为验收：解析分类 / 写库标记 / 自动脱池 / 喜加一展示链。

- evaluate 必须在 is_free 之前识别限时赠送（赠送中条目 is_free=true 且
  is_free_temporarily=true——先看 is_free 会把赠送误判成永久免费）；
- parse_options 保留 0 价（price/original 不再置 None）：赠送行 price=0 +
  original>0 能进写库层，永久免费 price=0 + original=0 不进 history；
- games.free_kind/promo_end_at 由写库层按价格行维护：付费价出现即清除，
  纯 locked/missing 响应（无 ok 行）不动标记；
- release_free_games：爬取收尾把 f2p 移出监控池（excluded 挡复活），promo
  不脱（赠送结束前要持续跟踪）；
- steam_free_offers：只出未过期的 promo（仪表盘「无赠送即整块隐藏」）。

隔离：tmp 库 + get_session_factory 打桩（test_history_delta_gate 模式）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.crawler import browse_store as bs
from app.crawler.db_writer import DbWriter, _classify_free_kind
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

APPID = 997_001
# 截止时刻相对「现在」取未来值：服务端按 promo_end_at > now 过滤，
# 写死绝对时刻必然随真实日期滑入过去
PROMO_END = int(__import__("time").time()) + 86_400


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    import app.crawler.db_writer as dw
    import app.domains.metadata.service as metadata_service
    import app.domains.wishlist.service as wishlist_service

    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    monkeypatch.setattr(metadata_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wishlist_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.monitoring.models  # noqa: F401（release 流程查 monitor_sources）
    import app.domains.rates.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── 解析层：evaluate 分类 + parse_options 0 价保留 ──────────────────────


def _promo_item() -> dict:
    """赠送中条目形态：is_free 与 is_free_temporarily 同时为 true。"""
    return {
        "appid": 2000040, "success": 1, "visible": True, "name": "Space Menace",
        "is_free": True, "is_free_temporarily": True,
        "best_purchase_option": {
            "packageid": 1827145, "final_price_in_cents": "0",
            "original_price_in_cents": "2900", "discount_pct": 100,
            "is_free_to_keep": True, "free_to_keep_ends": PROMO_END,
        },
        "purchase_options": [
            {"packageid": 1827145, "final_price_in_cents": "0",
             "original_price_in_cents": "2900", "discount_pct": 100},
            {"packageid": 722126, "final_price_in_cents": "2900"},
        ],
    }


def test_evaluate_classifies_free_promo_before_is_free() -> None:
    status, opts = bs.StoreBrowseAPI.evaluate(_promo_item())
    assert status == "free_promo"
    assert opts is not None
    promo = [o for o in opts if o["price_cents"] == 0]
    assert promo and promo[0]["original_cents"] == 2900
    assert promo[0]["discount_pct"] == 100
    assert promo[0]["free_to_keep_ends"] == PROMO_END


def test_evaluate_permanent_free_stays_free() -> None:
    item = {"appid": 570, "success": 1, "visible": True, "name": "Dota 2", "is_free": True}
    assert bs.StoreBrowseAPI.evaluate(item) == ("free", None)


def test_parse_options_keeps_zero_price() -> None:
    item = {
        "appid": 1, "success": 1, "visible": True, "name": "X",
        "purchase_options": [
            {"packageid": 10, "final_price_in_cents": "0",
             "original_price_in_cents": "2900", "discount_pct": 100},
        ],
    }
    (opt,) = bs.StoreBrowseAPI.parse_options(item)
    assert opt["price_cents"] == 0 and opt["original_cents"] == 2900


# ── 分类函数 ────────────────────────────────────────────────────────────


def _row(region: str, price: int, original: int, status: str = "ok", **extra) -> dict:
    return {
        "appid": APPID, "region_code": region, "currency": "CNY",
        "price": price, "original_price": original, "discount_percent": 100,
        "sub_id": 111, "is_gold": False, "version_suffix": None,
        "is_bundle": False, "price_status": status, **extra,
    }


def test_classify_promo_from_cn_row() -> None:
    rows = [_row("CN", 0, 2900, promo_end_ts=PROMO_END), _row("RU", 0, 4900)]
    assert _classify_free_kind(rows) == ("promo", PROMO_END)


def test_classify_f2p_and_paid_clear() -> None:
    assert _classify_free_kind([_row("CN", 0, 0)]) == ("f2p", None)
    assert _classify_free_kind([_row("CN", 6800, 6800)]) == (None, None)


def test_classify_no_ok_rows_is_no_signal() -> None:
    """纯 locked/missing 响应（无 ok 行）→ None：不得洗掉库内免费态。"""
    rows = [{"appid": APPID, "region_code": "CN", "currency": "CNY", "price": None,
             "original_price": None, "discount_percent": 0, "sub_id": 0,
             "is_gold": False, "version_suffix": None, "is_bundle": False,
             "price_status": "locked"}]
    assert _classify_free_kind(rows) is None


def test_classify_falls_back_off_cn() -> None:
    """CN 锁区时回退任意区 promo 行（分类以 CN 优先但不强求）。"""
    rows = [
        {"appid": APPID, "region_code": "CN", "currency": "CNY", "price": None,
         "original_price": None, "discount_percent": 0, "sub_id": 0,
         "is_gold": False, "version_suffix": None, "is_bundle": False,
         "price_status": "locked"},
        _row("RU", 0, 4900, promo_end_ts=PROMO_END),
    ]
    assert _classify_free_kind(rows) == ("promo", PROMO_END)


def test_classify_paid_off_cn_is_no_signal() -> None:
    """区服促销不同步护栏：非 CN 任务看到本区正价不得清 CN 的 promo 标。"""
    assert _classify_free_kind([_row("RU", 4900, 4900)]) is None
    # CN 行在场的付费判定才允许清标记
    assert _classify_free_kind([_row("CN", 2900, 2900)]) == (None, None)


# ── 写库层：free_kind 维护 + 0 价 cny_fen + 历史门 ───────────────────────


def _game():
    return {"appid": APPID, "name": "免费测试", "updated_at": datetime.now()}


async def _game_row(db):
    async with db() as session:
        return await session.get(Game, APPID)


async def _current_row(db, region="CN"):
    async with db() as session:
        return await session.get(GameCurrentPrice, (APPID, region))


async def _history_count(db) -> int:
    async with db() as session:
        return len((await session.execute(
            select(GamePriceHistory.id).where(GamePriceHistory.appid == APPID)
        )).all())


@pytest.mark.asyncio
async def test_upsert_permanent_free_marks_f2p(db):
    w = DbWriter()
    rows = [{**_row("CN", 0, 0), "crawled_at": datetime.now()}]
    assert await w.upsert_game_and_prices(_game(), rows) is True
    g = await _game_row(db)
    assert g.free_kind == "f2p" and g.promo_end_at is None
    cp = await _current_row(db)
    assert cp.price == 0 and cp.cny_fen == 0 and cp.price_status == "ok"
    assert await _history_count(db) == 0, "永久免费不进 history"


@pytest.mark.asyncio
async def test_upsert_promo_marks_and_writes_history(db):
    w = DbWriter()
    rows = [{**_row("CN", 0, 2900, promo_end_ts=PROMO_END), "crawled_at": datetime.now()}]
    await w.upsert_game_and_prices(_game(), rows)
    g = await _game_row(db)
    assert g.free_kind == "promo" and g.promo_end_at == PROMO_END
    cp = await _current_row(db)
    assert cp.price == 0 and cp.original_price == 2900 and cp.cny_fen == 0
    assert await _history_count(db) == 1, "赠送降到 0 是真实价格事件"
    # 同形态重爬：差量门禁不产生冗余快照
    await w.upsert_game_and_prices(_game(), rows)
    assert await _history_count(db) == 1


@pytest.mark.asyncio
async def test_upsert_paid_clears_promo_mark(db):
    w = DbWriter()
    promo_row = {**_row("CN", 0, 2900, promo_end_ts=PROMO_END), "crawled_at": datetime.now()}
    await w.upsert_game_and_prices(_game(), [promo_row])
    paid_row = {**_row("CN", 2900, 2900), "crawled_at": datetime.now()}
    await w.upsert_game_and_prices(_game(), [paid_row])
    g = await _game_row(db)
    assert g.free_kind is None and g.promo_end_at is None


@pytest.mark.asyncio
async def test_upsert_locked_only_keeps_mark(db):
    """赠送期内某区断网记 locked → 无 ok 行 → 不洗掉 promo 标。"""
    w = DbWriter()
    promo_row = {**_row("CN", 0, 2900, promo_end_ts=PROMO_END), "crawled_at": datetime.now()}
    await w.upsert_game_and_prices(_game(), [promo_row])
    locked_row = {"appid": APPID, "region_code": "CN", "currency": "CNY",
                  "price": None, "original_price": None, "discount_percent": 0,
                  "sub_id": 0, "is_gold": False, "version_suffix": None,
                  "is_bundle": False, "price_status": "locked",
                  "crawled_at": datetime.now()}
    await w.upsert_game_and_prices(_game(), [locked_row])
    g = await _game_row(db)
    assert g.free_kind == "promo" and g.promo_end_at == PROMO_END


# ── 自动脱池 + 展示链 ───────────────────────────────────────────────────


async def _seed_pool(db, appid: int, free_kind: str | None) -> None:
    from app.domains.wishlist.models import WishlistItem

    async with db() as session:
        session.add(Game(appid=appid, name="x", free_kind=free_kind))
        session.add(WishlistItem(steamid="76500000000000001", appid=appid, active=True))
        await session.commit()


@pytest.mark.asyncio
async def test_release_free_games_releases_f2p_only(db):
    from app.domains.wishlist.models import WishlistItem
    from app.domains.wishlist import service as wishlist_service

    await _seed_pool(db, 997_010, "f2p")
    await _seed_pool(db, 997_011, "promo")
    released = await wishlist_service.release_free_games([997_010, 997_011])
    assert released == 1
    async with db() as session:
        f2p_row = await session.get(WishlistItem, ("76500000000000001", 997_010))
        promo_row = await session.get(WishlistItem, ("76500000000000001", 997_011))
        assert f2p_row.active is False and f2p_row.excluded is True
        assert promo_row.active is True and promo_row.excluded is False


@pytest.mark.asyncio
async def test_steam_free_offers_hides_expired(db):
    import time

    from app.domains.metadata import service as metadata_service

    await _seed_pool(db, 997_020, "promo")
    async with db() as session:
        g = await session.get(Game, 997_020)
        g.promo_end_at = PROMO_END  # 未来时刻
        await session.commit()
    out = await metadata_service.steam_free_offers()
    assert out["ok"] is True
    assert [o["appid"] for o in out["offers"]] == [997_020]
    assert out["offers"][0]["endTs"] == PROMO_END

    # 过期 → 不出（模块隐藏）；标记清除后也不出
    async with db() as session:
        g = await session.get(Game, 997_020)
        g.promo_end_at = int(time.time()) - 3600
        await session.commit()
    out = await metadata_service.steam_free_offers()
    assert out["offers"] == []
