"""订阅节点体检记录唯一健康事实，旧 UI 状态只作为 ProxyNode 投影。

前端「节点体检」探生产端点（L2）与出口 IP（L1）；有效响应可恢复 DEAD / RETIRED，
目标服务错误只记观测，不单独增加节点故障数。
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
    SubscriptionSnapshot,
    node_fingerprint,
)
from app.domains.proxypool.registry import apply_snapshot
from app.domains.proxypool.state import NODE_ACTIVE, NODE_DEAD, NODE_RETIRED
from app.domains.proxypool.subscription import Snapshot

NOW = datetime(2026, 9, 28, 23, 0, 0)


@pytest.mark.asyncio
async def test_unprobed_node_does_not_receive_failure_evidence(env):
    await init_db()
    await _add_node("unprobed", NODE_ACTIVE, sub_id=1, name="one", exit_ip="1.1.1.1")
    async with get_session_factory()() as session:
        result = await B.ingest_node_check(
            session, subscription_id=1,
            results=[{"name": "one", "alive": False, "probed": False}], now=NOW,
        )
        await session.commit()
    assert result.matched == 0
    assert (await _node("unprobed")).state == NODE_ACTIVE
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
                subscription_id=sub_id,
                original_name=name or f"sub-{node_id}",
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
async def test_check_matches_runtime_name_without_source(env):
    """来源关联缺失但身份行仍在时，体检结果按 runtime_name「订阅id|原名」也要命中。

    订阅抓取失败 / 归属丢过一拍的行在池账本里只按 runtime_name 匹配也能命中，
    否则这类「手动体检已证明健康」的节点会全部落空、进不了池。
    """
    await init_db()
    async with get_session_factory()() as s:
        s.add(
            ProxyNode(
                node_id="n9",
                fingerprint="n9",
                runtime_name="4|香港-直连#1",
                proxy_type="http",
                server="10.0.0.1",
                normalized_config=dict(CONFIG),
                state=NODE_DEAD,
                consecutive_failures=2,
                first_seen=NOW,
                last_seen=NOW,
                last_source_seen=NOW,
            )
        )
        await s.commit()

    results = [{"name": "香港-直连#1", "alive": True, "exitIp": "1.2.3.4", "ms": 80}]
    async with get_session_factory()() as s:
        outcome = await B.ingest_node_check(
            s, subscription_id=4, results=results, now=NOW
        )
        await s.commit()

    assert outcome.matched == 1, "按运行名必须命中"
    assert outcome.activated == 1, "命中的健康节点必须复活进池"
    node = await _node("n9")
    assert node.state == NODE_ACTIVE
    assert node.exit_ip == "1.2.3.4"


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
    """体检失败是同一把 Steam 判据下的失败证据：计数 +1 并 fail-fast 判死
    （exit_ip 不擦除，复活靠下一轮成功或恢复轮）。"""
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

    assert outcome.matched == 1
    node = await _node("n4")
    assert node.state == NODE_DEAD
    assert node.consecutive_failures == 1
    assert node.exit_ip == "1.1.1.1", "失败证据不擦出口事实"


@pytest.mark.asyncio
async def test_ledger_sweep_covers_other_subscriptions(env):
    """全订阅兼容表由 ProxyNode 投影刷新，旧状态不能反向复活节点。"""
    await init_db()
    await _add_node("n5", NODE_ACTIVE, sub_id=2, name="sub-x", exit_ip="8.8.8.8")
    await _add_node("n6", NODE_DEAD, sub_id=3, name="sub-y", exit_ip="7.7.7.7")
    async with get_session_factory()() as s:
        n5 = (await s.execute(select(ProxyNode).where(ProxyNode.node_id == "n5"))).scalar_one()
        n6 = (await s.execute(select(ProxyNode).where(ProxyNode.node_id == "n6"))).scalar_one()
        n5.last_l2_at = NOW - timedelta(hours=1)
        n6.last_l2_at = NOW - timedelta(hours=1)
        s.add(
            ClashNode(
                subscription_id=2, name="sub-x", status="dead", exit_ip=None,
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
        outcome = await B.ingest_ledger(s, now=NOW)
        await s.commit()

    assert outcome.activated == 1
    assert outcome.deactivated == 1
    assert outcome.exit_ips_added == 1
    fresh = await _node("n5")
    assert fresh.state == NODE_ACTIVE and fresh.exit_ip == "8.8.8.8"
    stale = await _node("n6")
    assert stale.state == NODE_DEAD and stale.exit_ip == "7.7.7.7"


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
        sources = list((await s.execute(select(ProxyNode))).scalars())

    assert len(nodes) == 1, "改名不重铸身份：同配置只应有一行"
    assert nodes[0].fingerprint == node_fingerprint(CONFIG)
    assert len(sources) == 1
    assert sources[0].subscription_id == 5
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

    # 假内核没有真实监听端口：selector 回退路径的入口硬闸按「入口在听」放行
    monkeypatch.setattr(clash_manager.runtime, "entry_listening", lambda: True)
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
async def test_ledger_does_not_override_newer_pool_verdict(env):
    """池内已有更新的业务结论（last_l2_at）且节点正被判坏时不复活：
    避免「池判死 ↔ 体检账本复活」每拍互相推翻、节点状态翻烧饼。"""
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
        node.last_l2_at = NOW - timedelta(hours=1)
        await s.commit()

    async with get_session_factory()() as s:
        outcome = await B.ingest_ledger(s, now=NOW)
        await s.commit()

    assert outcome.activated == 0
    assert (await _node("n7")).state == NODE_DEAD


@pytest.mark.asyncio
async def test_legacy_check_never_resurrects_registry_node(env):
    """即使旧体检时间较新，兼容投影也不能反向改变 ProxyNode 健康状态。"""
    await init_db()
    await _add_node("n8", NODE_DEAD, sub_id=2, name="sub-x")
    async with get_session_factory()() as s:
        s.add(
            ClashNode(
                subscription_id=2, name="sub-x", status="ok", exit_ip="8.8.8.8",
                latency_ms=70, last_checked_at=NOW - timedelta(hours=1),
            )
        )
        node = (
            await s.execute(select(ProxyNode).where(ProxyNode.node_id == "n8"))
        ).scalar_one()
        node.last_l2_at = NOW - timedelta(hours=2)
        await s.commit()

    async with get_session_factory()() as s:
        outcome = await B.ingest_ledger(s, now=NOW)
        await s.commit()

    assert outcome.activated == 0
    assert outcome.deactivated == 1
    assert (await _node("n8")).state == NODE_DEAD


@pytest.mark.asyncio
async def test_target_http_error_is_observation_not_node_failure(env):
    await init_db()
    await _add_node("http-error", NODE_ACTIVE, sub_id=4, name="http-error")
    async with get_session_factory()() as session:
        result = await B.ingest_node_check(
            session,
            subscription_id=4,
            results=[{
                "name": "http-error", "alive": False, "steamStatus": 502,
                "exitIp": "9.8.7.6", "ms": 18, "probed": True,
            }],
            now=NOW,
        )
        await session.commit()

    node = await _node("http-error")
    assert result.matched == 1
    assert node.state == NODE_ACTIVE and node.consecutive_failures == 0
    assert node.exit_ip == "9.8.7.6"
    observations = await _observations("http-error")
    assert [(row.level, row.ok) for row in observations] == [("L1", True), ("L2", False)]
    assert observations[-1].detail == B.CLASH_STEAM_HTTP_502_DETAIL


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
        sources = list((await s.execute(select(ProxyNode))).scalars())
    assert len(sources) == 1
    assert sources[0].subscription_id == sub_id


@pytest.mark.asyncio
async def test_purge_orphan_subscription_sources(env):
    """归属已删除订阅的节点解除归属（挂空）并按状态机降级；现存订阅的节点不动。"""
    await init_db()
    seen = NOW
    async with get_session_factory()() as s:
        s.add(ProxySubscription(kind="clash", url="", label="live",
                                admission_status="ACTIVE"))
        await s.commit()
    async with get_session_factory()() as s:
        live_id = int((await s.execute(select(ProxySubscription.id))).scalar_one())
        orphan_id = live_id + 999  # 无订阅行的失主编号

        def _node(node_id: str, sub_id: int) -> ProxyNode:
            return ProxyNode(
                node_id=node_id, fingerprint=node_id, runtime_name=f"{sub_id}|{node_id}",
                subscription_id=sub_id, original_name=node_id,
                proxy_type="http", server="10.0.0.1",
                normalized_config={"name": node_id, "type": "http",
                                   "server": "10.0.0.1", "port": 1},
                state="ACTIVE", first_seen=seen, last_seen=seen, last_source_seen=seen,
            )

        s.add_all([_node("orphan-only", orphan_id), _node("shared", live_id)])
        s.add(SubscriptionSnapshot(
            subscription_id=orphan_id, sha256="sha-orphan", format="yaml",
            raw_path="orphan.bin", node_count=1, status="OK", fetched_at=seen, url="",
        ))
        await s.commit()

    async with get_session_factory()() as s:
        purged = await B.purge_orphan_subscription_sources(s, now=seen)
        await s.commit()

    assert purged == (orphan_id,)
    async with get_session_factory()() as s:
        sources = list((await s.execute(select(ProxyNode))).scalars())
        snaps = list((await s.execute(select(SubscriptionSnapshot))).scalars())
        states = dict((await s.execute(
            select(ProxyNode.node_id, ProxyNode.state))).all())
    assert {src.subscription_id for src in sources} == {None, live_id}
    assert snaps == [], "失主订阅的快照行随清扫走（retention 的每订阅保底留不住孤儿）"
    assert states["orphan-only"] == "STALE", "失主节点不得永远 ACTIVE"
    assert states["shared"] == "ACTIVE", "现存订阅的节点不受影响"

    async with get_session_factory()() as s:
        again = await B.purge_orphan_subscription_sources(s, now=seen)
    assert again == (), "幂等：清完即空转"
