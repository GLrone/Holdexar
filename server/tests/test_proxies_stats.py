"""proxies 域统计测试：pool_stats 的「一个出口 IP 算一个代理」口径。

不触真实网络/内核——monkeypatch ClashRuntime 状态；库隔离到临时 sqlite。
"""
import sys
from pathlib import Path

import pytest
import yaml
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import Proxy, ProxySubscription
from app.domains.proxypool.models import ProxyNode, node_fingerprint, subscription_tag

SUB_URL = "https://example.com/sub?token=abc"


@pytest.mark.asyncio
async def test_expired_subscription_old_healthy_nodes_are_not_available(db, monkeypatch):
    monkeypatch.setattr(clash_manager.runtime, "_startup_text", yaml.safe_dump({"proxies": [NODE_CONFIGS["one"]]}))
    async with db() as session:
        sub = ProxySubscription(kind="clash", url=SUB_URL, last_stats={"traffic": "expire=1"})
        session.add(sub)
        await session.flush()
        sid = sub.id
        session.add(ProxyNode(
            node_id=node_fingerprint(NODE_CONFIGS["one"], subscription_tag(SUB_URL)), subscription_id=sid, original_name="one",
            runtime_name=f"{sid}|one", proxy_type="http", normalized_config=NODE_CONFIGS["one"],
            state="ACTIVE", last_l2_at=proxies_service.get_beijing_time_obj(),
        ))
        await session.commit()
    monkeypatch.setattr(clash_manager.runtime, "status", lambda: {
        "running": True, "port": 7890, "configPath": None, "subscriptionUrl": SUB_URL,
    })
    stats = await proxies_service.pool_stats()
    assert stats["clash"]["nodes"] == 1
    assert stats["clash"]["okNodes"] == 0 and stats["available"] == 0

NODE_CONFIGS = {name: {"name": name, "type": "http", "server": "127.0.0.1", "port": 10000+i}
                for i,name in enumerate(["节点A", "节点B", "节点C", "节点D", "节点E", "旧节点", "one"])}
CONFIG_TEXT = yaml.safe_dump({"mixed-port": 7890, "proxies": [NODE_CONFIGS[name] for name in list(NODE_CONFIGS)[:5]]})


@pytest.fixture(autouse=True)
def _runtime_facts(monkeypatch):
    monkeypatch.setattr(clash_manager.runtime, "subscription_url", SUB_URL)
    monkeypatch.setattr(clash_manager.runtime, "_startup_text", CONFIG_TEXT)


@pytest.mark.asyncio
async def test_running_old_url_remains_readable_without_rebinding_source(db, monkeypatch):
    from sqlalchemy import select
    config = NODE_CONFIGS["one"]
    identity = node_fingerprint(config, subscription_tag(SUB_URL))
    async with db() as session:
        sub = ProxySubscription(kind="clash", url="https://example.test/new-url")
        session.add(sub)
        await session.flush()
        sub_id = sub.id
        session.add(ProxyNode(node_id=identity, runtime_name=f"{sub_id}|one", subscription_id=None,
                              original_name="one", proxy_type="http", normalized_config=config,
                              state="ACTIVE", last_l2_at=proxies_service.get_beijing_time_obj()))
        await session.commit()
    status = {"running": True, "configPath": f"clash-sub-{sub_id}.runtime.yaml"}
    monkeypatch.setattr(clash_manager.runtime, "_startup_text", yaml.safe_dump({"proxies": [config]}))
    monkeypatch.setattr(clash_manager.runtime, "status", lambda: status)
    assert (await proxies_service.pool_stats())["clash"]["nodes"] == 1
    assert (await proxies_service.clash_nodes_overview())["nodes"][0]["name"] == "one"
    with pytest.raises(ValueError, match="不一致"):
        await proxies_service._runtime_check_nodes(status, None)
    async with db() as session:
        assert (await session.scalar(select(ProxyNode))).subscription_id is None


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()
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
        for name, exit_ip, state in (
            ("节点A", "1.2.3.4", "ACTIVE"),
            ("节点B", "1.2.3.4", "ACTIVE"),
            ("节点C", "5.6.7.8", "ACTIVE"),
            ("节点D", "9.9.9.9", "DEAD"),
            ("节点E", None, "ACTIVE"),
            ("旧节点", "8.8.8.8", "ACTIVE"),
        ):
            session.add(ProxyNode(
                node_id=node_fingerprint(NODE_CONFIGS[name], subscription_tag(SUB_URL)), subscription_id=sub_id,
                original_name=name, runtime_name=f"{sub_id}|{name}",
                proxy_type="http", normalized_config=NODE_CONFIGS[name], state=state,
                exit_ip=exit_ip, last_l2_at=proxies_service.get_beijing_time_obj(),
            ))
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
