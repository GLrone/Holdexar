"""proxies 域统计测试：pool_stats 的「一个出口 IP 算一个代理」口径。

不触真实网络/内核——monkeypatch ClashRuntime 状态；库隔离到临时 sqlite。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import ClashNode, Proxy, ProxySubscription

SUB_URL = "https://example.com/sub?token=abc"

CONFIG_TEXT = f"""mixed-port: 7890
proxies:
  - name: 节点A
    type: ss
  - name: 节点B
    type: ss
  - name: 节点C
    type: ss
  - name: 节点D
    type: ss
  - name: 节点E
    type: ss
proxy-groups:
  - name: GLOBAL
    type: select
{SUB_URL}
"""


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


@pytest.mark.asyncio
async def test_pool_stats_exit_ip_dedup(db, tmp_path, monkeypatch):
    """Clash 按出口 IP 去重计数；内核没跑时 Clash 一律记 0。

    账本：A/B 同出口 1.2.3.4（算 1 个）、C 出口 5.6.7.8、D 死（出口只进总数）、
    E 出口未知（各自算 1 个）；「旧节点」已不在当前订阅内容 → 不计。
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": True, "port": 7890, "configPath": str(config_path)},
    )

    async with db() as session:
        sub = ProxySubscription(kind="clash", url=SUB_URL)
        session.add(sub)
        await session.flush()
        sub_id = sub.id
        session.add_all(
            [
                ClashNode(subscription_id=sub_id, name="节点A", exit_ip="1.2.3.4", status="ok"),
                ClashNode(subscription_id=sub_id, name="节点B", exit_ip="1.2.3.4", status="ok"),
                ClashNode(subscription_id=sub_id, name="节点C", exit_ip="5.6.7.8", status="ok"),
                ClashNode(subscription_id=sub_id, name="节点D", exit_ip="9.9.9.9", status="dead"),
                ClashNode(subscription_id=sub_id, name="节点E", exit_ip=None, status="ok"),
                ClashNode(subscription_id=sub_id, name="旧节点", exit_ip="8.8.8.8", status="ok"),
            ]
        )
        session.add_all(
            [
                Proxy(scheme="http", host="1.1.1.1", port=8080, enabled=True, status="ok"),
                Proxy(scheme="http", host="2.2.2.2", port=8080, enabled=True, status="failed"),
                Proxy(scheme="http", host="3.3.3.3", port=8080, enabled=False, status="ok"),
            ]
        )
        await session.commit()

    stats = await proxies_service.pool_stats()

    assert stats["pool"] == {"total": 3, "ok": 1}
    assert stats["clash"]["running"] is True
    assert stats["clash"]["subscriptionId"] == sub_id
    assert stats["clash"]["nodes"] == 5  # 旧节点已不在订阅内容，不计
    assert stats["clash"]["okNodes"] == 4
    # A/B 同落地去重 → {1.2.3.4, 5.6.7.8, node:E}；D 的死出口不进可用
    assert stats["clash"]["okExitIps"] == 3
    assert stats["clash"]["exitIps"] == 4  # 死出口 9.9.9.9 只进总数
    assert stats["available"] == 4  # 池 1 + Clash 3
    assert stats["total"] == 7  # 池 3 + Clash 4


@pytest.mark.asyncio
async def test_pool_stats_clash_not_running(db, monkeypatch):
    """内核没跑：账本再健康也没有流量走得到，Clash 一律记 0。"""
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None},
    )
    async with db() as session:
        session.add(Proxy(scheme="http", host="1.1.1.1", port=8080, enabled=True, status="ok"))
        await session.commit()

    stats = await proxies_service.pool_stats()

    assert stats["clash"] == {
        "running": False,
        "subscriptionId": None,
        "nodes": 0,
        "okNodes": 0,
        "exitIps": 0,
        "okExitIps": 0,
    }
    assert stats["available"] == 1
    assert stats["total"] == 1
