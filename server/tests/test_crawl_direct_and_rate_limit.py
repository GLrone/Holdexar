"""自动爬取直连放行 + 全局限流测试。

规则变更（browse 接口时代）：browse 按 country_code 参数返回各区价格，
出口 IP 不参与数据判定——直连成为标准形态（一般用户的加速器在系统网络
层透明生效，应用侧无需代理）。此前的「无可用代理不自动爬」前置闸门
整体退役：自动路径（调度器价格刷新/失败修复/账户同步追加首爬/榜单
反哺/CS 重探）无代理也照常启动，请求频率改由全局滑动窗口限流
（200 发/5 分钟，crawler/rate_limit.py）统一约束。

历史背景：闸门是 appdetails 时代的产物——当时 Steam 域直连基本不可用，
未保存订阅时自动任务直连全 41 区既浪费请求又拿不到数据。

隔离：不出网、不触生产库（域服务模块级 import 的 factory 打桩）。
"""
import asyncio
import sys
import time
from collections import deque
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
from app.crawler.rate_limit import SlidingWindowRateLimiter, steam_rate_limiter
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


def _crawl_env(monkeypatch):
    """start_job 环境依赖打桩（同 test_price_repair 模式）——不触代理策略
    引擎（直连形态下 start_job 不再解析代理）。"""
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
        session.add(Game(appid=998001, name="直连目标", created_at=now, updated_at=now))
        session.add(GameCurrentPrice(
            appid=998001, region_code="CN", currency="", price=None,
            sub_id=None, price_status="missing", fail_count=1,
            updated_at=now - timedelta(hours=25),
        ))
        await session.commit()


# ── 自动路径无代理照常启动（旧闸门退役的反向断言） ──


@pytest.mark.asyncio
async def test_start_job_without_runtime_refuses_to_start(db, monkeypatch):
    """受管爬取改为 fail closed：拿不到池 Runtime 就拒绝启动。

    旧契约（"无代理 → 直连是标准形态"）已被 P1.6-C 取代：静默退直连会把"池坏了"
    伪装成"爬取成功"。此测试随契约一起改，不是被删。
    """
    import app.domains.proxypool.runtime as pp_runtime
    from app.domains.proxypool.runtime import RuntimeUnavailableError

    _crawl_env(monkeypatch)
    # 确定性：不看本机是否有池 Runtime，直接声明"不可用"
    monkeypatch.setattr(pp_runtime, "current_runtime_proxy_url", lambda _d=None: None)
    with pytest.raises(RuntimeUnavailableError):
        await crawl_service.start_job(
            scope="appids", appids=[998001], kind="scheduled"
        )
    assert crawl_service._active is None, "拒绝启动不得留下活动任务"


@pytest.mark.asyncio
async def test_run_sequential_without_runtime_starts_nothing(db, monkeypatch):
    """无池 Runtime 时每个 spec 都启动不了（不再整链放行直连）。"""
    import app.domains.proxypool.runtime as pp_runtime

    _crawl_env(monkeypatch)
    monkeypatch.setattr(pp_runtime, "current_runtime_proxy_url", lambda _d=None: None)
    results = await crawl_service.run_sequential(
        [{"scope": "appids", "appids": [998001], "kind": "scheduled"}],
    )
    assert results == []
    assert crawl_service._active is None  # 链尾已清
    assert crawl_service._active is None  # 链尾已清


@pytest.mark.asyncio
async def test_repair_job_runs_with_runtime(db, monkeypatch):
    """修复轮（5min 空闲档）在**有池 Runtime** 时照常启动 kind=repair job。

    P1.6-C 起爬取前置条件变成"池 Runtime 可用"，所以这里给一个确定性的可用运行时；
    "拿不到就拒绝"由 fail-closed 那两条覆盖。
    """
    import app.domains.proxypool.runtime as pp_runtime

    _crawl_env(monkeypatch)
    monkeypatch.setattr(pp_runtime, "current_runtime_proxy_url",
                        lambda _d=None: "http://127.0.0.1:1")
    await _seed_target(db)
    sched_mod._price_cycle_busy = False
    crawl_service._active = None

    await sched_mod._job_price_repair()
    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(CrawlJob)
        )).scalars().all()
    assert [r.kind for r in rows] == ["repair"], "有池 Runtime 时修复轮要能启动"


@pytest.mark.asyncio
async def test_price_refresh_runs_with_runtime(db, monkeypatch):
    """主价格刷新轮（有池 Runtime）两层链照常（missing 层启动），
    链尾捆绑包存量刷新跟进；重锚照常执行。"""
    import app.domains.proxypool.runtime as pp_runtime

    _crawl_env(monkeypatch)
    monkeypatch.setattr(pp_runtime, "current_runtime_proxy_url",
                        lambda _d=None: "http://127.0.0.1:1")
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
    assert "missing" in kinds, f"有池 Runtime 时主轮 missing 层应启动：{kinds}"
    assert bundle_calls, "链尾捆绑包存量刷新应执行"
    assert sched_mod._price_cycle_busy is False, "busy 必须正常复位"


# ── 全局滑动窗口限流 ──


@pytest.mark.asyncio
async def test_rate_limiter_allows_within_window():
    """窗口内前 N 发直接放行（不等待）。"""
    lim = SlidingWindowRateLimiter(3, 300.0)
    start = time.monotonic()
    for _ in range(3):
        await lim.acquire()
    assert time.monotonic() - start < 0.5, "窗口内取号不得等待"


@pytest.mark.asyncio
async def test_rate_limiter_blocks_beyond_window(monkeypatch):
    """第 N+1 发阻塞：等到最旧一条出窗才放行（时间加速验证）。"""
    lim = SlidingWindowRateLimiter(2, 0.3)
    await lim.acquire()
    await lim.acquire()
    # 窗口满：acquire 应阻塞到首发出窗（~0.3s）后放行
    sleeps: list[float] = []

    async def _fast_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(
        "app.crawler.rate_limit.asyncio.sleep", _fast_sleep
    )
    await lim.acquire()
    assert sleeps, "窗口满时必须等待（测试中 sleep 被替换为记录）"
    assert sleeps[0] <= 0.3 + 1e-6, "等待时长 = 最旧请求出窗剩余时间"


@pytest.mark.asyncio
async def test_rate_limiter_window_slides(monkeypatch):
    """窗口滑动：时间推进后旧名额出窗，新请求立即可取。"""
    lim = SlidingWindowRateLimiter(2, 0.2)
    # 手动把两条时间戳回拨到窗外（模拟 0.2s 已流逝）
    await lim.acquire()
    await lim.acquire()
    lim._timestamps = deque(t - 1.0 for t in lim._timestamps)
    start = time.monotonic()
    await lim.acquire()
    assert time.monotonic() - start < 0.5, "旧时间戳出窗后应立即可取"


@pytest.mark.asyncio
async def test_rate_limiter_steam_singleton_shape():
    """进程单例形状：200 发 / 300s 窗口（实测定线）。"""
    assert steam_rate_limiter.max_requests == 200
    assert steam_rate_limiter.window_seconds == 300


# ── 调度器自动 job 静态接线（回归锚） ──


def test_scheduler_owned_jobs_no_gate_flag():
    """scheduler.py 侧自动入口不再携带代理闸门标志——静态断言：
    from_scheduler 已整体退役（出现即说明有人复活旧闸门接线）。"""
    import inspect

    src = inspect.getsource(sched_mod)
    assert "from_scheduler" not in src, "from_scheduler 闸门已退役，不得复活"
    assert src.count("run_sequential(") >= 5, "自动入口应不少于 5 处"
