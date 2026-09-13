"""断网容错三件套测试：本机断网判定 / 代理池加权选择 / direct_first 失败换代理。

不触真实网络——mock ping 与代理池；库隔离到临时文件。
"""
import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.crawler import network_check
from app.crawler.http_client import SteamHttpClient
from app.crawler.network_check import NetworkChecker
from app.domains.proxies import service as proxies_service
from app.domains.proxies.models import Proxy


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(proxies_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.proxies.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 每用例干净的检测器实例（模块单例会被跨用例污染）
    network_check.network_checker = NetworkChecker()


# ─── 断网检测器 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_offline_confirm_after_ping_failures():
    """连续传输失败 3 次 + ping 3 连败 → 判定断网。"""
    checker = NetworkChecker()
    checker._ping_once = lambda: _async_false()  # type: ignore[method-assign]
    for _ in range(network_check.FAILURE_THRESHOLD):
        await checker.report_failure()
    assert checker.is_offline is True


@pytest.mark.asyncio
async def test_ping_ok_resets_failures_not_offline():
    """失败达阈值但 ping 通 → 网络正常（失败源在代理/目标侧），不判断网。"""
    checker = NetworkChecker()
    checker._ping_once = lambda: _async_true()  # type: ignore[method-assign]
    for _ in range(network_check.FAILURE_THRESHOLD):
        await checker.report_failure()
    assert checker.is_offline is False
    assert checker._failure_count == 0


@pytest.mark.asyncio
async def test_report_success_overrides_offline():
    """真实请求成功直接推翻断网判定（网络已恢复的最高优先级事实）。"""
    checker = NetworkChecker()
    checker._ping_once = lambda: _async_false()  # type: ignore[method-assign]
    for _ in range(network_check.FAILURE_THRESHOLD):
        await checker.report_failure()
    assert checker.is_offline is True
    checker.report_success()
    assert checker.is_offline is False


@pytest.mark.asyncio
async def test_wait_until_online_stoppable():
    """断网等待模式：stop_event 置位 → 返回 False（用户停止可打断）。"""
    checker = NetworkChecker()
    checker._ping_once = lambda: _async_false()  # type: ignore[method-assign]
    checker._is_offline = True
    stop = asyncio.Event()
    stop.set()
    recovered = await checker.wait_until_online(stop)
    assert recovered is False


@pytest.mark.asyncio
async def test_wait_until_online_recovers():
    """断网等待模式：ping 通 → 返回 True 且状态清零。"""
    checker = NetworkChecker()
    checker._ping_once = lambda: _async_true()  # type: ignore[method-assign]
    checker._is_offline = True
    recovered = await checker.wait_until_online(None)
    assert recovered is True
    assert checker.is_offline is False


async def _async_true() -> bool:
    return True


async def _async_false() -> bool:
    return False


# ─── 手动池加权随机选择 ─────────────────────────────────────


async def _seed_pool(db, specs: list[tuple[int, int, int]]) -> list[int]:
    """种子代理池：[(latency_ms, consecutive_failures, enabled)]，返回 id 列表。"""
    ids = []
    async with db() as session:
        for i, (latency, fails, enabled) in enumerate(specs):
            p = Proxy(
                label=f"p{i}", scheme="http", host="10.0.0.1", port=8000 + i,
                enabled=bool(enabled), status="ok" if fails == 0 else "failed",
                latency_ms=latency, consecutive_failures=fails,
            )
            session.add(p)
        await session.commit()
        for p in (await session.execute(
            __import__("sqlalchemy").select(Proxy)
        )).scalars().all():
            ids.append(p.id)
    return ids


@pytest.mark.asyncio
async def test_weighted_pick_prefers_healthy_low_latency(db):
    """加权语义：健康快节点权重远高于慢节点/濒死节点。

    权重分布：p0(50ms健康)=99950 / p1(30s慢)=70000 / p2(失败4次)≈6243
    → p0 期望份额 ~57%，p2 期望份额 ~3.5%（50 抽固定种子验证）
    """
    import random

    random.seed(42)
    await _seed_pool(db, [
        (50, 0, 1),      # 快而健康 → 权重 ~99950（期望份额 57%）
        (30000, 0, 1),   # 30s 慢 → 权重 ~70000（期望份额 40%）
        (100, 4, 1),     # 连续失败 4 次未到禁用线 → 指数降权（期望份额 3.5%）
    ])
    enabled = await proxies_service.list_proxies(enabled_only=True)
    picks = [await proxies_service._weighted_pick(enabled) for _ in range(50)]
    from collections import Counter

    counter = Counter(picks)
    best = "http://10.0.0.1:8000"
    degraded = "http://10.0.0.1:8002"
    assert counter[best] > counter[degraded] * 4  # 健康快节点显著多于濒死节点
    assert counter[best] >= 20  # 且占多数（期望 57%，2σ 下界）


@pytest.mark.asyncio
async def test_weighted_pick_dead_node_never_chosen(db):
    """连续失败达禁用线（5 次）→ 权重 0，永不被选。"""
    await _seed_pool(db, [
        (50, 0, 1),
        (200, 5, 1),   # 濒死：权重 0
        (300, 5, 1),   # 濒死：权重 0
    ])
    for _ in range(30):
        url = await proxies_service._weighted_pick(await proxies_service.list_proxies(enabled_only=True))
        assert url == "http://10.0.0.1:8000"


@pytest.mark.asyncio
async def test_weighted_pick_all_dead_falls_back_round_robin(db, monkeypatch):
    """全池权重 0 → 退回顺序轮询（策略引擎不抛错）。"""
    await _seed_pool(db, [(100, 5, 1), (200, 5, 1)])
    # _round_robin 内部局部 import settings service —— patch 其模块级工厂
    from app.domains.settings import service as settings_service

    monkeypatch.setattr(settings_service, "get_session_factory", lambda: db)
    enabled = await proxies_service.list_proxies(enabled_only=True)
    url = await proxies_service._weighted_pick(enabled)
    assert url.startswith("http://10.0.0.1")


# ─── direct_first 失败换代理 ─────────────────────────────────


@pytest.mark.asyncio
async def test_failover_on_transport_error():
    """直连传输失败 → resolver 取代理 → 换出口重试成功。"""
    import aiohttp

    client = SteamHttpClient(timeout=2, max_retries=3, proxy_url=None)
    switched = []

    async def _resolver() -> str | None:
        switched.append(1)
        return "http://127.0.0.1:7897"

    client.failover_proxy_resolver = _resolver

    calls = {"n": 0}

    class FakeResp:
        status = 200

        async def json(self):
            return {"620": {"success": True}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None, proxy=None):
            calls["n"] += 1
            # 第一次直连：传输层失败（模拟不可达）
            # 第二次（已换代理）：成功
            if calls["n"] == 1:
                raise aiohttp.ClientConnectionError("connection refused")
            return FakeResp()

    data = await client.get_json(FakeSession(), "https://store.example/api", appid=620)
    assert data is not None
    assert client.proxy_url == "http://127.0.0.1:7897"
    assert switched, "resolver 应被调用"


@pytest.mark.asyncio
async def test_failover_skipped_when_same_exit():
    """resolver 返回与当前相同出口（无处可换）→ 沿用退避重试，不死循环。"""
    import aiohttp

    client = SteamHttpClient(timeout=2, max_retries=3, proxy_url="http://127.0.0.1:7897")

    async def _resolver() -> str | None:
        return "http://127.0.0.1:7897"  # 同一出口

    client.failover_proxy_resolver = _resolver
    network_check.network_checker = NetworkChecker()  # 隔离上报副作用

    calls = {"n": 0}

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None, proxy=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise aiohttp.ClientConnectionError("refused")
            class FakeResp:
                status = 200
                async def json(self):
                    return {"ok": True}
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
            return FakeResp()

    data = await client.get_json(FakeSession(), "https://store.example/api", appid=1)
    assert data == {"ok": True}
    assert client.proxy_url == "http://127.0.0.1:7897"  # 未被同出口覆盖


@pytest.mark.asyncio
async def test_failover_on_429():
    """真 429 → 熔断触发 + 换代理出口重试成功。"""
    client = SteamHttpClient(timeout=2, max_retries=3, proxy_url=None)
    from app.crawler.http_client import global_429_breaker
    await global_429_breaker.reset()

    async def _resolver() -> str | None:
        return "http://127.0.0.1:7897"

    client.failover_proxy_resolver = _resolver

    calls = {"n": 0}

    class Fake429Resp:
        status = 429
        url = "https://store.example/api"  # 熔断器日志取 str(response.url)

        async def text(self):
            return "Too Many Requests"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeResp:
        status = 200

        async def json(self):
            return {"620": {"success": True}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None, proxy=None):
            calls["n"] += 1
            return Fake429Resp() if calls["n"] == 1 else FakeResp()

    data = await client.get_json(FakeSession(), "https://store.example/api", appid=620)
    assert data is not None
    assert client.proxy_url == "http://127.0.0.1:7897"
    await global_429_breaker.reset()
