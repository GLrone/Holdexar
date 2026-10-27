"""订阅生命周期、原始失败证据与重建诊断。"""
from __future__ import annotations

from datetime import datetime
import json

import pytest
import yaml
from sqlalchemy import select

from app.core.database import get_session_factory, init_db
from app.domains.proxies.models import ProxySubscription
from app.domains.proxypool import admission as adm
from app.domains.proxypool import bootstrap as bs
from app.domains.proxypool import runtime as rt
from app.domains.proxypool.models import ProxyNode, SubscriptionSnapshot
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
def env(tmp_data_dir):
    return tmp_data_dir


async def _sub(label: str, url: str, *, status: str) -> int:
    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url=url, label=label,
                                created_at=NOW, admission_status=status)
        s.add(sub)
        await s.commit()
        return sub.id


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


async def _snapshots(sub_id):
    async with get_session_factory()() as session:
        return list((await session.scalars(
            select(SubscriptionSnapshot).where(SubscriptionSnapshot.subscription_id == sub_id)
            .order_by(SubscriptionSnapshot.id)
        )).all())


async def _fetch_status(sub_id: int) -> str | None:
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        return None if row is None else row.last_fetch_status




@pytest.mark.asyncio
async def test_subscription_failure_preserves_original_attempt(env, monkeypatch):
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

    snapshots = await _snapshots(sub_id)
    assert len(snapshots) == 1
    assert snapshots[0].status == "FAILED"
    assert snapshots[0].fetched_at == NOW
    assert json.loads(snapshots[0].attempts_json)[0]["http_status"] == 404
    assert "https://" not in snapshots[0].error


@pytest.mark.asyncio
async def test_each_failure_preserves_an_attempt(env, monkeypatch):
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

    assert len(await _snapshots(sub_id)) == 2






@pytest.mark.asyncio
async def test_promotion_uses_original_snapshot_and_is_idempotent(env, monkeypatch):
    await init_db()
    a = await _sub("A", "https://a.invalid", status="CANDIDATE")
    nodes = [{"name": "x", "type": "http", "server": "10.0.0.1", "port": 1}]
    sha = await _persist_ok_snapshot(a, env, nodes)
    monkeypatch.setattr(adm, "request_rebuild", lambda: [])

    from app.domains.proxies import router as proxies_router

    await proxies_router._auto_promote_after_check(a, alive=1)

    for _ in range(2):
        await proxies_router._auto_promote_after_check(a, alive=1)
        async with get_session_factory()() as session:
            sub = await session.get(ProxySubscription, a)
            nodes = list((await session.scalars(select(ProxyNode))).all())
        assert sub.admission_status == "ACTIVE"
        assert len(nodes) == 1
        assert nodes[0].subscription_id == a
        assert nodes[0].original_name == "x"
        assert [snap.sha256 for snap in await _snapshots(a)] == [sha]




class _FakeRuntime:
    secret = ""

    def stop(self):
        return None

    def start(self, exe_path, config_path, inject_lanes=True):
        return {"controllerUrl": "http://127.0.0.1:1"}


@pytest.mark.asyncio
async def test_rebuild_rejection_keeps_failure_diagnosis(
    env, monkeypatch, caplog
):
    """重建被对账拒绝 → 抛异常、调用方回滚，事件仍在库里（独立会话）。"""
    await init_db()

    async def _build_pool(session, *, data_dir):
        from app.domains.proxypool.pool import PoolBuild

        return PoolBuild(data_dir / "crawl-pool.yaml", 1, 1, ("n1",))

    monkeypatch.setattr(rt, "build_pool", _build_pool)
    monkeypatch.setattr(rt, "prepare_runtime_config", lambda _d, **kw: env / "c.runtime.yaml")
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

    assert any(record.levelname == "ERROR" and "n1" in record.message
               for record in caplog.records)


async def _empty():
    return frozenset()


async def _port():
    return 12345
