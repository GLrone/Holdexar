"""DEAD 恢复探测：DEAD 不在池文件里，池内 L0 永远探不到它。

没有这条路径，节点一旦 DEAD 就永久出局、失败计数也停住（退休线在生产路径不可达）。
本文件钉住：成功回 ACTIVE 并清零、失败保持 DEAD 并累加、分批轮转最久没探过的先来、
达标走既有退休终点、不进 crawl pool、未选中节点不做任何回填。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import health as H
from app.domains.proxypool import scheduling as sched
from app.domains.proxypool.models import HealthObservation, ProxyNode, ProxyNodeSource
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
        s.add(ProxyNode(node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                        proxy_type="http", server="10.0.0.1",
                        normalized_config=dict(CONFIG), state=state,
                        consecutive_failures=failures,
                        first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        s.add(ProxyNodeSource(node_id=node_id, subscription_id=1,
                              original_name=f"sub-{node_id}", first_seen=NOW, last_seen=NOW))
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
async def test_recovery_prioritises_nodes_already_accumulating(env, monkeypatch):
    """已在累计失败的节点先收口：否则要等轮转一整圈才回到它，退休线长期不可达。"""
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

    assert probed == ["sub-failing"], "累计中的先探，才能走到既有退休终点"
    assert (await _node("failing")).consecutive_failures == 2
    assert (await _node("fresh")).consecutive_failures == 0, "未选中节点不得被回填"