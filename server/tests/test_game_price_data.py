"""游戏列表项的 priceData（价格数据状态）测试。

覆盖列表接口下发的三件事：价格观察时间 / 新鲜度（对象级）、本轮覆盖率
（Cycle 冻结期望集口径）、以及「没有 Cycle 归属就不给覆盖率」。
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
from app.domains.crawl import coverage as coverage_mod
from app.domains.crawl.cycle import PriceCycle
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice

REGIONS = ["cn", "ru", "kz", "ua", "in"]
IN_SCOPE = (880001, 880002)
OUT_OF_SCOPE = 880005


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
    monkeypatch.setattr(coverage_mod, "get_session_factory", lambda: factory)

    async def _no_follows():
        return []

    monkeypatch.setattr(games_service, "_followed_appids", _no_follows)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _row(appid, region, status, when, price=1000):
    """一条当前价行；status=ok 给价，其余状态只带状态。"""
    return GameCurrentPrice(
        appid=appid,
        region_code=region.upper(),
        currency="CNY" if region == "cn" else "USD",
        price=price if status == "ok" else None,
        cny_fen=price if status == "ok" else None,
        price_status=status,
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


async def _add_cycle(db, status, appids, regions, started_at, finished_at):
    async with db() as session:
        cycle = PriceCycle(
            kind="scheduled",
            status=status,
            scope="pool",
            expected_json={"appids": list(appids), "regions": list(regions)},
            started_at=started_at,
            finished_at=finished_at,
        )
        session.add(cycle)
        await session.commit()
        return cycle.id


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
async def test_partial_coverage_counts_each_bucket(db):
    """期望 5 区、拿到 3 区：覆盖率按 ok/期望单元给，missing 不算失败于 coverage。"""
    now = datetime.now()
    started = now - timedelta(hours=2)
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=1)),
        _row(880001, "ru", "ok", now - timedelta(hours=1)),
        _row(880001, "kz", "ok", now - timedelta(hours=1)),
        _row(880001, "ua", "missing", now - timedelta(hours=1)),
        # in 区没有任何行 → 未观察
    ])
    await _add_cycle(db, "partial", IN_SCOPE, REGIONS, started, now)

    cov = (await _item(880001))["priceData"]["coverage"]
    assert cov["expectedUnits"] == 5
    assert (cov["ok"], cov["locked"], cov["missing"], cov["blocked"]) == (3, 0, 1, 0)
    assert cov["unobserved"] == 1
    assert cov["coverage"] == 0.6
    assert cov["coverageConfirmed"] == 0.6
    assert cov["cycleStatus"] == "partial"


@pytest.mark.asyncio
async def test_full_coverage_reaches_one(db):
    now = datetime.now()
    await _seed(db, [880002])
    await _add_prices(db, [
        _row(880002, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ])
    await _add_cycle(db, "completed", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    cov = (await _item(880002))["priceData"]["coverage"]
    assert cov["ok"] == 5
    assert cov["unobserved"] == 0
    assert cov["coverage"] == 1.0
    assert cov["coverageConfirmed"] == 1.0


@pytest.mark.asyncio
async def test_locked_counts_as_confirmed_but_not_coverage(db):
    """锁区是 Steam 给了答复，不是拿到价格：只进 confirmed，不进 coverage。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, "cn", "ok", now - timedelta(hours=1)),
        _row(880001, "ru", "ok", now - timedelta(hours=1)),
        _row(880001, "kz", "ok", now - timedelta(hours=1)),
        _row(880001, "ua", "locked", now - timedelta(hours=1)),
        _row(880001, "in", "locked", now - timedelta(hours=1)),
    ])
    await _add_cycle(db, "partial", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    cov = (await _item(880001))["priceData"]["coverage"]
    assert cov["locked"] == 2
    assert cov["coverage"] == 0.6
    assert cov["coverageConfirmed"] == 1.0


@pytest.mark.asyncio
async def test_out_of_window_rows_count_as_no_result(db):
    """窗口外的行等于本轮没结果（上界让历史轮次数值不随之后刷新变大）。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, r, "ok", now - timedelta(hours=5)) for r in REGIONS
    ])
    await _add_cycle(db, "partial", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    cov = (await _item(880001))["priceData"]["coverage"]
    assert cov["ok"] == 0
    assert cov["unobserved"] == 5
    assert cov["coverage"] == 0.0
    # 但价格数据本身仍在：新鲜度是另一个维度
    assert (await _item(880001))["priceData"]["freshness"] == "fresh"


@pytest.mark.asyncio
async def test_non_monitored_object_has_no_coverage(db):
    """不属本轮期望集 = 没有 Cycle 归属：coverage 为 null，不冒充 100%。"""
    now = datetime.now()
    await _seed(db, [OUT_OF_SCOPE])
    await _add_prices(db, [
        _row(OUT_OF_SCOPE, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ])
    await _add_cycle(db, "completed", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    price_data = (await _item(OUT_OF_SCOPE))["priceData"]
    assert price_data["coverage"] is None
    assert price_data["freshness"] == "fresh"
    assert price_data["observedAt"] is not None


@pytest.mark.asyncio
async def test_monitored_object_in_mix_gets_coverage_and_others_do_not(db):
    """同页混合：命中期望集的有覆盖率，没命中的没有。"""
    now = datetime.now()
    await _seed(db, [880001, OUT_OF_SCOPE])
    await _add_prices(db, [
        _row(880001, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ] + [
        _row(OUT_OF_SCOPE, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ])
    await _add_cycle(db, "completed", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    items = await _items([880001, OUT_OF_SCOPE])
    assert items[0]["priceData"]["coverage"]["coverage"] == 1.0
    assert items[1]["priceData"]["coverage"] is None


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
    # 一次都没抓过的对象（无国区价行）只在 LOCKED 视图里出现
    never = (await _items([880006], region="LOCKED"))[0]["priceData"]
    assert never == {
        "observedAt": None,
        "ageHours": None,
        "freshness": None,
        "coverage": None,
    }


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
async def test_latest_settled_cycle_wins_and_open_one_is_ignored(db):
    """取最近一个**已收敛**的 Cycle：正在跑的拿的是半截数字，卡片上像坏掉。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [
        _row(880001, r, "ok", now - timedelta(minutes=30)) for r in REGIONS
    ])
    old = await _add_cycle(
        db, "completed", IN_SCOPE, REGIONS, now - timedelta(hours=6), now - timedelta(hours=4)
    )
    # 更近的一轮还在跑：不采用
    await _add_cycle(db, "running", IN_SCOPE, REGIONS, now - timedelta(hours=1), None)

    cov = (await _item(880001))["priceData"]["coverage"]
    assert cov["cycleId"] == old
    assert cov["cycleStatus"] == "completed"


@pytest.mark.asyncio
async def test_no_cycle_leaves_coverage_null_but_keeps_freshness(db):
    """库里一轮都没跑过：覆盖率无从谈起，新鲜度照给。"""
    now = datetime.now()
    await _seed(db, [880001])
    await _add_prices(db, [_row(880001, "cn", "ok", now - timedelta(hours=3))])

    data = (await _item(880001))["priceData"]
    assert data["coverage"] is None
    assert data["freshness"] == "fresh"


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


@pytest.mark.asyncio
async def test_list_page_takes_coverage_once_per_page(db, monkeypatch):
    """页级取齐：覆盖率不按卡片数增长（一次列表请求只查一次）。"""
    now = datetime.now()
    await _seed(db, [880001, 880002])
    await _add_prices(db, [
        _row(880001, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ] + [
        _row(880002, r, "ok", now - timedelta(hours=1)) for r in REGIONS
    ])
    await _add_cycle(db, "completed", IN_SCOPE, REGIONS, now - timedelta(hours=2), now)

    original = coverage_mod.latest_appid_coverage
    seen: list[list[int]] = []

    async def _counting(appids):
        seen.append(list(appids))
        return await original(appids)

    monkeypatch.setattr(coverage_mod, "latest_appid_coverage", _counting)
    await games_service.list_games(limit=50)

    assert len(seen) == 1
    # 一次带上整页 appid，而不是每张卡片各查一次
    assert set(seen[0]) == {880001, 880002}
