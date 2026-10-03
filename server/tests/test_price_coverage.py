"""覆盖率（Coverage）与新鲜度（Freshness）测试。

两条不变量：
- Coverage 分母只来自 Cycle 冻结的期望集，监控池增删不改写它；
- Coverage 与 Freshness 互相独立，可以任意组合（100% + stale、<100% + fresh）。

隔离：tmp 库 + get_session_factory 打桩（cycle / coverage / freshness 均逐模块
打桩），不触生产库、不出网。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.crawl import coverage as coverage_mod
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import freshness as freshness_mod
from app.domains.crawl import router as crawl_router
from app.domains.crawl import stats as stats_mod
from app.domains.crawl.cycle import PriceCycle
from app.domains.games.models import Game, GameCurrentPrice

APP_A, APP_B = 780001, 780002
REGIONS = ("cn", "ru")


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (cycle_mod, coverage_mod, freshness_mod, stats_mod):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _make_cycle(
    db, appids, regions, *, started_hours_ago: float = 0.0, status: str | None = None
) -> int:
    """建一个 Cycle 并按其期望集口径冻结（等价 planning 的产出）。"""
    cid = await cycle_mod.create("scheduled", "pool")
    async with db() as session:
        cycle = await session.get(PriceCycle, cid)
        cycle.expected_json = {"appids": list(appids), "regions": list(regions)}
        cycle.started_at = datetime.now() - timedelta(hours=started_hours_ago)
        if status is not None:
            cycle.status = status
        await session.commit()
    return cid


async def _close_cycle(db, cid: int, *, hours_ago: float) -> None:
    """把 Cycle 收到终态并回填结束时刻（本轮结果窗口的上界）。"""
    async with db() as session:
        cycle = await session.get(PriceCycle, cid)
        cycle.status = cycle_mod.COMPLETED
        cycle.finished_at = datetime.now() - timedelta(hours=hours_ago)
        await session.commit()


async def _price(db, appid, region, status, *, hours_ago: float = 0.0) -> None:
    async with db() as session:
        session.add(GameCurrentPrice(
            appid=appid, region_code=region, currency="CNY",
            price=9900 if status == "ok" else None, sub_id=1,
            price_status=status, fail_count=0,
            updated_at=datetime.now() - timedelta(hours=hours_ago),
        ))
        await session.commit()


async def _game(db, appid) -> None:
    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=appid, name=f"g{appid}", created_at=now, updated_at=now))
        await session.commit()


# ── 分母：只认冻结的期望集 ──


@pytest.mark.asyncio
async def test_expected_set_is_the_only_denominator(db):
    """Cycle 创建后往库里加游戏，本轮分母不变。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "ok")
    await _price(db, APP_B, "cn", "ok")
    await _price(db, APP_B, "ru", "ok")

    before = await coverage_mod.cycle_coverage(cid)
    assert before["expectedUnits"] == 4

    await _game(db, 780099)  # 监控池新增（对覆盖率分母无影响）
    after = await coverage_mod.cycle_coverage(cid)
    assert after == before


@pytest.mark.asyncio
async def test_coverage_unknown_cycle_returns_none(db):
    assert await coverage_mod.cycle_coverage(99999) is None


@pytest.mark.asyncio
async def test_empty_expected_set_has_no_ratio(db):
    """期望集为空 = 本轮无事可做：分母 0，比值无意义（null）。"""
    cid = await _make_cycle(db, [], REGIONS)
    result = await coverage_mod.cycle_coverage(cid)
    assert result["expectedUnits"] == 0
    assert result["coverage"] is None
    assert result["coverageConfirmed"] is None
    assert result["unobserved"] == 0


# ── 五个桶 ──


@pytest.mark.asyncio
async def test_full_coverage(db):
    """expected = 4，全部 ok → 两个口径都是 100%。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "ok")

    result = await coverage_mod.cycle_coverage(cid)
    assert (result["ok"], result["locked"]) == (4, 0)
    assert result["coverage"] == 1.0
    assert result["coverageConfirmed"] == 1.0
    assert result["unobserved"] == 0


@pytest.mark.asyncio
async def test_locked_counts_only_in_confirmed(db):
    """锁区：coverage 75%，confirmed 100%——locked 不能塞进普通 coverage。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "ok")
    await _price(db, APP_B, "cn", "ok")
    await _price(db, APP_B, "ru", "locked")

    result = await coverage_mod.cycle_coverage(cid)
    assert (result["ok"], result["locked"]) == (3, 1)
    assert result["coverage"] == 0.75
    assert result["coverageConfirmed"] == 1.0


@pytest.mark.asyncio
async def test_missing_and_blocked_stay_out_of_numerator(db):
    """missing / blocked 进分母不进分子。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "missing")
    await _price(db, APP_B, "cn", "blocked")
    await _price(db, APP_B, "ru", "missing")

    result = await coverage_mod.cycle_coverage(cid)
    assert (result["ok"], result["missing"], result["blocked"]) == (1, 2, 1)
    assert result["coverage"] == 0.25
    assert result["coverageConfirmed"] == 0.25


@pytest.mark.asyncio
async def test_units_without_result_are_unobserved_but_counted(db):
    """没有任何本轮结果的期望单元记 unobserved，仍然进分母。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")

    result = await coverage_mod.cycle_coverage(cid)
    assert result["expectedUnits"] == 4
    assert result["unobserved"] == 3
    assert result["coverage"] == 0.25


@pytest.mark.asyncio
async def test_rows_older_than_cycle_window_are_unobserved(db):
    """窗口外的旧行不算本轮结果——不许把历史数据当成本轮产出。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS, started_hours_ago=1.0)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "ok", hours_ago=5.0)

    result = await coverage_mod.cycle_coverage(cid)
    assert result["ok"] == 0
    assert result["unobserved"] == 4


@pytest.mark.asyncio
async def test_rows_newer_than_cycle_window_are_unobserved(db):
    """收敛后的新写入不算本轮结果：历史 Cycle 的覆盖率不随之后的刷新变大。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS, started_hours_ago=2.0)
    await _close_cycle(db, cid, hours_ago=1.0)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "ok", hours_ago=0.5)

    result = await coverage_mod.cycle_coverage(cid)
    assert result["ok"] == 0
    assert result["unobserved"] == 4

    # 窗口内产生的行照常计入
    app_c = 780099
    cid2 = await _make_cycle(db, [app_c], ("cn",), started_hours_ago=2.0)
    await _close_cycle(db, cid2, hours_ago=1.0)
    await _price(db, app_c, "cn", "ok", hours_ago=1.5)
    assert (await coverage_mod.cycle_coverage(cid2))["ok"] == 1


@pytest.mark.asyncio
async def test_region_code_case_insensitive(db):
    """期望集是小写区服码，price 行落库是大写——不能因大小写整批漏配。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    for appid in (APP_A, APP_B):
        for region in ("CN", "RU"):
            await _price(db, appid, region, "ok")

    result = await coverage_mod.cycle_coverage(cid)
    assert result["ok"] == 4
    assert result["coverage"] == 1.0
    assert result["unobserved"] == 0


@pytest.mark.asyncio
async def test_rows_outside_expected_regions_ignored(db):
    """期望集外的区服结果不进计数（也不进分母）。"""
    cid = await _make_cycle(db, [APP_A], ("cn",))
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "missing")

    result = await coverage_mod.cycle_coverage(cid)
    assert result["expectedUnits"] == 1
    assert result["ok"] == 1
    assert result["missing"] == 0


@pytest.mark.asyncio
async def test_buckets_always_add_up_to_expected(db):
    """五桶之和恒等于期望单元数（含未知状态被按未确认计的情形）。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "weird-status")
    await _price(db, APP_B, "cn", "locked")

    result = await coverage_mod.cycle_coverage(cid)
    total = sum(result[b] for b in coverage_mod.BUCKETS)
    assert total == result["expectedUnits"] == 4
    assert result["missing"] == 1, "未知状态按未确认计"


# ── Freshness ──


@pytest.mark.asyncio
async def test_freshness_three_buckets(db):
    await _price(db, APP_A, "cn", "ok", hours_ago=1.0)
    await _price(db, APP_B, "cn", "ok", hours_ago=8.0)
    await _game(db, 780003)
    await _price(db, 780003, "cn", "ok", hours_ago=30.0)

    items = {i["appid"]: i for i in await freshness_mod.appid_freshness(
        [APP_A, APP_B, 780003, 780004]
    )}
    assert items[APP_A]["freshness"] == "fresh"
    assert items[APP_B]["freshness"] == "lagging"
    assert items[780003]["freshness"] == "stale"
    # 一行价格记录都没有：不是「很旧」，是「没有」
    assert items[780004]["freshness"] is None
    assert items[780004]["observedAt"] is None
    assert items[780004]["ageHours"] is None


@pytest.mark.asyncio
async def test_freshness_uses_latest_observation_across_regions(db):
    """多区取最新观察时刻（最后一次被观察是什么时候）。"""
    await _price(db, APP_A, "cn", "ok", hours_ago=30.0)
    await _price(db, APP_A, "ru", "missing", hours_ago=2.0)

    item = (await freshness_mod.appid_freshness([APP_A]))[0]
    assert item["freshness"] == "fresh"
    assert 1.9 < item["ageHours"] < 2.2


def test_freshness_boundaries():
    """边界归下一档：6h 起 lagging，12h 起 stale。"""
    assert freshness_mod.freshness_bucket(0.0) == "fresh"
    assert freshness_mod.freshness_bucket(5.999) == "fresh"
    assert freshness_mod.freshness_bucket(6.0) == "lagging"
    assert freshness_mod.freshness_bucket(11.999) == "lagging"
    assert freshness_mod.freshness_bucket(12.0) == "stale"

    base = datetime(2026, 9, 21, 8, 0, 0)
    assert freshness_mod.freshness_of(
        base, now=base + timedelta(hours=6)
    )["freshness"] == "lagging"
    assert freshness_mod.freshness_of(
        base, now=base + timedelta(hours=12)
    )["freshness"] == "stale"
    assert freshness_mod.freshness_of(
        base, now=base + timedelta(hours=5, minutes=59)
    )["freshness"] == "fresh"


# ── 两个维度独立 ──


@pytest.mark.asyncio
async def test_full_coverage_can_be_stale(db):
    """Coverage = 100% 且 Freshness = stale 同时成立。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS, started_hours_ago=20.0)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "ok", hours_ago=20.0)

    result = await coverage_mod.cycle_coverage(cid)
    items = await freshness_mod.appid_freshness([APP_A, APP_B])
    assert result["coverage"] == 1.0
    assert {i["freshness"] for i in items} == {"stale"}


@pytest.mark.asyncio
async def test_fresh_data_can_be_low_coverage(db):
    """Freshness = fresh 且 Coverage < 100% 同时成立。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "ok")

    result = await coverage_mod.cycle_coverage(cid)
    items = await freshness_mod.appid_freshness([APP_A])
    assert result["coverage"] == 0.5
    assert items[0]["freshness"] == "fresh"


# ── 端点守卫 ──


@pytest.mark.asyncio
async def test_coverage_endpoint_404_for_unknown_cycle(db):
    with pytest.raises(HTTPException) as exc:
        await crawl_router.cycle_coverage(99999)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_coverage_endpoint_returns_cycle_result(db):
    cid = await _make_cycle(db, [APP_A], ("cn",))
    await _price(db, APP_A, "cn", "ok")
    result = await crawl_router.cycle_coverage(cid)
    assert result["cycleId"] == cid
    assert result["coverage"] == 1.0


@pytest.mark.asyncio
async def test_freshness_endpoint_guards(db):
    with pytest.raises(HTTPException) as exc:
        await crawl_router.freshness(appid=[])
    assert exc.value.status_code == 400

    too_many = list(range(1, crawl_router.FRESHNESS_MAX_APPIDS + 2))
    with pytest.raises(HTTPException) as exc:
        await crawl_router.freshness(appid=too_many)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_freshness_endpoint_dedupes(db):
    await _price(db, APP_A, "cn", "ok", hours_ago=1.0)
    payload = await crawl_router.freshness(appid=[APP_A, APP_A])
    assert [i["appid"] for i in payload["items"]] == [APP_A]


@pytest.mark.asyncio
async def test_freshness_latest_endpoint(db):
    """岛时钟端点：全库最近成功观察时刻（观察章推进），空库为 null。"""
    payload = await crawl_router.freshness_latest()
    assert payload["lastSuccessAt"] is None

    async with db() as session:
        session.add(GameCurrentPrice(
            appid=APP_A, region_code="CN", currency="CNY",
            price=9900, sub_id=1, price_status="ok", fail_count=0,
            attempt_outcome="success",
            last_success_at=datetime.now() - timedelta(hours=1),
            updated_at=datetime.now(),
        ))
        await session.commit()

    payload = await crawl_router.freshness_latest()
    assert payload["lastSuccessAt"] is not None


# ── 对象级覆盖快照（收敛冻结，轮详情页 cycle_coverage 的证据源；
#    卡片覆盖已改活表尝试状态现算，不再读快照）──


@pytest.mark.asyncio
async def test_convergence_freezes_per_appid_snapshot(db):
    """收敛统计冻结每个对象的五桶计数进 Cycle 行，轮详情读快照。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS, started_hours_ago=2.0)
    await _price(db, APP_A, "cn", "ok", hours_ago=1.8)
    await _price(db, APP_A, "ru", "locked", hours_ago=1.8)
    await _price(db, APP_B, "cn", "ok", hours_ago=1.8)
    await _close_cycle(db, cid, hours_ago=1.0)

    stats = await stats_mod.record_stats(cid)
    snap = stats["perAppid"]
    assert snap[str(APP_A)] == {
        "ok": 1, "locked": 1, "missing": 0, "blocked": 0, "unobserved": 0,
        "regions": {"RU": "locked"},
    }
    # 没观察到的区进 regions，标 unobserved——悬停点名问题地区的依据
    assert snap[str(APP_B)] == {
        "ok": 1, "locked": 0, "missing": 0, "blocked": 0, "unobserved": 1,
        "regions": {"RU": "unobserved"},
    }


async def _touch_price(db, appid, region, status, *, hours_ago: float = 0.0) -> None:
    """周期外写入的活表语义：同一 (appid, region) 行被覆盖更新（时刻推到当下）。"""
    from sqlalchemy import update

    async with db() as session:
        await session.execute(
            update(GameCurrentPrice)
            .where(
                GameCurrentPrice.appid == appid,
                GameCurrentPrice.region_code == region,
            )
            .values(
                price_status=status,
                price=None if status != "ok" else 9900,
                updated_at=datetime.now() - timedelta(hours=hours_ago),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_cycle_coverage_reads_snapshot_over_window(db):
    """轮详情覆盖率同样快照优先：快照在，周期外写入不改已收敛轮的数字。"""
    cid = await _make_cycle(db, [APP_A], REGIONS, started_hours_ago=2.0)
    await _price(db, APP_A, "cn", "ok", hours_ago=1.8)
    await _price(db, APP_A, "ru", "locked", hours_ago=1.8)
    await _close_cycle(db, cid, hours_ago=1.0)
    await stats_mod.record_stats(cid)

    for region in REGIONS:
        await _touch_price(db, APP_A, region, "missing", hours_ago=0.0)

    result = await coverage_mod.cycle_coverage(cid)
    assert (result["ok"], result["locked"]) == (1, 1)
    assert result["unobserved"] == 0
    assert result["targetsDone"] == 1
    assert result["perAppid"][str(APP_A)]["locked"] == 1


@pytest.mark.asyncio
async def test_snapshot_excludes_objects_outside_expected_set(db):
    """不在期望集内的对象不进快照——轮详情只回答本轮该刷的对象。"""
    cid = await _make_cycle(db, [APP_A], REGIONS, started_hours_ago=2.0)
    await _price(db, APP_A, "cn", "ok", hours_ago=1.8)
    await _price(db, APP_A, "ru", "ok", hours_ago=1.8)
    await _price(db, APP_B, "cn", "ok", hours_ago=1.8)
    await _close_cycle(db, cid, hours_ago=1.0)
    await stats_mod.record_stats(cid)

    result = await coverage_mod.cycle_coverage(cid)
    assert str(APP_A) in result["perAppid"] and str(APP_B) not in result["perAppid"]
