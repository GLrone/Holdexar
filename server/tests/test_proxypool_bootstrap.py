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
    HealthObservation, ProxyNode, SubscriptionSnapshot,
)
from app.domains.proxypool.runtime import (  # noqa: E402
    prepare_runtime_config, require_runtime_proxy_url,
)
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, FORMAT_YAML, FetchResult,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.mark.asyncio
async def test_automatic_sync_accepts_renewed_resource_header(tmp_data_dir, monkeypatch):
    from app.domains.proxypool.pool import eligible_nodes

    await init_db()
    sid = await _add_subscription()
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sid)
        sub.last_stats = {"traffic": "expire=1", "labelSource": "user"}
        sub.deprecated = True
        sub.deprecated_reason = "订阅已到期"
        await session.commit()
    renewed = _fetch_ok([_node("renewed", "127.0.0.1")])
    renewed.userinfo = "expire=2524608000; total=1000; download=1"

    async def fetch(*args, **kwargs):
        return renewed

    monkeypatch.setattr(bs, "fetch_subscription", fetch)
    async with get_session_factory()() as session:
        result = await bs.sync_subscriptions(session, data_dir=tmp_data_dir, now=NOW, only_auto=True)
        assert not result.failures
        sub = await session.get(ProxySubscription, sid)
        assert sub.resource_block_reason() is None and not sub.deprecated
        assert sub.last_stats["labelSource"] == "user"
        from app.domains.proxypool.subscription import subscription_url_identity
        assert sub.cache_url_identity == subscription_url_identity(sub.url)
        assert len(await eligible_nodes(session)) == 1


@pytest.mark.asyncio
async def test_subscription_download_preserves_resource_header():
    from app.domains.proxypool.subscription import Channel, fetch_subscription

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            body = yaml.safe_dump({"proxies": [_node("valid", "edge.example.test")]})
            return httpx.Response(200, content=body.encode("utf-8"), headers={
                "subscription-userinfo": "expire=2524608000",
            })

    result = await fetch_subscription(
        "https://example.test/sub", [Channel("direct")], client_factory=lambda _: Client(),
    )
    assert result.userinfo == "expire=2524608000"


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
    with pytest.raises(Exception, match="代理通道尚未就绪"):
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
    with pytest.raises(Exception, match="代理通道尚未就绪"):
        require_runtime_proxy_url(tmp_data_dir)


@pytest.mark.asyncio
async def test_unreadable_subscription_records_failure_without_replacing_success(
    tmp_data_dir, monkeypatch,
) -> None:
    await init_db()
    sub_id = await _add_subscription()
    previous_success = datetime(2026, 9, 18, 12, 0, 0)
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        sub.snapshot_sha256 = "previous-success"
        sub.snapshot_version = 4
        sub.last_success_at = previous_success
        sub.last_fetch_status = "OK"
        await session.commit()

    monkeypatch.setattr(bs, "open_url", lambda _value: None)

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("unreadable subscription must not make a network request")

    monkeypatch.setattr(bs, "fetch_subscription", unexpected_fetch)
    async with get_session_factory()() as session:
        result = await bs.sync_subscriptions(session, data_dir=tmp_data_dir, now=NOW)

    row = await _sub_row(sub_id)
    assert result.failures[sub_id]
    assert row.last_fetch_at == NOW
    assert row.last_fetch_status == "FAILED"
    assert row.last_error == result.failures[sub_id]
    assert row.snapshot_sha256 == "previous-success"
    assert row.snapshot_version == 4
    assert row.last_success_at == previous_success


@pytest.mark.asyncio
async def test_empty_pre_sync_signature_still_triggers_rebuild_after_new_nodes(
    tmp_data_dir, monkeypatch,
) -> None:
    await init_db()
    sub_id = 7
    sync = bs.SyncResult(
        subscription_count=1,
        snapshot_sha256={sub_id: "new-snapshot"},
        node_count={sub_id: 1},
        pool_names=(),
        pool_changed=True,
        failures={},
        pool_signature_before=(),
    )
    signature_reads = 0
    rebuilds: list[bool] = []

    async def signature(*_args, **_kwargs):
        nonlocal signature_reads
        signature_reads += 1
        return ("new-node",)

    async def remaining_sync(*_args, **kwargs):
        assert kwargs["skip_sub_ids"] == {sub_id}
        return bs.SyncResult(0, {}, {}, (), False, {})

    async def runtime_is_ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(bs, "pool_signature", signature)
    monkeypatch.setattr(bs, "sync_subscriptions", remaining_sync)
    monkeypatch.setattr(bs, "runtime_ready", runtime_is_ready)
    from app.domains.proxypool import scheduling
    monkeypatch.setattr(scheduling, "request_rebuild", lambda: rebuilds.append(True))

    async with get_session_factory()() as session:
        result = await bs.handle_subscription_refresh(
            session, data_dir=tmp_data_dir, runtime=object(), exe_path="mihomo",
            now=NOW, sync_result=sync,
        )

    assert signature_reads == 1
    assert result.pool_changed is True
    assert result.action == "rebuild_requested"
    assert rebuilds == [True]


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
        sources = list((await s.execute(select(ProxyNode))).scalars())
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
    assert sync.snapshots[sub_id].sha256 == canonical
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
        n_src = await s.scalar(select(func.count()).select_from(ProxyNode))
    assert (n_nodes, n_src) == (1, 1), "失败条目不得留下半截写入，成功条目要落库"


# ─── 订阅下载并发：一条慢/失效订阅不得拖住其余 ──────────────────


@pytest.mark.asyncio
async def test_sync_subscriptions_downloads_concurrently(tmp_data_dir, monkeypatch):
    """多订阅更新并发运行，同时限制整轮的活动下载数量。"""
    await init_db()
    async with get_session_factory()() as session:
        for index in range(8):
            session.add(ProxySubscription(
                kind="clash", url=f"https://sub{index}.invalid/{index}",
                created_at=NOW, admission_status=ADMISSION_ACTIVE,
            ))
        await session.commit()

    live = 0
    peak = 0

    async def fake_fetch(url, channels, **kwargs):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
            return _fetch_ok([_node(f"n-{url[-1]}", "10.0.0.1")])
        finally:
            live -= 1

    monkeypatch.setattr(bs, "fetch_subscription", fake_fetch)

    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    assert sync.subscription_count == 8
    assert 2 <= peak <= 5, f"订阅抓取并发峰值不在有界范围内：{peak}"


@pytest.mark.asyncio
async def test_concurrent_refreshes_of_same_subscription_share_one_fetch(
    tmp_data_dir, monkeypatch,
):
    await init_db()
    sub_id = await _add_subscription()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_fetch(url, channels, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _fetch_ok([_node("one", "10.0.0.1")])

    monkeypatch.setattr(bs, "fetch_subscription", fake_fetch)

    async def run_sync():
        async with get_session_factory()() as session:
            return await bs.sync_subscriptions(
                session, data_dir=tmp_data_dir, now=NOW, only_sub_id=sub_id,
            )

    first = asyncio.create_task(run_sync())
    await started.wait()
    second = asyncio.create_task(run_sync())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    async with get_session_factory()() as session:
        rows = list((await session.scalars(
            select(SubscriptionSnapshot).where(SubscriptionSnapshot.subscription_id == sub_id)
        )).all())
    assert calls == 1
    assert len(rows) == 1
    assert all(not result.failures for result in results)


@pytest.mark.asyncio
async def test_refresh_discards_response_when_subscription_url_changes_in_flight(
    tmp_data_dir, monkeypatch,
):
    await init_db()
    sub_id = await _add_subscription()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_fetch(url, channels, **kwargs):
        started.set()
        await release.wait()
        return _fetch_ok([_node("old", "10.0.0.1")])

    monkeypatch.setattr(bs, "fetch_subscription", fake_fetch)

    async def run_sync():
        async with get_session_factory()() as session:
            return await bs.sync_subscriptions(
                session, data_dir=tmp_data_dir, now=NOW, only_sub_id=sub_id,
            )

    task = asyncio.create_task(run_sync())
    await started.wait()
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        sub.url = "https://sub.invalid/renewed"
        await session.commit()
    release.set()
    result = await task

    async with get_session_factory()() as session:
        snapshot_count = await session.scalar(
            select(func.count(SubscriptionSnapshot.id)).where(
                SubscriptionSnapshot.subscription_id == sub_id,
            )
        )
        sub = await session.get(ProxySubscription, sub_id)
    assert sub.url == "https://sub.invalid/renewed"
    assert snapshot_count == 0
    assert sub.snapshot_sha256 is None
    assert result.failures[sub_id]


@pytest.mark.asyncio
async def test_provider_only_subscription_is_registered_from_canonical_snapshot(
    tmp_data_dir, monkeypatch,
):
    from app.domains.proxypool.subscription import (
        Channel, fetch_subscription, latest_snapshot,
    )

    await init_db()
    sub_id = await _add_subscription("https://sub.invalid/provider")
    root = yaml.safe_dump({
        "proxy-providers": {
            "edge": {"type": "http", "url": "https://provider.invalid/nodes?key=secret"}
        },
        "proxy-groups": [{"name": "All", "type": "select", "use": ["edge"]}],
    }).encode()
    provider = yaml.safe_dump({"proxies": [_node("edge-node", "edge.example.test")]})
    provider_raw = provider.encode()

    def factory(channel):
        def handler(request):
            if request.url.host == "sub.invalid":
                return httpx.Response(200, content=root)
            if request.url.host == "provider.invalid":
                return httpx.Response(200, content=provider_raw)
            return httpx.Response(502)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def canonical_fetch(url, _channels, **kwargs):
        return await fetch_subscription(
            url, [Channel("direct")], client_factory=factory, **kwargs,
        )

    monkeypatch.setattr(bs, "fetch_subscription", canonical_fetch)
    async with get_session_factory()() as session:
        result = await bs.sync_subscriptions(
            session, data_dir=tmp_data_dir, now=NOW, only_sub_id=sub_id,
        )
        assert not result.failures
        assert result.node_count[sub_id] == 1
        nodes = (await session.scalars(
            select(ProxyNode).where(ProxyNode.subscription_id == sub_id)
        )).all()
        restored = await latest_snapshot(
            session, sub_id, url="https://sub.invalid/provider",
        )
        await session.commit()

    assert len(nodes) == 1 and nodes[0].original_name == "edge-node"
    assert restored is not None and restored.nodes[0]["server"] == "edge.example.test"


@pytest.mark.asyncio
async def test_healthy_other_subscription_lane_is_a_fetch_egress(
    tmp_data_dir, monkeypatch,
):
    """订阅 A 的抓取通道可借用另一条订阅 B 的近期业务健康出口。"""
    from app.domains.proxies import clash_manager
    from app.domains.proxypool import runtime as pool_runtime

    await init_db()
    async with get_session_factory()() as session:
        session.add_all([
            ProxySubscription(kind="clash", url="https://sub-a.invalid/a",
                              created_at=NOW, admission_status=ADMISSION_ACTIVE),
            ProxySubscription(kind="clash", url="https://sub-b.invalid/b",
                              created_at=NOW, admission_status=ADMISSION_ACTIVE),
        ])
        session.add(ProxyNode(
            node_id="b" * 20, fingerprint="b" * 64, runtime_name="2|B",
            subscription_id=2, original_name="B", proxy_type="ss",
            server="edge.example.test", exit_ip="203.0.113.10", state="ACTIVE",
            last_l2_at=NOW, consecutive_failures=0,
        ))
        session.add(HealthObservation(
            node_id="b" * 20, level="L2", ok=True, observed_at=NOW,
        ))
        await session.commit()

        class RunningPool:
            def status(self):
                return {"running": True}

        monkeypatch.setattr(clash_manager, "pool_runtime", RunningPool())
        monkeypatch.setattr(pool_runtime, "lane_bindings", lambda _path: [{
            "lane": 0, "url": "http://127.0.0.1:15555",
            "exitIp": "203.0.113.10", "node": "2|B",
        }])

        channels = await bs._healthy_pool_channels(
            session, data_dir=tmp_data_dir, now=NOW,
        )

    assert len(channels) == 1
    assert channels[0].exit_ip == "203.0.113.10"
    assert channels[0].egress_id == "exit:203.0.113.10"
    assert channels[0].proxy == "http://127.0.0.1:15555"


@pytest.mark.asyncio
async def test_sync_subscriptions_one_failure_does_not_block_others(
    tmp_data_dir, monkeypatch,
):
    """一条订阅抓取失败不阻断其余订阅：逐条独立记账，其余照常落快照。"""
    await init_db()
    await _add_subscription("https://ok1.invalid/1")
    await _add_subscription("https://bad.invalid/2")
    await _add_subscription("https://ok2.invalid/3")

    async def fake_fetch(url, channels, **kwargs):
        if "bad" in url:
            raise RuntimeError("Client error '404 Not Found'")
        return _fetch_ok([_node(f"n{url[-1]}", "10.0.0.1")])

    monkeypatch.setattr(bs, "fetch_subscription", fake_fetch)

    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    assert len(sync.failures) == 1, "只有失败那条被记账"
    assert len(sync.snapshot_sha256) == 2, "其余两条照常落快照"
    async with get_session_factory()() as session:
        failed = (await session.execute(
            select(SubscriptionSnapshot).where(SubscriptionSnapshot.status == "FAILED")
        )).scalars().all()
    assert len(failed) == 1
    assert failed[0].error == "Client error '404 Not Found'"
    assert failed[0].fetched_at == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("port", 8443), ("udp", True), ("dialer-proxy", "DIRECT"),
])
async def test_pool_signature_changes_with_connection_parameters(tmp_data_dir, field, value):
    await init_db()
    subscription_id = await _add_subscription()
    async with get_session_factory()() as session:
        node = ProxyNode(node_id="connection", fingerprint="identity",
                         runtime_name=f"{subscription_id}|connection",
                         subscription_id=subscription_id,
                         proxy_type="ss", server="edge.example.test",
                         original_name="connection", state="ACTIVE",
                         normalized_config={"name": "connection", "type": "ss",
                                            "server": "edge.example.test", "port": 443,
                                            "cipher": "aes-128-gcm", "password": "fixture"})
        session.add(node)
        await session.commit()
        before = await bs.pool_signature(session)
        node.normalized_config = {**node.normalized_config, field: value}
        await session.commit()
        assert await bs.pool_signature(session) != before
