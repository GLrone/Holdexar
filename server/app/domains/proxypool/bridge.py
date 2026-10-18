"""订阅节点体检 → ProxyNode：记录健康事实并刷新兼容 UI 投影。

前端「节点体检」探的是**生产端点**（IStoreBrowseService/GetItems）与出口 IP，
这正是池健康账本的 L2（真实业务）与 L1（出口身份）证据。本模块把它们按
「订阅来源」对齐到 Registry：

    (subscription_id, 节点名)  →  ProxyNode（按归属订阅 + 原名匹配）

命中节点按有效 L2 成功或真实传输失败推进唯一状态机，并分别记录 L1 出口与 L2
业务证据。普通 HTTP 错误响应和无效正文只记观测，不作为节点故障。健康状态只由
ProxyNode 保存，旧表只供 UI 与检测节流读取。

不做的事：不新建调度、不新建生命周期、不重铸名字、不降级节点。合格集或出口集
变化只置既有的 rebuild_pending，重建仍归既有的池重建通道。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxies.models import ClashNode
from app.domains.proxies.subscription_secret import open_url
from app.domains.proxypool.health import L1_LEVEL, L2_LEVEL
from app.domains.proxypool.models import (
    HealthObservation,
    ProxyNode,
    SubscriptionSnapshot,
    is_info_placeholder,
    subscription_code,
)
from app.domains.proxypool.registry import _name_base
from app.domains.proxypool.state import (
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_NEW,
    NODE_RETIRED,
    NODE_STALE,
    evaluate_node_state,
)

logger = logging.getLogger(__name__)

# 观测明细标签（HealthObservation.detail 是固定枚举，不放自由文本）
CLASH_STEAM_OK_DETAIL = "CLASH_STEAM_OK"
CLASH_STEAM_HTTP_ERROR_DETAIL = "CLASH_STEAM_HTTP_ERROR"
CLASH_STEAM_HTTP_502_DETAIL = "CLASH_STEAM_HTTP_502"
CLASH_STEAM_HTTP_504_DETAIL = "CLASH_STEAM_HTTP_504"
CLASH_STEAM_INVALID_DETAIL = "CLASH_STEAM_INVALID_RESPONSE"
CLASH_STEAM_TRANSPORT_DETAIL = "CLASH_STEAM_TRANSPORT_FAILURE"
CLASH_EXIT_IP_DETAIL = "CLASH_EXIT_IP"


@dataclass
class IngestionResult:
    """一次对齐的账目摘要（只记事实，不下结论）。"""

    matched: int = 0
    activated: int = 0
    deactivated: int = 0
    exit_ips_added: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.activated or self.deactivated or self.exit_ips_added)


def project_node_health(row: ClashNode, node: ProxyNode) -> tuple[str, str | None]:
    """把权威节点状态投影到旧 UI 行，返回投影前的状态与出口地址。"""
    prior_status, prior_exit = row.status, row.exit_ip
    if node.state in (NODE_DEAD, NODE_RETIRED):
        row.status = "dead"
    elif node.state in (NODE_ACTIVE, NODE_STALE) and node.last_l2_at is not None:
        row.status = "ok"
    elif node.state in (NODE_NEW, NODE_ACTIVE):
        row.status = "unknown"
    else:
        row.status = "dead"
    row.fail_count = int(node.consecutive_failures or 0)
    row.revive_passes = 0
    row.exit_ip = node.exit_ip
    return prior_status, prior_exit


def _name_keys(name: str) -> list[str]:
    """体检结果里的节点名 → 候选匹配键：原名 + 去内容版本后缀的主干。"""
    text = str(name or "").strip()
    if not text:
        return []
    keys = [text]
    base = _name_base(text)
    if base and base != text:
        keys.append(base)
    return keys


async def _load_index(
    session: AsyncSession,
) -> tuple[dict[tuple[int, str], ProxyNode], dict[str, ProxyNode]]:
    """来源关联索引：(订阅, 原名) → 节点；运行名 → 节点。

    运行名索引服务「体检直接读池内核配置」的形态——那时体检拿到的名字已经是
    runtime_name，按来源名匹配不到。
    """
    # by_runtime 覆盖**全部身份行**：来源关联缺失的节点（订阅抓取失败、来源关联
    # 丢过一拍）仍必须能被体检结论按运行名命中，否则手动体检证明健康的节点永远
    # 进不了池——身份行的可见性不能挂在「有没有来源关联」上。
    by_runtime: dict[str, ProxyNode] = {
        str(node.runtime_name): node
        for node in (await session.execute(select(ProxyNode))).scalars()
    }
    by_source: dict[tuple[int, str], ProxyNode] = {
        (int(node.subscription_id), str(node.original_name)): node
        for node in by_runtime.values()
        if node.subscription_id is not None
    }
    return by_source, by_runtime


def _match(
    by_source: dict[tuple[int, str], ProxyNode],
    by_runtime: dict[str, ProxyNode],
    subscription_id: int,
    name: str,
) -> ProxyNode | None:
    """把体检结果里的一个节点名定位到池账本行；定位不到返回 None。

    名称有两套口径，必须都试：体检给的是**订阅原名**，池账本的 runtime_name
    形如「订阅id|原名」。只按来源名匹配，会让「来源关联缺失但身份行在」或
    「体检内核报的是运行名」的结论全部落空——手动体检出健康节点却进不了池。
    """
    sub = int(subscription_id)
    for key in _name_keys(name):
        node = by_source.get((sub, key))
        if node is not None:
            return node
    for key in _name_keys(name):
        node = by_runtime.get(f"{sub}|{key}")
        if node is not None:
            return node
    node = by_runtime.get(name)
    if node is not None:
        return node
    # 名字可能带着池内前缀「订阅|原名」：拆出来按来源再试一次
    prefix, sep, rest = str(name or "").partition("|")
    if sep and prefix.isdigit():
        for key in _name_keys(rest):
            node = by_source.get((int(prefix), key))
            if node is not None:
                return node
    return None


def _apply_success(
    node: ProxyNode, *, exit_ip: str | None, latency_ms: int | None, now: datetime
) -> tuple[bool, bool]:
    """把一次成功体检作用到节点行，返回 (状态变了, 新拿到出口 IP)。"""
    state_changed = False
    target = evaluate_node_state(
        node.state, source_seen=True, probe_ok=True, consecutive_failures=0
    )
    if target != node.state:
        node.state = target
        state_changed = True
    node.consecutive_failures = 0
    exit_added = False
    if exit_ip:
        if not node.exit_ip:
            exit_added = True
        node.exit_ip = str(exit_ip)
        node.last_l1_at = now
    if isinstance(latency_ms, int) and latency_ms > 0:
        node.last_l2_at = now
    return state_changed, exit_added


def _write_observations(
    session: AsyncSession,
    node: ProxyNode,
    *,
    exit_ip: str | None,
    latency_ms: int | None,
    now: datetime,
    run_id: int | None = None,
    channel: str = "manual",
) -> None:
    """成功的体检落两条观测：L2 真实业务 +（有出口 IP 时）L1 出口身份。"""
    session.add(
        HealthObservation(
            node_id=node.node_id,
            level=L2_LEVEL,
            ok=True,
            latency_ms=latency_ms if isinstance(latency_ms, int) else None,
            detail=CLASH_STEAM_OK_DETAIL,
            observed_at=now,
            run_id=run_id,
            channel=channel,
        )
    )
    if exit_ip:
        session.add(
            HealthObservation(
                node_id=node.node_id,
                level=L1_LEVEL,
                ok=True,
                latency_ms=None,
                detail=CLASH_EXIT_IP_DETAIL,
                observed_at=now,
                run_id=run_id,
                channel=channel,
            )
        )


def _apply_steam_failure(node: ProxyNode, *, now: datetime) -> bool:
    """一次 Steam 业务失败证据：失败计数 +1，状态机按 fail-fast 判死
    （回 ACTIVE 靠恢复轮或下一轮成功证据——失败不写墓碑）。"""
    failures = (node.consecutive_failures or 0) + 1
    node.consecutive_failures = failures
    target = evaluate_node_state(
        node.state, source_seen=True, probe_ok=False,
        consecutive_failures=failures,
    )
    changed = target != node.state
    node.state = target
    node.last_l2_at = now
    return changed


async def ingest_node_check(
    session: AsyncSession,
    *,
    subscription_id: int,
    results: list[dict],
    now: datetime,
    run_id: int | None = None,
    channel: str = "manual",
) -> IngestionResult:
    """把一轮体检结果对齐进池账本。

    results 是 test_clash_nodes 的逐节点结果（含 name / alive / exitIp / ms）。
    存活节点按成功证据推进（复活 + 出口 IP 落账）；失败节点是**同一把
    Steam 有效响应推进业务健康；收到 HTTP 错误或无效业务正文只记为目标响应，
    不惩罚节点。只有 Steam 与出口身份探测都无法完成时才记节点传输失败。
    """
    by_source, by_runtime = await _load_index(session)
    outcome = IngestionResult()
    code = subscription_code(subscription_id)

    for row in results:
        if row.get("probed") is False:
            continue
        name = str(row.get("name") or "")
        node = _match(by_source, by_runtime, int(subscription_id), name)
        if node is None:
            if name:
                outcome.unmatched.append(name)
            continue
        outcome.matched += 1
        exit_ip = row.get("exitIp")
        latency_ms = row.get("ms")
        if not row.get("alive"):
            exit_added = bool(exit_ip and not node.exit_ip)
            if exit_ip:
                node.exit_ip = str(exit_ip)
                node.last_l1_at = now
                session.add(
                    HealthObservation(
                        node_id=node.node_id,
                        level=L1_LEVEL,
                        ok=True,
                        detail=CLASH_EXIT_IP_DETAIL,
                        observed_at=now,
                        run_id=run_id,
                        channel=channel,
                    )
                )
                if exit_added:
                    outcome.exit_ips_added += 1
            if row.get("steamStatus") is None and not exit_ip:
                if _apply_steam_failure(node, now=now):
                    outcome.deactivated += 1
            status = row.get("steamStatus")
            detail = (
                CLASH_STEAM_TRANSPORT_DETAIL
                if status is None and not exit_ip
                else CLASH_STEAM_INVALID_DETAIL if status == 200
                else CLASH_STEAM_HTTP_502_DETAIL if status == 502
                else CLASH_STEAM_HTTP_504_DETAIL if status == 504
                else CLASH_STEAM_HTTP_ERROR_DETAIL
            )
            session.add(
                HealthObservation(
                    node_id=node.node_id,
                    level=L2_LEVEL,
                    ok=False,
                    latency_ms=latency_ms if isinstance(latency_ms, int) else None,
                    detail=detail,
                    observed_at=now,
                    run_id=run_id,
                    channel=channel,
                )
            )
            row["sourceCode"] = code
            row["poolState"] = node.state
            continue
        state_changed, exit_added = _apply_success(
            node, exit_ip=exit_ip, latency_ms=latency_ms, now=now
        )
        if state_changed:
            outcome.activated += 1
        if exit_added:
            outcome.exit_ips_added += 1
        # 带 run_id 的轮次（手动体检/首检）逐节点落明细：HealthRun.total 数的
        # 是全部实探节点，明细只在变化时才写会让台账与流水对不上账；周期账本
        # 对齐（run_id=None）保持只记变化，避免每拍刷观测行。
        if state_changed or exit_added or run_id is not None:
            _write_observations(
                session, node, exit_ip=exit_ip, latency_ms=latency_ms, now=now,
                run_id=run_id, channel=channel,
            )
        row["sourceCode"] = code
        row["poolState"] = node.state

    return outcome


async def ingest_ledger(
    session: AsyncSession,
    *,
    now: datetime,
) -> IngestionResult:
    """将 ProxyNode 状态同步到旧 UI/节流表；不从旧表反向改变健康结论。"""
    from app.domains.proxypool.models import ProxyNode as RegistryNode

    nodes = list((await session.execute(select(RegistryNode))).scalars())
    observations: dict[str, HealthObservation] = {}
    if nodes:
        rows = (await session.execute(
            select(HealthObservation)
            .where(
                HealthObservation.node_id.in_([node.node_id for node in nodes]),
                HealthObservation.level == L2_LEVEL,
            )
            .order_by(HealthObservation.observed_at.desc(), HealthObservation.id.desc())
        )).scalars()
        for observation in rows:
            observations.setdefault(observation.node_id, observation)

    legacy_rows = list((await session.execute(select(ClashNode))).scalars())
    legacy_by_key = {(row.subscription_id, row.name): row for row in legacy_rows}
    outcome = IngestionResult()
    for node in nodes:
        if not node.original_name:
            continue
        source_id = node.subscription_id
        if source_id is None:
            prefix, separator, _ = str(node.runtime_name).partition("|")
            source_id = int(prefix) if separator and prefix.isdigit() else None
        if source_id is None:
            continue
        key = (int(source_id), str(node.original_name))
        row = legacy_by_key.get(key)
        if row is None:
            row = ClashNode(
                subscription_id=key[0], name=key[1], created_at=now,
            )
            session.add(row)
            legacy_by_key[key] = row
        prior_status, prior_exit = project_node_health(row, node)
        latest = observations.get(node.node_id)
        if latest is not None:
            row.latency_ms = latest.latency_ms
            if row.last_checked_at is None:
                row.last_checked_at = node.last_l2_at or latest.observed_at
        outcome.matched += 1
        if row.status == "ok" and prior_status != "ok":
            outcome.activated += 1
        if row.status == "dead" and prior_status != "dead":
            outcome.deactivated += 1
        if node.exit_ip and node.exit_ip != prior_exit:
            outcome.exit_ips_added += 1

    projected_keys = {
        (int(node.subscription_id) if node.subscription_id is not None else None,
         str(node.original_name or ""))
        for node in nodes
        if node.subscription_id is not None and node.original_name
    }
    projected_keys.update(
        (int(str(node.runtime_name).partition("|")[0]), str(node.original_name))
        for node in nodes
        if node.subscription_id is None
        and node.original_name
        and str(node.runtime_name).partition("|")[0].isdigit()
    )
    for key, row in legacy_by_key.items():
        if key in projected_keys:
            continue
        if row.status == "ok":
            outcome.deactivated += 1
        row.status = "unknown"
        row.fail_count = 0
        row.revive_passes = 0
        row.exit_ip = None
        row.latency_ms = None
    return outcome


async def rebind_missing_sources(
    session: AsyncSession, *, now: datetime, max_subs: int = 8
) -> tuple[int, ...]:
    """按每个已准入订阅的最新成功快照，补回缺失的来源关联。

    来源关联是「当前事实」，按快照整体替换。快照里存在、来源关联里缺失的节点
    说明这条订阅的来源关联丢过一拍——不补回，节点就以「无来源僵尸行」留在账本里
    （不合格、任何路径都探不到）。只对真的有缺口的订阅调 apply_snapshot，内容未变
    的轮次零成本空转；节点身份行不受影响（apply_snapshot 只对齐来源）。
    """
    from app.domains.proxies.models import ProxySubscription
    from app.domains.proxypool.admission import is_admitted
    from app.domains.proxypool.models import node_fingerprint, subscription_tag
    from app.domains.proxypool.registry import apply_snapshot
    from app.domains.proxypool.subscription import SnapshotRecoveryError, latest_snapshot

    subs = (
        await session.execute(
            select(ProxySubscription)
            .where(ProxySubscription.kind == "clash")
            .order_by(ProxySubscription.id)
        )
    ).scalars().all()

    repaired: list[int] = []
    for sub in subs[: max(1, int(max_subs))]:
        if not is_admitted(sub):
            continue
        try:
            snap = await latest_snapshot(session, int(sub.id))
        except SnapshotRecoveryError:
            continue
        if snap is None:
            continue
        tag = subscription_tag(open_url(sub.url) or "")
        wanted = {
            node_fingerprint(node, tag)
            for node in snap.nodes
            if not is_info_placeholder(str(node.get("name") or ""))
        }
        have = set(
            await session.scalars(
                select(ProxyNode.fingerprint).where(
                    ProxyNode.subscription_id == int(sub.id)
                )
            )
        )
        if not (wanted - have):
            continue
        await apply_snapshot(
            session, subscription_id=int(sub.id), snapshot=snap, now=now, tag=tag
        )
        repaired.append(int(sub.id))
    return tuple(repaired)


async def purge_orphan_subscription_sources(
    session: AsyncSession, *, now: datetime, max_subs: int = 8
) -> tuple[int, ...]:
    """清理指向已不存在订阅的来源关联与快照行（级联断账的存量自愈）。

    订阅删除级联生效前的存量：来源行还挂着已删除的订阅号，池合格判定把它们
    当「仍被提供」——只由已删订阅提供的健康节点因此一直占运行位。这是
    `rebind_missing_sources` 相对的另一侧修复（那边给现存订阅补缺失来源，
    这边删失主来源）：删来源行 + 快照行，来源全部流失的节点按注册表同一条
    收尾规则向状态机要判定（快照行随订阅走，retention 的「每订阅最新 3 条
    永不删」会把孤儿快照永远留住）。只 flush，不 commit。
    """
    from app.domains.proxies.models import ProxySubscription
    from app.domains.proxypool.registry import (
        lock_subscription_mutation,
        settle_nodes_after_source_change,
    )

    # 归属订阅已不存在（订阅被删但节点行没跟着清）的存量自愈
    orphan_ids = list(await session.scalars(
        select(ProxyNode.subscription_id)
        .where(ProxyNode.subscription_id.is_not(None))
        .where(ProxyNode.subscription_id.not_in(select(ProxySubscription.id)))
        .distinct()
    ))
    purged: list[int] = []
    for sub_id in orphan_ids[: max(1, int(max_subs))]:
        await lock_subscription_mutation(session, int(sub_id))
        node_ids = set(await session.scalars(
            select(ProxyNode.node_id).where(
                ProxyNode.subscription_id == int(sub_id)
            )
        ))
        await session.execute(
            update(ProxyNode)
            .where(ProxyNode.subscription_id == int(sub_id))
            .values(subscription_id=None)
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(SubscriptionSnapshot)
            .where(SubscriptionSnapshot.subscription_id == int(sub_id))
        )
        await settle_nodes_after_source_change(session, node_ids, now=now)
        purged.append(int(sub_id))
    return tuple(purged)
