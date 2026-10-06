"""直连默认策略 + 订阅导入自动切换 + 结构化诊断端点。

不触真实网络/内核——clash runtime 状态与解析器 monkeypatch；DB 隔离到临时库。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(proxies_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.alerts.models  # noqa: F401
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.proxies.models  # noqa: F401
    import app.domains.proxypool.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_default_strategy_is_direct_only(db):
    """全新库（无策略 KV）→ 默认 direct_only：初次打开软件即直连模式。"""
    info = await proxies_service.get_strategy()
    assert info["strategy"] == "direct_only"


@pytest.mark.asyncio
async def test_autoswitch_on_valid_subscription(db):
    """默认直连 + 导入有效订阅（节点>0）→ 自动切代理优先，返回 True。"""
    switched = await proxies_service.maybe_autoswitch_on_subscription(42)
    assert switched is True
    info = await proxies_service.get_strategy()
    assert info["strategy"] == "proxy_first"


@pytest.mark.asyncio
async def test_autoswitch_respects_explicit_choice(db):
    """用户显式选过策略（set_strategy 打标）→ 自动切换永不覆盖。"""
    await proxies_service.set_strategy("direct_only")
    assert await proxies_service.maybe_autoswitch_on_subscription(42) is False
    info = await proxies_service.get_strategy()
    assert info["strategy"] == "direct_only"


@pytest.mark.asyncio
async def test_autoswitch_noop_cases(db):
    """无效节点 / 已在代理形态 → 静默不切。"""
    assert await proxies_service.maybe_autoswitch_on_subscription(None) is False
    assert await proxies_service.maybe_autoswitch_on_subscription(0) is False
    await proxies_service.set_strategy("proxy_first")
    assert await proxies_service.maybe_autoswitch_on_subscription(42) is False


@pytest.mark.asyncio
async def test_diagnostics_endpoint(db, monkeypatch):
    """诊断端点聚合四面 + 派生信号：失败轮 / 写锁饥饿 / 失败作业都能被机器读到。"""
    from app.core.logging import log_event, ring_log_handler
    from app.domains.crawl import cycle as cycle_module
    from app.domains.crawl import service as crawl_service
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.proxypool import jobruns as jobruns_module
    from app.domains.proxypool.models import ProxyJobRun
    from app.domains.system import router as system_router

    # 账本播种：一轮失败 Cycle + 一次失败作业
    async with db() as session:
        session.add(PriceCycle(
            kind="scheduled", status="failed", error="测试拒因：出口全不可用",
            started_at=datetime(2026, 10, 6, 12, 0, 0),
            finished_at=datetime(2026, 10, 6, 12, 3, 0),
        ))
        session.add(ProxyJobRun(
            status="failed", kind="crawl", started_at=datetime(2026, 10, 6, 12, 0, 0),
        ))
        await session.commit()

    # 各面依赖换成测试替身（端点内 import 是模块属性，运行时解析）
    async def _fake_list_cycles(limit=10):
        async with db() as session:
            from sqlalchemy import select

            rows = (
                await session.execute(
                    select(PriceCycle).order_by(PriceCycle.id.desc()).limit(limit)
                )
            ).scalars().all()
        from app.domains.crawl.cycle import _to_dict

        return [_to_dict(c) for c in rows]

    async def _fake_list_runs(session, *, limit=20, offset=0, now=None):
        async with db() as s2:
            from sqlalchemy import select

            rows = (
                await s2.execute(
                    select(ProxyJobRun).order_by(ProxyJobRun.id.desc()).limit(limit)
                )
            ).scalars().all()
        return {
            "summary": {},
            "total": len(rows),
            "items": [
                {"id": r.id, "status": r.status, "kind": r.kind} for r in rows
            ],
        }

    monkeypatch.setattr(cycle_module, "list_cycles", _fake_list_cycles)
    monkeypatch.setattr(jobruns_module, "list_runs", _fake_list_runs)
    monkeypatch.setattr(crawl_service, "has_pending_missing", lambda: _async_true())
    monkeypatch.setattr(
        clash_manager.runtime, "status", lambda: {"running": False}
    )

    async def _fake_pool_stats():
        return {"available": 0}

    monkeypatch.setattr(proxies_service, "pool_stats", _fake_pool_stats)

    # 写账样本经真实 ring handler（挂 root + 抬 INFO 后发一条，测完还原）
    import logging

    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(ring_log_handler)
    try:
        log_event(
            logging.getLogger("app.crawler.db_writer"),
            "写库分段完成：300 款，共耗时 45000 毫秒",
            detail={"款数": "300", "总计": "45000毫秒", "等写锁": "45000毫秒"},
        )
        payload = await system_router.diagnostics()
    finally:
        root.removeHandler(ring_log_handler)
        root.setLevel(old_level)

    assert payload["identity"]["strategy"] == "direct_only"
    assert payload["cycles"]["last"]["status"] == "failed"
    codes = {s["code"] for s in payload["signals"]}
    assert "last_cycle_failed" in codes
    assert "write_lock_wait_high" in codes
    assert "job_runs_failed" in codes
    assert "missing_backlog" in codes
    # proxy_first/only 才产「池不可用」信号：默认直连不该产
    assert "proxy_pool_unavailable" not in codes


async def _async_true():
    return True
