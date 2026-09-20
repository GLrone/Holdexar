"""L0 的连续失败计数输入：真实计数进状态机，台账不再出现「DEAD 且计数 0」。

计数口径：成功归零、失败在既有值上累加；它同时是状态机的输入（退休线判据）与台账事实。
阈值（DEAD/RETIRED 判定线）本文件不做任何修改，只钉住"送进去的是真实计数"。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import health as H
from app.domains.proxypool.models import ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import pool_path
from app.domains.proxypool.state import (
    DEFAULT_RETIRE_AFTER_FAILED_PROBES,
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_RETIRED,
    evaluate_node_state,
)

NOW = datetime(2026, 9, 20, 22, 0, 0)
NAME = "1|probe-node"
NODE_ID = "fp-probe"


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


async def _prepare(data_dir, *, state: str = "NEW", failures: int = 0) -> None:
    await init_db()
    (data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    pool_path(data_dir).write_text(
        yaml.safe_dump({"proxies": [{"name": NAME, "type": "http",
                                     "server": "10.0.0.1", "port": 1}]}),
        encoding="utf-8",
    )
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=NODE_ID, fingerprint=NODE_ID, runtime_name=NAME,
                        proxy_type="http", server="10.0.0.1",
                        normalized_config={"name": "probe-node"}, state=state,
                        consecutive_failures=failures,
                        first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        s.add(ProxyNodeSource(node_id=NODE_ID, subscription_id=1,
                              original_name="probe-node", first_seen=NOW, last_seen=NOW))
        await s.commit()


async def _probe(data_dir, monkeypatch, ok: bool):
    async def fake_probe(controller_url, secret, runtime_name, *, url=None, timeout_ms=None):
        return H.ProbeResult(runtime_name=runtime_name, ok=ok,
                             delay_ms=5 if ok else None,
                             detail="" if ok else "内核探测失败（HTTP 503）")

    monkeypatch.setattr(H, "probe_node", fake_probe)
    async with get_session_factory()() as s:
        outcomes = await H.health_check_pool(
            s, data_dir=data_dir, controller_url="http://127.0.0.1:9",
            secret="s", now=NOW,
        )
        await s.commit()
    return outcomes


async def _node() -> ProxyNode:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.node_id == NODE_ID)
        )).scalar_one()


@pytest.mark.asyncio
async def test_single_failure_records_count_one(env, monkeypatch):
    await _prepare(env)
    await _probe(env, monkeypatch, ok=False)
    node = await _node()
    assert node.consecutive_failures == 1, "一次失败必须记 1，不能是 0"
    assert node.state == NODE_DEAD


@pytest.mark.asyncio
async def test_second_consecutive_failure_increments(env, monkeypatch):
    await _prepare(env)
    await _probe(env, monkeypatch, ok=False)
    assert (await _node()).consecutive_failures == 1
    await _probe(env, monkeypatch, ok=False)
    node = await _node()
    assert node.consecutive_failures == 2, "连续失败必须累加"
    assert node.state == NODE_DEAD


@pytest.mark.asyncio
async def test_success_resets_count_and_marks_active(env, monkeypatch):
    await _prepare(env)
    await _probe(env, monkeypatch, ok=False)
    assert (await _node()).consecutive_failures == 1
    await _probe(env, monkeypatch, ok=True)
    node = await _node()
    assert node.consecutive_failures == 0, "成功必须把连续失败清零"
    assert node.state == NODE_ACTIVE


@pytest.mark.asyncio
async def test_dead_node_ledger_is_consistent(env, monkeypatch):
    """DEAD 节点的计数与状态必须自洽：不能出现 DEAD 且计数 0。"""
    await _prepare(env)
    await _probe(env, monkeypatch, ok=False)
    node = await _node()
    assert node.state == NODE_DEAD
    assert node.consecutive_failures >= 1, "DEAD 必须带着失败次数"
    expected = evaluate_node_state(
        "NEW",                                   # 探针前的状态
        source_seen=True, probe_ok=False,
        consecutive_failures=node.consecutive_failures,
    )
    assert node.state == expected, "状态必须与状态机按同计数算出的结果一致"


@pytest.mark.asyncio
async def test_retire_threshold_consumes_real_count(env, monkeypatch):
    """真实计数必须真的被退休线消费（阈值本身不变）。"""
    await _prepare(env, state=NODE_DEAD, failures=DEFAULT_RETIRE_AFTER_FAILED_PROBES - 1)
    await _probe(env, monkeypatch, ok=False)
    node = await _node()
    assert node.consecutive_failures == DEFAULT_RETIRE_AFTER_FAILED_PROBES
    assert node.state == NODE_RETIRED