"""失败记录修复线程测试（5min 空闲档）。

规则：主价格刷新爬虫**结束工作**后（空闲门禁看进程内任务表，非 6h
间隔），每 5 分钟扫全库 price_status='missing' 失败记录，走与主爬虫
一致的 kind=repair 定向补抓通道（按区分组凑批发、每发只装该区欠账
行、低 worker、无预检、冷却 4min）；仍失败照常 missing 计账（失败标志保留，
fail_count 递增，穷尽 5 次转 blocked），成功清账。

隔离：tmp 库 + get_session_factory 打桩（test_rates_schedule 模式）；
start_job 的出网/区服/出网依赖全部打桩——只验证判定与接线语义。
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import external_time
from app.core import scheduler as sched_mod
from app.core.database import Base
from app.core.events import bus
from app.crawler import runner as runner_mod
from app.crawler.db_writer import DbWriter
from app.domains.crawl import service as crawl_service
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
from app.crawler.utils import get_beijing_time_obj

APPID = 997_001  # 修复目标：CN/UA 两区 missing（过期冷却）
APPID2 = 997_002  # 冷却未到（刚失败 1min）——修复轮不应拾取
APPID3 = 997_003  # blocked 终态——不在任何补抓通道
APPID4 = 997_004  # ok 正常行——与修复无关


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    # service/db_writer/settings 均模块级 import factory——逐个打桩；
    # settings 漏桩会让 price_auto_enabled 读真实库的总开关，
    # 本机把 crawl.auto_price 关掉时主轮/修复轮全部提前返回
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    import app.crawler.db_writer as dw
    import app.domains.settings.service as settings_service

    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def _seed(db):
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with db() as session:
        session.add_all(
            [
                Game(appid=APPID, name="待修复", created_at=now, updated_at=now),
                Game(appid=APPID2, name="冷却中", created_at=now, updated_at=now),
                Game(appid=APPID3, name="终态", created_at=now, updated_at=now),
                Game(appid=APPID4, name="正常", created_at=now, updated_at=now),
            ]
        )
        session.add_all(
            [
                GameCurrentPrice(appid=APPID, region_code="CN", currency="",
                                 price=None, sub_id=None, price_status="missing",
                                 fail_count=1, updated_at=now - timedelta(hours=1)),
                GameCurrentPrice(appid=APPID, region_code="UA", currency="",
                                 price=None, sub_id=None, price_status="missing",
                                 fail_count=1, updated_at=now - timedelta(hours=1)),
                # 1 分钟前刚失败：冷却 4min 未过，repair 不拾取（missing 层
                # 24h 冷却更不拾取）
                GameCurrentPrice(appid=APPID2, region_code="CN", currency="",
                                 price=None, sub_id=None, price_status="missing",
                                 fail_count=1, updated_at=now - timedelta(minutes=1)),
                GameCurrentPrice(appid=APPID3, region_code="CN", currency="",
                                 price=None, sub_id=None, price_status="blocked",
                                 fail_count=5, updated_at=now - timedelta(days=3)),
                GameCurrentPrice(appid=APPID4, region_code="CN", currency="CNY",
                                 price=9900, sub_id=1, price_status="ok",
                                 fail_count=0, updated_at=now),
            ]
        )
        await session.commit()
    yield
    crawl_service._active = None
    sched_mod._price_cycle_busy = False


def _stub_start_env(monkeypatch, *, captured_specs=None):
    """start_job 出网/区服/收尾钩子全打桩（不发任何真实请求、不触生产库）。
    直连形态下 start_job 不再解析代理——无代理桩可打。"""
    async def _regions(regions=None):
        return ["CN", "UA"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async def _value(key, default=None):
        return default

    # start_job 函数内局部 import —— 须打桩源模块属性
    import app.domains.settings.service as settings_service

    monkeypatch.setattr(settings_service, "get_value", _value)

    # 出口 IP 统计打桩：无可用出口（直连形态）→ worker 数回退默认口径；
    # 不桩会走真实 proxies 域会话工厂（触生产库）
    import app.domains.proxies.service as proxies_service

    async def _pool_stats():
        return {"available": 0}

    monkeypatch.setattr(proxies_service, "pool_stats", _pool_stats)

    # _execute 收尾钩子（提醒/史低/排序）模块级绑定各自服务的
    # get_session_factory——全部打桩防触生产库
    async def _noop(*a, **kw):
        return 0

    monkeypatch.setattr(crawl_service.alerts_service, "check_appids", _noop)
    monkeypatch.setattr(crawl_service.alerts_service, "check_new_lows", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_hl_flags", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_pp_flags", _noop)
    monkeypatch.setattr(crawl_service.games_service, "refresh_sort_cache", _noop)

    # run_crawl 打桩短路（stats 形态对齐 runner 返回）
    async def _run_crawl(pairs, *, config, stop_event=None, pre_tasks=None):
        return {
            "total": len(pairs or []) + len(pre_tasks or []),
            "processed": 0,
            "ok": 0,
            "fail": 0,
            "skipped": 0,
        }

    monkeypatch.setattr(crawl_service, "run_crawl", _run_crawl)


def _stub_bundle_tail(monkeypatch):
    """链尾捆绑包存量刷新打桩：refresh_bundles 短路
    （真实实现会出网）。"""
    from app.domains.bundles import refresh as bundles_refresh

    async def _noop_refresh():
        return {"ok": True, "updated": 0, "total": 0}

    monkeypatch.setattr(bundles_refresh, "refresh_bundles", _noop_refresh)


# ── 空闲门禁核心语义：看任务表，不看 6h 间隔 ──


def test_crawler_idle_when_no_tasks(db):
    """无任务在跑 + 主轮不占线 → 空闲。"""
    crawl_service._active = None
    sched_mod._price_cycle_busy = False
    assert sched_mod._crawler_idle() is True


def test_crawler_busy_when_price_cycle_running(db):
    """主价格刷新轮占线（busy 标志）→ 修复让路——即使任务表空。"""
    crawl_service._active = None
    sched_mod._price_cycle_busy = True
    assert sched_mod._crawler_idle() is False


@pytest.mark.asyncio
async def test_crawler_busy_when_any_job_running(db, monkeypatch):
    """用户手动/任何爬取任务在跑（_active 未完成）→ 修复让路。"""
    async def _forever():
        await asyncio.sleep(999)

    task = asyncio.create_task(_forever())
    try:
        crawl_service._active = crawl_service.JobHandle(
            id=1, task=task, stop_event=asyncio.Event()
        )
        sched_mod._price_cycle_busy = False
        assert sched_mod._crawler_idle() is False
    finally:
        task.cancel()


def test_crawler_idle_when_task_finished(db):
    """任务已 done（_active 残留未清）→ 视为空闲——修复可进场。"""
    async def _done():
        return

    task = asyncio.ensure_future(_done())
    asyncio.get_event_loop_policy()  # noqa: pointless
    # 同步上下文拿不到运行循环里的结果——直接构造已完成的假 task
    class _FakeDone:
        done = staticmethod(lambda: True)

    crawl_service._active = crawl_service.JobHandle(
        id=1, task=_FakeDone(), stop_event=asyncio.Event()  # type: ignore[arg-type]
    )
    sched_mod._price_cycle_busy = False
    assert sched_mod._crawler_idle() is True
    crawl_service._active = None


# ── 修复轮行为（真实 start_job，出网链全打桩）──


@pytest.mark.asyncio
async def test_repair_job_runs_when_idle(db, monkeypatch):
    """空闲档：repair 轮真实走 run_sequential→start_job（run_crawl 打桩），
    job 行 kind=repair 落库——端到端接线验证。"""
    _stub_start_env(monkeypatch)
    sched_mod._price_cycle_busy = False
    crawl_service._active = None

    await sched_mod._job_price_repair()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    kinds = [r.kind for r in rows]
    assert "repair" in kinds, f"repair job 未启动：{kinds}"


@pytest.mark.asyncio
async def test_repair_picks_only_expired_missing(db, monkeypatch):
    """repair 冷却 4min：拾取过期失败行（APPID CN/UA 聚合单任务），
    冷却中（APPID2）与终态 blocked（APPID3）不进。"""
    _stub_start_env(monkeypatch)
    result = await crawl_service.start_job(kind="repair")
    async with db() as session:
        job = await session.get(CrawlJob, result["id"])
    assert job is not None and job.kind == "repair"
    # start_job 摘要 count 按欠账行数计（appid × 区）：APPID 1 个 appid
    # 在 cn/ua 各 1 行欠账 → count = 2
    assert result["count"] == 2, "count 应为欠账行数（APPID × cn/ua 两行）"
    # 生效区注入检查：repair 任务带 regions
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    assert rows and rows[-1].regions_json == ["CN", "UA"]


@pytest.mark.asyncio
async def test_repair_cooldown_semantics(db):
    """冷却 4min 语义：1 分钟前失败的行不可见（repair 查询判定层）。"""
    db_ = DbWriter()
    tasks = await db_.generate_missing_tasks(cooldown_minutes=4)
    covered = {a for t in tasks for a in t.get("appids", [])}
    assert APPID in covered
    assert APPID2 not in covered, "冷却 4min 未过不应被 repair 拾取"
    assert APPID3 not in covered, "blocked 终态不进修复"


@pytest.mark.asyncio
async def test_repair_yields_when_busy(db, monkeypatch):
    """主价格刷新占线（busy）→ 修复轮整体让路（不触 run_sequential）。"""
    called = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)
    sched_mod._price_cycle_busy = True
    crawl_service._active = None
    await sched_mod._job_price_repair()
    assert called == [], "busy 期间修复轮必须让路"


@pytest.mark.asyncio
async def test_repair_yields_when_job_running(db, monkeypatch):
    """任何爬取任务在跑 → 修复让路（看任务表，非 6h 间隔）。"""
    called = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)

    async def _slow():
        await asyncio.sleep(30)

    task = asyncio.create_task(_slow())
    crawl_service._active = crawl_service.JobHandle(
        id=99, task=task, stop_event=asyncio.Event()
    )
    sched_mod._price_cycle_busy = False
    try:
        await sched_mod._job_price_repair()
        assert called == [], "任务在跑时修复轮必须让路"
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_price_refresh_sets_busy_and_drains(db, monkeypatch):
    """主轮 busy 生命周期：置位 → 修复探测让路 → 链完成 → 复位。
    busy 置位于排干等待之前（等待窗口内修复也让路）。"""
    monkeypatch.setattr(external_time, "fetch_pacific_dst",
                        lambda: asyncio.sleep(0, result=(True, "stub")))
    _stub_bundle_tail(monkeypatch)

    states: list[bool] = []

    async def _spy(specs, **kw):
        states.append(sched_mod._price_cycle_busy)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)
    crawl_service._active = None
    await sched_mod._job_price_refresh()
    assert states == [True], "三层链执行期间 busy 必须为 True（修复让路）"
    assert sched_mod._price_cycle_busy is False, "链结束后 busy 必须复位"


@pytest.mark.asyncio
async def test_price_refresh_drains_running_repair_before_chain(db, monkeypatch):
    """排干等待：修复任务在跑时主轮等它结束再开三层链（不撞锁丢 spec）。"""
    monkeypatch.setattr(external_time, "fetch_pacific_dst",
                        lambda: asyncio.sleep(0, result=(True, "stub")))
    _stub_bundle_tail(monkeypatch)
    # 排干轮询提速（10s × 60 太慢）：0.05s × 200
    monkeypatch.setattr(sched_mod, "_DRAIN_POLL_SECONDS", 0.05)
    monkeypatch.setattr(sched_mod, "_DRAIN_MAX_POLLS", 200)

    chain_specs: list[dict] = []

    async def _spy(specs, **kw):
        chain_specs.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)

    release = asyncio.Event()

    async def _repair_running():
        await release.wait()

    repair_task = asyncio.create_task(_repair_running())
    crawl_service._active = crawl_service.JobHandle(
        id=501, task=repair_task, stop_event=asyncio.Event()
    )

    async def _release_soon():
        await asyncio.sleep(0.2)
        release.set()
        await repair_task

    asyncio.create_task(_release_soon())
    sched_mod._price_cycle_busy = False
    try:
        await sched_mod._job_price_refresh()
        # 排干完成 → busy=True 期间三层链才启动
        assert chain_specs, "排干后三层链必须执行"
        assert sched_mod._price_cycle_busy is False
    finally:
        release.set()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await repair_task
        crawl_service._active = None


# ── 失败标志保留语义（账本闭环）──


@pytest.mark.asyncio
async def test_repair_failure_keeps_missing_flag(db):
    """修复仍失败 → mark_region_status('missing') 照常计账：失败标志
    保留、fail_count 递增（穷尽 5 次转 blocked）——账本机制复用，不因
    5min 高频改语义。"""
    db_ = DbWriter()
    await db_.mark_region_status(APPID, "CN", "missing")
    async with db() as session:
        row = await session.get(GameCurrentPrice, (APPID, "CN"))
    assert row is not None
    assert row.price_status == "missing"
    assert row.fail_count == 2  # 播种 1 + 本次失败 1


@pytest.mark.asyncio
async def test_repair_success_clears_ledger(db):
    """修复成功路径：upsert ok 价 fail_count 归零（清账闭环）——
    upsert_game_and_prices 的既有行为，修复通道共享同一写入器。"""
    db_ = DbWriter()
    # 模拟 handler 成功返回：该区 ok 有价
    await db_.upsert_game_and_prices(
        {"appid": APPID, "name": "待修复", "type": "game"},
        [
            {"appid": APPID, "region_code": "CN", "currency": "CNY", "price": 12345,
             "original_price": 12345, "discount_percent": 0, "sub_id": 5,
             "price_status": "ok", "is_gold": False,
             "version_suffix": None, "is_bundle": False},
        ],
    )
    async with db() as session:
        row = await session.get(GameCurrentPrice, (APPID, "CN"))
    assert row is not None
    assert row.price_status == "ok"
    assert row.fail_count == 0, "补抓成功必须清账"


# ── 自动价格链总开关（KV crawl.auto_price）────────
# 关掉只停自动链（主轮 price_refresh + 修复轮 price_repair 到点让路）；
# 手动爬取不受影响——只想手动抓的用户在监控页关这里。


def _set_auto_price(monkeypatch, on: bool):
    """桩 settings.get_value：crawl.auto_price 返回指定值，其余回默认。

    price_auto_enabled 内部 `from ... import get_value` 逐调用解析——
    打桩源模块属性即可（同 _stub_start_env 的 settings_service 桩法）。
    """
    import app.domains.settings.service as settings_service

    async def _value(key, default=None):
        if key == "crawl.auto_price":
            return on
        return default

    monkeypatch.setattr(settings_service, "get_value", _value)


@pytest.mark.asyncio
async def test_price_auto_disabled_blocks_main_cycle(db, monkeypatch):
    """开关关闭 → 主轮到点直接让路：不置 busy、不重锚、不开三层链。"""
    _set_auto_price(monkeypatch, False)

    reanchored = []
    monkeypatch.setattr(
        sched_mod, "_reanchor_price_refresh",
        lambda reason: reanchored.append(reason) or asyncio.sleep(0),
    )
    called = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)
    crawl_service._active = None
    sched_mod._price_cycle_busy = False

    await sched_mod._job_price_refresh()

    assert called == [], "自动价格关闭后主轮不得开爬"
    assert reanchored == [], "关闭期间连重锚都不必（开启后首轮自会重锚）"
    assert sched_mod._price_cycle_busy is False


@pytest.mark.asyncio
async def test_price_auto_disabled_blocks_repair_cycle(db, monkeypatch):
    """开关关闭 → 修复轮让路（自动修复也是自动爬取的一部分）。"""
    _set_auto_price(monkeypatch, False)

    called = []

    async def _spy(specs, **kw):
        called.append(specs)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)
    crawl_service._active = None
    sched_mod._price_cycle_busy = False

    await sched_mod._job_price_repair()
    assert called == [], "自动价格关闭后修复轮不得开爬"


@pytest.mark.asyncio
async def test_price_auto_enabled_allows_main_cycle(db, monkeypatch):
    """开关开启（默认）→ 主轮照常走链：busy 生命周期与开爬不受影响。"""
    _set_auto_price(monkeypatch, True)

    monkeypatch.setattr(external_time, "fetch_pacific_dst",
                        lambda: asyncio.sleep(0, result=(True, "stub")))
    _stub_bundle_tail(monkeypatch)

    states: list[bool] = []

    async def _spy(specs, **kw):
        states.append(sched_mod._price_cycle_busy)
        return []

    monkeypatch.setattr(crawl_service, "run_sequential", _spy)
    crawl_service._active = None
    sched_mod._price_cycle_busy = False

    await sched_mod._job_price_refresh()

    assert states == [True], "开启态主轮必须照常开爬且 busy 置位"
    assert sched_mod._price_cycle_busy is False
