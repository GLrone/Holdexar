"""池收敛：促升后触发重建 + `ensure_pool_runtime` 的「池文件 vs 合格集」集合对账。

- 促升：apply 让 eligible 变化 → 走既有 `request_rebuild()`；幂等促升不重复制造重建。
- 复用口径：**集合相等**才算 current；只可达但池文件落后（或数量相同、集合不同）不算，
  改走既有重建链（`request_rebuild` → `run_pending_rebuild`），占线时如实返回未 current。
"""
from __future__ import annotations

from datetime import datetime

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxies.models import ProxySubscription
from app.domains.proxypool import admission as adm
from app.domains.proxypool import bootstrap as bs
from app.domains.proxypool import scheduling as sched
from app.domains.proxypool.admission import promote_to_active
from app.domains.proxypool.models import ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import pool_path
from app.domains.proxypool.subscription import (
    FetchAttempt, FetchResult, build_snapshot, detect_format, persist_snapshot,
)

NOW = datetime(2026, 9, 21, 1, 0, 0)


async def _admission_of(sub_id: int) -> str:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxySubscription).where(ProxySubscription.id == sub_id)
        )).scalar_one()
        return str(row.admission_status)


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


def _write_pool(data_dir, names: list[str]) -> None:
    (data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    pool_path(data_dir).write_text(
        yaml.safe_dump({"proxies": [{"name": n, "type": "http",
                                     "server": "10.0.0.1", "port": 1} for n in names]}),
        encoding="utf-8",
    )


async def _add_node(node_id: str, state: str = "NEW") -> None:
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=node_id, fingerprint=node_id, runtime_name=f"1|{node_id}",
                        proxy_type="http", server="10.0.0.1",
                        normalized_config={"name": node_id, "type": "http",
                                           "server": "10.0.0.1", "port": 1},
                        state=state, first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        # 合格集口径含「至少一个当前来源」
        s.add(ProxyNodeSource(node_id=node_id, subscription_id=1, original_name=node_id,
                              first_seen=NOW, last_seen=NOW))
        await s.commit()


@pytest.fixture
def runtime_stub(monkeypatch):
    """让「可达 + 端点 + proxy_url」都不依赖真内核。"""
    monkeypatch.setattr(bs, "runtime_ready", lambda session, *, data_dir: _true())
    monkeypatch.setattr(bs, "controller_endpoint_of",
                        lambda data_dir: ("http://127.0.0.1:9", "secret"))
    monkeypatch.setattr(bs, "current_runtime_proxy_url",
                        lambda data_dir: "http://127.0.0.1:9")
    return monkeypatch


async def _true():
    return True


# ── A. 促升触发重建 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_promote_requests_rebuild_when_eligible_grows(env, monkeypatch):
    await init_db()
    rebuilds: list[int] = []
    monkeypatch.setattr(adm, "request_rebuild", lambda: rebuilds.append(1))

    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url="https://sub.invalid/a", label="A",
                                created_at=NOW, admission_status="CANDIDATE")
        s.add(sub)
        await s.commit()
        sub_id = sub.id
        nodes = [{"name": "n1", "type": "http", "server": "10.0.0.1", "port": 1}]
        raw = yaml.safe_dump({"proxies": nodes}).encode("utf-8")
        res = FetchResult(raw=raw, http_status=200, content_type="text/yaml",
                          channel="direct", fmt=detect_format(raw),
                          attempts=[FetchAttempt("direct", True, 200, "yaml")])
        snap = build_snapshot(sub_id, "https://sub.invalid/a", res, nodes, now=NOW)
        await persist_snapshot(s, snap, data_dir=env)
        await s.commit()

    async with get_session_factory()() as s:
        result = await promote_to_active(s, subscription_id=sub_id, data_dir=env, now=NOW)
        await s.commit()

    assert result.promoted is True
    assert await _admission_of(sub_id) == "ACTIVE"
    assert rebuilds == [1], "eligible 增长必须触发既有重建链"


@pytest.mark.asyncio
async def test_idempotent_promote_does_not_request_rebuild(env, monkeypatch):
    await init_db()
    rebuilds: list[int] = []
    monkeypatch.setattr(adm, "request_rebuild", lambda: rebuilds.append(1))

    async with get_session_factory()() as s:
        sub = ProxySubscription(kind="clash", url="https://sub.invalid/b", label="B",
                                created_at=NOW, admission_status="CANDIDATE")
        s.add(sub)
        await s.commit()
        sub_id = sub.id
        nodes = [{"name": "n1", "type": "http", "server": "10.0.0.1", "port": 1}]
        raw = yaml.safe_dump({"proxies": nodes}).encode("utf-8")
        res = FetchResult(raw=raw, http_status=200, content_type="text/yaml",
                          channel="direct", fmt=detect_format(raw),
                          attempts=[FetchAttempt("direct", True, 200, "yaml")])
        snap = build_snapshot(sub_id, "https://sub.invalid/b", res, nodes, now=NOW)
        await persist_snapshot(s, snap, data_dir=env)
        await s.commit()

    async with get_session_factory()() as s:
        await promote_to_active(s, subscription_id=sub_id, data_dir=env, now=NOW)
        await s.commit()
    assert rebuilds == [1]

    async with get_session_factory()() as s:
        again = await promote_to_active(s, subscription_id=sub_id, data_dir=env, now=NOW)
        await s.commit()
    assert again.promoted is False and again.detail == "已是 ACTIVE"
    assert rebuilds == [1], "幂等促升不得重复制造重建"


# ── B. ensure_pool_runtime 集合对账 ────────────────────────────────
@pytest.mark.asyncio
async def test_stale_pool_file_is_not_reported_current(env, runtime_stub, monkeypatch):
    """eligible ≠ 池文件（可达也一样）→ 不声称 ready，并走既有重建链。"""
    await init_db()
    await _add_node("a")
    await _add_node("b")
    _write_pool(env, ["1|a"])                      # 池文件落后：缺 1|b
    calls: list[str] = []
    monkeypatch.setattr(sched, "request_rebuild", lambda: calls.append("request"))
    monkeypatch.setattr(sched, "run_pending_rebuild", _no_rebuild)
    monkeypatch.setattr(bs, "eligible_runtime_names", _eligible)

    async with get_session_factory()() as s:
        boot = await bs.ensure_pool_runtime(s, data_dir=env, runtime=object(),
                                            exe_path="x", now=NOW)
    assert boot.ready is False, "池文件落后时不得声称 current"
    assert calls == ["request"], "必须触发既有重建链"
    assert "不一致" in boot.detail


@pytest.mark.asyncio
async def test_same_count_but_different_set_still_rebuilds(env, runtime_stub, monkeypatch):
    """数量相同、集合不同 → 仍必须重建（比集合，不比数量）。"""
    await init_db()
    await _add_node("a")
    await _add_node("b")
    _write_pool(env, ["1|a", "1|c"])               # 数量 2 == 2，但集合不同
    calls: list[str] = []
    monkeypatch.setattr(sched, "request_rebuild", lambda: calls.append("request"))
    monkeypatch.setattr(sched, "run_pending_rebuild", _no_rebuild)
    monkeypatch.setattr(bs, "eligible_runtime_names", _eligible)

    async with get_session_factory()() as s:
        boot = await bs.ensure_pool_runtime(s, data_dir=env, runtime=object(),
                                            exe_path="x", now=NOW)
    assert boot.ready is False
    assert calls == ["request"], "数量相同但集合不同也必须重建"


@pytest.mark.asyncio
async def test_equal_sets_reuse_without_rebuild(env, runtime_stub, monkeypatch):
    await init_db()
    await _add_node("a")
    await _add_node("b")
    _write_pool(env, ["1|a", "1|b"])
    calls: list[str] = []
    monkeypatch.setattr(sched, "request_rebuild", lambda: calls.append("request"))
    monkeypatch.setattr(sched, "run_pending_rebuild", _no_rebuild)
    monkeypatch.setattr(bs, "eligible_runtime_names", _eligible)

    async with get_session_factory()() as s:
        boot = await bs.ensure_pool_runtime(s, data_dir=env, runtime=object(),
                                            exe_path="x", now=NOW)
    assert boot.ready is True and boot.bootstrapped is False
    assert calls == [], "集合一致应正常复用，不制造重建"
    assert boot.detail == "已有可用 Runtime"


@pytest.mark.asyncio
async def test_rebuild_then_current(env, runtime_stub, monkeypatch):
    """重建真正执行后：池文件跟上合格集 → 可以声称 current。"""
    await init_db()
    await _add_node("a")
    await _add_node("b")
    _write_pool(env, ["1|a"])
    monkeypatch.setattr(sched, "request_rebuild", lambda: None)
    monkeypatch.setattr(bs, "eligible_runtime_names", _eligible)

    async def _rebuild(session, *, data_dir, controller_url, secret, runtime, exe_path):
        _write_pool(data_dir, ["1|a", "1|b"])       # 重建把池文件写到当前合格集
        return "rebuilt"

    monkeypatch.setattr(sched, "run_pending_rebuild", _rebuild)

    async with get_session_factory()() as s:
        boot = await bs.ensure_pool_runtime(s, data_dir=env, runtime=object(),
                                            exe_path="x", now=NOW)
    assert boot.ready is True and boot.bootstrapped is True


async def _no_rebuild(session, *, data_dir, controller_url, secret, runtime, exe_path):
    return None


async def _eligible(session):
    from app.domains.proxypool.pool import eligible_runtime_names
    return await eligible_runtime_names(session)
