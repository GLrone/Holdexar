"""自动爬取代理前置闸门测试。

规则：未保存订阅（无可用代理）时，一切**自动**路径的爬取（调度器
价格刷新/失败修复/账户同步追加首爬/榜单反哺/CS 重探）一律不执行——
不许在无代理下降级直连硬打 Steam 41 区；只有用户手动启动（路由层
POST /crawl/run 直通）可以强制直连。direct_only/direct_first 是用户
显式配置的直连策略 = 授权直连，闸门放行。

数据事故背景：用户未保存订阅，服务启动 5 分钟后 wishlist_sync 自动首爬
直连全 41 区（proxy_first 的 resolve_proxy_url 层层兜底最后 return
None 直连）——「没订阅就自己开抓」被用户判定为 bug。

隔离：resolve_proxy_url/get_strategy/clash runtime 打桩——不触生产
库、不发真实网络请求（记忆坑位：域服务模块级 import 的 factory 不
打桩就静默读生产库）。
"""
import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import external_time
from app.core import scheduler as sched_mod
from app.core.database import Base
from app.domains.crawl import service as crawl_service
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
from app.crawler.utils import get_beijing_time_obj


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    import app.crawler.db_writer as dw

    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    crawl_service._active = None
    sched_mod._price_cycle_busy = False


def _proxy_env(monkeypatch, *, strategy="proxy_first", proxy="http://127.0.0.1:7890"):
    """代理策略全打桩：strategy + resolve 结果。proxy=None = 无可用代理。"""
    from app.domains.proxies import service as proxies_service

    async def _strategy():
        return {"strategy": strategy}

    async def _resolve():
        if strategy == "proxy_only" and proxy is None:
            raise RuntimeError("策略 proxy_only 需要至少一条启用的代理")
        return proxy

    monkeypatch.setattr(proxies_service, "get_strategy", _strategy)
    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _resolve)


def _crawl_env(monkeypatch):
    """start_job 环境其余依赖打桩（同 test_price_repair 模式）。"""
    async def _regions(regions=None):
        return ["CN", "UA"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async def _value(key, default=None):
        return default

    import app.domains.settings.service as settings_service

    monkeypatch.setattr(settings_service, "get_value", _value)

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


def _stub_bundles_refresh(monkeypatch, calls):
    """链尾捆绑包存量刷新打桩（真实实现会出网并触生产库）；调用记入 calls。"""
    from app.domains.bundles import refresh as bundles_refresh

    async def _noop():
        calls.append(True)
        return {"ok": True, "updated": 0, "total": 0, "regionPrices": 0,
                "failed": [], "droppedSingletons": 0}

    monkeypatch.setattr(bundles_refresh, "refresh_bundles", _noop)


async def _seed_target(db):
    """种一个过期 missing 欠账行（修复轮拾取目标；25h 前落账，
    过主轮 missing 层 24h 冷却）。"""
    from datetime import timedelta

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with db() as session:
        session.add(Game(appid=998001, name="闸门目标", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=998001, region_code="CN", currency="", price=None,
            sub_id=None, price_status="missing", fail_count=1,
            updated_at=now - timedelta(hours=25),
        ))
        await session.commit()


# ── ensure_proxy_available 判定矩阵 ──


@pytest.mark.asyncio
async def test_gate_blocks_when_no_proxy(db, monkeypatch):
    """proxy_first + 解析结果直连（None）= 无可用代理 → 自动路径拦。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    with pytest.raises(ValueError, match="无可用代理"):
        await crawl_service.ensure_proxy_available()


@pytest.mark.asyncio
async def test_gate_passes_with_proxy(db, monkeypatch):
    """proxy_first + 有可用代理 → 放行。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy="http://127.0.0.1:7890")
    await crawl_service.ensure_proxy_available()  # 不抛即过


@pytest.mark.asyncio
async def test_gate_passes_direct_strategies(db, monkeypatch):
    """direct_only / direct_first = 用户显式授权直连 → 自动路径也放行。"""
    _proxy_env(monkeypatch, strategy="direct_only", proxy=None)
    await crawl_service.ensure_proxy_available()
    _proxy_env(monkeypatch, strategy="direct_first", proxy=None)
    await crawl_service.ensure_proxy_available()


# ── start_job / run_sequential 接线 ──


@pytest.mark.asyncio
async def test_start_job_scheduler_flag_gated(db, monkeypatch):
    """from_scheduler=True + 无代理 → ValueError 拦截（不启动任务）。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    _crawl_env(monkeypatch)
    with pytest.raises(ValueError, match="无可用代理"):
        await crawl_service.start_job(
            scope="appids", appids=[998001], from_scheduler=True
        )
    assert crawl_service._active is None, "被闸门拦下时不得留任务"


@pytest.mark.asyncio
async def test_start_job_manual_direct_allowed(db, monkeypatch):
    """手动启动（from_scheduler 默认 False）+ 无代理 → 放行直连
    ——用户强制执行不受限。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    _crawl_env(monkeypatch)
    result = await crawl_service.start_job(scope="appids", appids=[998001])
    assert result["count"] == 1
    assert crawl_service._active is not None
    await crawl_service._active.task


@pytest.mark.asyncio
async def test_run_sequential_gates_all_specs(db, monkeypatch):
    """run_sequential(from_scheduler=True) 整链过闸：无代理时每个 spec
    都被跳过，返回空（既有 ValueError 跳过语义），不炸不启动。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    _crawl_env(monkeypatch)
    results = await crawl_service.run_sequential(
        [{"scope": "appids", "appids": [998001], "kind": "scheduled"}],
        from_scheduler=True,
    )
    assert results == []
    assert crawl_service._active is None


# ── 调度器自动 job 真实接线 ──


@pytest.mark.asyncio
async def test_repair_job_gated_without_proxy(db, monkeypatch):
    """修复轮（5min 空闲档）无代理 → 闸门拦，不产生 job 行。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    _crawl_env(monkeypatch)
    await _seed_target(db)
    sched_mod._price_cycle_busy = False
    crawl_service._active = None

    await sched_mod._job_price_repair()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    assert rows == [], "无可用代理时修复轮不得启动任何任务"


@pytest.mark.asyncio
async def test_repair_job_runs_with_proxy(db, monkeypatch):
    """修复轮有可用代理 → 正常启动 kind=repair job（闸门不误伤）。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy="http://127.0.0.1:7890")
    _crawl_env(monkeypatch)
    await _seed_target(db)
    sched_mod._price_cycle_busy = False
    crawl_service._active = None

    await sched_mod._job_price_repair()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    assert [r.kind for r in rows] == ["repair"]


@pytest.mark.asyncio
async def test_price_refresh_gated_without_proxy(db, monkeypatch):
    """主价格刷新轮无代理 → 三层链整轮不启动（闸门在 run_sequential 层拦
    每个 spec）；链尾捆绑包存量刷新同过闸门，一样让路；重锚照常执行
    （排程不受闸门影响）。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy=None)
    _crawl_env(monkeypatch)
    bundle_calls: list = []
    _stub_bundles_refresh(monkeypatch, bundle_calls)
    await _seed_target(db)
    crawl_service._active = None

    monkeypatch.setattr(external_time, "fetch_pacific_dst",
                        lambda: asyncio.sleep(0, result=(True, "stub")))

    await sched_mod._job_price_refresh()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    assert rows == [], "无可用代理时主价格刷新不得启动任何任务"
    assert bundle_calls == [], "无可用代理时链尾捆绑包刷新也不得执行"
    assert sched_mod._price_cycle_busy is False, "busy 必须正常复位"


@pytest.mark.asyncio
async def test_price_refresh_runs_with_proxy(db, monkeypatch):
    """主价格刷新轮有代理 → 闸门放行，三层链正常走（至少 missing 层启动），
    链尾捆绑包存量刷新跟进。"""
    _proxy_env(monkeypatch, strategy="proxy_first", proxy="http://127.0.0.1:7890")
    _crawl_env(monkeypatch)
    bundle_calls: list = []
    _stub_bundles_refresh(monkeypatch, bundle_calls)
    await _seed_target(db)
    crawl_service._active = None

    monkeypatch.setattr(external_time, "fetch_pacific_dst",
                        lambda: asyncio.sleep(0, result=(True, "stub")))

    await sched_mod._job_price_refresh()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    kinds = [r.kind for r in rows]
    assert "missing" in kinds, f"有代理时主轮 missing 层应启动：{kinds}"
    assert bundle_calls, "有代理时链尾捆绑包存量刷新应跟进"


@pytest.mark.asyncio
async def test_scheduler_owned_jobs_pass_flag(db, monkeypatch):
    """scheduler.py 侧自动入口全部带 from_scheduler=True——静态接线
    断言：每个 run_sequential( 调用点都有配对的 from_scheduler=True
    （无括号注释不计数；数量相等 = 无漏网入口）。"""
    import inspect

    src = inspect.getsource(sched_mod)
    calls = src.count("run_sequential(")
    flagged = src.count("from_scheduler=True")
    assert calls >= 5, f"自动入口应不少于 5 处，实际 {calls}"
    assert calls == flagged, (
        f"run_sequential 调用 {calls} 处，带 from_scheduler=True 的只有 {flagged} 处"
        "——存在未过闸的自动入口"
    )
