"""游戏列表项的 priceData（价格数据状态）测试。

覆盖列表接口下发的三件事：价格观察时间 / 新鲜度（对象级）、覆盖（活表
尝试状态现算：分母 = 启用区服，成功观察 = 拿到 Steam 明确答复）、以及
失败保留旧价的展示语义。
隔离：tmp 库 + get_session_factory 打桩，关注集打桩为空，不触生产库。
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
from app.domains.games.models import Game, GameCurrentPrice

REGIONS = ["cn", "ru", "kz", "ua", "in"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    # 域模块都是模块级 import 的 factory——漏一个就会读到生产库
    monkeypatch.setattr(games_service, "get_session_factory", lambda: factory)

    async def _no_follows():
        return []

    monkeypatch.setattr(games_service, "_followed_appids", _no_follows)

    # 覆盖分母 = 启用区服（games service 模块级 from-import，桩它的名字空间）
    async def _regions(regions=None):
        return list(REGIONS)

    monkeypatch.setattr(games_service, "effective_regions", _regions)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _row(appid, region, status, when, price=1000, outcome=None, answer=None,
         last_success=None):
    """一条当前价行；status=ok 给价，其余状态只带状态。观察三元组缺省时
    按 status 推导（与启动期迁移回填同口径）。"""
    return GameCurrentPrice(
        appid=appid,
        region_code=region.upper(),
        currency="CNY" if region == "cn" else "USD",
        price=price if status == "ok" else None,
        cny_fen=price if status == "ok" else None,
        price_status=status,
        attempt_outcome=(
            outcome if outcome is not None
            else ("success" if status in ("ok", "locked") else "failed")
        ),
        steam_answer=(
            answer if answer is not None
            else {"ok": "ok", "locked": "locked"}.get(status)
        ),
        last_success_at=(
            last_success if last_success is not None
            else (when if status in ("ok", "locked") else None)
        ),
        updated_at=when,
    )


async def _seed(db, appids):
    now = datetime.now()
    async with db() as session:
        for appid in appids:
            session.add(
                Game(
                    appid=appid,
                    name=f"g{appid}",
                    created_at=now,
                    updated_at=now - timedelta(days=30),
                )
            )
        await session.commit()


async def _add_prices(db, rows):
    async with db() as session:
        for row in rows:
            session.add(row)
        await session.commit()


async def _items(appids, region=""):
    """默认列表是国区 INNER JOIN——没有国区价行的对象只在 LOCKED 视图里出现。"""
    result = await games_service.list_games(limit=50, region=region)
    by_appid = {item["appid"]: item for item in result["items"]}
    return [by_appid[a] for a in appids if a in by_appid]


async def _item(appid):
    items = await _items([appid])
    assert items, f"appid {appid} 不在列表里"
    return items[0]


@pytest.mark.asyncio
async def test_price_data_is_a_nested_structure(db):
    """契约：价格数据状态挂在 priceData 下，不平铺进列表项。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [_row(880001, "cn", "ok", now - timedelta(hours=1))])

    item = await _item(880001)
    assert set(item["priceData"]) == {"observedAt", "ageHours", "freshness", "coverage"}
    # 报价字段仍在顶层，没有被 priceData 收编
    assert "basePriceFen" in item and "priceMatrix" in item


@pytest.mark.asyncio
async def test_freshness_buckets_are_object_level(db):
    """对象级三档：取该对象全部价格行的最后观察时刻。"""
    now = datetime.now()
    await _seed(db, [880001, 880002, 880005, 880006])
    await _add_prices(db, [
        # 2h 前 / 8h 前 / 20h 前 / 全无
        _row(880001, "cn", "ok", now - timedelta(hours=2)),
        _row(880002, "cn", "ok", now - timedelta(hours=8)),
        _row(880005, "cn", "ok", now - timedelta(hours=20)),
    ])

    assert (await _item(880001))["priceData"]["freshness"] == "fresh"
    assert (await _item(880002))["priceData"]["freshness"] == "lagging"
    assert (await _item(880005))["priceData"]["freshness"] == "stale"
    # 一次都没抓过的对象（无国区价行）只在 LOCKED 视图里出现；
    # 覆盖如实给「全未尝试」（活表无行 = 没试过，不再用 None 掩盖）
    never = (await _items([880006], region="LOCKED"))[0]["priceData"]
    assert never["observedAt"] is None and never["freshness"] is None
    assert never["coverage"]["notAttempted"] == 5
    assert never["coverage"]["coverage"] == 0.0


@pytest.mark.asyncio
async def test_observed_at_is_max_over_regions(db):
    """观察时刻取全区最大 updated_at：单区新刷新就代表这个对象新。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=9)),
        _row(880001, "ru", "ok", now - timedelta(hours=1)),
    ])

    data = (await _item(880001))["priceData"]
    assert data["freshness"] == "fresh"
    assert data["ageHours"] == pytest.approx(1.0, abs=0.1)


@pytest.mark.asyncio
async def test_updated_at_semantics_unchanged(db):
    """updatedAt 仍是实体更新时间：与价格观察时间是两回事，不能互相顶替。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [_row(880001, "cn", "ok", now - timedelta(hours=1))])

    item = await _item(880001)
    assert item["updatedAt"] is not None
    assert item["updatedAt"].startswith((now - timedelta(days=30)).isoformat()[:10])
    assert not item["priceData"]["observedAt"].startswith(
        (now - timedelta(days=30)).isoformat()[:10]
    )


# ── 覆盖（活表尝试状态现算）──


@pytest.mark.asyncio
async def test_coverage_counts_success_observations(db):
    """覆盖 = 成功观察区数 / 启用区数：locked 也是成功观察（拿到明确答复）。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=1)),
        _row(880001, "ru", "ok", now - timedelta(hours=1)),
        _row(880001, "kz", "locked", now - timedelta(hours=1)),
        # 传输失败区：行在，attempt=failed
        _row(880001, "ua", "missing", now - timedelta(hours=1),
             outcome="failed"),
    ])

    cov = (await _item(880001))["priceData"]["coverage"]
    assert cov["expectedUnits"] == 5
    assert cov["success"] == 3
    assert cov["failed"] == 1
    assert cov["notAttempted"] == 1
    assert cov["coverage"] == 0.6
    assert cov["regions"]["UA"]["outcome"] == "failed"
    assert cov["regions"]["IN"]["outcome"] == "notAttempted"
    assert "kz" not in cov["regions"], "成功观察区不进问题点名"


@pytest.mark.asyncio
async def test_failed_attempt_keeps_last_good_price(db):
    """本次失败不推翻曾经拿到：矩阵保留旧价并带 stale 标记，
    覆盖按失败计，lastSuccessAt 供「展示的是 X 时刻的数据」。"""
    now = datetime.now()
    good_at = now - timedelta(hours=4)
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=1)),
        # ru：4h 前成功拿到 1200 分，1h 前传输失败——行上保价
        GameCurrentPrice(
            appid=880001,
            region_code="RU",
            currency="USD",
            price=1200,
            cny_fen=8600,
            price_status="missing",
            attempt_outcome="failed",
            steam_answer=None,
            last_success_at=good_at,
            updated_at=now - timedelta(hours=1),
        ),
    ])

    item = await _item(880001)
    assert item["priceMatrix"]["RU"][4] is True, "旧价带 stale 标记"
    assert item["priceMatrix"]["RU"][2] == 1200
    assert "RU" in item["unavailableRegions"]
    cov = item["priceData"]["coverage"]
    assert cov["regions"]["RU"]["outcome"] == "failed"
    assert cov["regions"]["RU"]["lastSuccessAt"] == good_at.isoformat()


@pytest.mark.asyncio
async def test_success_attempt_clears_stale(db):
    """重新观察成功：stale 消失，覆盖回到成功侧。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=1)),
        _row(880001, "ru", "ok", now - timedelta(minutes=5)),
    ])

    item = await _item(880001)
    assert item["priceMatrix"]["RU"][4] is False
    cov = item["priceData"]["coverage"]
    assert cov["failed"] == 0 and cov["notAttempted"] == 3


@pytest.mark.asyncio
async def test_full_coverage_reaches_one(db):
    """全部启用区都有成功观察 → 覆盖 1.0，无问题点名。"""
    now = datetime.now()
    await _seed(db, [880002])
    await _add_prices(db, [
        _row(880002, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ])

    cov = (await _item(880002))["priceData"]["coverage"]
    assert cov["coverage"] == 1.0
    assert cov["regions"] == {}
