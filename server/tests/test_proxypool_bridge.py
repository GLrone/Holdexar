"""订阅节点体检 → 池账本 桥接层：体检通过的健康出口节点必须被池用上。

前端「节点体检」探生产端点（L2）与出口 IP（L1），比池内 L0 更强：命中节点
按成功证据复活（DEAD / RETIRED → ACTIVE）、出口 IP 落进池账本、订阅来源特征码
回到体检结果行。失败结论不在这里降级，池内状态机自有口径。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxies.models import ClashNode, ProxySubscription
from app.domains.proxypool import bridge as B
from app.domains.proxypool.models import (
    HealthObservation,
    ProxyNode,
    ProxyNodeSource,
    SubscriptionSnapshot,
    node_fingerprint,
)
from app.domains.proxypool.registry import apply_snapshot
from app.domains.proxypool.state import NODE_ACTIVE, NODE_DEAD, NODE_RETIRED
from app.domains.proxypool.subscription import Snapshot

NOW = datetime(2026, 9, 28, 23, 0, 0)
CONFIG = {"name": "a", "type": "http", "server": "10.0.0.1", "port": 1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _snapshot(sub_id: int, nodes: list[dict], sha: str | None = None) -> Snapshot:
    return Snapshot(
        subscription_id=sub_id,
        url="https://example.test/sub",
        fetched_at=NOW,
        http_status=200,
        content_type="text/yaml",
        sha256=sha or f"sha{sub_id}-{len(nodes)}",
        raw_content=b"",
        fmt="yaml",
        node_count=len(nodes),
        source_channel="direct",
        nodes=tuple(nodes),
    )


async def _add_node(
    node_id: str,
    state: str,
    *,
    sub_id: int = 1,
    name: str | None = None,
    exit_ip: str | None = None,
    failures: int = 0,
) -> None:
    async with get_session_factory()() as s:
        s.add(
            ProxyNode(
                node_id=node_id,
                fingerprint=node_id,
                runtime_name=f"{sub_id}|{name or node_id}",
                proxy_type="http",
                server="10.0.0.1",
                normalized_config=dict(CONFIG),
                state=state,
                exit_ip=exit_ip,
                consecutive_failures=failures,
                first_seen=NOW,
                last_seen=NOW,
                last_source_seen=NOW,
            )
        )
        s.add(
            ProxyNodeSource(
                node_id=node_id,
                subscription_id=sub_id,
                original_name=name or f"sub-{node_id}",
                source_code=f"sd{sub_id}",
                first_seen=NOW,
                last_seen=NOW,
            )
        )
        await s.commit()


async def _node(node_id: str) -> ProxyNode:
    async with get_session_factory()() as s:
        return (
            await s.execute(select(ProxyNode).where(ProxyNode.node_id == node_id))
        ).scalar_one()


async def _observations(node_id: str) -> list[HealthObservation]:
    async with get_session_factory()() as s:
        return list(
            (
                await s.execute(
                    select(HealthObservation)
                    .where(HealthObservation.node_id == node_id)
                    .order_by(HealthObservation.id)
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_check_promotes_dead_node_and_carries_exit_ip(env):
    """一次成功体检把 DEAD 节点推回 ACTIVE，并带上出口 IP 与 L1/L2 观测。"""
    await init_db()
    await _add_node("n1", NODE_DEAD, sub_id=1, name="sub-a", failures=3)
    results = [{"name": "sub-a", "alive": True, "exitIp": "9.9.9.9", "ms": 120}]

    async with get_session_factory()() as s:
        outcome = await B.ingest_node_check(
            s, subscription_id=1, results=results, now=NOW
        )
        await s.commit()

    assert outcome.matched == 1
    assert outcome.activated == 1
    assert outcome.exit_ips_added == 1
    assert outcome.changed is True
    node = await _node("n1")
    assert node.state == NODE_ACTIVE
    assert node.exit_ip == "9.9.9.9"
    assert node.consecutive_failures == 0
    assert node.last_l1_at == NOW
    assert node.last_l2_at == NOW
    levels = {o.level for o in await _observations("n1")}
    assert levels == {"L1", "L2"}
    assert results[0]["sourceCode"] == "sd1"
    assert results[0]["poolState"] == NODE_ACTIVE


@pytest.mark.asyncio
async def test_check_matches_runtime_name_and_name_base(env):
    """体检名字是池内运行名、或带内容版本后缀时，都要能定位到同一行。"""
    await init_db()
    await _add_node("n2", NODE_DEAD, sub_id=1, name="sub-a")
    await _add_node("n3", NODE_RETIRED, sub_id=1, name="sub-b")

    async with get_session_factory()() as s:
        r = await B.ingest_node_check(
            s,
            subscription_id=1,
            results=[{"name": "1|sub-a", "alive": True, "exitIp": None, "ms": 90}],
            now=NOW,
        )
        r2 = await B.ingest_node_check(
            s,
            subscription_id=1,
            results=[{"name": "sub-b#42", "alive": True, "exitIp": None, "ms": 80}],
            now=NOW,
        )
        await s.commit()

    assert r.matched == 1 and (await _node("n2")).state == NODE_ACTIVE
    assert r2.matched == 1 and (await _node("n3")).state == NODE_ACTIVE


@pytest.mark.asyncio
async def test_check_does_not_demote_on_failure(env):
    """体检失败不在桥接层降级；池内状态机是唯一降级入口。"""
    await init_db()
    await _add_node("n4", NODE_ACTIVE, sub_id=1, name="sub-a", exit_ip="1.1.1.1")

    async with get_session_factory()() as s:
        outcome = await B.ingest_node_check(
            s,
            subscription_id=1,
            results=[{"name": "sub-a", "alive": False, "exitIp": None, "ms": None}],
            now=NOW,
        )
        await s.commit()

    assert outcome.matched == 0
    node = await _node("n4")
    assert node.state == NODE_ACTIVE
    assert node.exit_ip == "1.1.1.1"


@pytest.mark.asyncio
async def test_ledger_sweep_covers_other_subscriptions(env):
    """已落库的体检结论按来源对齐进池——覆盖当前内核没在跑的订阅。"""
    await init_db()
    await _add_node("n5", NODE_DEAD, sub_id=2, name="sub-x")
    await _add_node("n6", NODE_DEAD, sub_id=3, name="sub-y")
    async with get_session_factory()() as s:
        s.add(
            ClashNode(
                subscription_id=2, name="sub-x", status="ok", exit_ip="8.8.8.8",
                latency_ms=70, last_checked_at=NOW - timedelta(hours=1),
            )
        )
        s.add(
            ClashNode(
                subscription_id=3, name="sub-y", status="ok", exit_ip="7.7.7.7",
                latency_ms=60, last_checked_at=NOW - timedelta(hours=100),
            )
        )
        await s.commit()

    async with get_session_factory()() as s:
        outcome = await B.ingest_ledger(s, now=NOW, fresh_hours=72)
        await s.commit()

    assert outcome.activated == 1
    assert outcome.exit_ips_added == 1
    assert outcome.skipped_stale == 1, "超过新鲜窗口的旧结论不当当前健康"
    fresh = await _node("n5")
    assert fresh.state == NODE_ACTIVE and fresh.exit_ip == "8.8.8.8"
    stale = await _node("n6")
    assert stale.state == NODE_DEAD and stale.exit_ip is None


@pytest.mark.asyncio
async def test_apply_snapshot_writes_subscription_code_and_keeps_identity(env):
    """来源关联带订阅特征码；改名（配置不变=同指纹）复用同一行、只刷新原名。"""
    await init_db()
    async with get_session_factory()() as s:
        await apply_snapshot(s, subscription_id=5, snapshot=_snapshot(5, [CONFIG]), now=NOW)
        await s.commit()
    renamed = dict(CONFIG, name="renamed")
    async with get_session_factory()() as s:
        await apply_snapshot(
            s, subscription_id=5, snapshot=_snapshot(5, [renamed], sha="sha5-renamed"), now=NOW
        )
        await s.commit()

    async with get_session_factory()() as s:
        nodes = list((await s.execute(select(ProxyNode))).scalars())
        sources = list((await s.execute(select(ProxyNodeSource))).scalars())

    assert len(nodes) == 1, "改名不重铸身份：同配置只应有一行"
    assert nodes[0].fingerprint == node_fingerprint(CONFIG)
    assert len(sources) == 1
    assert sources[0].source_code == "sd5"
    assert sources[0].original_name == "renamed"


class _CtlResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeCtl:
    """控制器替身：只服务体检汇聚点用到的 /proxies 读与 selector 写。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, **kwargs) -> _CtlResponse:
        return _CtlResponse(
            {"proxies": {"PROXY": {"type": "selector", "now": "sub-a"}}}
        )

    async def put(self, url, **kwargs) -> _CtlResponse:
        return _CtlResponse({})


@pytest.mark.asyncio
async def test_clash_test_choke_point_feeds_pool(env, monkeypatch):
    """真实汇聚点：test_clash_nodes 跑完即把体检结论灌进池账本。

    这是「前端点体检 → 大池用上」的端到端接线证明：走真实 test_clash_nodes，
    只把内核控制器与逐节点探测换成替身。
    """
    from app.domains.proxies import clash_manager, service as proxies_service

    await init_db()
    cfg = env / "sub.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"proxies": [{"name": "sub-a", "type": "http",
                          "server": "10.0.0.1", "port": 1}]}
        ),
        encoding="utf-8",
    )
    async with get_session_factory()() as s:
        sub = ProxySubscription(
            kind="clash", url="", label="t", admission_status="ACTIVE"
        )
        s.add(sub)
        await s.commit()
        sub_id = int(sub.id)
    await _add_node("choke", NODE_DEAD, sub_id=sub_id, name="sub-a")

    monkeypatch.setattr(clash_manager.runtime, "probe_lanes", [])
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {
            "running": True,
            "controllerUrl": "http://127.0.0.1:9",
            "secret": "",
            "port": 7890,
            "configPath": str(cfg),
        },
    )
    monkeypatch.setattr(proxies_service.httpx, "AsyncClient", _FakeCtl)

    async def _fake_selector(ctl, base, headers, mixed_port, names, selector):
        return [
            {"name": n, "alive": True, "steamOk": True, "exitIp": "5.5.5.5",
             "ms": 42, "duplicate": False, "probed": True}
            for n in names
        ]

    monkeypatch.setattr(proxies_service, "_probe_nodes_via_selector", _fake_selector)

    result = await proxies_service.test_clash_nodes(sub_id, probe_all=True)

    node = await _node("choke")
    assert node.state == NODE_ACTIVE, "体检通过必须让池账本用上该节点"
    assert node.exit_ip == "5.5.5.5"
    assert node.consecutive_failures == 0
    by_name = {r["name"]: r for r in result["nodes"]}
    assert by_name["sub-a"]["sourceCode"] == f"sd{sub_id}"
    assert by_name["sub-a"]["poolState"] == NODE_ACTIVE


@pytest.mark.asyncio
async def test_ledger_does_not_override_newer_l0_verdict(env):
    """池内已有更新的 L0 结论时不复活：避免「L0 判死 ↔ 体检复活」每拍互推翻。"""
    await init_db()
    await _add_node("n7", NODE_DEAD, sub_id=2, name="sub-x")
    async with get_session_factory()() as s:
        s.add(
            ClashNode(
                subscription_id=2, name="sub-x", status="ok", exit_ip="8.8.8.8",
                latency_ms=70, last_checked_at=NOW - timedelta(hours=2),
            )
        )
        node = (
            await s.execute(select(ProxyNode).where(ProxyNode.node_id == "n7"))
        ).scalar_one()
        node.last_l0_at = NOW - timedelta(hours=1)
        await s.commit()

    async with get_session_factory()() as s:
        outcome = await B.ingest_ledger(s, now=NOW)
        await s.commit()

    assert outcome.activated == 0
    assert (await _node("n7")).state == NODE_DEAD


@pytest.mark.asyncio
async def test_rebind_restores_missing_source_rows(env):
    """来源关联丢失的订阅按最新成功快照补齐，节点因此重返合格集判定。"""
    await init_db()
    body = yaml.safe_dump({"proxies": [dict(CONFIG)]}).encode("utf-8")
    path = env / "snap.yaml"
    path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()

    async with get_session_factory()() as s:
        s.add(
            ProxySubscription(
                kind="clash", url="", label="t", admission_status="ACTIVE"
            )
        )
        await s.commit()
    async with get_session_factory()() as s:
        sub_id = int((await s.execute(select(ProxySubscription.id))).scalar_one())
        s.add(
            SubscriptionSnapshot(
                subscription_id=sub_id, sha256=sha, format="yaml",
                raw_path=str(path), node_count=1, status="OK", fetched_at=NOW,
                url="",
            )
        )
        await s.commit()

    async with get_session_factory()() as s:
        repaired = await B.rebind_missing_sources(s, now=NOW)
        await s.commit()

    assert repaired == (sub_id,)
    async with get_session_factory()() as s:
        sources = list((await s.execute(select(ProxyNodeSource))).scalars())
    assert len(sources) == 1
    assert sources[0].source_code == f"sd{sub_id}"
