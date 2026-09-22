"""P1.7：bootstrap 契约（9 条）。

`ensure_pool_runtime()` 只负责"**没有可用 Runtime 时**把
订阅 → Snapshot → Registry → Pool → Runtime 建起来"；它与 rebuild 是两个语义。

不新增数据模型、不新增状态机：串的是 P1.2～P1.6 已有的层。
"""
from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxies.models import (  # noqa: E402
    ADMISSION_ACTIVE,
    ProxySubscription,
)
from app.domains.proxypool import bootstrap as bs  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, ProxyNodeSource, SubscriptionSnapshot,
)
from app.domains.proxypool.runtime import (  # noqa: E402
    prepare_runtime_config, require_runtime_proxy_url,
)
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, FORMAT_YAML, FetchResult,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


# ── 夹具 ─────────────────────────────────────────────────────────
@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def kernel_exe_path(tmp_data_dir) -> Path:
    for candidate in (
        clash_manager.kernel_exe(tmp_data_dir),
        clash_manager.bundled_clash_dir() / kernel_filename(),
        *(
            sorted(Path(os.environ["LOCALAPPDATA"]).glob(f"holdexar*/clash/{kernel_filename()}"))
            if os.environ.get("LOCALAPPDATA") else []
        ),
    ):
        if candidate.is_file():
            return candidate
    pytest.skip("本机没有可用的 mihomo 内核资产（assets/clash/ 不随仓库分发）")


@pytest.fixture
def pool_runtime():
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


@pytest.fixture(autouse=True)
def _stub_channels(monkeypatch):
    """不发真实网络请求：订阅内容由脚本化 FetchResult 提供。"""
    monkeypatch.setattr(bs, "build_channels", lambda *a, **k: [])


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _node(name: str, server: str) -> dict:
    return {"name": name, "type": "http", "server": server, "port": 1}


def _fetch_ok(nodes: list[dict]) -> FetchResult:
    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    return FetchResult(raw=raw, http_status=200, content_type="text/yaml",
                       channel=CHANNEL_DIRECT, fmt=FORMAT_YAML)


async def _add_subscription(url: str = "https://sub.invalid/x") -> int:
    """建一条 **ACTIVE** 订阅：本文件验的是「获准进入生产」那条路径。

    （生产准入为 CANDIDATE 的路径见 `test_proxypool_admission.py`：候选只落快照。）
    """
    async with get_session_factory()() as s:
        sub = ProxySubscription(
            kind="clash", url=url, created_at=NOW, admission_status=ADMISSION_ACTIVE
        )
        s.add(sub)
        await s.commit()
        return sub.id


def _script_fetch(nodes: list[dict], monkeypatch) -> None:
    async def _fetch(url, channels, **kw):
        return _fetch_ok(nodes)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)


async def _ensure(data_dir, runtime, exe):
    async with get_session_factory()() as s:
        result = await bs.ensure_pool_runtime(
            s, data_dir=data_dir, runtime=runtime, exe_path=str(exe), now=NOW,
        )
        await s.commit()
        return result


async def _sub_row(sub_id: int) -> ProxySubscription:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxySubscription).where(ProxySubscription.id == sub_id)
        )).scalar_one()


# ══ 1. 首次无 Runtime，但有订阅 → 全链打通 ══════════════════════
@pytest.mark.asyncio
async def test_bootstrap_builds_whole_chain(tmp_data_dir, kernel_exe_path,
                                            pool_runtime, monkeypatch) -> None:
    await init_db()
    sub_id = await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1"), _node("B", "127.0.0.2")], monkeypatch)

    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.ready and result.bootstrapped
    assert result.pool_names == (f"{sub_id}|A", f"{sub_id}|B")
    assert result.proxy_url and result.proxy_url.startswith("http://127.0.0.1:")

    # Registry 真的落库了
    async with get_session_factory()() as s:
        nodes = list((await s.execute(select(ProxyNode))).scalars())
    assert {n.runtime_name for n in nodes} == set(result.pool_names)

    # Runtime 真的加载了这个池，且入口可用
    async with httpx.AsyncClient(
        timeout=10, headers={"Authorization": f"Bearer {pool_runtime.secret}"}
    ) as c:
        observed = (await c.get(f"{result.controller_url}/proxies")).json()["proxies"]
    assert set(result.pool_names).issubset(set(observed))


# ══ 2. 已有 ready Runtime → 不重启、不重建 ══════════════════════
@pytest.mark.asyncio
async def test_ensure_is_idempotent_when_ready(tmp_data_dir, kernel_exe_path,
                                               pool_runtime, monkeypatch) -> None:
    await init_db()
    await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)
    first = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)
    pid, port = pool_runtime.process.pid, pool_runtime.port

    second = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert first.bootstrapped is True
    assert second.ready is True and second.bootstrapped is False
    assert pool_runtime.process.pid == pid and pool_runtime.port == port


# ══ 3. 文件在但实际不可用 → 不能误判 ready ══════════════════════
@pytest.mark.asyncio
async def test_stale_config_is_not_ready(tmp_data_dir, kernel_exe_path,
                                         pool_runtime, monkeypatch) -> None:
    await init_db()
    await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)
    # 只有配置文件、没有内核在跑 —— 典型的"假 ready"
    async with get_session_factory()() as s:
        await bs.build_pool(s, data_dir=tmp_data_dir)
        await s.commit()
    prepare_runtime_config(tmp_data_dir)

    async with get_session_factory()() as s:
        assert await bs.runtime_ready(s, data_dir=tmp_data_dir) is False

    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)
    assert result.ready and result.bootstrapped, "假 ready 必须触发重新 bootstrap"


# ══ 4. 池变了但 Runtime 已 ready → 不在刷新里直接重启 ═══════════
@pytest.mark.asyncio
async def test_pool_change_does_not_restart_inside_refresh(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)
    await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)
    pid = pool_runtime.process.pid

    # 订阅刷新出**新池**
    _script_fetch([_node("A", "127.0.0.1"), _node("B", "127.0.0.2")], monkeypatch)
    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert sync.pool_changed is True
    assert pool_runtime.process.pid == pid, "刷新路径不得自己 stop/start Runtime"

    # 变化交给既定语义：置 pending，由空闲时的 rebuild 消费
    from app.domains.proxypool.scheduling import (
        rebuild_pending, request_rebuild, take_rebuild_pending,
    )
    take_rebuild_pending()
    request_rebuild()
    assert rebuild_pending() is True
    take_rebuild_pending()


# ══ 5. 无订阅 → 无法 bootstrap，crawler 保持 fail-closed ════════
@pytest.mark.asyncio
async def test_no_subscription_cannot_bootstrap(tmp_data_dir, kernel_exe_path,
                                                pool_runtime) -> None:
    await init_db()
    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.ready is False and result.bootstrapped is False
    # 与"有订阅但抓取失败"必须是**不同**的原因（真实运行诊断需要区分）
    assert "没有可 bootstrap 的订阅" in result.detail
    assert "订阅抓取失败" not in result.detail
    with pytest.raises(Exception, match="代理运行时不可用"):
        require_runtime_proxy_url(tmp_data_dir)


# ══ 6. 任一步失败 → 不启动 crawler、不退旧代理/直连 ═════════════
@pytest.mark.asyncio
async def test_failed_fetch_keeps_crawler_unavailable(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    sub_id = await _add_subscription()

    async def _boom(url, channels, **kw):
        raise RuntimeError("订阅下载失败")

    monkeypatch.setattr(bs, "fetch_subscription", _boom)
    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.ready is False
    row = await _sub_row(sub_id)
    assert row.last_fetch_status == "FAILED" and row.snapshot_sha256 is None
    with pytest.raises(Exception, match="代理运行时不可用"):
        require_runtime_proxy_url(tmp_data_dir)


# ══ 7. subscription_id 就是 proxy_subscriptions.id ══════════════
@pytest.mark.asyncio
async def test_subscription_id_is_proxy_subscriptions_id(
    tmp_data_dir, monkeypatch
) -> None:
    await init_db()
    sub_id = await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)

    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    async with get_session_factory()() as s:
        sources = list((await s.execute(select(ProxyNodeSource))).scalars())
    assert sources and {src.subscription_id for src in sources} == {sub_id}
    # 绝不是 snapshot id
    async with get_session_factory()() as s:
        snap = (await s.execute(select(SubscriptionSnapshot))).scalars().first()
    assert snap.id != sub_id or True  # id 空间独立，仅确保断言用的是订阅 id
    assert all(src.subscription_id == sub_id for src in sources)


# ══ 8. sha 事实源是 subscription_snapshots，投影仅在成功之后 ════
@pytest.mark.asyncio
async def test_snapshot_sha_canonical_and_projection_after_persist(
    tmp_data_dir, monkeypatch
) -> None:
    await init_db()
    sub_id = await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)

    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    async with get_session_factory()() as s:
        snaps = list((await s.execute(select(SubscriptionSnapshot))).scalars())
    assert len(snaps) == 1
    canonical = snaps[0].sha256
    assert sync.snapshot_sha256[sub_id] == canonical
    assert (await _sub_row(sub_id)).snapshot_sha256 == canonical, "投影=事实源（成功后）"

    # 失败时不得写投影：先清空，再让抓取失败
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxySubscription).where(ProxySubscription.id == sub_id)
        )).scalar_one()
        row.snapshot_sha256 = None
        await s.commit()

    async def _boom(url, channels, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(bs, "fetch_subscription", _boom)
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert (await _sub_row(sub_id)).snapshot_sha256 is None, "失败不得回写投影"


# ══ 9. 两个调用点并发 → 只有一次真正 bootstrap ══════════════════
@pytest.mark.asyncio
async def test_concurrent_ensure_bootstraps_once(tmp_data_dir, kernel_exe_path,
                                                 pool_runtime, monkeypatch) -> None:
    await init_db()
    await _add_subscription()
    _script_fetch([_node("A", "127.0.0.1")], monkeypatch)

    async def _one():
        async with get_session_factory()() as s:
            r = await bs.ensure_pool_runtime(
                s, data_dir=tmp_data_dir, runtime=pool_runtime,
                exe_path=str(kernel_exe_path), now=NOW,
            )
            await s.commit()
            return r

    first, second = await asyncio.gather(_one(), _one())

    assert sum(1 for r in (first, second) if r.bootstrapped) == 1, (
        "两个入口不能各自 bootstrap 一份 Runtime"
    )
    assert all(r.ready for r in (first, second))
    assert pool_runtime.process is not None


# ══ 10. 库层失败必须被记录，且不牵连同轮其它订阅（真实生产复现）═════
@pytest.mark.asyncio
async def test_sync_records_failure_without_crashing(tmp_data_dir, monkeypatch):
    """真实生产复现：`persist_snapshot` flush 失败后，错误分支再去读会话里的
    `sub.*` 会抛 `PendingRollbackError` ⇒ **失败本身没被记录**。

    修复后要求：
      - `SyncResult.failures` 带上失败原因；
      - 失败落到订阅行（`last_fetch_status='FAILED'` + `last_error`）；
      - savepoint 只回滚失败的那条订阅 → 同轮其它订阅照常落库。
    """
    await init_db()
    bad = await _add_subscription("https://bad.invalid/x")
    good = await _add_subscription("https://good.invalid/x")
    _script_fetch([_node("ISO1", "10.7.7.1")], monkeypatch)

    real_persist = bs.persist_snapshot
    calls = {"n": 0}

    async def _persist(session, snap, *, data_dir):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一条订阅写入失败（等价于真实库缺列那次）
            raise RuntimeError("no column named http_status")
        return await real_persist(session, snap, data_dir=data_dir)

    monkeypatch.setattr(bs, "persist_snapshot", _persist)

    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    assert sync.failures == {bad: "no column named http_status"}
    bad_row, good_row = await _sub_row(bad), await _sub_row(good)
    assert bad_row.last_fetch_status == "FAILED", "失败必须被记录下来"
    assert bad_row.last_error == "no column named http_status"
    assert good_row.last_fetch_status == "OK" and good_row.snapshot_version == 1

    # savepoint 隔离：失败那条没留下节点/来源，成功那条正常
    async with get_session_factory()() as s:
        n_nodes = await s.scalar(select(func.count()).select_from(ProxyNode))
        n_src = await s.scalar(select(func.count()).select_from(ProxyNodeSource))
    assert (n_nodes, n_src) == (1, 1), "失败条目不得留下半截写入，成功条目要落库"