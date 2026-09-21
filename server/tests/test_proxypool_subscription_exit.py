"""订阅退出生产池：置回 CANDIDATE + 移除本订阅来源，身份账本保留。

来源保护：别的订阅仍提供同一指纹时该节点不受影响；最后一个来源被移除的节点
按合格集口径（状态合格 + 配置完整 + 至少一个当前来源）退出池，并由既有重建链收敛。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxies.models import ProxySubscription
from app.domains.proxypool import admission as adm
from app.domains.proxypool.admission import exit_from_production, promote_to_active
from app.domains.proxypool.models import ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import eligible_runtime_names
from app.domains.proxypool.subscription import (
    FetchAttempt, FetchResult, build_snapshot, detect_format, persist_snapshot,
)

NOW = datetime(2026, 9, 21, 9, 0, 0)


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


async def _sub(label: str, url: str) -> int:
    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url=url, label=label,
                                created_at=NOW, admission_status="ACTIVE")
        s.add(sub)
        await s.commit()
        return sub.id


async def _node(node_id: str, sources: list[int], state: str = "ACTIVE") -> None:
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                        proxy_type="http", server="10.0.0.1",
                        normalized_config={"name": node_id, "type": "http",
                                           "server": "10.0.0.1", "port": 1},
                        state=state, first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        for sid in sources:
            s.add(ProxyNodeSource(node_id=node_id, subscription_id=sid,
                                  original_name=node_id, first_seen=NOW, last_seen=NOW))
        await s.commit()


async def _source_count(node_id: str) -> int:
    async with get_session_factory()() as s:
        return int(await s.scalar(
            select(func.count()).select_from(ProxyNodeSource)
            .where(ProxyNodeSource.node_id == node_id)) or 0)


async def _admission(sub_id: int) -> str:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxySubscription).where(ProxySubscription.id == sub_id))).scalar_one()
        return str(row.admission_status)


@pytest.mark.asyncio
async def test_other_subscription_exit_leaves_shared_node_untouched(env, monkeypatch):
    """A、B 提供 X；C 退出（不提供 X）→ X 不受影响。"""
    await init_db()
    a, b, c = await _sub("A", "https://a.invalid"), await _sub("B", "https://b.invalid"), \
        await _sub("C", "https://c.invalid")
    await _node("x", [a, b])
    await _node("y", [c])
    rebuilds: list[int] = []
    monkeypatch.setattr(adm, "request_rebuild", lambda: rebuilds.append(1))

    async with get_session_factory()() as s:
        result = await exit_from_production(s, subscription_id=c)
        await s.commit()

    assert result.exited is True and result.removed_sources == 1
    assert await _source_count("x") == 2, "C 退出不得碰 X 的来源"
    assert await _source_count("y") == 0
    async with get_session_factory()() as s:
        eligible = set(await eligible_runtime_names(s))
    assert "1|x" in eligible and "1|y" not in eligible
    assert rebuilds == [1], "合格集变化必须触发既有重建链"


@pytest.mark.asyncio
async def test_one_of_two_sources_exits_keeps_node_eligible(env, monkeypatch):
    """A、B 都提供 X；A 退出 → X 仍有 B 来源，继续合格。"""
    await init_db()
    a, b = await _sub("A", "https://a.invalid"), await _sub("B", "https://b.invalid")
    await _node("x", [a, b])
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    async with get_session_factory()() as s:
        await exit_from_production(s, subscription_id=a)
        await s.commit()

    assert await _source_count("x") == 1
    async with get_session_factory()() as s:
        eligible = set(await eligible_runtime_names(s))
    assert "1|x" in eligible, "仍有来源的共享节点必须继续合格"


@pytest.mark.asyncio
async def test_last_source_exit_shrinks_eligible_and_keeps_identity(env, monkeypatch):
    """A、B 都退出 → X 来源 0 → 不再合格；ProxyNode 身份保留。"""
    await init_db()
    a, b = await _sub("A", "https://a.invalid"), await _sub("B", "https://b.invalid")
    await _node("x", [a, b])
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    async with get_session_factory()() as s:
        await exit_from_production(s, subscription_id=a)
        await s.commit()
    async with get_session_factory()() as s:
        await exit_from_production(s, subscription_id=b)
        await s.commit()

    assert await _source_count("x") == 0
    async with get_session_factory()() as s:
        eligible = set(await eligible_runtime_names(s))
        still_there = int(await s.scalar(
            select(func.count()).select_from(ProxyNode)
            .where(ProxyNode.node_id == "x")) or 0)
    assert "1|x" not in eligible, "没有来源的节点必须退出池"
    assert still_there == 1, "身份账本不得删除"
    assert await _admission(a) == "CANDIDATE" and await _admission(b) == "CANDIDATE"


@pytest.mark.asyncio
async def test_exit_is_idempotent(env, monkeypatch):
    await init_db()
    a = await _sub("A", "https://a.invalid")
    await _node("x", [a])
    rebuilds: list[int] = []
    monkeypatch.setattr(adm, "request_rebuild", lambda: rebuilds.append(1))

    async with get_session_factory()() as s:
        first = await exit_from_production(s, subscription_id=a)
        await s.commit()
    async with get_session_factory()() as s:
        again = await exit_from_production(s, subscription_id=a)
        await s.commit()

    assert first.exited is True and again.exited is False
    assert again.detail == "本来就不在池里"
    assert rebuilds == [1], "幂等退出不得重复制造重建"


@pytest.mark.asyncio
async def test_repromote_after_exit_restores_sources(env, monkeypatch):
    """退出可逆：再次 Promote 用当前 URL 的最新成功快照恢复来源与 ACTIVE。"""
    await init_db()
    a = await _sub("A", "https://a.invalid")
    nodes = [{"name": "x", "type": "http", "server": "10.0.0.1", "port": 1}]
    raw = yaml.safe_dump({"proxies": nodes}).encode("utf-8")
    res = FetchResult(raw=raw, http_status=200, content_type="text/yaml", channel="direct",
                      fmt=detect_format(raw),
                      attempts=[FetchAttempt("direct", True, 200, "yaml")])
    async with get_session_factory()() as s:
        snap = build_snapshot(a, "https://a.invalid", res, nodes, now=NOW)
        await persist_snapshot(s, snap, data_dir=env)
        await s.commit()
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    async with get_session_factory()() as s:
        await promote_to_active(s, subscription_id=a, data_dir=env, now=NOW)
        await s.commit()
    assert await _admission(a) == "ACTIVE"

    async with get_session_factory()() as s:
        await exit_from_production(s, subscription_id=a)
        await s.commit()
    assert await _admission(a) == "CANDIDATE"
    async with get_session_factory()() as s:
        assert await s.scalar(select(func.count()).select_from(ProxyNodeSource)) == 0

    async with get_session_factory()() as s:
        again = await promote_to_active(s, subscription_id=a, data_dir=env, now=NOW)
        await s.commit()
    assert again.promoted is True
    assert await _admission(a) == "ACTIVE"
    async with get_session_factory()() as s:
        assert (await s.scalar(
            select(func.count()).select_from(ProxyNodeSource)
            .where(ProxyNodeSource.subscription_id == a))) == 1