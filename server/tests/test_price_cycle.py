"""价格刷新周期（Price Refresh Cycle）测试。

覆盖一轮价格刷新的归属与生命周期：Cycle 创建、job 归属、状态推进、期望集
冻结、终态判定、异常路径。Coverage / Freshness / Price Event / 生产统计
不在本轮范围（后续阶段挂到 Cycle 上）。

隔离：tmp 库 + get_session_factory 打桩；start_job 的出网依赖（run_crawl、
区服、worker 统计）全部打桩——不发任何真实请求、不触生产库。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import scheduler as sched_mod
from app.core.database import Base
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import service as crawl_service
from app.domains.crawl.cycle import PriceCycle
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
# pool / catalog 作用域现在走 Monitoring 层：表要注册进 Base.metadata
from app.domains.monitoring import models as _monitoring_models  # noqa: F401
from app.domains.monitoring.models import MonitorTarget

APPIDS = (760001, 760002)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module
    import app.crawler.db_writer as dw

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    # cycle / crawl.service / db_writer / monitoring 都是模块级 import 的
    # factory——逐个打桩，漏一个就会读到生产库
    monkeypatch.setattr(cycle_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    import app.domains.monitoring.service as monitoring_service

    monkeypatch.setattr(monitoring_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    crawl_service._active = None
    sched_mod._price_cycle_busy = False


def _crawl_env(monkeypatch):
    """start_job 出网/区服/收尾钩子全打桩（不发任何真实请求、不触生产库）。"""
    async def _regions(regions=None):
        return ["CN", "UA"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async def _value(key, default=None):
        return default

    import app.domains.proxies.service as proxies_service
    import app.domains.settings.service as settings_service

    monkeypatch.setattr(settings_service, "get_value", _value)

    async def _pool_stats():
        return {"available": 0}

    monkeypatch.setattr(proxies_service, "pool_stats", _pool_stats)

    # 受管爬取的前置条件是「池 Runtime 可用」（fail closed）：这里给一个确定性的
    # 可用入口，断言的是周期编排本身，不是运行时可用性。
    import app.domains.proxypool.runtime as pp_runtime

    monkeypatch.setattr(pp_runtime, "current_runtime_proxy_url",
                        lambda _d=None: "http://127.0.0.1:1")

    async def _noop(*a, **kw):
        return 0

    monkeypatch.setattr(crawl_service.alerts_service, "check_appids", _noop)
    monkeypatch.setattr(crawl_service.alerts_service, "check_new_lows", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_hl_flags", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_pp_flags", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_sort_cache", _noop)

    async def _run_crawl(pairs, *, config, stop_event=None, pre_tasks=None):
        return {"total": len(pairs or []) + len(pre_tasks or []), "processed": 0}

    monkeypatch.setattr(crawl_service, "run_crawl", _run_crawl)


def _stub_scheduler_env(monkeypatch):
    """主轮调度入口的旁路打桩（重锚 / 总开关 / 链尾捆绑包）。"""
    async def _auto_on():
        return True

    monkeypatch.setattr(sched_mod, "price_auto_enabled", _auto_on)

    async def _no_reanchor(reason):
        return None

    monkeypatch.setattr(sched_mod, "_reanchor_price_refresh", _no_reanchor)

    from app.domains.bundles import refresh as bundles_refresh

    async def _no_bundles():
        return {"ok": True, "updated": 0, "total": 0, "regionPrices": 0,
                "failed": [], "droppedSingletons": 0}

    monkeypatch.setattr(bundles_refresh, "refresh_bundles", _no_bundles)


async def _seed_games(db, appids):
    """Catalog 行 + active 监控对象（pool 作用域取后者）。"""
    now = datetime.now()
    async with db() as session:
        for appid in appids:
            session.add(Game(appid=appid, name=f"g{appid}", created_at=now,
                             updated_at=now))
            session.add(MonitorTarget(
                target_type="game", target_id=appid, state="active",
                created_at=now, updated_at=now, activated_at=now,
            ))
        await session.commit()


async def _seed_missing(db, appid, hours_ago=25):
    """种一条过冷却的 missing 欠账行（补抓层 / 修复轮的拾取目标）。"""
    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=appid, name=f"m{appid}", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=appid, region_code="CN", currency="", price=None, sub_id=None,
            price_status="missing", fail_count=1,
            updated_at=now - timedelta(hours=hours_ago),
        ))
        await session.commit()


async def _job_rows(db):
    async with db() as session:
        return (await session.execute(select(CrawlJob).order_by(CrawlJob.id))
                ).scalars().all()


# ── Cycle 创建与 job 归属 ──


@pytest.mark.asyncio
async def test_price_refresh_creates_one_cycle(db, monkeypatch):
    """一次价格主轮 = 一个 Cycle（不再是一串互不相识的 job）。"""
    _crawl_env(monkeypatch)
    _stub_scheduler_env(monkeypatch)
    await _seed_games(db, APPIDS)

    await sched_mod._job_price_refresh()

    rows = await cycle_mod.list_cycles(10)
    assert len(rows) == 1, f"一次主轮只应产生一个 Cycle：{rows}"
    assert rows[0]["kind"] == "scheduled"
    assert rows[0]["status"] in cycle_mod.TERMINAL_STATES


@pytest.mark.asyncio
async def test_all_round_jobs_share_one_cycle(db, monkeypatch):
    """本轮启动的多个 job 全部挂到同一个 cycle_id。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)
    await _seed_missing(db, 760003)

    await sched_mod._run_price_cycle([{"kind": "missing"}, {"scope": "pool"}])

    cycles = await cycle_mod.list_cycles(10)
    assert len(cycles) == 1
    jobs = await _job_rows(db)
    assert len(jobs) >= 2, "补抓层与监控层各一个 job"
    assert {j.cycle_id for j in jobs} == {cycles[0]["id"]}
    assert {j["id"] for j in cycles[0]["jobs"]} == {j.id for j in jobs}


@pytest.mark.asyncio
async def test_legacy_job_without_cycle_not_attributed(db, monkeypatch):
    """cycle_id 为 NULL 的历史 job 不被归入本轮。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)
    async with db() as session:
        session.add(CrawlJob(kind="manual", status="done", started_at=datetime.now()))
        await session.commit()

    cid = await cycle_mod.create("scheduled", "pool")
    await cycle_mod.freeze_expected(cid, [{"scope": "pool"}])
    await crawl_service.run_sequential([{"scope": "pool"}], cycle_id=cid)

    jobs = await _job_rows(db)
    assert [j.cycle_id for j in jobs] == [None, cid]
    assert [j["kind"] for j in await cycle_mod.jobs_of(cid)] == ["scheduled"]


# ── Expected Set 冻结 ──


@pytest.mark.asyncio
async def test_expected_set_frozen_after_planning(db, monkeypatch):
    """冻结后监控池增删不改本轮期望集——覆盖率分母必须可复现。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)

    cid = await cycle_mod.create("scheduled", "pool")
    regions, units = await cycle_mod.freeze_expected(cid, [{"scope": "pool"}])
    assert regions == ["CN", "UA"]
    assert units == len(APPIDS) * len(regions)

    # 本轮开跑后监控池新增游戏：本轮分母不跟着涨
    await _seed_games(db, [760099])
    snap = await cycle_mod.get(cid)
    assert snap["expectedUnits"] == units


@pytest.mark.asyncio
async def test_expected_set_freezes_regions(db, monkeypatch):
    """地区在 planning 时刻冻结：之后区服配置变化不改写本轮地区。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)

    cid = await cycle_mod.create("scheduled", "pool")
    await cycle_mod.freeze_expected(cid, [{"scope": "pool"}])

    async def _more_regions(regions=None):
        return ["CN", "UA", "KZ", "IN"]

    monkeypatch.setattr(crawl_service, "effective_regions", _more_regions)
    snap = await cycle_mod.get(cid)
    assert snap["expectedUnits"] == len(APPIDS) * 2


@pytest.mark.asyncio
async def test_unresolvable_spec_skipped_in_expected(db, monkeypatch):
    """阶段解析不出对象（未知 scope / 空列表）跳过该阶段，不掀翻整轮记账。"""
    _crawl_env(monkeypatch)
    cid = await cycle_mod.create("scheduled", "pool")
    regions, units = await cycle_mod.freeze_expected(
        cid, [{"scope": "bogus"}, {"scope": "pool"}]
    )
    assert regions == ["CN", "UA"]
    assert units == 0
    assert (await cycle_mod.get(cid))["status"] == cycle_mod.PLANNING


# ── 状态推进 ──


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    ["running", "finalizing", "completed"],
    ["running", "finalizing", "partial"],
    ["running", "finalizing", "failed"],
    ["running", "failed"],
    ["running", "cancelled"],
    ["running", "repairing", "finalizing", "completed"],
    ["running", "repairing", "finalizing", "partial"],
    ["running", "repairing", "cancelled"],
])
async def test_allowed_transitions(db, path):
    """合法生命周期路径全部可达（planning → … → 终态）。"""
    cid = await cycle_mod.create("scheduled", "pool")
    assert (await cycle_mod.get(cid))["status"] == cycle_mod.PLANNING
    for status in path:
        assert await cycle_mod.advance(cid, status), f"{status} 应可推进"
    snap = await cycle_mod.get(cid)
    assert snap["status"] == path[-1]
    assert snap["finishedAt"] is not None, "终态必须落结束时刻"


@pytest.mark.asyncio
async def test_illegal_transition_rejected(db):
    """非法跃迁与终态再推进一律拒绝（Cycle 状态只由明确生命周期推进）。"""
    cid = await cycle_mod.create("scheduled", "pool")
    # planning 不能直接跳终态，也不能跳 repairing
    assert not await cycle_mod.advance(cid, cycle_mod.COMPLETED)
    assert not await cycle_mod.advance(cid, cycle_mod.REPAIRING)
    assert (await cycle_mod.get(cid))["status"] == cycle_mod.PLANNING

    assert await cycle_mod.advance(cid, cycle_mod.RUNNING)
    assert await cycle_mod.advance(cid, cycle_mod.CANCELLED)
    # 终态不可再推进
    assert not await cycle_mod.advance(cid, cycle_mod.RUNNING)
    assert not await cycle_mod.advance(cid, cycle_mod.FINALIZING)
    assert (await cycle_mod.get(cid))["status"] == cycle_mod.CANCELLED


@pytest.mark.asyncio
async def test_advance_outside_cycle_is_noop(db):
    """未挂 Cycle 的本轮（cycle_id 为 None）：状态推进静默跳过。"""
    assert await cycle_mod.advance(None, cycle_mod.RUNNING) is False
    assert await cycle_mod.advance(99999, cycle_mod.RUNNING) is False


# ── 终态判定 ──


def test_decide_terminal_partial_success_is_not_failed():
    """部分成功归 partial；终态反映整轮，不是最后一个 job 的状态。"""
    d = cycle_mod.decide_terminal
    assert d(["done", "failed"], expected_units=10, entered_repairing=False) == "partial"
    assert d(["done", "stopped"], expected_units=10, entered_repairing=False) == "partial"
    assert d(["failed", "done"], expected_units=10, entered_repairing=False) == "partial"
    assert d(["done"], expected_units=10, entered_repairing=False) == "completed"
    assert d(["done"], expected_units=10, entered_repairing=True) == "partial"
    assert d(["failed"], expected_units=10, entered_repairing=False) == "failed"
    assert d(["stopped"], expected_units=10, entered_repairing=False) == "cancelled"
    assert d([], expected_units=0, entered_repairing=False) == "completed"
    assert d([], expected_units=5, entered_repairing=False) == "failed"


# ── 异常路径 ──


@pytest.mark.asyncio
async def test_chain_exception_lands_on_failed(db, monkeypatch):
    """主链异常：Cycle 收敛到 failed，不留中间态。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)

    async def _boom(specs, **kw):
        raise RuntimeError("链炸了")

    monkeypatch.setattr(crawl_service, "run_sequential", _boom)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.FAILED
    assert rows[0]["error"]


@pytest.mark.asyncio
async def test_no_enabled_region_fails_cycle_but_chain_still_attempted(
    db, monkeypatch,
):
    """区服不可用：本轮无区可爬 → failed；链照常尝试（各 spec 自行跳过）。"""
    _crawl_env(monkeypatch)

    async def _no_region(regions=None):
        raise ValueError("未启用任何区服")

    monkeypatch.setattr(crawl_service, "effective_regions", _no_region)
    called: list = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    assert called, "区服不可用同样要让链走一遍（既有行为）"
    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.FAILED


@pytest.mark.asyncio
async def test_cycle_create_failure_does_not_block_crawl(db, monkeypatch):
    """Cycle 记账失败：本轮抓取照常进行，只是这轮没有归属可查。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)
    called: list = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)

    async def _boom(*a, **kw):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(cycle_mod, "create", _boom)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    assert called, "记账失败不得让用户少一轮价格"
    assert await cycle_mod.list_cycles(5) == []


@pytest.mark.asyncio
async def test_stopped_round_lands_on_cancelled(db, monkeypatch):
    """本轮 job 全部被用户停止 → cancelled（不是 failed）。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)

    async def _stopped(specs, **kw):
        async with db() as session:
            session.add(CrawlJob(
                kind="scheduled", status="stopped", cycle_id=kw.get("cycle_id"),
                started_at=datetime.now(), finished_at=datetime.now(),
            ))
            await session.commit()
        return [{"id": 1}]

    monkeypatch.setattr(crawl_service, "run_sequential", _stopped)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.CANCELLED


@pytest.mark.asyncio
async def test_pending_missing_marks_repairing(db, monkeypatch):
    """本轮结束仍有待补欠账 → 记 repairing，终态 partial（有单元没拿到）。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)
    await _seed_missing(db, 760004)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["enteredRepairing"] is True
    assert rows[0]["repairingAt"] is not None
    assert rows[0]["status"] == cycle_mod.PARTIAL


@pytest.mark.asyncio
async def test_clean_round_without_debt_is_completed(db, monkeypatch):
    """本轮无欠账、job 全成功 → completed。"""
    _crawl_env(monkeypatch)
    await _seed_games(db, APPIDS)

    await sched_mod._run_price_cycle([{"scope": "pool"}])

    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.COMPLETED
    assert rows[0]["enteredRepairing"] is False


# ── 进程重启收敛 ──


@pytest.mark.asyncio
async def test_orphan_cycles_converged_on_startup(db):
    """上一进程遗留的未收敛 Cycle 在启动时收敛到 failed。"""
    cid = await cycle_mod.create("scheduled", "pool")
    await cycle_mod.advance(cid, cycle_mod.RUNNING)
    cid2 = await cycle_mod.create("scheduled", "pool")

    assert await cycle_mod.cleanup_orphan_cycles() == 2

    assert (await cycle_mod.get(cid))["status"] == cycle_mod.FAILED
    assert (await cycle_mod.get(cid2))["status"] == cycle_mod.FAILED
    async with db() as session:
        left = (await session.execute(
            select(PriceCycle).where(PriceCycle.status.in_(cycle_mod.OPEN_STATES))
        )).scalars().all()
    assert left == []
