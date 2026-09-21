"""P1.7 bootstrap：把「订阅 → Snapshot → Registry → Pool → Runtime」串起来的最小幂等原语。

职责只有一个：

> **没有可用 Runtime 时，把这条链第一次建起来。**

因此它与 rebuild 是**两个语义**，不合并成 `ensure_everything()`：

- `ensure_pool_runtime()`：Runtime 根本还没有 / 池刚产生；
- `request_rebuild()` / `rebuild_runtime()`：Runtime 已存在，因池变化而重建。

**幂等的严格定义**：不是"文件存在就算已有"，而是"池非空 + 入口在听 + controller 可访问
+ 对账通过"才算 ready；否则重新 bootstrap。

**single-flight**：生产有两个调用点（启动链、订阅刷新），两者可能同时认为"Runtime 不存在"。
进程内一把锁保证只有一个调用者真正执行 bootstrap，第二个等锁后重新检查——不引入
`RuntimeLease`，也不落任何持久化状态。

**失败语义**：无订阅 / 任一步失败 → Runtime 不可用 → crawler 保持 fail-closed（P1.6-C），
不退旧订阅代理、不退直连；下一个既有调度周期再试。**不新增 `BOOTSTRAP_FAILED` 状态**——
失败是一次操作结果，不是状态资产。

**数据语义**：`ProxyNodeSource.subscription_id` 就是 `proxy_subscriptions.id`（与既有
`clash_nodes.subscription_id` 同一约定）；`subscription_snapshots.sha256` 是快照身份的
**唯一事实源**，`proxy_subscriptions.snapshot_sha256` 只是"最近一次成功快照"的**投影**，
且**只在快照落库成功之后**回写。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import yaml

from app.domains.proxies.models import ProxySubscription
from app.domains.proxypool.admission import is_admitted
from app.domains.proxypool.pool import (
    build_pool, eligible_runtime_names, pool_path,
)
from app.domains.proxypool.registry import apply_snapshot
from app.domains.proxypool.runtime import (
    DEFAULT_WAIT_TIMEOUT,
    _KernelRuntime,
    apply_global_selection,
    controller_endpoint_of,
    current_global_selection,
    current_runtime_proxy_url,
    prepare_runtime_config,
    reconcile,
    restore_selection,
    wait_mixed_port,
    wait_proxy_names,
)
from app.domains.proxypool.subscription import (
    build_channels,
    build_snapshot,
    fetch_subscription,
    parse_nodes,
    persist_snapshot,
)

_single_flight = asyncio.Lock()

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class BootstrapResult:
    ready: bool
    bootstrapped: bool
    proxy_url: str | None
    controller_url: str | None
    pool_names: tuple[str, ...]
    detail: str


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


async def sync_subscriptions(
    session: AsyncSession, *, data_dir: Path, now: datetime,
    kernel_proxy: str | None = None,
    pool_proxy: str | None = None,
) -> SyncResult:
    """订阅 → 快照 → Registry。**Runtime 层不参与，也拿不到 subscription URL。**

    生产准入（见 `admission.py`）：**CANDIDATE 在落完快照后停止**——不调用
    `apply_snapshot`，因此不产生 ProxyNode/ProxyNodeSource、不进 eligible/pool、
    不改 `pool_signature`、不触发 rebuild、不进健康与作业台账的来源事实；
    它的证据仍然完整（`subscription_snapshots` 行 + 内容寻址字节）。
    ACTIVE 保持既有行为（persist → apply）。

    抓取走既有通道链语义：`direct → kernel_proxy → pool_proxy`。直连被墙/超时是常态，
    所以 kernel_proxy（现有 Mihomo 内核的代理）是真实可用的第二通道——但它**只是订阅
    获取的辅助通道**，绝不是 crawler 的代理，也不构成 proxypool Runtime 的回退。
    """
    subs = await subscription_sources(session)
    before = await eligible_runtime_names(session)
    shas: dict[int, str] = {}
    counts: dict[int, int] = {}
    failures: dict[int, str] = {}
    skipped: list[int] = []
    channels = build_channels(kernel_proxy=kernel_proxy, pool_proxy=pool_proxy)

    for sub in subs:
        sub_id = sub.id  # 先取出来：savepoint 回滚后 ORM 对象会过期，不能再靠它
        try:
            # 每条订阅一个 SAVEPOINT：本条目的半截写入只回滚自己，
            # 同轮其它订阅已完成的成果留在外层事务里
            async with session.begin_nested():
                result = await fetch_subscription(str(sub.url), channels)
                nodes = parse_nodes(result.raw, result.fmt)
                snap = build_snapshot(sub_id, str(sub.url), result, nodes, now=now)
                await persist_snapshot(session, snap, data_dir=data_dir)
                # 准入判定必须**现读**：`subs` 是轮询开始时加载的，抓取可能耗时几十秒，
                # 期间这条订阅可能已经被晋升（或本来就是候选）。用陈旧对象判定会出现：
                # 同步落盘了更新的快照、却因为"当初是候选"而跳过 apply → 订阅已是 ACTIVE
                # 而 Registry 仍停在旧快照。刷新一次再判，代价是一条 SELECT。
                await session.refresh(sub)
                if is_admitted(sub):
                    await apply_snapshot(
                        session, subscription_id=sub_id, snapshot=snap, now=now
                    )
                else:
                    skipped.append(sub_id)
                    logger.info(
                        "[同步] 订阅 %s 为 CANDIDATE：快照已落盘（%s，%d 条），不进 Registry",
                        sub_id, snap.sha256[:10], len(nodes),
                    )
        except Exception as exc:  # noqa: BLE001 —— 单条订阅失败不阻断其它
            message = str(exc)[:500] or type(exc).__name__
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

        # 投影必须在**快照落库成功之后**；事实源始终是 subscription_snapshots.sha256
        sub.snapshot_sha256 = snap.sha256
        sub.snapshot_version = int(sub.snapshot_version or 0) + 1
        sub.last_fetch_at = now
        sub.last_fetch_status = "OK"
        sub.last_success_at = now
        sub.last_error = None
        shas[sub.id] = snap.sha256
        counts[sub.id] = len(nodes)

    names = await eligible_runtime_names(session)
    return SyncResult(
        subscription_count=len(subs),
        snapshot_sha256=shas,
        node_count=counts,
        pool_names=names,
        pool_changed=names != before,
        failures=failures,
        skipped_candidates=tuple(skipped),
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


def default_kernel_proxy() -> str | None:
    """现有 Mihomo 内核（老 Clash）的混合入口——**仅作订阅获取的第二通道**。

    直连被墙/超时是常态，而老 Clash 往往已经能出网。注意边界：它只是"替调用方下载订阅"
    的外部条件，**不是 crawler 的代理**，也不构成 proxypool Runtime 的回退。
    """
    try:
        from app.domains.proxies import clash_manager

        status = clash_manager.runtime.status()
        if status.get("running") and status.get("port"):
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
    pool_proxy: str | None = None,
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
        if not skip_sync:
            sync = await sync_subscriptions(
                session, data_dir=data_dir, now=now,
                kernel_proxy=(
                    default_kernel_proxy() if kernel_proxy is None else kernel_proxy
                ),
                pool_proxy=pool_proxy,
            )
        if not await eligible_runtime_names(session):
            return BootstrapResult(
                False, False, None, None, (),
                await _no_pool_reason(session, sync),
            )

        build = await build_pool(session, data_dir=data_dir)
        config = prepare_runtime_config(data_dir)
        runtime.stop()
        status = runtime.start(exe_path, str(config))
        base = status["controllerUrl"]
        secret = getattr(runtime, "secret", "")
        observed = await wait_proxy_names(base, secret, timeout=wait_timeout)
        port = await wait_mixed_port(data_dir, timeout=wait_timeout)

        ledger = reconcile(
            registry_names=set(build.runtime_names),
            pool_names=set(build.runtime_names),
            observed_names=observed,
        )
        if not ledger.ok:
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
        return BootstrapResult(
            True, True, f"http://127.0.0.1:{port}", base, build.runtime_names,
            "bootstrap 完成",
        )


# ══ 订阅刷新链的分流 ═════════════════════════════════════════════
# 判定"池变了"用的是 **Runtime Pool signature**，不是 Snapshot SHA：快照整体变了
# （例如来源元数据、无关节点变化）但合格运行集没变时，重启内核毫无意义。


async def pool_signature(session: AsyncSession) -> tuple[str, ...]:
    """当前 Runtime 池的签名：eligible 节点的 `runtime_name` + 配置指纹。

    只看**会进内核的东西**——所以 `state` 变化导致节点进出池会改变签名，而订阅侧
    改名/换来源不会（`runtime_name` 铸造一次即固定、`name` 本来就不参与指纹）。
    """
    from app.domains.proxypool.models import node_fingerprint
    from app.domains.proxypool.pool import eligible_nodes

    nodes = await eligible_nodes(session)
    return tuple(
        f"{node.runtime_name}:{node_fingerprint(dict(node.normalized_config or {}))}"
        for node in nodes
    )


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
) -> RefreshTriageResult:
    """订阅刷新后的严格分流（**绝不在这里 stop/start Runtime**）：

    - 同步失败 → 什么都不做：旧 Runtime 继续工作，没有 Runtime 就继续不可用；
    - 没有可用 Runtime → `ensure_pool_runtime(skip_sync=True)`（吃刚同步好的 Registry，
      不再重复抓订阅，也不产生多余的 pending 重建）；
    - 有 Runtime 且池签名变了 → 只 `request_rebuild()`，重建交给 P1.6-C 空闲时消费；
    - 有 Runtime 且签名没变 → 什么都不做。
    """
    from app.domains.proxypool.scheduling import request_rebuild

    before = await pool_signature(session)
    sync = await sync_subscriptions(session, data_dir=data_dir, now=now)
    synced = bool(sync.snapshot_sha256)
    if not synced:
        return RefreshTriageResult(False, False, "none", None)

    after = await pool_signature(session)
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
