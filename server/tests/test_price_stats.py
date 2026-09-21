"""生产 Cycle 统计测试（P4）。

统计是 Cycle 的观测结果：数字必须能从「冻结期望集 + 本轮价格结果 + 阶段时刻」
推出来，推不出来的指标宁可不写。

隔离：tmp 库 + get_session_factory 打桩（cycle / coverage / freshness / stats
逐模块打桩），不触生产库、不出网。
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
from app.domains.crawl import coverage as coverage_mod
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import freshness as freshness_mod
from app.domains.crawl import stats as stats_mod
from app.domains.crawl.cycle import PriceCycle
from app.domains.games.models import Game, GameCurrentPrice

APP_A, APP_B = 790001, 790002
APP_STALE = 790003
REGIONS = ("cn", "ru")  # 2 对象 × 2 区 = 4 单元


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


async def _make_cycle(db, appids, regions) -> int:
    cid = await cycle_mod.create("scheduled", "pool")
    async with db() as session:
        cycle = await session.get(PriceCycle, cid)
        cycle.expected_json = {"appids": list(appids), "regions": list(regions)}
        await session.commit()
    return cid


async def _price(db, appid, region, status, *, hours_ago: float = 0.0) -> None:
    """播一个单元的价格结果（同一对象多次调用只建一次 games 行）。"""
    now = datetime.now()
    async with db() as session:
        if await session.get(Game, appid) is None:
            session.add(Game(appid=appid, name=f"g{appid}", created_at=now,
                             updated_at=now))
            await session.commit()
    async with db() as session:
        session.add(GameCurrentPrice(
            appid=appid, region_code=region, currency="CNY",
            price=9900 if status == "ok" else None, sub_id=1,
            price_status=status, fail_count=0,
            updated_at=datetime.now() - timedelta(hours=hours_ago),
        ))
        await session.commit()


async def _finish(cid: int) -> None:
    """走一遍真实状态机到终态（阶段时刻由 advance 自己落）。"""
    await cycle_mod.advance(cid, cycle_mod.RUNNING)
    await cycle_mod.advance(cid, cycle_mod.FINALIZING)
    await cycle_mod.advance(cid, cycle_mod.COMPLETED)


# ── 阶段耗时：纯函数口径 ──


def test_stage_ms_skipped_stages_are_zero():
    """没进过的阶段记 0，不估算；进入过的按相邻时刻相减。"""
    base = datetime(2026, 9, 21, 8, 0, 0)
    # 没进 repairing：planning → running → finalizing → 终态
    stages = stats_mod.stage_ms(
        started_at=base,
        running_at=base + timedelta(seconds=10),
        repairing_at=None,
        finalizing_at=base + timedelta(seconds=70),
        finished_at=base + timedelta(seconds=80),
    )
    assert stages == {
        "planning": 10_000,
        "running": 60_000,
        "repairing": 0,
        "finalizing": 10_000,
    }

    # 进过 repairing：running 止于 repairing，repairing 再到 finalizing
    stages = stats_mod.stage_ms(
        started_at=base,
        running_at=base + timedelta(seconds=5),
        repairing_at=base + timedelta(seconds=65),
        finalizing_at=base + timedelta(seconds=75),
        finished_at=base + timedelta(seconds=90),
    )
    assert stages == {
        "planning": 5_000,
        "running": 60_000,
        "repairing": 10_000,
        "finalizing": 15_000,
    }


def test_stage_ms_running_failure_falls_back_to_finished():
    """从 running 直接异常终止：running 一直算到本轮结束时刻。"""
    base = datetime(2026, 9, 21, 8, 0, 0)
    stages = stats_mod.stage_ms(
        started_at=base,
        running_at=base + timedelta(seconds=1),
        repairing_at=None,
        finalizing_at=None,
        finished_at=base + timedelta(seconds=31),
    )
    assert stages["running"] == 30_000
    assert stages["repairing"] == 0
    assert stages["finalizing"] == 0


def test_stage_ms_without_running_is_all_zero():
    """创建后即失败（没进过 running）：全部阶段记 0。"""
    base = datetime(2026, 9, 21, 8, 0, 0)
    stages = stats_mod.stage_ms(
        started_at=base, running_at=None, repairing_at=None,
        finalizing_at=None, finished_at=base + timedelta(seconds=2),
    )
    assert stages == {"planning": 0, "running": 0, "repairing": 0, "finalizing": 0}


# ── 完整 Cycle ──


@pytest.mark.asyncio
async def test_full_cycle_stats(db):
    """expected = 4，全 ok → coverage / confirmed 都是 100%，对象全部处理完。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "ok")
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert (recorded["targetsTotal"], recorded["targetsDone"]) == (2, 2)
    assert recorded["unitsExpected"] == 4
    assert (recorded["unitsOk"], recorded["unitsLocked"]) == (4, 0)
    assert (recorded["unitsFailed"], recorded["unitsUnobserved"]) == (0, 0)
    assert recorded["coverage"] == 1.0
    assert recorded["coverageConfirmed"] == 1.0
    assert recorded["staleCount"] == 0
    assert recorded["durationSeconds"] >= 0

    # 落库后从 Cycle 行可读回
    snap = await cycle_mod.get(cid)
    assert snap["stats"]["unitsOk"] == 4
    assert snap["stats"]["targetsDone"] == 2
    assert set(snap["stats"]["stageMs"]) == {
        "planning", "running", "repairing", "finalizing",
    }


# ── 部分 Cycle：口径关系 ──


@pytest.mark.asyncio
async def test_partial_cycle_relationships(db):
    """ok 3 / locked 1 / missing 2 / unobserved 2：三者的关系必须自洽。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "locked")
    await _price(db, APP_B, "cn", "missing")
    await _price(db, APP_B, "ru", "blocked")
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["unitsExpected"] == 4
    assert (recorded["unitsOk"], recorded["unitsLocked"]) == (1, 1)
    assert recorded["unitsFailed"] == 2, "missing + blocked 都算明确失败"
    assert recorded["unitsUnobserved"] == 0
    assert recorded["coverage"] == 0.25
    assert recorded["coverageConfirmed"] == 0.5
    # 五个单元桶之和恒等于期望单元数
    assert (
        recorded["unitsOk"] + recorded["unitsLocked"]
        + recorded["unitsFailed"] + recorded["unitsUnobserved"]
    ) == recorded["unitsExpected"]


@pytest.mark.asyncio
async def test_targets_done_is_not_success(db):
    """「处理完」≠「成功」：全区 missing 的对象算 done，有 unobserved 的不算。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    # A 两个区都明确失败 → 已处理完
    await _price(db, APP_A, "cn", "missing")
    await _price(db, APP_A, "ru", "blocked")
    # B 只有一区有结果 → 未处理完
    await _price(db, APP_B, "cn", "ok")
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["targetsTotal"] == 2
    assert recorded["targetsDone"] == 1
    assert recorded["coverage"] == 0.25
    assert recorded["unitsUnobserved"] == 1
    # targetsDone 满格 + coverage 不满也可以同时成立
    assert recorded["targetsDone"] < recorded["targetsTotal"]


@pytest.mark.asyncio
async def test_all_failed_targets_are_done(db):
    """全部区都明确失败：对象 100% 处理完，但 coverage = 0。"""
    cid = await _make_cycle(db, [APP_A, APP_B], REGIONS)
    for appid in (APP_A, APP_B):
        for region in REGIONS:
            await _price(db, appid, region, "missing")
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["targetsDone"] == recorded["targetsTotal"] == 2
    assert recorded["unitsFailed"] == 4
    assert recorded["coverage"] == 0.0
    assert recorded["coverageConfirmed"] == 0.0


# ── stale_count（对象级，与 Freshness 同口径）──


@pytest.mark.asyncio
async def test_stale_count_counts_objects(db):
    """staleCount 数的是对象：该对象全部区服都已 ≥12h 未更新才算。"""
    cid = await _make_cycle(db, [APP_A, APP_STALE], REGIONS)
    await _price(db, APP_A, "cn", "ok")
    await _price(db, APP_A, "ru", "ok")
    await _price(db, APP_STALE, "cn", "ok", hours_ago=30.0)
    await _price(db, APP_STALE, "ru", "ok", hours_ago=13.0)
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["staleCount"] == 1


@pytest.mark.asyncio
async def test_fresh_object_with_one_stale_region_is_not_stale(db):
    """对象级口径：只要有任一区在阈值内，整个对象就不算 stale。"""
    cid = await _make_cycle(db, [APP_A], REGIONS)
    await _price(db, APP_A, "cn", "ok", hours_ago=1.0)
    await _price(db, APP_A, "ru", "ok", hours_ago=30.0)
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["staleCount"] == 0


# ── 没有统计 vs 有统计 ──


@pytest.mark.asyncio
async def test_unrecorded_cycle_has_no_stats(db):
    """没写过统计的 Cycle（未收敛 / 进程中断）读出来是 null，不是一堆 0。"""
    cid = await _make_cycle(db, [APP_A], REGIONS)
    await cycle_mod.advance(cid, cycle_mod.RUNNING)
    snap = await cycle_mod.get(cid)
    assert snap["stats"] is None


@pytest.mark.asyncio
async def test_record_stats_unknown_cycle_returns_none(db):
    assert await stats_mod.record_stats(99999) is None


@pytest.mark.asyncio
async def test_empty_expected_set_still_records(db):
    """期望集为空（本轮无事可做）：统计照样落库，比值是 null。"""
    cid = await _make_cycle(db, [], REGIONS)
    await _finish(cid)

    recorded = await stats_mod.record_stats(cid)
    assert recorded["targetsTotal"] == 0
    assert recorded["unitsExpected"] == 0
    assert recorded["coverage"] is None
    snap = await cycle_mod.get(cid)
    assert snap["stats"] is not None, "写了 0 与「没写过」必须能区分"


@pytest.mark.asyncio
async def test_stats_do_not_leak_into_cycle_state(db):
    """统计是观测：写统计不改 Cycle 状态，也不新增状态字。"""
    cid = await _make_cycle(db, [APP_A], ("cn",))
    await _price(db, APP_A, "cn", "ok")
    await _finish(cid)

    before = await cycle_mod.get(cid)
    await stats_mod.record_stats(cid)
    after = await cycle_mod.get(cid)

    assert before["status"] == after["status"] == cycle_mod.COMPLETED
    assert before["finishedAt"] == after["finishedAt"]
    async with db() as session:
        statuses = (await session.execute(
            select(PriceCycle.status)
        )).scalars().all()
    assert set(statuses) <= set(
        cycle_mod.TERMINAL_STATES + cycle_mod.OPEN_STATES
    ), "统计不得引入新的状态取值"
