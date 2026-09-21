"""编排事件流水：生产生命周期是否真的留下了可回溯的事件行。

覆盖四件事：
1. **真实生产调用链触发 writer**——不是直接 INSERT，而是走
   `sync_subscriptions` / `promote` / `exit` / `rebuild_runtime` 各自的入口；
2. **字段真实**——run/subscription、时间、计数、SHA 都取自调用上下文；
3. **append-only**——每次真实发生各一行，幂等空转不写；同一生命周期不会刷出几十行；
4. **事务语义**——同事务写的事件与业务行同生共死；抛异常路径的事件独立落库，
   不随调用方回滚消失。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxies.models import ProxySubscription
from app.domains.proxypool import admission as adm
from app.domains.proxypool import bootstrap as bs
from app.domains.proxypool import events, router as pp_router
from app.domains.proxypool import runtime as rt
from app.domains.proxypool.models import (
    OrchestrationEvent,
    ProxyNode,
    ProxyNodeSource,
)
from app.domains.proxypool.subscription import (
    FetchAttempt,
    FetchFailedError,
    FetchResult,
    build_snapshot,
    detect_format,
    persist_snapshot,
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


async def _sub(label: str, url: str, *, status: str) -> int:
    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url=url, label=label,
                                created_at=NOW, admission_status=status)
        s.add(sub)
        await s.commit()
        return sub.id


async def _node(node_id: str, sources: list[int]) -> None:
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                        proxy_type="http", server="10.0.0.1",
                        normalized_config={"name": node_id, "type": "http",
                                           "server": "10.0.0.1", "port": 1},
                        state="ACTIVE", first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        for sid in sources:
            s.add(ProxyNodeSource(node_id=node_id, subscription_id=sid,
                                  original_name=node_id, first_seen=NOW, last_seen=NOW))
        await s.commit()


async def _persist_ok_snapshot(sub_id: int, data_dir, nodes: list[dict]) -> str:
    raw = yaml.safe_dump({"proxies": nodes}).encode("utf-8")
    res = FetchResult(raw=raw, http_status=200, content_type="text/yaml", channel="direct",
                      fmt=detect_format(raw),
                      attempts=[FetchAttempt("direct", True, 200, "yaml")])
    async with get_session_factory()() as s:
        snap = build_snapshot(sub_id, "https://a.invalid", res, nodes, now=NOW)
        await persist_snapshot(s, snap, data_dir=data_dir)
        await s.commit()
        return snap.sha256


async def _events(kind: str | None = None) -> list[OrchestrationEvent]:
    async with get_session_factory()() as s:
        stmt = select(OrchestrationEvent).order_by(OrchestrationEvent.id)
        if kind is not None:
            stmt = stmt.where(OrchestrationEvent.kind == kind)
        return list((await s.execute(stmt)).scalars().all())


async def _fetch_status(sub_id: int) -> str | None:
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        return None if row is None else row.last_fetch_status


# ── 1. 订阅同步失败：真实调用链 + 同事务 ──────────────────────────


@pytest.mark.asyncio
async def test_subscription_failure_writes_event_alongside_failed_projection(env, monkeypatch):
    """`sync_subscriptions` 抓到失败 → FAILED 投影与事件同事务落地。"""
    await init_db()
    sub_id = await _sub("A", "https://a.invalid", status="ACTIVE")

    async def _boom(url, channels, **kw):
        raise FetchFailedError(url, [FetchAttempt("direct", False, 404, "html")])

    monkeypatch.setattr(bs, "fetch_subscription", _boom)

    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=env, now=NOW)
        await s.commit()

    assert list(sync.failures) == [sub_id]
    assert await _fetch_status(sub_id) == "FAILED"

    rows = await _events(events.KIND_SUBSCRIPTION_FAILED)
    assert len(rows) == 1
    row = rows[0]
    assert row.level == events.LEVEL_WARN
    assert row.ts == NOW
    assert row.payload_json == {"subscriptionId": sub_id}
    # message 用生产异常原文（含失败通道与状态码），不是占位串
    assert "direct:404" in row.message
    assert "https://" not in row.message, "事件不得落订阅链接"


@pytest.mark.asyncio
async def test_each_failure_appends_its_own_event(env, monkeypatch):
    """append-only：失败两次就是两行，不做去重、不合并成一行计数。"""
    await init_db()
    sub_id = await _sub("A", "https://a.invalid", status="ACTIVE")

    async def _boom(url, channels, **kw):
        raise FetchFailedError(url, [FetchAttempt("direct", False, 404, "html")])

    monkeypatch.setattr(bs, "fetch_subscription", _boom)
    for _ in range(2):
        async with get_session_factory()() as s:
            await bs.sync_subscriptions(s, data_dir=env, now=NOW)
            await s.commit()

    assert len(await _events(events.KIND_SUBSCRIPTION_FAILED)) == 2
    assert sub_id  # 同一订阅的两次失败各留一行


@pytest.mark.asyncio
async def test_event_written_with_session_follows_caller_rollback(env):
    """同事务语义：调用方回滚 → 事件一并消失（不会留下「事件说发生了、业务却没有」）。"""
    await init_db()
    async with get_session_factory()() as s:
        await events.record("probe_kind", "m", session=s, now=NOW)
        await s.rollback()
    assert await _events("probe_kind") == []


# ── 2. 订阅退出 / 晋升：路由生产路径 ──────────────────────────────


@pytest.mark.asyncio
async def test_exit_writes_event_and_idempotent_second_call_writes_nothing(env, monkeypatch):
    await init_db()
    a = await _sub("A", "https://a.invalid", status="ACTIVE")
    await _node("x", [a])
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    first = await pp_router.exit_subscription(a)
    assert first["exited"] is True

    rows = await _events(events.KIND_SUBSCRIPTION_EXITED)
    assert len(rows) == 1
    assert rows[0].payload_json == {"subscriptionId": a, "removedSources": 1}
    assert rows[0].level == events.LEVEL_WARN

    # 幂等空转：本来就不在池里 → 不写第二行（同一生命周期只有一次退出）
    second = await pp_router.exit_subscription(a)
    assert second["exited"] is False
    assert len(await _events(events.KIND_SUBSCRIPTION_EXITED)) == 1


@pytest.mark.asyncio
async def test_promote_writes_event_with_real_snapshot_facts(env, monkeypatch):
    await init_db()
    a = await _sub("A", "https://a.invalid", status="CANDIDATE")
    nodes = [{"name": "x", "type": "http", "server": "10.0.0.1", "port": 1}]
    sha = await _persist_ok_snapshot(a, env, nodes)
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    first = await pp_router.promote_subscription(a)
    assert first["promoted"] is True

    rows = await _events(events.KIND_SUBSCRIPTION_PROMOTED)
    assert len(rows) == 1
    assert rows[0].payload_json == {
        "subscriptionId": a, "snapshotSha256": sha, "appliedNodes": 1,
    }
    assert sha[:10] in rows[0].message
    assert rows[0].ts is not None

    second = await pp_router.promote_subscription(a)
    assert second["promoted"] is False
    assert len(await _events(events.KIND_SUBSCRIPTION_PROMOTED)) == 1


# ── 3. 对账拒绝：抛异常路径的事件独立落库 ─────────────────────────


class _FakeRuntime:
    secret = ""

    def stop(self):
        return None

    def start(self, exe_path, config_path):
        return {"controllerUrl": "http://127.0.0.1:1"}


@pytest.mark.asyncio
async def test_rebuild_reconcile_rejection_writes_event_outside_caller_transaction(
    env, monkeypatch
):
    """重建被对账拒绝 → 抛异常、调用方回滚，事件仍在库里（独立会话）。"""
    await init_db()

    class _Build:
        runtime_names = ("n1",)

    async def _build_pool(session, *, data_dir):
        return _Build()

    monkeypatch.setattr(rt, "build_pool", _build_pool)
    monkeypatch.setattr(rt, "prepare_runtime_config", lambda _d: env / "c.runtime.yaml")
    monkeypatch.setattr(rt, "wait_proxy_names", lambda *a, **kw: _empty())
    monkeypatch.setattr(rt, "wait_mixed_port", lambda *a, **kw: _port())
    monkeypatch.setattr(
        rt, "reconcile",
        lambda **kw: rt.RuntimeReconcile(
            registry_names=frozenset({"n1"}), pool_names=frozenset({"n1"}),
            runtime_names=frozenset(), missing=frozenset({"n1"}), unexpected=frozenset(),
            observed_total=0, ok=False,
        ),
    )

    async with get_session_factory()() as s:
        with pytest.raises(rt.RuntimeRebuildError):
            await rt.rebuild_runtime(
                s, data_dir=env, controller_url=None, secret="",
                runtime=_FakeRuntime(), exe_path="mihomo",
            )
        await s.rollback()

    rows = await _events(events.KIND_RECONCILE_REJECTED)
    assert len(rows) == 1
    assert rows[0].level == events.LEVEL_ERROR
    assert rows[0].payload_json["phase"] == "rebuild"
    assert rows[0].payload_json["missingCount"] == 1
    assert "n1" in rows[0].message


async def _empty():
    return frozenset()


async def _port():
    return 12345


# ── 4. retention 仍然覆盖 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_telemetry_still_removes_old_events(env):
    """事件由同一张表承载 → 既有保留期清理照常生效（不因新增 writer 失效）。"""
    from app.domains.proxypool import retention

    await init_db()
    old = NOW - timedelta(days=200)
    await events.record(events.KIND_SUBSCRIPTION_EXITED, "旧事件", now=old)
    await events.record(events.KIND_SUBSCRIPTION_EXITED, "新事件", now=NOW)

    async with get_session_factory()() as s:
        result = await retention.prune_telemetry(s, NOW)
        await s.commit()

    assert result.orchestration_events == 1
    left = await _events(events.KIND_SUBSCRIPTION_EXITED)
    assert [r.message for r in left] == ["新事件"]


# ── 5. writer 自身失败不影响生产 ─────────────────────────────────


@pytest.mark.asyncio
async def test_record_is_fail_soft(env, monkeypatch):
    """写不进去只返回 False，绝不把异常抛给生产调用方。"""
    await init_db()

    def _explode():
        raise RuntimeError("会话工厂不可用")

    monkeypatch.setattr(events, "get_session_factory", _explode)
    assert await events.record("probe_kind", "m") is False
