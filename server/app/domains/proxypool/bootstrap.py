"""bootstrap：订阅 → Snapshot → Registry → Pool → Runtime 的首次建立，幂等原语。
与 rebuild 是两个语义不合并：ensure_pool_runtime=Runtime 还没有；request_rebuild/
rebuild_runtime=已存在因池变化重建。幂等严格定义：池非空+入口在听+controller
可访问+对账通过才 ready。single-flight：生产两个调用点（启动链/订阅刷新），
进程内一把锁串行，第二个等锁后重查，不落持久化状态。失败语义：无订阅或任一步
失败 → Runtime 不可用 → crawler fail-closed，不退旧订阅代理不退直连，下个调度
周期再试；不新增 BOOTSTRAP_FAILED 状态（失败是操作结果不是状态资产）。
数据语义：snapshot sha256 唯一事实源在 subscription_snapshots.sha256，订阅行
只保留抓取结果标记（last_fetch_*），不投影快照身份。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import yaml

from app.domains.proxies.models import ProxySubscription
from app.domains.proxies.subscription_secret import UNREADABLE_MESSAGE, open_url
from app.core.database import WritePriority, write_gate
from app.core.logging import log_event
from app.domains.proxypool.admission import auto_admit_healthy_candidate, is_admitted
from app.domains.proxypool.exits import select_exit_slots
from app.domains.proxypool.health import latest_l0_delays
from app.domains.proxypool.pool import (
    build_pool, eligible_nodes, eligible_runtime_names, pool_auxiliary_names, pool_path,
)
from app.domains.proxypool.registry import apply_snapshot
from app.domains.proxypool.runtime import (
    DEFAULT_WAIT_TIMEOUT,
    _KernelRuntime,
    align_lanes,
    apply_global_selection,
    controller_endpoint_of,
    current_global_selection,
    current_runtime_proxy_url,
    invalidate_lane_plan,
    prepare_runtime_config,
    reconcile,
    restore_selection,
    wait_mixed_port,
    wait_proxy_names,
)
from app.domains.proxypool.subscription import (
    Channel,
    build_channels,
    build_snapshot,
    fetch_egress_success_scores,
    fetch_subscription,
    parse_nodes,
    persist_fetch_failure,
    persist_snapshot,
    preferred_fetch_egresses,
    saved_proxy_channels,
    subscription_url_identity,
)
from app.domains.proxypool.models import subscription_tag

_single_flight = asyncio.Lock()
_subscription_sync_locks: dict[int, asyncio.Lock] = {}

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SUBSCRIPTION_FETCHES = 5
POOL_FETCH_HEALTH_MAX_AGE = timedelta(hours=72)


@dataclass(frozen=True)
class SyncResult:
    subscription_count: int
    snapshot_sha256: dict[int, str]
    node_count: dict[int, int]
    pool_names: tuple[str, ...]
    pool_changed: bool
    failures: dict[int, str]
    # 本次只落快照、**未进 Registry** 的订阅（生产准入为 CANDIDATE）。
    # 事实留存（快照 + 字节），生产侧无感：不产生节点/来源、不进 eligible、不改签名。
    skipped_candidates: tuple[int, ...] = ()
    snapshots: dict[int, object] = field(default_factory=dict)
    pool_signature_before: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BootstrapResult:
    ready: bool
    bootstrapped: bool
    proxy_url: str | None
    controller_url: str | None
    pool_names: tuple[str, ...]
    detail: str


async def _healthy_pool_channels(session: AsyncSession, *, data_dir: Path,
                                 now: datetime) -> list:
    """Return live pool lanes backed by a recent successful L2 observation."""
    from app.domains.proxies import clash_manager
    from app.domains.proxypool.models import HealthObservation, ProxyNode
    from app.domains.proxypool.runtime import lane_bindings
    from app.domains.proxypool.state import NODE_ACTIVE
    from app.domains.proxypool.subscription import Channel, CHANNEL_POOL

    try:
        if not clash_manager.pool_runtime.status().get("running"):
            return []
        bindings = lane_bindings(data_dir)
    except Exception:  # noqa: BLE001 —— an unavailable pool is not a fetch dependency
        return []
    lane_nodes = {
        str(binding.get("node")): binding
        for binding in bindings
        if binding.get("node") and binding.get("exitIp") and binding.get("url")
    }
    if not lane_nodes:
        return []

    nodes = (
        await session.execute(
            select(ProxyNode).where(ProxyNode.runtime_name.in_(lane_nodes))
        )
    ).scalars().all()
    by_name = {str(node.runtime_name): node for node in nodes}
    node_ids = [str(node.node_id) for node in nodes]
    latest_l2: dict[str, HealthObservation] = {}
    if node_ids:
        observations = (
            await session.execute(
                select(HealthObservation)
                .where(HealthObservation.node_id.in_(node_ids))
                .where(HealthObservation.level == "L2")
                .order_by(HealthObservation.observed_at.desc(),
                          HealthObservation.id.desc())
            )
        ).scalars().all()
        for observation in observations:
            latest_l2.setdefault(str(observation.node_id), observation)

    cutoff = now - POOL_FETCH_HEALTH_MAX_AGE
    channels: list[Channel] = []
    used_exits: set[str] = set()
    for runtime_name, binding in lane_nodes.items():
        node = by_name.get(runtime_name)
        if (
            node is None
            or node.state != NODE_ACTIVE
            or int(node.consecutive_failures or 0) != 0
            or str(node.exit_ip or "") != str(binding.get("exitIp") or "")
        ):
            continue
        observation = latest_l2.get(str(node.node_id))
        if (
            observation is None
            or not observation.ok
            or observation.observed_at is None
            or not cutoff <= observation.observed_at <= now
        ):
            continue
        exit_ip = str(node.exit_ip)
        if exit_ip in used_exits:
            continue
        used_exits.add(exit_ip)
        channels.append(Channel(
            CHANNEL_POOL, str(binding["url"]), f"exit:{exit_ip}",
        ))
    return channels


async def subscription_sources(session: AsyncSession) -> list[ProxySubscription]:
    """订阅入口：`proxy_subscriptions`（kind='clash'）。

    排序固定，保证 bootstrap 的结果可复现。
    """
    rows = await session.execute(
        select(ProxySubscription)
        .where(ProxySubscription.kind == "clash")
        .order_by(ProxySubscription.id)
    )
    return list(rows.scalars())


def subscription_sync_lock(subscription_id: int) -> asyncio.Lock:
    """Return the process-local lock shared by URL edits and subscription refreshes."""
    return _subscription_sync_locks.setdefault(int(subscription_id), asyncio.Lock())


async def sync_subscriptions(
    session: AsyncSession, *, data_dir: Path, now: datetime,
    kernel_proxy: str | None = None,
    only_auto: bool = False,
    only_sub_id: int | None = None,
    saved_proxies: list[str] | None = None,
    skip_sub_ids: set[int] | None = None,
) -> SyncResult:
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("订阅同步不能复用含未提交修改的会话")
    initial = await subscription_sources(session)
    if only_sub_id is not None:
        initial = [sub for sub in initial if sub.id == only_sub_id]
    elif only_auto:
        initial = [sub for sub in initial if _auto_refresh_on(sub)]
    if skip_sub_ids:
        initial = [sub for sub in initial if sub.id not in skip_sub_ids]
    locked_ids = {int(sub.id) for sub in initial}
    baseline = {
        int(sub.id): (sub.last_fetch_at, open_url(sub.url) or "")
        for sub in initial
    }
    locks = [subscription_sync_lock(sub_id) for sub_id in sorted(locked_ids)]
    acquired: list[asyncio.Lock] = []
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        return await _sync_subscriptions_under_locks(
            session, data_dir=data_dir, now=now, kernel_proxy=kernel_proxy,
            only_auto=only_auto, only_sub_id=only_sub_id,
            saved_proxies=saved_proxies, skip_sub_ids=skip_sub_ids,
            locked_ids=locked_ids, baseline=baseline,
        )
    finally:
        for lock in reversed(acquired):
            lock.release()


async def _sync_subscriptions_under_locks(
    session: AsyncSession, *, data_dir: Path, now: datetime,
    kernel_proxy: str | None,
    only_auto: bool,
    only_sub_id: int | None,
    saved_proxies: list[str] | None,
    skip_sub_ids: set[int] | None,
    locked_ids: set[int],
    baseline: dict[int, tuple[datetime | None, str]],
) -> SyncResult:
    """订阅 → 快照 → Registry。**Runtime 层不参与，也拿不到 subscription URL。**

    每份成功快照都会对齐节点身份与来源。CANDIDATE 仍由准入状态挡在生产池外，
    但它的节点身份和健康证据可以与通用代理 Runtime 共用。

    抓取走既有通道链语义：`direct → kernel_proxy → 池 lane → 本机混合端口`。
    直连被墙/超时是常态，所以 kernel_proxy（现有 Mihomo 内核的代理）是真实可用的
    第二通道——但它**只是订阅获取的辅助通道**，绝不是 crawler 的代理，也不构成
    proxypool Runtime 的回退。`kernel_proxy` 不给就现取（老 Clash 在跑就用它），
    避免调用方漏传时整条链只剩直连一条。
    """
    if kernel_proxy is None:
        kernel_proxy = default_kernel_proxy()
    subs = await subscription_sources(session)
    if only_sub_id is not None:
        # 单订阅口径（手动重拉喂池子）：无视 autoRefresh 开关——用户点了重拉
        # 就是明确要这条订阅的最新内容进池
        subs = [s for s in subs if s.id == only_sub_id]
    elif only_auto:
        # 关掉自动更新的订阅不参与本轮：限时订阅只能在窗口内下载、下载后可长期
        # 使用，每轮都撞一次注定失败的抓取只会刷出噪音 FAILED。手动重拉不受此限。
        auto_subs = [s for s in subs if _auto_refresh_on(s)]
        off = [s.id for s in subs if not _auto_refresh_on(s)]
        if off:
            log_event(
                logger,
                f"{len(off)} 条关闭了自动更新的订阅本轮跳过同步",
                tag="跳过",
                detail={"订阅编号": "、".join(str(i) for i in off)},
            )
        subs = auto_subs
    if skip_sub_ids:
        subs = [sub for sub in subs if sub.id not in skip_sub_ids]
    subs = [sub for sub in subs if int(sub.id) in locked_ids]
    for sub in subs:
        await session.refresh(sub)
    shas: dict[int, str] = {}
    counts: dict[int, int] = {}
    snapshots: dict[int, object] = {}
    failures: dict[int, str] = {}
    skipped: list[int] = []
    selected_count = len(subs)
    coalesced: set[int] = set()
    from app.domains.proxypool.subscription import latest_snapshot

    for sub in subs:
        sub_id = int(sub.id)
        prior_fetch_at, prior_url = baseline.get(sub_id, (None, ""))
        current_url = open_url(sub.url) or ""
        if current_url != prior_url or sub.last_fetch_at == prior_fetch_at:
            continue
        coalesced.add(sub_id)
        if sub.last_fetch_status == "OK" and current_url:
            snapshot = await latest_snapshot(session, sub_id, url=current_url)
            if snapshot is not None:
                snapshots[sub_id] = snapshot
                shas[sub_id] = snapshot.sha256
                counts[sub_id] = snapshot.node_count
                continue
        failures[sub_id] = sub.last_error or "并发刷新已完成，但没有可恢复的成功快照"
    subs = [sub for sub in subs if int(sub.id) not in coalesced]
    apply_ids = tuple(int(sub.id) for sub in subs)
    healthy_pool_channels = await _healthy_pool_channels(
        session, data_dir=data_dir, now=now,
    )
    channels = build_channels(
        kernel_proxy=kernel_proxy,
        pool_channels=healthy_pool_channels,
    )
    existing_proxies = {channel.proxy for channel in channels}
    channels.extend(
        channel for channel in saved_proxy_channels(saved_proxies or [])
        if channel.proxy not in existing_proxies
    )
    # ── 有界并发下载（纯 I/O），落库仍串行（SQLite 单写者）──
    sub_urls = {sub.id: open_url(sub.url) or "" for sub in subs}
    fetched: dict[int, object] = {}
    readable_urls = {sid: url for sid, url in sub_urls.items() if url}
    egress_scores = await fetch_egress_success_scores(session)
    preferred_egresses = await preferred_fetch_egresses(session, readable_urls)
    fetch_slots = asyncio.Semaphore(MAX_CONCURRENT_SUBSCRIPTION_FETCHES)

    async def _prefetch(sub) -> None:
        url = sub_urls[sub.id]
        if not url:
            return
        try:
            async with fetch_slots:
                fetched[sub.id] = await fetch_subscription(
                    url, channels,
                    preferred_egress=preferred_egresses.get(sub.id),
                    egress_scores=egress_scores,
                )
        except Exception as exc:  # noqa: BLE001 —— 失败在下方按订阅记账
            fetched[sub.id] = exc

    await asyncio.gather(*(_prefetch(s) for s in subs))

    # 落库段进闸：并发下载已在闸外完成，闸内只允许本轮流库 I/O（savepoint /
    # 事件 / 投影）与收尾提交——网络等待进闸会横持写者位，饿死全体写。
    await session.rollback()
    async with write_gate(WritePriority.BACKGROUND, label="subscription_apply"):
        await session.execute(text("BEGIN IMMEDIATE"))
        current = {
            int(sub.id): sub for sub in await subscription_sources(session)
            if int(sub.id) in set(apply_ids)
        }
        for missing_id in set(apply_ids) - set(current):
            failures[missing_id] = "订阅在刷新期间已删除，本次结果未应用"
        subs = [current[sub_id] for sub_id in apply_ids if sub_id in current]
        before = await eligible_runtime_names(session)
        signature_before = await pool_signature(session, data_dir=data_dir)
        for sub in subs:
            sub_id = sub.id  # 先取出来：savepoint 回滚后 ORM 对象会过期，不能再靠它
            try:
                # 每条订阅一个 SAVEPOINT：本条目的半截写入只回滚自己，
                # 同轮其它订阅已完成的成果留在外层事务里
                async with session.begin_nested():
                    sub_url = sub_urls.get(sub_id) or ""
                    if not sub_url:
                        # 链接解不开（密钥属主机绑定）：本轮跳过并留原因，不伪装成抓取失败
                        skipped.append(sub_id)
                        message = UNREADABLE_MESSAGE
                        failures[sub_id] = message
                        await session.execute(
                            update(ProxySubscription)
                            .where(ProxySubscription.id == sub_id)
                            .values(last_fetch_at=now, last_fetch_status="FAILED", last_error=message)
                        )
                        continue
                    result = fetched.get(sub_id)
                    if isinstance(result, Exception):
                        raise result
                    if result is None:  # 预取缺失（不可达）：按失败记账，不静默丢
                        raise RuntimeError("订阅下载未返回结果")
                    await session.refresh(sub)
                    if (open_url(sub.url) or "") != sub_url:
                        message = "订阅地址在刷新期间发生变化，本次结果未应用"
                        failures[sub_id] = message
                        continue
                    nodes = result.resolved_nodes or parse_nodes(result.raw, result.fmt)
                    snap = build_snapshot(sub_id, sub_url, result, nodes, now=now)
                    await persist_snapshot(session, snap, data_dir=data_dir)
                    snapshots[sub_id] = snap
                    # 准入判定必须**现读**：`subs` 是轮询开始时加载的，抓取可能耗时几十秒，
                    # 期间这条订阅可能已经被晋升（或本来就是候选）。用陈旧对象判定会出现：
                    # 同步落盘了更新的快照、却因为"当初是候选"而跳过 apply → 订阅已是 ACTIVE
                    # 而 Registry 仍停在旧快照。刷新一次再判，代价是一条 SELECT。
                    if result.userinfo is not None:
                        was_blocked = sub.resource_block_reason() is not None
                        sub.last_stats = {**(sub.last_stats or {}), "traffic": result.userinfo}
                        if was_blocked and sub.resource_block_reason() is None:
                            sub.deprecated = False
                            sub.deprecated_at = None
                            sub.deprecated_reason = None
                    applied = await apply_snapshot(
                        session, subscription_id=sub_id, snapshot=snap, now=now,
                        tag=subscription_tag(sub_url),
                    )
                    if is_admitted(sub):
                        # 出库留痕：快照对齐删除的来源与随之结算的节点是最高频的
                        # 出库通道，零日志会让「这批节点为什么变 STALE」不可追溯
                        if applied.removed_sources or applied.state_changes:
                            log_event(
                                logger,
                                f"订阅 {sub_id} 快照对齐：移除 {len(applied.removed_sources)} 个失效来源，"
                                f"{len(applied.state_changes)} 个节点状态随来源变动结算",
                                detail={
                                    "订阅编号": sub_id,
                                    "移除来源": len(applied.removed_sources),
                                    "状态结算": len(applied.state_changes),
                                },
                            )
                    else:
                        # 候选订阅的健康自动转正：最近一次体检有可用节点即进池，
                        # 准入依据是体检事实而不是人工点头（快照刚落盘，转正 apply 的就是它）
                        admitted = await auto_admit_healthy_candidate(
                            session, subscription_id=sub_id, data_dir=data_dir, now=now
                        )
                        if admitted is not None:
                            log_event(
                                logger,
                                f"订阅 {sub_id} 体检健康，自动转正进生产池",
                                tag="成功",
                                detail={
                                    "订阅编号": sub_id,
                                    "可用节点数": (sub.last_stats or {}).get("alive"),
                                    "快照": snap.sha256[:10],
                                    "节点数": len(nodes),
                                },
                            )
                        else:
                            skipped.append(sub_id)
                            log_event(
                                logger,
                                f"订阅 {sub_id} 体检无可用节点或尚未体检，保持候选（快照已落盘、不进生产池）",
                                tag="跳过",
                                detail={
                                    "订阅编号": sub_id,
                                    "快照": snap.sha256[:10],
                                    "节点数": len(nodes),
                                },
                            )
            except Exception as exc:  # noqa: BLE001 —— 单条订阅失败不阻断其它
                message = str(exc)[:500] or type(exc).__name__
                fetch_error = fetched.get(sub_id)
                if sub_urls.get(sub_id) and isinstance(fetch_error, Exception):
                    attempts = getattr(fetch_error, "attempts", [])
                    try:
                        async with session.begin_nested():
                            await persist_fetch_failure(
                                session, subscription_id=sub_id,
                                url=sub_urls[sub_id], attempts=attempts,
                                error=message,
                                duration_ms=getattr(fetch_error, "duration_ms", 0),
                                now=now,
                            )
                            await session.flush()
                    except Exception:  # noqa: BLE001 —— 原始失败投影仍需落地
                        log_event(
                            logger,
                            f"订阅 {sub_id} 抓取失败明细保存失败",
                            level=logging.ERROR,
                            exc_info=True,
                            detail={"订阅编号": sub_id},
                        )
                # savepoint 已把本条目回滚掉；失败用 Core UPDATE 记录，**不碰已过期的 ORM
                # 对象**——真实生产已复现：flush 失败后再读 sub.* 会抛 PendingRollbackError，
                # 于是原始失败反而没有被记录下来。
                await session.execute(
                    update(ProxySubscription)
                    .where(ProxySubscription.id == sub_id)
                    .values(last_fetch_at=now, last_fetch_status="FAILED", last_error=message)
                )
                failures[sub_id] = message
                continue

            # 事实源始终是 subscription_snapshots.sha256；订阅行不再投影快照身份
            sub.last_fetch_at = now
            sub.last_fetch_status = "OK"
            sub.last_success_at = now
            sub.last_error = None
            sub.cache_url_identity = subscription_url_identity(sub_url)
            shas[sub.id] = snap.sha256
            counts[sub.id] = len(nodes)

        # 提交在闸内完成：SQLite 文件写锁随提交释放，闸界与锁界对齐——
        # 否则后续写者拿到闸却撞上未释放的文件锁，空等 busy_timeout
        await session.commit()

    names = await eligible_runtime_names(session)
    return SyncResult(
        subscription_count=selected_count,
        snapshot_sha256=shas,
        node_count=counts,
        pool_names=names,
        pool_changed=names != before,
        failures=failures,
        skipped_candidates=tuple(skipped),
        snapshots=snapshots,
        pool_signature_before=signature_before,
    )


async def runtime_ready(session: AsyncSession, *, data_dir: Path) -> bool:
    """ready = 内核活着、入口在听、controller 可访问，且**与它启动时那份池产物一致**。

    刻意**不**要求"Registry == 内核"：池刚变脏（订阅带进新节点）正是等着 rebuild 的状态，
    那时内核落后于 Registry 是正常的。若把这种状态判成 not ready，刷新链会误以为
    "没有可用 Runtime"而重新 bootstrap，把一个本该 rebuild 的场景变成一次多余的内核重启。
    """
    if current_runtime_proxy_url(data_dir) is None:
        return False
    artifact = _artifact_names(data_dir)
    if not artifact:
        return False
    try:
        base, secret = controller_endpoint_of(data_dir)
        observed = await wait_proxy_names(base, secret, timeout=3.0)
    except Exception:  # noqa: BLE001
        return False
    ledger = reconcile(
        registry_names=set(artifact), pool_names=set(artifact),
        observed_names=observed,
        auxiliary_names=set(pool_auxiliary_names(data_dir)),
    )
    return ledger.ok


def _artifact_names(data_dir: Path) -> tuple[str, ...]:
    """内核启动时那份池产物（`crawl-pool.yaml`）里的名字。"""
    try:
        doc = yaml.safe_load(pool_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(doc, dict):
        return ()
    return tuple(str(p["name"]) for p in doc.get("proxies", []) if isinstance(p, dict))


def _auto_refresh_on(sub) -> bool:
    """订阅是否参与定时刷新。列缺失（历史行未补列）按开处理。"""
    value = getattr(sub, "auto_refresh", None)
    return True if value is None else bool(value)


def default_kernel_proxy() -> str | None:
    """现有 Mihomo 内核（老 Clash）的混合入口——**仅作订阅获取的第二通道**。

    直连被墙/超时是常态，而老 Clash 往往已经能出网。注意边界：它只是"代为下载订阅"
    的外部条件，**不是 crawler 的代理**，也不构成 proxypool Runtime 的回退。
    """
    try:
        from app.domains.proxies import clash_manager

        status = clash_manager.runtime.status()
        # 入口硬闸：端口没在听（解析出的默认值无人监听/被占）不交假通道
        if status.get("running") and status.get("port") and clash_manager.runtime.entry_listening():
            return f"http://127.0.0.1:{status['port']}"
    except Exception:  # noqa: BLE001 —— 老 Clash 不在也不影响直连通道
        return None
    return None


async def _no_pool_reason(session: AsyncSession, sync: SyncResult | None) -> str:
    """"建不起来"的**真实原因**——三种情况不能混成同一句模糊话。"""
    subs = await subscription_sources(session)
    if not subs:
        return "没有可 bootstrap 的订阅（proxy_subscriptions 里没有 clash 订阅）"
    if sync is not None and sync.failures:
        detail = "；".join(
            f"sub {sid}: {msg}" for sid, msg in sorted(sync.failures.items())
        )
        return f"订阅抓取失败：{detail}"
    if sync is not None and sync.subscription_count:
        return "订阅抓取成功但没有产出可用节点（解析结果为空）"
    return "无可用节点：无法 bootstrap"


async def ensure_pool_runtime(
    session: AsyncSession, *,
    data_dir: Path,
    runtime: _KernelRuntime,
    exe_path: str,
    now: datetime,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    skip_sync: bool = False,
    kernel_proxy: str | None = None,
) -> BootstrapResult:
    """幂等原语：**池文件与合格集集合相等、且 Runtime 可达**才算"已有可用 Runtime"。

    `skip_sync=True` 供"调用方刚刚自己同步过"的场景（订阅刷新链）：直接吃最新
    Registry 建 Runtime，**不再重复抓一次订阅**。

    Runtime 可达只说明内核在跑，不说明池文件是最新的：促升/合并新订阅会让 eligible
    变化，而池文件要等一次重建才跟上。集合不一致时不声称 current，走既有重建链
    （`request_rebuild` → `run_pending_rebuild` → `rebuild_runtime`）；占线时由它让路，
    本轮如实返回"尚未 current"。比较用**集合**——数量相同但节点不同也必须重建。
    """
    from app.domains.proxypool.pool import pool_file_names
    from app.domains.proxypool.scheduling import request_rebuild, run_pending_rebuild

    stale = set(await eligible_runtime_names(session)) != set(pool_file_names(data_dir))

    if await runtime_ready(session, data_dir=data_dir):
        base, secret = controller_endpoint_of(data_dir)
        if not stale:
            return BootstrapResult(
                True, False, current_runtime_proxy_url(data_dir), base,
                await eligible_runtime_names(session), "已有可用 Runtime",
            )
        # 可达但池文件落后：交给既有重建链，避免"复用一个过期池"
        request_rebuild()
        rebuilt = await run_pending_rebuild(
            session, data_dir=data_dir, controller_url=base, secret=secret,
            runtime=runtime, exe_path=exe_path,
        )
        if rebuilt is None:
            return BootstrapResult(
                False, False, current_runtime_proxy_url(data_dir), base,
                await eligible_runtime_names(session),
                "池文件与合格集不一致，本轮未重建（占线或重建让路）",
            )
        # 重建已执行：池文件此刻应已跟到合格集，才算 current
        now_base, _ = controller_endpoint_of(data_dir)
        if set(await eligible_runtime_names(session)) != set(pool_file_names(data_dir)):
            return BootstrapResult(
                False, True, current_runtime_proxy_url(data_dir), now_base,
                await eligible_runtime_names(session),
                "重建后池文件仍与合格集不一致",
            )
        return BootstrapResult(
            True, True, current_runtime_proxy_url(data_dir), now_base,
            await eligible_runtime_names(session),
            "池文件落后于合格集，已按既有重建链重建",
        )

    async with _single_flight:
        # 等锁期间前一个调用者可能已经建好了——必须重新检查，否则会建第二份
        if await runtime_ready(session, data_dir=data_dir):
            base, _ = controller_endpoint_of(data_dir)
            return BootstrapResult(
                True, False, current_runtime_proxy_url(data_dir), base,
                await eligible_runtime_names(session), "已有可用 Runtime（等锁期间建好）",
            )

        sync: SyncResult | None = None
        if not skip_sync and not await eligible_runtime_names(session):
            # 只有 Registry 为空（真·首建：全新安装，或全部订阅从未拉成过）才在
            # 起核前拉订阅——内核就位不等网络。Registry 有货（重启场景，库是
            # 持久化的）直接吃现存合格集起核；订阅下载交给启动链后位的同步步骤
            # 与 30min 刷新，池变化经 rebuild_pending → 重建（热重载优先）跟进。
            sync = await sync_subscriptions(
                session, data_dir=data_dir, now=now,
                kernel_proxy=(
                    default_kernel_proxy() if kernel_proxy is None else kernel_proxy
                ),
            )
        if not await eligible_runtime_names(session):
            return BootstrapResult(
                False, False, None, None, (),
                await _no_pool_reason(session, sync),
            )

        build = await build_pool(session, data_dir=data_dir)
        slots = select_exit_slots(
            await eligible_nodes(session), delays=await latest_l0_delays(session)
        )
        config = prepare_runtime_config(data_dir, lanes=len(slots) or None)
        invalidate_lane_plan(data_dir)
        runtime.stop()
        status = runtime.start(exe_path, str(config), inject_lanes=False)
        base = status["controllerUrl"]
        secret = getattr(runtime, "secret", "")
        observed = await wait_proxy_names(base, secret, timeout=wait_timeout)
        port = await wait_mixed_port(data_dir, timeout=wait_timeout)

        ledger = reconcile(
            registry_names=set(build.runtime_names),
            pool_names=set(build.runtime_names),
            observed_names=observed,
            auxiliary_names=set(build.auxiliary_names),
        )
        if not ledger.ok:
            log_event(
                logger,
                f"bootstrap 对账拒绝：缺 {sorted(ledger.missing)}",
                level=logging.ERROR,
                detail={
                    "phase": "bootstrap",
                    "registryCount": len(ledger.registry_names),
                    "poolCount": len(ledger.pool_names),
                    "runtimeCount": len(ledger.runtime_names),
                    "missingCount": len(ledger.missing),
                    "unexpectedCount": len(ledger.unexpected),
                    "observedTotal": ledger.observed_total,
                },
            )
            return BootstrapResult(
                False, True, None, base, build.runtime_names,
                f"对账失败：缺 {sorted(ledger.missing)}",
            )

        previous = await current_global_selection(base, secret)
        chosen = restore_selection(previous, build.runtime_names)
        if chosen is not None:
            applied = await apply_global_selection(base, secret, chosen)
            if applied != chosen:
                return BootstrapResult(
                    False, True, None, base, build.runtime_names,
                    f"建立 GLOBAL 失败：now={applied!r}，期望 {chosen!r}",
                )
        # lane 入口就绪 + 逐条绑定：内核起来时各 lane 组默认选组内第一项，不建立绑定
        # 就等于所有 lane 走同一个节点（"多入口"退化成单出口，系统却自称 ready）
        await align_lanes(
            data_dir=data_dir, controller_url=base, secret=secret,
            pool_names=build.runtime_names, slots=slots or None,
            timeout=wait_timeout,
        )
        return BootstrapResult(
            True, True, f"http://127.0.0.1:{port}", base, build.runtime_names,
            "bootstrap 完成",
        )


# ══ 订阅刷新链的分流 ═════════════════════════════════════════════
# 判定"池变了"用的是 **Runtime Pool signature**，不是 Snapshot SHA：快照整体变了
# （例如来源元数据、无关节点变化）但合格运行集没变时，重启内核毫无意义。


async def pool_signature(
    session: AsyncSession, *, data_dir: Path | None = None,
) -> tuple[str, ...]:
    """按完整节点参数及必要连接依赖组判定运行集变化。"""
    from app.core.config import get_settings
    from app.domains.proxypool.pool import (
        PoolBuildError, eligible_nodes, render_pool, source_documents_for_nodes,
    )

    nodes = await eligible_nodes(session)
    signatures = []
    for node in nodes:
        payload = {**dict(node.normalized_config or {}), "name": node.runtime_name}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
        signatures.append(f"{node.runtime_name}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}")
    errors: dict[int, str] = {}
    sources = await source_documents_for_nodes(
        session, nodes, data_dir=data_dir or get_settings().data_dir, errors=errors,
    )
    try:
        text, _ = render_pool(nodes, source_documents=sources)
        dependencies = yaml.safe_load(text).get("proxy-groups") or []
    except PoolBuildError as exc:
        dependencies = {"invalid": str(exc)}
    if dependencies or errors:
        blob = json.dumps({"groups": dependencies, "errors": errors}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        signatures.append(f"dependencies:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}")
    return tuple(signatures)


@dataclass(frozen=True)
class RefreshTriageResult:
    synced: bool
    pool_changed: bool
    # none | bootstrapped | unavailable | rebuild_requested
    action: str
    bootstrap: BootstrapResult | None


async def handle_subscription_refresh(
    session: AsyncSession, *,
    data_dir: Path,
    runtime: _KernelRuntime,
    exe_path: str,
    now: datetime,
    sync_result: SyncResult | None = None,
) -> RefreshTriageResult:
    """订阅刷新后的严格分流（**绝不在这里 stop/start Runtime**）：

    - 同步失败 → 什么都不做：旧 Runtime 继续工作，没有 Runtime 就继续不可用；
    - 没有可用 Runtime → `ensure_pool_runtime(skip_sync=True)`（吃刚同步好的 Registry，
      不再重复抓订阅，也不产生多余的 pending 重建）；
    - 有 Runtime 且池签名变了 → 只 `request_rebuild()`，重建交给 P1.6-C 空闲时消费；
    - 有 Runtime 且签名没变 → 什么都不做。
    """
    from app.domains.proxypool.scheduling import request_rebuild

    sync = sync_result
    before = (
        sync.pool_signature_before
        if sync is not None and sync.pool_signature_before is not None
        else await pool_signature(session, data_dir=data_dir)
    )
    remaining = await sync_subscriptions(
        session, data_dir=data_dir, now=now, only_auto=True,
        skip_sub_ids=(
            set(sync.snapshot_sha256) | set(sync.failures)
            if sync is not None else None
        ),
    )
    synced = bool(
        remaining.snapshot_sha256 or (sync is not None and sync.snapshot_sha256)
    )
    if not synced:
        return RefreshTriageResult(False, False, "none", None)

    after = await pool_signature(session, data_dir=data_dir)
    pool_changed = after != before

    if not await runtime_ready(session, data_dir=data_dir):
        boot = await ensure_pool_runtime(
            session, data_dir=data_dir, runtime=runtime, exe_path=exe_path,
            now=now, skip_sync=True,
        )
        return RefreshTriageResult(
            True, pool_changed, "bootstrapped" if boot.ready else "unavailable", boot
        )

    if pool_changed:
        request_rebuild()
        return RefreshTriageResult(True, True, "rebuild_requested", None)
    return RefreshTriageResult(True, False, "none", None)
