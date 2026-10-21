"""DEAD 恢复探测：DEAD 不在池文件里，池内 L0 永远探不到它。

没有这条路径，节点一旦 DEAD 就永久出局、失败计数也停住（退休线在生产路径不可达）。
本文件钉住：成功回 ACTIVE 并清零、失败保持 DEAD 并累加、分批轮转最久没探过的先来、
达标走既有退休终点、不进 crawl pool、未选中节点不做任何回填。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import health as H
from app.domains.proxypool import scheduling as sched
from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
from app.domains.proxypool.models import HealthObservation, ProxyNode
from app.domains.proxypool.pool import eligible_runtime_names, pool_path
from app.domains.proxypool.state import (
    DEFAULT_RETIRE_AFTER_FAILED_PROBES,
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_RETIRED,
)

NOW = datetime(2026, 9, 20, 23, 0, 0)
CONFIG = {"name": "probe", "type": "http", "server": "10.0.0.1", "port": 1}


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


async def _add_node(*, node_id: str, state: str, failures: int = 0,
                    last_probe: datetime | None = None) -> None:
    async with get_session_factory()() as s:
        # 订阅行必须真实存在：L0 的「失主来源清理」会把指向不存在订阅的
        # 来源关联删掉，恢复探测因此跳过无来源节点
        if await s.get(ProxySubscription, 1) is None:
            s.add(ProxySubscription(id=1, kind="plain", url="http://sub.example/list",
                                    admission_status=ADMISSION_ACTIVE))
        s.add(ProxyNode(node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                        proxy_type="http", server="10.0.0.1",
                        normalized_config=dict(CONFIG), state=state,
                        consecutive_failures=failures,
                        first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        await s.execute(update(ProxyNode)
            .where(ProxyNode.node_id == node_id)
            .values(subscription_id=1, original_name=f"sub-{node_id}"))
        if last_probe is not None:
            s.add(HealthObservation(node_id=node_id, level="L0", ok=False,
                                    observed_at=last_probe))
        await s.commit()


def _empty_pool(data_dir) -> None:
    (data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    pool_path(data_dir).write_text(yaml.safe_dump({"proxies": []}), encoding="utf-8")


def _stub_probe(monkeypatch, ok: bool) -> list[str]:
    probed: list[str] = []

    async def fake_probe(controller_url, secret, runtime_name, *, url=None, timeout_ms=None):
        probed.append(runtime_name)
        return H.ProbeResult(runtime_name=runtime_name, ok=ok,
                             delay_ms=3 if ok else None,
                             detail="" if ok else "内核探测失败（HTTP 503）")

    monkeypatch.setattr(H, "probe_node", fake_probe)
    return probed


async def _node(node_id: str) -> ProxyNode:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.node_id == node_id)
        )).scalar_one()


async def _recover(monkeypatch, *, ok: bool, batch: int | None = None):
    _stub_probe(monkeypatch, ok)
    async with get_session_factory()() as s:
        outcomes = await H.recover_dead_nodes(
            s, controller_url="http://127.0.0.1:9", secret="", now=NOW,
            **(({"batch": batch} if batch is not None else {})),
        )
        await s.commit()
    return outcomes


@pytest.mark.asyncio
async def test_recovery_success_returns_active_and_zeroes_count(env, monkeypatch):
    await init_db()
    await _add_node(node_id="d1", state=NODE_DEAD, failures=2)
    outcomes = await _recover(monkeypatch, ok=True)
    assert len(outcomes) == 1
    node = await _node("d1")
    assert node.state == NODE_ACTIVE, "恢复成功必须回 ACTIVE"
    assert node.consecutive_failures == 0, "恢复成功必须清零连续失败"


@pytest.mark.asyncio
async def test_recovery_failure_keeps_dead_and_increments(env, monkeypatch):
    await init_db()
    await _add_node(node_id="d2", state=NODE_DEAD, failures=1)
    await _recover(monkeypatch, ok=False)
    node = await _node("d2")
    assert node.state == NODE_DEAD, "恢复失败保持 DEAD"
    assert node.consecutive_failures == 2, "恢复失败必须累加计数"


@pytest.mark.asyncio
async def test_consecutive_recovery_failures_reach_retire_threshold(env, monkeypatch):
    await init_db()
    await _add_node(node_id="d3", state=NODE_DEAD,
                    failures=DEFAULT_RETIRE_AFTER_FAILED_PROBES - 1)
    await _recover(monkeypatch, ok=False)
    node = await _node("d3")
    assert node.consecutive_failures == DEFAULT_RETIRE_AFTER_FAILED_PROBES
    assert node.state == NODE_RETIRED, "达标必须走既有退休终点"


@pytest.mark.asyncio
async def test_recovery_success_reenters_pool_and_requests_rebuild(env, monkeypatch):
    """pool=0 时，恢复成功让节点重新合格，并按既有语义请求重建。"""
    await init_db()
    _empty_pool(env)
    await _add_node(node_id="d4", state=NODE_DEAD, failures=1)
    _stub_probe(monkeypatch, True)
    rebuilds: list[int] = []
    monkeypatch.setattr(sched, "request_rebuild", lambda: rebuilds.append(1))

    async with get_session_factory()() as s:
        await sched.run_l0_cycle(
            s, data_dir=env, controller_url="http://127.0.0.1:9", secret="",
            now=NOW, recovery_controller=("http://127.0.0.1:8", ""),
        )
        await s.commit()
        eligible = await eligible_runtime_names(s)

    assert eligible == ("1|d4",), "恢复成功必须重新进入合格集（= crawl pool 的输入）"
    assert rebuilds == [1], "合格集变化必须按既有语义请求重建"


@pytest.mark.asyncio
async def test_recovery_failure_does_not_enter_pool(env, monkeypatch):
    """恢复失败不得让节点进池（不能"先入池再爬"）。"""
    await init_db()
    _empty_pool(env)
    await _add_node(node_id="d5", state=NODE_DEAD, failures=0)
    before = pool_path(env).read_text(encoding="utf-8")
    await _recover(monkeypatch, ok=False)

    async with get_session_factory()() as s:
        eligible = await eligible_runtime_names(s)
    assert eligible == (), "恢复失败仍不得进池"
    assert pool_path(env).read_text(encoding="utf-8") == before, "恢复探测不得改池文件"
    node = await _node("d5")
    assert node.consecutive_failures == 1


@pytest.mark.asyncio
async def test_recovery_batch_is_limited_and_oldest_first(env, monkeypatch):
    """分批轮转：只探 batch 个、同为未累计失败时最久没探过的先来；未选中节点不做回填。"""
    await init_db()
    await _add_node(node_id="old", state=NODE_DEAD, failures=0,
                    last_probe=NOW - timedelta(hours=3))
    await _add_node(node_id="mid", state=NODE_DEAD, failures=0,
                    last_probe=NOW - timedelta(hours=2))
    await _add_node(node_id="new", state=NODE_DEAD, failures=0,
                    last_probe=NOW - timedelta(hours=1))
    probed = _stub_probe(monkeypatch, False)
    async with get_session_factory()() as s:
        outcomes = await H.recover_dead_nodes(
            s, controller_url="http://127.0.0.1:9", secret="", now=NOW, batch=2,
        )
        await s.commit()

    assert len(outcomes) == 2, "一次只探 batch 个"
    assert probed == ["sub-old", "sub-mid"], "最久没探过的先来（用来源订阅名打到内核）"
    assert (await _node("old")).consecutive_failures == 1
    assert (await _node("mid")).consecutive_failures == 1
    untouched = await _node("new")
    assert untouched.consecutive_failures == 0, "未选中节点不得被回填"
    assert untouched.state == NODE_DEAD


@pytest.mark.asyncio
async def test_recovery_rotates_by_least_recently_probed(env, monkeypatch):
    """轮转次序只由「上次被探的时刻」决定，累计失败次数不插队。

    慢性失败节点若永远排最前，其余节点要等轮转一整圈才回到它，回收集的
    覆盖率会长期为零——轮转的职责是让每个出池节点都被碰到。
    """
    await init_db()
    await _add_node(node_id="fresh", state=NODE_DEAD, failures=0,
                    last_probe=NOW - timedelta(hours=5))
    await _add_node(node_id="failing", state=NODE_DEAD, failures=1,
                    last_probe=NOW - timedelta(minutes=5))
    probed = _stub_probe(monkeypatch, False)
    async with get_session_factory()() as s:
        await H.recover_dead_nodes(
            s, controller_url="http://127.0.0.1:9", secret="", now=NOW, batch=1,
        )
        await s.commit()

    assert probed == ["sub-fresh"], "最久没探过的先来，与累计失败次数无关"
    assert (await _node("fresh")).consecutive_failures == 1
    assert (await _node("failing")).consecutive_failures == 1, "未选中节点不得被回填"


# ── 自建临时内核恢复（旧链路内核不在时的唯一出路）────────────────────


def _stub_self_hosted(monkeypatch, outcomes: dict[str, bool]) -> list[str]:
    """替掉自建内核探测，只验「结论 → 状态推进」这一段。"""
    from app.domains.proxypool import dead_probe as DP

    probed: list[str] = []

    async def fake_probe(nodes, *, exe_path, work_dir, url, dependencies=(), source_documents=None,
                         source_errors=None):
        result = []
        for n in nodes:
            ok = outcomes.get(n.node_id, False)
            probed.append(n.node_id)
            result.append(DP.ProbeOutcome(
                node_id=n.node_id, runtime_name=n.runtime_name,
                ok=ok, delay_ms=10 if ok else None,
                detail="" if ok else "探测未通过（HTTP 503）",
            ))
        return result

    monkeypatch.setattr(DP, "probe_out_of_pool_nodes", fake_probe)
    return probed


def _stub_kernel_exe(monkeypatch, env) -> None:
    from app.domains.proxies import clash_manager as cm

    exe = env / "proxypool" / "mihomo.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(cm, "kernel_exe", lambda data_dir: exe)


async def _recover_self_hosted(env, monkeypatch, *, ok: bool):
    from app.domains.proxypool import dead_probe as DP

    _stub_kernel_exe(monkeypatch, env)
    probed = _stub_self_hosted(monkeypatch, {"sh1": ok, "sh2": ok})
    async with get_session_factory()() as s:
        outcomes = await DP.recover_dead_nodes_self_hosted(
            s, data_dir=env, now=NOW,
        )
    return outcomes, probed


@pytest.mark.asyncio
async def test_self_hosted_recovery_success_returns_active(env, monkeypatch):
    """自建内核探通即回 ACTIVE 并清零计数（与借道恢复同一结论口径）。"""
    await init_db()
    await _add_node(node_id="sh1", state=NODE_DEAD, failures=2)

    outcomes, probed = await _recover_self_hosted(env, monkeypatch, ok=True)

    assert len(outcomes) == 1
    assert probed == ["sh1"]
    node = await _node("sh1")
    assert node.state == NODE_ACTIVE, "自建内核探通必须回 ACTIVE"
    assert node.consecutive_failures == 0


@pytest.mark.asyncio
async def test_self_hosted_recovery_failure_keeps_dead(env, monkeypatch):
    """探不通保持 DEAD 并累加计数 —— 真死的节点不该被救活。"""
    await init_db()
    await _add_node(node_id="sh2", state=NODE_DEAD, failures=1)

    await _recover_self_hosted(env, monkeypatch, ok=False)

    node = await _node("sh2")
    assert node.state == NODE_DEAD
    assert node.consecutive_failures == 2


@pytest.mark.asyncio
async def test_self_hosted_recovery_skipped_without_kernel_binary(env, monkeypatch):
    """内核二进制不存在时直接空转，不抛异常、不改状态。"""
    from app.domains.proxypool import dead_probe as DP
    from app.domains.proxies import clash_manager as cm

    await init_db()
    await _add_node(node_id="sh3", state=NODE_DEAD, failures=1)
    monkeypatch.setattr(cm, "kernel_exe", lambda data_dir: env / "不存在" / "mihomo.exe")

    async with get_session_factory()() as s:
        outcomes = await DP.recover_dead_nodes_self_hosted(s, data_dir=env, now=NOW)

    assert outcomes == ()
    node = await _node("sh3")
    assert node.state == NODE_DEAD
    assert node.consecutive_failures == 1, "跳过时不得改动计数"


@pytest.mark.asyncio
async def test_self_hosted_renders_config_without_geo_rules():
    """渲染出的配置不得含 GEOIP/GEOSITE —— 否则缺地理数据时内核会联网阻塞。

    这是「不依赖随包地理数据」这条设计的回归防线：只要有人往配置里加回
    GEOIP/GEOSITE 规则，临时内核就会在缺数据的主机上卡住而无法及时启动。
    """
    from app.domains.proxypool.dead_probe import RecoveredNode, _render_config

    nodes = [
        RecoveredNode("id1", "1|甲", "甲",
                      {"type": "trojan", "server": "1.2.3.4", "port": 443, "password": "p"}),
        RecoveredNode("id2", "1|乙", "乙",
                      {"type": "ss", "server": "1.2.3.5", "port": 8388,
                       "cipher": "aes-256-gcm", "password": "p"}),
    ]

    text, ports = _render_config(
        nodes, controller_port=23200, secret="s", listener_base=23220
    )

    assert "GEOIP" not in text, "恢复探测配置不得携带 GEOIP 规则"
    assert "GEOSITE" not in text, "恢复探测配置不得携带 GEOSITE 规则"
    config = yaml.safe_load(text)
    assert config["rules"] == ["MATCH,DIRECT"]
    assert len(config["proxies"]) == 2
    assert len(config["listeners"]) == 2
    assert ports == {"1|甲": 23220, "1|乙": 23221}


def test_self_hosted_rewrites_dialer_dependency():
    from app.domains.proxypool.dead_probe import RecoveredNode, _render_config

    nodes = [
        RecoveredNode("endpoint", "1|endpoint", "endpoint",
                      {**CONFIG, "dialer-proxy": "current-hop"}),
        RecoveredNode("hop", "1|old-hop", "current-hop", dict(CONFIG)),
    ]
    text, _ = _render_config(nodes, controller_port=23200, secret="s", listener_base=23220)
    proxies = yaml.safe_load(text)["proxies"]
    assert proxies[0]["dialer-proxy"] == "1|old-hop"


def test_self_hosted_rejects_missing_dialer_dependency():
    from app.domains.proxypool.dead_probe import RecoveredNode, _render_config

    with pytest.raises(ValueError, match="依赖"):
        _render_config([
            RecoveredNode("endpoint", "1|endpoint", "endpoint",
                          {**CONFIG, "dialer-proxy": "missing-hop"}),
        ], controller_port=23200, secret="s", listener_base=23220)


def test_self_hosted_keeps_same_named_relays_in_their_subscription():
    from types import SimpleNamespace
    from app.domains.proxypool.dead_probe import _render_config

    def node(node_id, subscription_id, original_name, **extra):
        return SimpleNamespace(
            node_id=node_id, subscription_id=subscription_id,
            runtime_name=f"{subscription_id}|{original_name}",
            original_name=original_name, config={**CONFIG, **extra},
        )

    nodes = [
        node("leaf", 1, "leaf", **{"dialer-proxy": "hop"}),
        node("hop-one", 1, "hop"),
        node("hop-two", 2, "hop"),
    ]
    text, _ = _render_config(nodes, controller_port=23200, secret="s", listener_base=23220)
    proxies = {item["name"]: item for item in yaml.safe_load(text)["proxies"]}

    assert proxies["1|leaf"]["dialer-proxy"] == "1|hop"


async def _add_recovery_graph(entries):
    async with get_session_factory()() as session:
        session.add(ProxySubscription(id=1, kind="plain", url="http://sub.example/list"))
        session.add_all([
            ProxyNode(
                node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                subscription_id=1, original_name=node_id, proxy_type="http",
                server="10.0.0.1", normalized_config={**CONFIG, "name": node_id, **extra},
                state=state, consecutive_failures=1,
                first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
            )
            for node_id, state, extra in entries
        ])
        await session.commit()


def _stub_precheck(monkeypatch, failure=None):
    """替换内核 `-t` 预检：明确给出放行或拒绝结论，并记录收到的调用。"""
    from app.domains.proxies import clash_manager as cm

    calls = []

    def precheck(exe_path, config_path, work_dir):
        calls.append(config_path)
        if failure is not None:
            raise failure

    monkeypatch.setattr(cm, "validate_config", precheck)
    return calls


def _stub_ephemeral_kernel(monkeypatch, env):
    from pathlib import Path
    from app.domains.proxypool import dead_probe as DP

    _stub_kernel_exe(monkeypatch, env)
    _stub_precheck(monkeypatch)
    rendered = []
    probed = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            return 0

    def spawn(command, **kwargs):
        rendered.append(yaml.safe_load(Path(command[-1]).read_text(encoding="utf-8")))
        return Process()

    async def ready(*args):
        return True

    async def probe(base_url, secret, name, url):
        probed.append(name)
        return H.ProbeResult(name, True, 7, "")

    monkeypatch.setattr(DP, "_free_port", lambda preferred: preferred)
    monkeypatch.setattr(DP.subprocess, "Popen", spawn)
    monkeypatch.setattr(DP, "_wait_controller", ready)
    monkeypatch.setattr(DP, "_probe_one", probe)
    return rendered, probed


@pytest.mark.asyncio
async def test_self_hosted_loads_healthy_relay_without_probing_or_updating_it(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([
        ("leaf", NODE_DEAD, {"dialer-proxy": "hop"}),
        ("hop", NODE_ACTIVE, {}),
    ])
    rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)

    async with get_session_factory()() as session:
        inputs = await DP.load_recoverable_nodes(session)
        assert getattr(inputs[0], "subscription_id", None) == 1
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    assert [outcome.node_id for outcome in outcomes] == ["leaf"]
    assert probed == ["1|leaf"]
    assert {proxy["name"] for proxy in rendered[0]["proxies"]} == {"1|leaf", "1|hop"}
    assert [listener["proxy"] for listener in rendered[0]["listeners"]] == ["1|leaf"]
    assert [observation.node_id for observation in observations] == ["leaf"]
    assert (await _node("hop")).consecutive_failures == 1
    assert (await _node("leaf")).state == NODE_ACTIVE


@pytest.mark.asyncio
async def test_self_hosted_failed_dependencies_advance_rotation_past_full_batch(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([
        *[(f"a-{i:02}", NODE_DEAD, {"dialer-proxy": "missing"}) for i in range(24)],
        ("z-independent", NODE_DEAD, {}),
    ])
    _rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)

    async with get_session_factory()() as session:
        first = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        second = await DP.recover_dead_nodes_self_hosted(
            session, data_dir=env, now=NOW + timedelta(minutes=1),
        )
        observations = (await session.scalars(select(HealthObservation))).all()

    assert len(first) == 24
    assert all(not result.ok and "依赖" in result.detail for result in first)
    assert any(result.node_id == "z-independent" and result.ok for result in second)
    assert probed == ["1|z-independent"]
    assert {observation.node_id for observation in observations} == {
        *[f"a-{i:02}" for i in range(24)], "z-independent",
    }
    assert (await _node("a-00")).consecutive_failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle", [False, True])
async def test_self_hosted_bad_dependency_chain_does_not_block_independent_target(
    env, monkeypatch, cycle,
):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([
        ("a", NODE_DEAD, {"dialer-proxy": "b"}),
        ("b", NODE_DEAD, {"dialer-proxy": "a" if cycle else "missing"}),
        ("c", NODE_DEAD, {}),
    ])
    _rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    by_id = {result.node_id: result for result in outcomes}
    assert not by_id["a"].ok and not by_id["b"].ok
    assert by_id["c"].ok
    assert probed == ["1|c"]
    assert {item.node_id for item in observations} == {"a", "b", "c"}
    assert (await _node("a")).consecutive_failures == 1
    assert (await _node("c")).state == NODE_ACTIVE


@pytest.mark.asyncio
async def test_self_hosted_port_failure_does_not_count_as_node_failure(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_kernel_exe(monkeypatch, env)
    monkeypatch.setattr(DP, "_free_port", lambda preferred: None)

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    assert len(outcomes) == 1 and not outcomes[0].ok
    assert len(observations) == 1 and not observations[0].ok
    assert (await _node("leaf")).consecutive_failures == 1


@pytest.mark.asyncio
async def test_self_hosted_loads_relay_group_from_existing_source(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP
    from app.domains.proxies.clash_manager import subscription_config_path

    await init_db()
    await _add_recovery_graph([
        ("leaf", NODE_DEAD, {"dialer-proxy": "relay"}),
        ("hop", NODE_ACTIVE, {}),
    ])
    source = subscription_config_path(env, 1)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(yaml.safe_dump({
        "proxies": [{**CONFIG, "name": "hop"}],
        "proxy-groups": [{"name": "relay", "type": "select", "proxies": ["hop"]}],
        "rules": ["GEOSITE,cn,DIRECT"],
    }), encoding="utf-8")
    rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)

    assert len(outcomes) == 1 and outcomes[0].ok
    assert probed == ["1|leaf"]
    config = rendered[0]
    assert config["proxy-groups"][0]["name"] == "HlRelay|1|relay"
    assert config["proxy-groups"][0]["proxies"] == ["1|hop"]
    assert next(p for p in config["proxies"] if p["name"] == "1|leaf")["dialer-proxy"] == "HlRelay|1|relay"
    assert "GEOSITE" not in yaml.safe_dump(config)
    assert not list((env / "proxypool" / "_dead_probe").glob("run-*"))


@pytest.mark.asyncio
async def test_self_hosted_delays_use_existing_probe_concurrency(env, monkeypatch):
    import asyncio
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("a", NODE_DEAD, {}), ("b", NODE_DEAD, {})])
    _stub_ephemeral_kernel(monkeypatch, env)
    started = set()
    both_started = asyncio.Event()

    async def probe(base_url, secret, name, url):
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return H.ProbeResult(name, True, 7, "")

    monkeypatch.setattr(DP, "_probe_one", probe)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)

    assert len(outcomes) == 2 and all(result.ok for result in outcomes)
    assert started == {"1|a", "1|b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, cause", [
    ("admission", "config"), ("rejected", "kernel"), ("timeout", "kernel"),
    ("unlaunchable", "kernel"), ("changed", "kernel"),
])
async def test_self_hosted_precheck_failure_refuses_launch_without_node_evidence(
    env, monkeypatch, kind, cause,
):
    from app.domains.proxies.kernel_process import KernelPrecheckError
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_kernel_exe(monkeypatch, env)
    calls = _stub_precheck(monkeypatch, KernelPrecheckError(kind, f"预检拒绝：{kind}"))
    monkeypatch.setattr(DP, "_free_port", lambda preferred: preferred)
    monkeypatch.setattr(
        DP.subprocess, "Popen",
        lambda *a, **k: pytest.fail("未通过预检的配置不得拉起内核"),
    )

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    assert len(calls) == 1
    assert len(outcomes) == 1 and not outcomes[0].ok and not outcomes[0].node_failure
    assert outcomes[0].cause == cause and kind in outcomes[0].detail
    assert len(observations) == 1 and not observations[0].ok
    node = await _node("leaf")
    assert node.state == NODE_DEAD and node.consecutive_failures == 1, "基础设施失败不得累计节点失败"


@pytest.mark.asyncio
async def test_self_hosted_launch_failure_and_controller_failure_have_distinct_causes(
    env, monkeypatch,
):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_kernel_exe(monkeypatch, env)
    _stub_precheck(monkeypatch)
    monkeypatch.setattr(DP, "_free_port", lambda preferred: preferred)

    def cannot_start(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(DP.subprocess, "Popen", cannot_start)
    async with get_session_factory()() as session:
        launch = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
    assert launch[0].cause == "kernel" and not launch[0].node_failure

    _stub_ephemeral_kernel(monkeypatch, env)

    async def unavailable(*args):
        return False

    monkeypatch.setattr(DP, "_wait_controller", unavailable)
    async with get_session_factory()() as session:
        controller = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
    assert controller[0].cause == "controller" and not controller[0].node_failure
    assert (await _node("leaf")).consecutive_failures == 1


@pytest.mark.asyncio
async def test_self_hosted_genuine_connect_failure_is_node_evidence(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_ephemeral_kernel(monkeypatch, env)

    async def dead_node(base_url, secret, name, url):
        return H.ProbeResult(name, False, None, "内核探测失败（HTTP 504）")

    monkeypatch.setattr(DP, "_probe_one", dead_node)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
    assert outcomes[0].node_failure and outcomes[0].cause == "node"
    assert (await _node("leaf")).consecutive_failures == 2


@pytest.mark.asyncio
async def test_self_hosted_controller_not_ready_does_not_mark_node_failed(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_ephemeral_kernel(monkeypatch, env)

    async def unavailable(*args):
        return False

    monkeypatch.setattr(DP, "_wait_controller", unavailable)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)

    assert len(outcomes) == 1 and not outcomes[0].ok
    assert not outcomes[0].node_failure
    assert (await _node("leaf")).consecutive_failures == 1


@pytest.mark.asyncio
async def test_self_hosted_unloaded_node_is_not_health_failure(env, monkeypatch):
    import httpx
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    real_probe = DP._probe_one
    _stub_ephemeral_kernel(monkeypatch, env)
    monkeypatch.setattr(DP, "_probe_one", real_probe)
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"message": "not found"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)

    assert len(outcomes) == 1 and not outcomes[0].ok
    assert not outcomes[0].node_failure
    assert (await _node("leaf")).consecutive_failures == 1


@pytest.mark.asyncio
async def test_self_hosted_invalid_auxiliary_config_does_not_block_independent_target(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    await _add_recovery_graph([
        ("a", NODE_DEAD, {"dialer-proxy": "hop"}),
        ("hop", NODE_ACTIVE, {"server": None}),
        ("z", NODE_DEAD, {}),
    ])
    rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)

    assert [(result.node_id, result.ok) for result in outcomes] == [("a", False), ("z", True)]
    assert probed == ["1|z"]
    assert {p["name"] for p in rendered[0]["proxies"]} == {"1|z"}


@pytest.mark.asyncio
async def test_self_hosted_bad_source_only_blocks_its_dependent_targets(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP
    from app.domains.proxies.clash_manager import subscription_config_path

    await init_db()
    await _add_recovery_graph([
        ("a-dependent", NODE_DEAD, {"dialer-proxy": "relay"}),
        ("b-independent", NODE_DEAD, {}),
    ])
    async with get_session_factory()() as session:
        session.add(ProxySubscription(id=2, kind="clash", url="http://second.example/list"))
        session.add_all([
            ProxyNode(
                node_id=node_id, fingerprint=node_id, runtime_name=f"2|{name}",
                subscription_id=2, original_name=name, proxy_type="http", server="10.0.0.1",
                normalized_config={**CONFIG, "name": name, **extra}, state=state,
                consecutive_failures=1, first_seen=NOW, last_source_seen=NOW,
            )
            for node_id, name, state, extra in [
                ("second-leaf", "leaf", NODE_DEAD, {"dialer-proxy": "relay"}),
                ("second-hop", "hop", NODE_ACTIVE, {}),
            ]
        ])
        await session.commit()
    bad_source = subscription_config_path(env, 1)
    bad_source.parent.mkdir(parents=True, exist_ok=True)
    bad_source.write_text("proxies: [", encoding="utf-8")
    subscription_config_path(env, 2).write_text(yaml.safe_dump({
        "proxies": [
            {**CONFIG, "name": "leaf", "dialer-proxy": "relay"},
            {**CONFIG, "name": "hop"},
        ],
        "proxy-groups": [{"name": "relay", "type": "select", "proxies": ["hop"]}],
    }), encoding="utf-8")
    rendered, probed = _stub_ephemeral_kernel(monkeypatch, env)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    by_id = {result.node_id: result for result in outcomes}
    assert not by_id["a-dependent"].ok and not by_id["a-dependent"].node_failure
    assert "快照" in by_id["a-dependent"].detail
    assert by_id["b-independent"].ok and by_id["second-leaf"].ok
    assert set(probed) == {"1|b-independent", "2|leaf"}
    assert {item.node_id for item in observations} == {"a-dependent", "b-independent", "second-leaf"}
    assert (await _node("a-dependent")).consecutive_failures == 1
    assert rendered[0]["proxy-groups"][0]["name"] == "HlRelay|2|relay"


@pytest.mark.asyncio
async def test_self_hosted_recovery_uses_source_membership_at_result_commit(env, monkeypatch):
    from app.domains.proxypool import dead_probe as DP
    from app.domains.proxypool.state import NODE_STALE

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    _stub_kernel_exe(monkeypatch, env)

    async def probe(nodes, **kwargs):
        async with get_session_factory()() as session:
            await session.execute(
                update(ProxyNode).where(ProxyNode.node_id == "leaf")
                .values(subscription_id=None)
            )
            await session.commit()
        return [DP.ProbeOutcome("leaf", "1|leaf", True, 7, "")]

    monkeypatch.setattr(DP, "probe_out_of_pool_nodes", probe)
    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        assert await eligible_runtime_names(session) == ()

    node = await _node("leaf")
    assert outcomes[0].ok
    assert node.subscription_id is None
    assert node.state == NODE_STALE
    assert node.last_l2_at == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_self_hosted_valid_verdict_dates_prevent_older_ledger_revival(env, monkeypatch, ok):
    from app.domains.proxies.models import ClashNode
    from app.domains.proxypool import bridge, dead_probe as DP

    await init_db()
    await _add_recovery_graph([("leaf", NODE_DEAD, {})])
    async with get_session_factory()() as session:
        node = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == "leaf"))
        node.last_l2_at = NOW - timedelta(hours=2)
        session.add(ClashNode(
            subscription_id=1, name="leaf", status="ok", exit_ip="1.2.3.4",
            last_checked_at=NOW - timedelta(hours=1), latency_ms=5,
        ))
        await session.commit()
    _stub_kernel_exe(monkeypatch, env)
    _stub_self_hosted(monkeypatch, {"leaf": ok})

    async with get_session_factory()() as session:
        await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
    node = await _node("leaf")
    assert node.state == (NODE_ACTIVE if ok else NODE_DEAD)
    if not ok:
        async with get_session_factory()() as session:
            result = await bridge.ingest_ledger(session, now=NOW + timedelta(minutes=1))
            await session.commit()
        assert result.activated == 0
        assert (await _node("leaf")).state == NODE_DEAD
    assert node.last_l2_at == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["dependency", "controller"])
async def test_self_hosted_unavailable_probe_preserves_valid_verdict_date(env, monkeypatch, failure):
    from app.domains.proxypool import dead_probe as DP

    await init_db()
    config = {"dialer-proxy": "missing"} if failure == "dependency" else {}
    await _add_recovery_graph([("leaf", NODE_DEAD, config)])
    previous = NOW - timedelta(hours=2)
    async with get_session_factory()() as session:
        node = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == "leaf"))
        node.last_l2_at = previous
        await session.commit()
    _stub_ephemeral_kernel(monkeypatch, env)
    if failure == "controller":
        async def unavailable(*args):
            return False

        monkeypatch.setattr(DP, "_wait_controller", unavailable)

    async with get_session_factory()() as session:
        outcomes = await DP.recover_dead_nodes_self_hosted(session, data_dir=env, now=NOW)
        observations = (await session.scalars(select(HealthObservation))).all()

    node = await _node("leaf")
    assert len(outcomes) == len(observations) == 1
    assert not outcomes[0].ok and not outcomes[0].node_failure
    assert node.last_l2_at == previous
    assert node.state == NODE_DEAD and node.consecutive_failures == 1
