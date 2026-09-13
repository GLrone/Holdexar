"""proxies 域策略解析测试：proxy_first 分支（Clash 在跑 → 内核端口）+ resolve 端点语义。

不触真实网络/内核——monkeypatch ClashRuntime 状态；settings 键值层隔离到临时库。
"""
import sys
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
    import app.domains.rates.models  # noqa: F401
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_resolve_proxy_first_clash_running(db, monkeypatch):
    """proxy_first + 内核在跑 → 返回内核混合端口（登录窗代理优先的取数源）。"""
    await settings_service.set_value("proxy.strategy", "proxy_first")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": True, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )
    url = await proxies_service.resolve_proxy_url()
    assert url == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_resolve_proxy_first_nothing_falls_back_direct(db, monkeypatch):
    """proxy_first + 无内核无池 + 本地混合端口死 → None（直连兜底，不报错）。

    端口探活 mock 为死：真实机器上用户自启的 Clash 可能活着（会让
    本用例假失败），测试语义是「真的一无所有时直连」。
    """
    await settings_service.set_value("proxy.strategy", "proxy_first")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None, "controllerUrl": None},
    )
    async def _dead() -> bool:
        return False

    monkeypatch.setattr(proxies_service, "_local_clash_alive", _dead)
    assert await proxies_service.resolve_proxy_url() is None


@pytest.mark.asyncio
async def test_resolve_proxy_first_local_clash_port_alive(db, monkeypatch):
    """proxy_first + 无自管内核无池 + 本地混合端口活着（用户自启 Verge）→ 走该端口。

    steam 域直连基本被墙，活着的本地代理永远优于直连兜底。
    """
    await settings_service.set_value("proxy.strategy", "proxy_first")
    await settings_service.set_value("proxy.clash_port", 7890)
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None, "controllerUrl": None},
    )
    async def _alive() -> bool:
        return True

    monkeypatch.setattr(proxies_service, "_local_clash_alive", _alive)
    assert await proxies_service.resolve_proxy_url() == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_resolve_direct_only(db):
    """direct_only 策略 → 恒直连。"""
    await settings_service.set_value("proxy.strategy", "direct_only")
    assert await proxies_service.resolve_proxy_url() is None


@pytest.mark.asyncio
async def test_removed_strategies_migrate_to_proxy_first(db):
    """下架策略迁移：pinned/clash 存量库值被 get_strategy 一次性迁移到
    proxy_first（含返回体不再带 pinnedId），pinned_id 设置键清账。"""
    for removed in ("pinned", "clash"):
        await settings_service.set_value("proxy.strategy", removed)
        await settings_service.set_value("proxy.pinned_id", 42)
        info = await proxies_service.get_strategy()
        assert info["strategy"] == "proxy_first"
        assert "pinnedId" not in info  # 字段随策略下架移除
        assert await settings_service.get_value("proxy.pinned_id") is None


@pytest.mark.asyncio
async def test_set_strategy_rejects_removed_strategy(db):
    """下架后 set_strategy 对 pinned/clash 报未知策略（前端已无入口，API 层兜底）。"""
    for removed in ("pinned", "clash"):
        with pytest.raises(ValueError, match="未知策略"):
            await proxies_service.set_strategy(removed)
