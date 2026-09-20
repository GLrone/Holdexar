"""订阅级生产准入（Active/Candidate 第一切片）行为测试。

本文件验的是**准入**这一层，不是身份/来源语义（后者在 `test_proxypool_registry.py`）：

1. 新订阅走产品入口（`proxies_service.add_subscription`）默认是 **CANDIDATE**；
2. CANDIDATE 同步只落快照：不产生 ProxyNode / ProxyNodeSource、不进 eligible、
   不改 `pool_signature`、不置 rebuild、不落池文件；
3. 晋升**没有成功快照**时失败（无快照 / 只有失败快照 / 快照字节丢失三种）；
4. 晋升成功时 `ACTIVE + Registry 来源`落在**同一事务**里（回滚可整体撤销）；
5. 晋升后重复同步幂等（不新建节点、不重铸 `runtime_name`）；
6. 已有 ACTIVE 订阅行为与切片前一致；两者混存时只有 ACTIVE 进生产。

节点配置是**测试夹具**（小规模合成），不是生产数据；真实订阅字节的回放验收在
隔离环境脚本里做，见交接文档 §15/§16。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings  # noqa: E402
from app.core.database import (  # noqa: E402
    get_engine,
    get_session_factory,
    init_db,
)
from app.domains.proxies import service as proxies_service  # noqa: E402
from app.domains.proxies.models import (  # noqa: E402
    ADMISSION_ACTIVE,
    ADMISSION_CANDIDATE,
    ProxySubscription,
)
from app.domains.proxypool import bootstrap as bs  # noqa: E402
from app.domains.proxypool import scheduling  # noqa: E402
from app.domains.proxypool.admission import (  # noqa: E402
    PromotionError,
    promote_to_active,
)
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode,
    ProxyNodeSource,
    SubscriptionSnapshot,
    node_fingerprint,
)
from app.domains.proxypool.pool import pool_path  # noqa: E402
from app.domains.proxypool.subscription import (  # noqa: E402
    FetchAttempt,
    FetchResult,
    detect_format,
)

import yaml  # noqa: E402

from datetime import datetime  # noqa: E402

NOW = datetime(2026, 9, 21, 10, 0, 0)


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


@pytest.fixture(autouse=True)
def _reset_rebuild_pending():
    """rebuild_pending 是模块级信号：跑前跑后都清干净，避免跨用例串味。"""
    scheduling.take_rebuild_pending()
    yield
    scheduling.take_rebuild_pending()


def _node(name: str, server: str) -> dict:
    return {"name": name, "type": "http", "server": server, "port": 1}


def _fetch_result(nodes: list[dict]) -> FetchResult:
    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    return FetchResult(
        raw=raw, http_status=200, content_type="text/yaml", channel="test",
        fmt=detect_format(raw),
        attempts=[FetchAttempt("test", True, 200, "yaml")],
    )


def _script_fetch(nodes: list[dict], monkeypatch) -> None:
    async def _fetch_stub(url, channels, **kw):
        return _fetch_result(nodes)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_stub)


async def _add_row(*, url: str, status: str, kind: str = "clash") -> int:
    async with get_session_factory()() as s:
        sub = ProxySubscription(
            kind=kind, url=url, created_at=NOW, admission_status=status
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _counts() -> tuple[int, int, int]:
    async with get_session_factory()() as s:
        nodes = int(await s.scalar(select(func.count()).select_from(ProxyNode)))
        srcs = int(await s.scalar(select(func.count()).select_from(ProxyNodeSource)))
        snaps = int(
            await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))
        )
    return nodes, srcs, snaps


async def _status(sub_id: int) -> str | None:
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        return row.admission_status if row else None


def _fps(nodes: list[dict]) -> set[str]:
    return {node_fingerprint(n) for n in nodes}


async def _source_fps(sub_id: int) -> set[str]:
    async with get_session_factory()() as s:
        rows = await s.execute(
            select(ProxyNode.fingerprint)
            .join(ProxyNodeSource, ProxyNodeSource.node_id == ProxyNode.node_id)
            .where(ProxyNodeSource.subscription_id == sub_id)
        )
    return {r[0] for r in rows.all()}


async def _sync(data_dir) -> None:
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=data_dir, now=NOW)
        await s.commit()


# ══ 1. 新订阅走产品入口默认 CANDIDATE ══════════════════════════════
@pytest.mark.asyncio
async def test_add_subscription_defaults_to_candidate(tmp_data_dir, monkeypatch):
    """产品新增订阅的真实调用路径 = `proxies_service.add_subscription`（唯一调用方是
    `POST /proxies/subscriptions`）：默认进 **CANDIDATE**，不无条件沿用 ACTIVE。"""
    await init_db()

    async def fake_download(sub_url, data_dir, proxy_url=None):
        return {"path": "/tmp/x.yaml", "title": "某机场", "userinfo": None,
                "nodes": 3, "cached": False}

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service.clash_manager, "detect_kernel",
                        lambda d: {"found": True})

    out = await proxies_service.add_subscription("clash", "https://new.invalid/x", None)
    assert await _status(out["id"]) == ADMISSION_CANDIDATE

    # 列表出口把它呈现出来（NULL 兼容成 ACTIVE，新行不是 NULL）
    listed = {s["id"]: s for s in await proxies_service.list_subscriptions("clash")}
    assert listed[out["id"]]["admissionStatus"] == ADMISSION_CANDIDATE


@pytest.mark.asyncio
async def test_promote_refuses_snapshot_from_previous_url(tmp_data_dir, monkeypatch):
    """订阅换链接后**不得**用旧链接的成功快照晋升。

    场景：A) 候选 URL=A → B) 成功抓取并落 Snapshot(A) → C) 不再重新抓取 →
    D) 把同一个订阅的 URL 改成 B → E) 晋升。

    禁止态：`subscription.url = B` 而 Registry 来自 `Snapshot(A)`。
    既有语义裁定为「**拒绝晋升，要求重新成功抓取当前 URL**」：与 `latest_snapshot`
    的 url 过滤、以及原定的"不用失败快照/旧缓存替代"一致（换链接等于换机场，
    见 `update_subscription` 的注释）。
    """
    await init_db()
    sub_id = await _add_row(url="https://a.invalid/x", status=ADMISSION_CANDIDATE)
    nodes_a = [_node("a1", "10.0.0.1")]

    async def _fetch_a(url, channels, **kw):
        return _fetch_result(nodes_a)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_a)
    await _sync(tmp_data_dir)  # Snapshot(A) 落盘（url=A）

    # 换链接，但**不重新抓取**（真实世界里"改 URL 后抓取失败/还没到下一轮"）
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        row.url = "https://b.invalid/x"
        await s.commit()

    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="当前订阅 URL 还没有成功快照"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert await _source_fps(sub_id) == set(), "旧快照的节点绝不能进 Registry"

    # 抓到 B 之后才允许晋升，且 Registry 必须是 **B** 的节点
    nodes_b = [_node("b1", "10.0.0.2")]

    async def _fetch_b(url, channels, **kw):
        assert str(url) == "https://b.invalid/x", "必须抓当前 URL"
        return _fetch_result(nodes_b)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_b)
    await _sync(tmp_data_dir)
    async with get_session_factory()() as s:
        res = await promote_to_active(
            s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
    assert res.promoted is True
    assert await _source_fps(sub_id) == _fps(nodes_b)
    assert await _status(sub_id) == ADMISSION_ACTIVE


# ══ 1b. plain 没有 Candidate admission 生命周期 ════════════════════
@pytest.mark.asyncio
async def test_plain_subscription_is_active_not_candidate(tmp_data_dir):
    """plain（明文代理订阅）直接进旧手工代理池，不参与 proxypool admission：
    新 plain 行必须是 **ACTIVE**；即便库里残留被早期写成 CANDIDATE 的 plain 行，
    列表出口也归正为 ACTIVE（不需要数据迁移）。"""
    await init_db()
    out = await proxies_service.add_subscription(
        "plain", "https://plain.example/list.txt", "明文来源"
    )
    assert await _status(out["id"]) == ADMISSION_ACTIVE
    listed = {s["id"]: s for s in await proxies_service.list_subscriptions()}
    assert listed[out["id"]]["admissionStatus"] == ADMISSION_ACTIVE

    # 历史残留：手工把 plain 行写成 CANDIDATE，出口仍呈现 ACTIVE
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, out["id"])
        row.admission_status = ADMISSION_CANDIDATE
        await s.commit()
    listed2 = {s["id"]: s for s in await proxies_service.list_subscriptions()}
    assert listed2[out["id"]]["admissionStatus"] == ADMISSION_ACTIVE
    # clash 行不受这条归正影响
    clash = await _add_row(url="https://clash.example/x", status=ADMISSION_CANDIDATE)
    listed3 = {s["id"]: s for s in await proxies_service.list_subscriptions()}
    assert listed3[clash]["admissionStatus"] == ADMISSION_CANDIDATE


# ══ 2. CANDIDATE 同步：只落快照，不产生任何生产事实 ════════════════
@pytest.mark.asyncio
async def test_candidate_sync_keeps_snapshot_out_of_registry(tmp_data_dir, monkeypatch):
    await init_db()
    sub_id = await _add_row(url="https://cand.invalid/x", status=ADMISSION_CANDIDATE)
    _script_fetch([_node("n1", "10.0.0.1"), _node("n2", "10.0.0.2")], monkeypatch)

    async with get_session_factory()() as s:
        first = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert first.skipped_candidates == (sub_id,)
    assert first.pool_names == ()
    assert first.pool_changed is False
    assert sub_id in first.snapshot_sha256, "候选仍然抓取并留证据（sha 有它）"

    nodes, srcs, snaps = await _counts()
    assert (nodes, srcs, snaps) == (0, 0, 1), "候选只该留快照行，Registry 一行不建"

    # 签名/池/rebuild 全部无感
    async with get_session_factory()() as s:
        assert await bs.pool_signature(s) == ()
    assert scheduling.rebuild_pending() is False
    assert pool_path(tmp_data_dir).is_file() is False, "候选不该产生池文件"

    # 再刷一次（内容变了）仍不进 Registry，但快照继续追加
    _script_fetch([_node("n1", "10.0.0.1"), _node("n3", "10.0.0.3")], monkeypatch)
    async with get_session_factory()() as s:
        second = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert second.skipped_candidates == (sub_id,)
    nodes2, srcs2, snaps2 = await _counts()
    assert (nodes2, srcs2, snaps2) == (0, 0, 2)
    assert scheduling.rebuild_pending() is False


# ══ 3. 晋升没有成功快照 → 失败（三种） ═════════════════════════════
@pytest.mark.asyncio
async def test_promote_without_snapshot_fails(tmp_data_dir):
    await init_db()
    sub_id = await _add_row(url="https://nosnap.invalid/x", status=ADMISSION_CANDIDATE)
    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="没有成功快照"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert (await _counts())[:2] == (0, 0)


@pytest.mark.asyncio
async def test_promote_ignores_failed_snapshot(tmp_data_dir):
    """只有失败快照行 → 视为没有成功快照（不许拿失败快照顶替）。"""
    await init_db()
    sub_id = await _add_row(url="https://fail.invalid/x", status=ADMISSION_CANDIDATE)
    async with get_session_factory()() as s:
        s.add(SubscriptionSnapshot(
            subscription_id=sub_id, sha256="f" * 64, format="yaml", node_count=0,
            status="FAILED", fetched_at=NOW,
        ))
        await s.commit()
    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="没有成功快照"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert (await _counts())[:2] == (0, 0)


@pytest.mark.asyncio
async def test_promote_fails_when_snapshot_bytes_lost(tmp_data_dir, monkeypatch):
    """成功快照行在、但原始字节丢了 → fail-closed（不猜、不重抓）。"""
    await init_db()
    sub_id = await _add_row(url="https://lost.invalid/x", status=ADMISSION_CANDIDATE)
    _script_fetch([_node("n1", "10.0.0.1")], monkeypatch)
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    # 删掉落盘字节，模拟字节丢失
    for p in (tmp_data_dir / "proxypool" / "snapshots").rglob("*.bin"):
        p.unlink()
    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="不可恢复"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert (await _counts())[:2] == (0, 0)


# ══ 4. 晋升成功：ACTIVE + 来源同事务 ═══════════════════════════════
@pytest.mark.asyncio
async def test_promotion_is_atomic_with_registry_apply(tmp_data_dir, monkeypatch):
    await init_db()
    sub_id = await _add_row(url="https://ok.invalid/x", status=ADMISSION_CANDIDATE)
    _script_fetch([_node("n1", "10.0.0.1"), _node("n2", "10.0.0.2")], monkeypatch)
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    # ① 只 flush 不 commit：此刻同事务内两边都成立
    async with get_session_factory()() as s:
        result = await promote_to_active(
            s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
        )
        assert result.promoted is True
        assert result.applied_nodes == 2
        assert result.snapshot_sha256
        row = await s.get(ProxySubscription, sub_id)
        assert row.admission_status == ADMISSION_ACTIVE
        assert int(await s.scalar(select(func.count()).select_from(ProxyNode))) == 2
        assert int(await s.scalar(
            select(func.count()).select_from(ProxyNodeSource))) == 2
        # ② 回滚 → 状态与来源**一起**撤销（证明两个动作在同一事务里）
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert (await _counts())[:2] == (0, 0)

    # ③ 正常提交一次 → 两边同时成立；再晋升是幂等空转
    async with get_session_factory()() as s:
        result = await promote_to_active(
            s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
        assert result.promoted is True
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert (await _counts())[:2] == (2, 2)
    async with get_session_factory()() as s:
        again = await promote_to_active(
            s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
    assert again.promoted is False and again.applied_nodes == 0


@pytest.mark.asyncio
async def test_promotion_requests_rebuild_only_when_eligible_set_changes(
    tmp_data_dir, monkeypatch
):
    """晋升带来的 eligible 集合变化 → 由**既有** `request_rebuild()` 置信号；
    只给已有节点加一个来源（集合没变）→ **不置**信号（与 §15.3 情况 1 一致）。"""
    await init_db()
    a = await _add_row(url="https://m1.invalid/x", status=ADMISSION_ACTIVE)
    # B 与 A 提供**同一个节点**（同配置 → 同指纹）
    same = [_node("shared", "10.0.0.9")]
    b_same = await _add_row(url="https://m2.invalid/x", status=ADMISSION_CANDIDATE)
    # C 提供 A **没有**的节点 → 晋升会改变 eligible 集合
    c_new = await _add_row(url="https://m3.invalid/x", status=ADMISSION_CANDIDATE)

    payload = {
        "https://m1.invalid/x": same,
        "https://m2.invalid/x": same,
        "https://m3.invalid/x": [_node("extra", "10.0.0.10")],
    }

    async def _fetch_three(url, channels, **kw):
        return _fetch_result(payload[str(url)])

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_three)
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert (await _counts())[:2] == (1, 1), "只有 A 进 Registry"

    # ① 只加来源：eligible 集合不变 → 不置 rebuild
    scheduling.take_rebuild_pending()
    async with get_session_factory()() as s:
        await promote_to_active(
            s, subscription_id=b_same, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
    assert scheduling.rebuild_pending() is False
    assert (await _counts())[:2] == (1, 2)
    async with get_session_factory()() as s:
        names = {
            r.runtime_name for r in (await s.execute(select(ProxyNode))).scalars()
        }
    assert {n.split("|")[0] for n in names} == {str(a)}, "同来源穷举：名字仍由 A 铸造"

    # ② 新节点进入 eligible → 置 rebuild（由既有重建链消费）
    async with get_session_factory()() as s:
        await promote_to_active(
            s, subscription_id=c_new, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
    assert scheduling.rebuild_pending() is True
    assert (await _counts())[:2] == (2, 3)
    scheduling.take_rebuild_pending()


@pytest.mark.asyncio
async def test_promotion_rejects_non_clash_subscription(tmp_data_dir):
    await init_db()
    sub_id = await _add_row(
        url="http://plain.invalid/x", status=ADMISSION_CANDIDATE, kind="plain"
    )
    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="不是 clash 订阅"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
        await s.rollback()


# ══ 5. 晋升后重复同步幂等 ═════════════════════════════════════════
@pytest.mark.asyncio
async def test_resync_after_promotion_is_idempotent(tmp_data_dir, monkeypatch):
    await init_db()
    sub_id = await _add_row(url="https://idem.invalid/x", status=ADMISSION_CANDIDATE)
    _script_fetch([_node("n1", "10.0.0.1"), _node("n2", "10.0.0.2")], monkeypatch)
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    async with get_session_factory()() as s:
        await promote_to_active(
            s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()

    async with get_session_factory()() as s:
        before = {
            r.runtime_name: r.node_id
            for r in (await s.execute(select(ProxyNode))).scalars()
        }
        resync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
    assert resync.skipped_candidates == ()
    assert resync.pool_names == tuple(before)
    nodes, srcs, snaps = await _counts()
    assert (nodes, srcs) == (2, 2), "重复同步不新建节点/来源"
    assert snaps == 2, "快照照常追加（这次不是候选了，走完整链路）"
    async with get_session_factory()() as s:
        after = {
            r.runtime_name: r.node_id
            for r in (await s.execute(select(ProxyNode))).scalars()
        }
    assert after == before, "runtime_name 不重铸"


# ══ 6. 已有 ACTIVE 行为不变 + 混存时只有 ACTIVE 进生产 ══════════════
@pytest.mark.asyncio
async def test_active_subscription_behaviour_unchanged(tmp_data_dir, monkeypatch):
    await init_db()
    sub_id = await _add_row(url="https://act.invalid/x", status=ADMISSION_ACTIVE)
    _script_fetch([_node("n1", "10.0.0.1"), _node("n2", "10.0.0.2")], monkeypatch)
    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
        sig = await bs.pool_signature(s)
    assert sync.skipped_candidates == ()
    assert sync.pool_changed is True
    assert len(sync.pool_names) == 2 and len(sig) == 2
    assert (await _counts())[:2] == (2, 2)


@pytest.mark.asyncio
async def test_mixed_active_and_candidate_only_active_in_production(
    tmp_data_dir, monkeypatch
):
    """A=ACTIVE + B=CANDIDATE：只有 A 进 Registry/eligible/签名；B 只有快照。"""
    await init_db()
    a = await _add_row(url="https://a.invalid/x", status=ADMISSION_ACTIVE)
    b = await _add_row(url="https://b.invalid/x", status=ADMISSION_CANDIDATE)
    nodes_by_url = {
        "https://a.invalid/x": [_node("a1", "10.0.0.1")],
        "https://b.invalid/x": [_node("b1", "10.0.0.2")],
    }

    async def _fetch_mixed(url, channels, **kw):
        return _fetch_result(nodes_by_url[str(url)])

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_mixed)
    async with get_session_factory()() as s:
        sync = await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()
        sig = await bs.pool_signature(s)
        names = {
            r.runtime_name
            for r in (await s.execute(select(ProxyNode))).scalars()
        }
    assert sync.skipped_candidates == (b,)
    assert len(names) == 1 and all(n.startswith(f"{a}|") for n in names)
    assert len(sig) == 1
    nodes, srcs, snaps = await _counts()
    assert (nodes, srcs, snaps) == (1, 1, 2), "两条都留快照，只有 A 建 Registry"

    # B 晋升后两条都在生产里（同一个节点名空间：各自 runtime_name 前缀）
    async with get_session_factory()() as s:
        result = await promote_to_active(
            s, subscription_id=b, data_dir=tmp_data_dir, now=NOW
        )
        await s.commit()
    assert result.promoted is True
    assert (await _counts())[:2] == (2, 2)
    async with get_session_factory()() as s:
        names2 = {
            r.runtime_name
            for r in (await s.execute(select(ProxyNode))).scalars()
        }
    assert {n.split("|")[0] for n in names2} == {str(a), str(b)}