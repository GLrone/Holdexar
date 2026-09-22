"""准入切片 · 并发/交错一致性验证（**不是性能测试**）。

要证明的只有一件事：在真实 `sync_subscriptions` 与 `promote_to_active` 交错时，
不会出现

  - `admission_status = ACTIVE` 而 Registry 里**没有**该订阅的来源；
  - Registry 反映的是"不是合法成功快照"的版本；
  - latest successful snapshot 与 Registry 事实**无法解释**。

两个交错（用真实的模块级函数，只在抓取层/快照读取点上做确定性卡点）：

  A. sync(candidate) 开始 → promote → sync 完成
     （sync 的会话在 promote 提交前就加载了订阅行 → 本轮仍视其为 CANDIDATE → 跳过 apply）
  B. promote 读到旧 Snapshot → 新的成功 Snapshot 落库 → promote 提交

锁与环境事实（`app/core/database.py`）：`journal_mode=WAL` + `busy_timeout=60000`
+ `foreign_keys=ON`，会话 `expire_on_commit=False`。写事务串行化；读不阻塞写。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings  # noqa: E402
from app.core.database import (  # noqa: E402
    get_engine,
    get_session_factory,
    init_db,
)
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
from app.domains.proxypool.registry import apply_snapshot  # noqa: E402
from app.domains.proxypool.subscription import (  # noqa: E402
    FetchAttempt,
    FetchResult,
    build_snapshot,
    detect_format,
    latest_snapshot,
)

NOW = datetime(2026, 9, 21, 12, 0, 0)


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
    scheduling.take_rebuild_pending()
    yield
    scheduling.take_rebuild_pending()


def _node(name: str, server: str) -> dict:
    return {"name": name, "type": "http", "server": server, "port": 1}


def _fps(nodes: list[dict]) -> set[str]:
    return {node_fingerprint(n) for n in nodes}


def _fetch_result(nodes: list[dict]) -> FetchResult:
    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    return FetchResult(
        raw=raw, http_status=200, content_type="text/yaml", channel="test",
        fmt=detect_format(raw), attempts=[FetchAttempt("test", True, 200, "yaml")],
    )


async def _add_candidate(url: str) -> int:
    async with get_session_factory()() as s:
        sub = ProxySubscription(
            kind="clash", url=url, created_at=NOW,
            admission_status=ADMISSION_CANDIDATE,
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _sync(data_dir) -> None:
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=data_dir, now=NOW)
        await s.commit()


async def _sync_at(data_dir, when) -> None:
    """按指定时间戳跑一轮同步（用来构造"更新的成功快照"）。"""
    async with get_session_factory()() as s:
        await bs.sync_subscriptions(s, data_dir=data_dir, now=when)
        await s.commit()


async def _promote(sub_id: int, data_dir) -> None:
    async with get_session_factory()() as s:
        await promote_to_active(
            s, subscription_id=sub_id, data_dir=data_dir, now=NOW
        )
        await s.commit()


async def _status(sub_id: int) -> str | None:
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        return row.admission_status if row else None


async def _source_fps(sub_id: int) -> set[str]:
    async with get_session_factory()() as s:
        rows = await s.execute(
            select(ProxyNode.fingerprint)
            .join(ProxyNodeSource, ProxyNodeSource.node_id == ProxyNode.node_id)
            .where(ProxyNodeSource.subscription_id == sub_id)
        )
    return {r[0] for r in rows.all()}


async def _counts() -> tuple[int, int, int]:
    async with get_session_factory()() as s:
        return (
            int(await s.scalar(select(func.count()).select_from(ProxyNode))),
            int(await s.scalar(select(func.count()).select_from(ProxyNodeSource))),
            int(await s.scalar(select(func.count()).select_from(SubscriptionSnapshot))),
        )


async def _apply_snapshot_nodes(sub_id: int, nodes: list[dict], data_dir) -> None:
    """用给定节点集合直接 apply 一份快照（等价于"这一轮同步抓到的内容"）。"""
    import hashlib

    from app.domains.proxypool.subscription import Snapshot

    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    snap = Snapshot(
        subscription_id=sub_id, url="", fetched_at=NOW, http_status=200,
        content_type="text/yaml", sha256=hashlib.sha256(raw).hexdigest(),
        raw_content=raw, fmt="yaml", node_count=len(nodes),
        source_channel="test", nodes=nodes,
    )
    async with get_session_factory()() as s:
        await apply_snapshot(s, subscription_id=sub_id, snapshot=snap, now=NOW)
        await s.commit()


async def _latest_ok_sha(sub_id: int) -> str | None:
    async with get_session_factory()() as s:
        return await s.scalar(
            select(SubscriptionSnapshot.sha256)
            .where(SubscriptionSnapshot.subscription_id == sub_id,
                   SubscriptionSnapshot.status == "OK")
            .order_by(SubscriptionSnapshot.id.desc())
            .limit(1)
        )


async def _persist_nodes(sub_id: int, nodes: list[dict], now: datetime, data_dir):
    """把一份节点集合真实落库（生成快照行 + 内容寻址字节），返回可用于 apply 的 Snapshot。"""
    from app.domains.proxypool.subscription import persist_snapshot

    snap = _snapshot_of(sub_id, nodes, now)
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=data_dir)
        await s.commit()
    return snap


def _snapshot_of(sub_id: int, nodes: list[dict], when: datetime):
    """构造（不落库）一份快照；同一批节点内容 → 同一 sha，可用来找它对应的行。"""
    return build_snapshot(
        sub_id, "https://race-f.invalid/x", _fetch_result(nodes), nodes, now=when
    )


# ══ A. sync(candidate) 抓取中 → promote 提交 ACTIVE → sync 完成 ══════
@pytest.mark.asyncio
async def test_interleave_sync_then_promote(tmp_data_dir, monkeypatch):
    """sync 在抓取中、promote 先提交 ACTIVE，之后 sync 才落盘 S2。

    修好后 sync 的准入判定是**现读**的：它看到刚晋升的 ACTIVE，于是把更新的 S2
    apply 进 Registry（不是"当初是候选"就跳过）→ 最终严格等于 S2，不是并集。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-a.invalid/x")
    s1 = [_node("a1", "10.0.0.1"), _node("a2", "10.0.0.2")]
    s2 = [_node("a1", "10.0.0.1"), _node("a3", "10.0.0.3")]

    gate = asyncio.Event()
    calls = {"n": 0}

    async def _fetch(url, channels, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            await gate.wait()  # 卡在抓取：此时**一个字节都还没写**
        return _fetch_result(s1 if calls["n"] == 1 else s2)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)

    # 第一轮（候选）：S1 快照落盘，Registry 一行不建
    await _sync(tmp_data_dir)
    assert (await _counts())[:2] == (0, 0)

    # 第二轮卡在抓取处
    task = asyncio.create_task(_sync(tmp_data_dir))
    while calls["n"] < 2:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert not task.done(), "第二轮应停在 fetch 里（尚未写库）"

    # promote：取边界 → 读 S1 → apply → ACTIVE → commit
    await _promote(sub_id, tmp_data_dir)
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(s1)

    gate.set()
    await asyncio.wait_for(task, timeout=20)

    # 现读准入：sync 看到 ACTIVE → 落 S2 并 apply → 最终是 S2（最新成功快照）
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(s2), "现读准入后必须落到最新那份"
    assert await _source_fps(sub_id) != _fps(s1) | _fps(s2), "不得是并集"
    nodes, sources, snaps = await _counts()
    # 来源只有 S2 的两条；节点行是身份台账（a2 的来源流失后行仍在，state→DEAD，不删行）
    assert sources == 2
    assert nodes == 3, "a1/a3 + 被撤下来源的 a2（行保留）"
    assert snaps == 2, "两轮的成功快照都在"


# ══ B. promote 先持边界 → sync 等待 → 之后 sync 应用最新 ══════════════
@pytest.mark.asyncio
async def test_promote_boundary_first_sync_waits_then_applies_newest(
    tmp_data_dir, monkeypatch
):
    """Promote 先取得写串行化边界（暂停在边界内、读快照处）→ 并发 Sync 只能等；
    Promote 提交后 Sync 才落盘 S2，并且必须（现读准入）把 S2 应用进 Registry。

    证明不存在"最新成功快照已提交、Registry 仍是旧 S1"的已提交中间态：
    落盘与 apply 在同一事务里，且落盘前同步要重新读准入。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-b.invalid/x")
    s1 = [_node("b1", "10.0.0.1"), _node("b2", "10.0.0.2")]
    s2 = [_node("b1", "10.0.0.1"), _node("b3", "10.0.0.3")]

    async def _fetch_s1(url, channels, **kw):
        return _fetch_result(s1)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_s1)
    await _sync(tmp_data_dir)  # S1 落盘（候选）

    import app.domains.proxypool.admission as adm

    real_latest = adm.latest_snapshot
    in_boundary = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_latest(session, subscription_id, **kw):
        snap = await real_latest(session, subscription_id, **kw)
        in_boundary.set()  # 走到这里时 promote 已持写锁
        await release.wait()
        return snap

    monkeypatch.setattr(adm, "latest_snapshot", _blocking_latest)
    promo = asyncio.create_task(_promote(sub_id, tmp_data_dir))
    await asyncio.wait_for(in_boundary.wait(), 5)

    async def _fetch_s2(url, channels, **kw):
        return _fetch_result(s2)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_s2)
    sync_task = asyncio.create_task(_sync(tmp_data_dir))
    await asyncio.sleep(0.2)
    assert not sync_task.done(), "边界被 promote 持有时，sync 不得落盘"

    release.set()
    await asyncio.wait_for(promo, timeout=20)
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(s1)

    await asyncio.wait_for(sync_task, timeout=20)
    assert await _source_fps(sub_id) == _fps(s2), "sync 必须把最新 S2 应用进 Registry"
    assert await _status(sub_id) == ADMISSION_ACTIVE
    async with get_session_factory()() as s:
        row = await s.get(ProxySubscription, sub_id)
        assert row.snapshot_sha256 == await _latest_ok_sha(sub_id)


# ══ C. 你点名的竞态（场景 A）：同步先落更新的快照 ═══════════════════
@pytest.mark.asyncio
async def test_promote_applies_newest_when_sync_lands_before_boundary(
    tmp_data_dir, monkeypatch
):
    """初始：候选 + Snapshot A + Registry 空。

      T1 Promote 启动 → **暂停在取边界之前**
      T2 Sync 完整跑一轮：落盘更新的 Snapshot B，因仍是候选而跳过 apply
      T1 继续 → 取边界 → 读到的已是最新 B → 必须应用 B 之后才允许 ACTIVE

    修复前：T1 拿着 A 进入 apply → 被"最新快照胜"守卫跳过 → 仍写 ACTIVE
    ⇒ **ACTIVE + Registry 空**。修复后：ACTIVE 时 Registry 已经是 B 的来源。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-c3.invalid/x")
    nodes_a = [_node("i1", "10.0.0.1")]
    nodes_b = [_node("i2", "10.0.0.2")]
    later = datetime(2026, 9, 21, 13, 0, 0)

    async def _fetch_a(url, channels, **kw):
        return _fetch_result(nodes_a)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_a)
    await _sync_at(tmp_data_dir, NOW)  # Snapshot A（候选 → 只落快照）
    assert await _source_fps(sub_id) == set()

    import app.domains.proxypool.admission as adm

    real_lock = adm.lock_subscription_mutation
    started = asyncio.Event()
    go = asyncio.Event()
    calls = {"n": 0}

    async def _blocking_lock(session, subscription_id):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await go.wait()  # 暂停在"取边界之前"
        await real_lock(session, subscription_id)

    monkeypatch.setattr(adm, "lock_subscription_mutation", _blocking_lock)
    task = asyncio.create_task(_promote(sub_id, tmp_data_dir))
    await asyncio.wait_for(started.wait(), 5)

    # 并发 sync 完整一轮：B 落盘 + 候选跳过
    async def _fetch_b(url, channels, **kw):
        return _fetch_result(nodes_b)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch_b)
    await _sync_at(tmp_data_dir, later)
    assert await _status(sub_id) == ADMISSION_CANDIDATE
    assert await _source_fps(sub_id) == set(), "候选跳过，Registry 仍空"

    go.set()
    await asyncio.wait_for(task, timeout=20)
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(nodes_b), "必须应用当前最新的 B"
    assert await _source_fps(sub_id) != set(), "绝不允许 ACTIVE + Registry 空"
    assert await _source_fps(sub_id) != _fps(nodes_a), "不得是旧 A"


# ══ D. 兜底：apply 被判 skipped_stale 时绝不写 ACTIVE ════════════════
@pytest.mark.asyncio
async def test_promote_never_active_when_apply_is_skipped_stale(
    tmp_data_dir, monkeypatch
):
    """把已被取代的 S1 硬塞给 promote（打桩 latest_snapshot）→ apply 必被判
    `skipped_stale` → promote 必须**失败并保持 CANDIDATE**，Registry 保持空。
    这条直接钉死"skipped_stale 后照样 ACTIVE"。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-d2.invalid/x")
    nodes_a = [_node("j1", "10.0.0.1")]
    nodes_b = [_node("j2", "10.0.0.2")]
    later = datetime(2026, 9, 21, 13, 0, 0)

    snap_a = await _persist_nodes(sub_id, nodes_a, NOW, tmp_data_dir)
    await _persist_nodes(sub_id, nodes_b, later, tmp_data_dir)  # 更新的 B 已在库里

    import app.domains.proxypool.admission as adm

    async def _stale_latest(session, subscription_id, **kw):
        return snap_a  # 故意交一份已被取代的快照

    monkeypatch.setattr(adm, "latest_snapshot", _stale_latest)
    async with get_session_factory()() as s:
        with pytest.raises(PromotionError, match="取代"):
            await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=later
            )
        await s.rollback()
    assert await _status(sub_id) == ADMISSION_CANDIDATE, "必须保持候选"
    assert await _source_fps(sub_id) == set(), "不得留下任何来源"


# ══ C. 两份不同快照交错：最终严格等于最后生效那份，绝不是并集 ═══════
@pytest.mark.asyncio
async def test_interleaved_applies_never_union_last_writer_wins(
    tmp_data_dir, monkeypatch
):
    """**回归**：Snapshots A={1,2} 与 B={3,4} 交错执行 → 最终 Registry 必须**严格等于
    最后生效的那一份**，不得是并集 {1,2,3,4}。

    串行化边界 = 同一订阅 apply 的写事务（`apply_snapshot` 开头先取写锁再读）：
    第二个 apply 只能在写锁上等待，无法把读改写插进第一个的读与写之间。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-c.invalid/x")
    nodes_first = [_node("c1", "10.0.0.1"), _node("c2", "10.0.0.2")]
    nodes_second = [_node("c3", "10.0.0.3"), _node("c4", "10.0.0.4")]

    import app.domains.proxypool.registry as registry

    real_sources_of = registry._sources_of
    calls = {"n": 0}
    first_read = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_sources_of(session, subscription_id):
        rows = await real_sources_of(session, subscription_id)
        calls["n"] += 1
        if calls["n"] == 1:  # 第一个 writer 读完后停住（此刻它已持写锁）
            first_read.set()
            await release.wait()
        return rows

    monkeypatch.setattr(registry, "_sources_of", _blocking_sources_of)
    first = asyncio.create_task(
        _apply_snapshot_nodes(sub_id, nodes_first, tmp_data_dir)
    )
    await asyncio.wait_for(first_read.wait(), 5)
    second = asyncio.create_task(
        _apply_snapshot_nodes(sub_id, nodes_second, tmp_data_dir)
    )
    await asyncio.sleep(0.1)
    assert not second.done(), "第二个 apply 必须在写锁上等待，不能与第一个交错"
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=20)
    monkeypatch.setattr(registry, "_sources_of", real_sources_of)

    got = await _source_fps(sub_id)
    assert len(got) == 2, f"来源集合只能是**一份**快照的规模，实得 {len(got)}：{got}"
    assert got != _fps(nodes_first) | _fps(nodes_second), "绝不能是两份快照的并集"
    assert got == _fps(nodes_second), "必须严格等于最后生效的那份快照"


@pytest.mark.asyncio
async def test_concurrent_applies_always_end_at_one_snapshot(tmp_data_dir):
    """不加卡点、真并发多轮：任何一轮结束后集合都只能是「其中一份」，不能是并集。"""
    await init_db()
    sub_id = await _add_candidate("https://race-c2.invalid/x")
    sets = [
        [_node("e1", "10.0.0.1"), _node("e2", "10.0.0.2")],
        [_node("e3", "10.0.0.3"), _node("e4", "10.0.0.4")],
    ]
    for _ in range(5):
        await asyncio.gather(
            _apply_snapshot_nodes(sub_id, sets[0], tmp_data_dir),
            _apply_snapshot_nodes(sub_id, sets[1], tmp_data_dir),
            return_exceptions=True,
        )
        got = await _source_fps(sub_id)
        assert got in (_fps(sets[0]), _fps(sets[1])), f"出现并集/混合：{got}"


# ══ E. 强制时序：第二个 promote 被边界挡住 → 幂等空转 ════════════════
@pytest.mark.asyncio
async def test_forced_interleave_second_promote_waits_then_noops(
    tmp_data_dir, monkeypatch
):
    """**强制时序**（不是普通 gather 就算数）：

        T1 取得写串行化边界 → 读到 CANDIDATE → 暂停（**持边界**）
        T2 读同一 subscription：**做不到**——它被挡在自己的取边界语句上
        T1 完成 apply + ACTIVE + commit
        T2 这才继续：读到 ACTIVE → 幂等空转（不写任何东西）

    边界前移到"读之前"之后这是**更强**的形态：不再存在"两个请求同时读到
    CANDIDATE"的窗口。要求：T2 不出现 SQLITE_BUSY_SNAPSHOT / IntegrityError /
    任何异常，最终 ACTIVE + 一份完整集合，T2 语义为**幂等空转**。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-e.invalid/x")
    nodes = [_node("f1", "10.0.0.1"), _node("f2", "10.0.0.2")]

    async def _fetch(url, channels, **kw):
        return _fetch_result(nodes)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)
    await _sync(tmp_data_dir)  # 成功快照落盘

    import app.domains.proxypool.admission as adm

    real_latest = adm.latest_snapshot
    ready = {"T1": asyncio.Event(), "T2": asyncio.Event()}
    go = {"T1": asyncio.Event(), "T2": asyncio.Event()}

    async def _blocking_latest(session, subscription_id, **kw):
        snap = await real_latest(session, subscription_id, **kw)
        # 能走到这里 ⟹ 本任务已通过 `is_admitted(sub)` 的 CANDIDATE 状态检查
        # （已是 ACTIVE 会在 latest_snapshot 之前就早退）
        tag = asyncio.current_task().get_name()
        ready[tag].set()
        await go[tag].wait()
        return snap

    monkeypatch.setattr(adm, "latest_snapshot", _blocking_latest)

    async def _promote(tag: str) -> bool:
        asyncio.current_task().set_name(tag)
        async with get_session_factory()() as s:
            res = await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
            await s.commit()
            return res.promoted

    t1 = asyncio.create_task(_promote("T1"))
    await asyncio.wait_for(ready["T1"].wait(), 5)  # T1 已持写边界并暂停
    t2 = asyncio.create_task(_promote("T2"))
    await asyncio.sleep(0.2)
    assert not ready["T2"].is_set(), "T2 不该越过边界去读快照"
    assert not t2.done(), "T2 应被挡在自己的取边界语句上"
    assert await _status(sub_id) == ADMISSION_CANDIDATE, "T1 未提交，库里仍是 CANDIDATE"

    go["T1"].set()  # T1 先完成 apply + ACTIVE + commit
    r1 = await asyncio.wait_for(t1, timeout=20)
    assert r1 is True
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(nodes)

    # T2 这才继续：取边界 → 读订阅行（新鲜）→ 已是 ACTIVE → 幂等空转
    results = await asyncio.gather(t2, return_exceptions=True)
    assert not isinstance(results[0], BaseException), (
        f"T2 不得出现 SQLITE_BUSY_SNAPSHOT / IntegrityError / 任何异常：{results[0]!r}"
    )
    assert results[0] is False, f"T2 应为幂等空转（已是 ACTIVE），实得 {results[0]!r}"
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(nodes), "最终仍是一份完整集合"


# ══ F. 顺序语义：最新成功快照胜（out-of-order 不得回退） ═════════════
@pytest.mark.asyncio
async def test_out_of_order_older_snapshot_does_not_regress_registry(tmp_data_dir):
    """确定性 out-of-order 回归：

        Snapshot A：较早 fetched_at
        Snapshot B：较晚 fetched_at
        让 **B 先 apply**，再让 **A 后 apply**
        → 最终 Registry 必须仍对应 B（不能因为"最后写"就退回 A）。

    依据（代码事实）：`ProxySubscription.snapshot_sha256` / `snapshot_version` 是
    「最近一次成功快照」的投影，`latest_snapshot()` 也按最新成功快照取值；两轮并发
    同步里"先抓到的后到"完全可能，若按最后提交者胜就会让投影与 Registry 对不上。
    """
    await init_db()
    sub_id = await _add_candidate("https://race-f.invalid/x")
    nodes_a = [_node("g1", "10.0.0.1")]
    nodes_b = [_node("g2", "10.0.0.2")]
    earlier = datetime(2026, 9, 21, 10, 0, 0)
    later = datetime(2026, 9, 21, 11, 0, 0)

    snap_a = await _persist_nodes(sub_id, nodes_a, earlier, tmp_data_dir)
    snap_b = await _persist_nodes(sub_id, nodes_b, later, tmp_data_dir)

    async with get_session_factory()() as s:  # B 先
        await apply_snapshot(s, subscription_id=sub_id, snapshot=snap_b, now=later)
        await s.commit()
    assert await _source_fps(sub_id) == _fps(nodes_b)

    async with get_session_factory()() as s:  # A 后（更旧）
        result = await apply_snapshot(
            s, subscription_id=sub_id, snapshot=snap_a, now=later
        )
        await s.commit()
    assert result.skipped_stale is True, "更旧的快照必须整体跳过"
    assert await _source_fps(sub_id) == _fps(nodes_b), "旧快照不得回退 Registry"

    # 反例守卫：新快照仍然照常生效（不能把跳过规则做成"永不更新"）
    nodes_c = [_node("g3", "10.0.0.3")]
    snap_c = await _persist_nodes(
        sub_id, nodes_c, datetime(2026, 9, 21, 12, 0, 0), tmp_data_dir
    )
    async with get_session_factory()() as s:
        result2 = await apply_snapshot(
            s, subscription_id=sub_id, snapshot=snap_c, now=datetime(2026, 9, 21, 12, 0, 0)
        )
        await s.commit()
    assert result2.skipped_stale is False
    assert await _source_fps(sub_id) == _fps(nodes_c)


# ══ H. 「最新」的事实源 = 成功落库顺序（id），不是 fetched_at ════════
@pytest.mark.asyncio
async def test_latest_means_last_landed_not_last_fetched(tmp_data_dir):
    """裁定回归：「最新成功快照」= **成功落库顺序**（`SubscriptionSnapshot.id` 最大者），
    与 `latest_snapshot()` / `retention` 同口径。

        A：先抓取（fetched_at 更早）但**后落库**（id 更大）← 最后成功落库的那份
        B：后抓取（fetched_at 更晚）但先落库（id 更小）

    要求：`latest_snapshot()` 取到 A；apply(A) **不得**被判陈旧；最终 Registry 严格
    保持 A；随后 apply(B) 必须被判陈旧并保持 A。

    （若判据用 fetched_at，A 会被跳过、promote 永久失败——本用例就是钉死这一点。）
    """
    await init_db()
    sub_id = await _add_candidate("https://race-h.invalid/x")
    nodes_a = [_node("k1", "10.0.0.1")]
    nodes_b = [_node("k2", "10.0.0.2")]
    fetched_early = datetime(2026, 9, 21, 10, 0, 0)
    fetched_late = datetime(2026, 9, 21, 11, 0, 0)

    # B 后抓取但先落库；A 先抓取但后落库 ⇒ id_A > id_B，fetched_at_A < fetched_at_B
    await _persist_nodes(sub_id, nodes_b, fetched_late, tmp_data_dir)
    snap_a = await _persist_nodes(sub_id, nodes_a, fetched_early, tmp_data_dir)

    async with get_session_factory()() as s:
        latest = await latest_snapshot(s, sub_id)
    assert latest is not None and latest.sha256 == snap_a.sha256, (
        "latest_snapshot 必须按落库顺序取到 A"
    )

    async with get_session_factory()() as s:  # 最后成功落库的那份必须能应用
        first = await apply_snapshot(
            s, subscription_id=sub_id, snapshot=latest, now=fetched_early
        )
        await s.commit()
    assert first.skipped_stale is False, "最后落库的快照不得被判陈旧"
    assert await _source_fps(sub_id) == _fps(nodes_a)

    async with get_session_factory()() as s:  # 更早落库的那份必须被跳过
        older = await apply_snapshot(
            s, subscription_id=sub_id, snapshot=_snapshot_of(sub_id, nodes_b, fetched_late),
            now=fetched_late,
        )
        await s.commit()
    assert older.skipped_stale is True
    assert await _source_fps(sub_id) == _fps(nodes_a), "Registry 必须保持最后落库那份"


# ══ G. 双击晋升（同快照并发）→ 不 500，最终仍是一份完整集合 ══════════
@pytest.mark.asyncio
async def test_double_promote_same_snapshot_stays_consistent(tmp_data_dir, monkeypatch):
    """两次并发 promote（同一条候选、同一份成功快照，双击场景）：
    两个请求都**不得**把数据库异常抛出来（不得变成 HTTP 500）；
    最终必须 ACTIVE + **一份完整**来源集合。"""
    await init_db()
    sub_id = await _add_candidate("https://race-d.invalid/x")
    nodes = [_node("d1", "10.0.0.1"), _node("d2", "10.0.0.2")]

    async def _fetch(url, channels, **kw):
        return _fetch_result(nodes)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)
    await _sync(tmp_data_dir)  # 候选快照落盘

    async def _promote_once() -> bool:
        async with get_session_factory()() as s:
            res = await promote_to_active(
                s, subscription_id=sub_id, data_dir=tmp_data_dir, now=NOW
            )
            await s.commit()
            return res.promoted

    results = await asyncio.gather(
        _promote_once(), _promote_once(), return_exceptions=True
    )
    assert [r for r in results if isinstance(r, BaseException)] == [], (
        f"并发 promote 不得抛异常（尤其不得是 IntegrityError → 500）：{results}"
    )
    assert any(r is True for r in results), f"至少一个请求应完成晋升：{results}"
    assert await _status(sub_id) == ADMISSION_ACTIVE
    assert await _source_fps(sub_id) == _fps(nodes), "最终必须是一份完整的来源集合"
