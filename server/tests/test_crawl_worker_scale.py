"""主轮 worker 数测试：按可用出口 IP 节点数开启。

被测：`crawl/service._resolve_worker_count`（start_job 启动期解析）。
规则：一个可用出口 IP 开一个 worker，上限 WORKERS_MAX（60）；
显式配置 crawl.workers（正整数）优先；无出口数据 / 统计失败回退
DEFAULT_WORKER_COUNT（直连标准形态行为不变）。

隔离：库隔离到临时 sqlite（crawl / proxies / settings 三处模块级工厂打桩），
Clash 标记为未运行——available 只由手动池贡献（「一个出口 IP 算一个」口径
见 test_proxies_stats.py），不触真实网络/内核/生产库。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.crawler.config import DEFAULT_WORKER_COUNT, WORKERS_MAX
from app.domains.crawl import service as crawl_service
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import Proxy
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    for mod in (crawl_service, proxies_service, settings_service):
        monkeypatch.setattr(mod, "get_session_factory", lambda: factory)
    # 内核没跑：Clash 一律记 0，available 只由手动池贡献
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None},
    )
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.proxies.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_proxies(db, *, ok=0, failed=0, disabled=0) -> None:
    """种手动池行：ok = 可用出口；failed/disabled 不进 available。"""
    async with db() as session:
        for i in range(ok):
            session.add(Proxy(scheme="http", host=f"10.0.0.{i + 1}", port=8080,
                              enabled=True, status="ok"))
        for i in range(failed):
            session.add(Proxy(scheme="http", host=f"10.0.1.{i + 1}", port=8080,
                              enabled=True, status="failed"))
        for i in range(disabled):
            session.add(Proxy(scheme="http", host=f"10.0.2.{i + 1}", port=8080,
                              enabled=False, status="ok"))
        await session.commit()


@pytest.mark.asyncio
async def test_no_exit_ip_falls_back_to_default(db):
    """无出口数据（直连形态）→ 回退默认 30，行为与既有版本一致。"""
    assert await crawl_service._resolve_worker_count() == DEFAULT_WORKER_COUNT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ok", "failed", "disabled", "expected"),
    [
        (3, 2, 1, 3),             # 一比一（failed/disabled 不计入）
        (12, 2, 1, 12),
        (80, 5, 5, WORKERS_MAX),  # 出口数超上限 → 封顶 60
    ],
)
async def test_one_worker_per_exit_ip(db, ok, failed, disabled, expected):
    """一个可用出口 IP 开一个 worker，上限 WORKERS_MAX。"""
    await _seed_proxies(db, ok=ok, failed=failed, disabled=disabled)
    assert await crawl_service._resolve_worker_count() == expected


@pytest.mark.asyncio
async def test_explicit_setting_wins(db):
    """显式配置 crawl.workers → 尊重显式值，分类不介入。"""
    await _seed_proxies(db, ok=12)
    await settings_service.set_value("crawl.workers", 17)
    assert await crawl_service._resolve_worker_count() == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5, "abc", True])
async def test_invalid_explicit_value_falls_through(db, bad):
    """脏配置（0/负数/非数字/布尔）不生效 → 走出口数分类。"""
    await _seed_proxies(db, ok=12)
    await settings_service.set_value("crawl.workers", bad)
    assert await crawl_service._resolve_worker_count() == 12


@pytest.mark.asyncio
async def test_stats_failure_falls_back(db, monkeypatch):
    """统计不可用（异常）→ 回退默认 30，不阻断爬取。"""
    async def _boom():
        raise RuntimeError("stats down")

    monkeypatch.setattr(proxies_service, "pool_stats", _boom)
    assert await crawl_service._resolve_worker_count() == DEFAULT_WORKER_COUNT
