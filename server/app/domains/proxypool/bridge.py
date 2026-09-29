"""订阅节点体检 → 池账本：把体检结论对齐到 Registry。

前端「节点体检」探的是**生产端点**（IStoreBrowseService/GetItems）与出口 IP，
这正是池健康账本的 L2（真实业务）与 L1（出口身份）证据。本模块把它们按
「订阅来源」对齐到 Registry：

    (subscription_id, 节点名)  →  ProxyNodeSource  →  ProxyNode

命中节点按成功证据推进状态（evaluate_node_state(probe_ok=True)）、清零失败
计数、带上出口 IP 与 L1/L2 观测。判定标准只有一条：**体检通过的健康出口节点
必须被池用上**——体检是比池内 L0 更强的证据（L0 只证明内核能连，L2 证明能服务
生产流量），所以它能复活池里的 DEAD / RETIRED 行。

不做的事：不新建调度、不新建生命周期、不重铸名字、不降级节点。合格集或出口集
变化只置既有的 rebuild_pending，重建仍归既有的池重建通道。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxies.models import ClashNode
from app.domains.proxypool.health import L1_LEVEL, L2_LEVEL
from app.domains.proxypool.models import (
    HealthObservation,
    ProxyNode,
    ProxyNodeSource,
    is_info_placeholder,
    subscription_code,
)
from app.domains.proxypool.registry import _name_base
from app.domains.proxypool.state import evaluate_node_state

logger = logging.getLogger(__name__)

# 落库的体检结论只在这个窗口内当作「当前健康证据」。更早的 ok 行代表过去
# 某一刻的事实，不能直接当今天的健康——池内 L0/L2 每拍会重新给出结论。
LEDGER_FRESH_HOURS = 72

# 观测明细标签（HealthObservation.detail 是固定枚举，不放自由文本）
CLASH_STEAM_OK_DETAIL = "CLASH_STEAM_OK"
CLASH_EXIT_IP_DETAIL = "CLASH_EXIT_IP"


@dataclass
class IngestionResult:
    """一次对齐的账目摘要（只记事实，不下结论）。"""

    matched: int = 0
    activated: int = 0
    exit_ips_added: int = 0
    unmatched: list[str] = field(default_factory=list)
    skipped_stale: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.activated or self.exit_ips_added)


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
    rows = (
        await session.execute(
            select(ProxyNodeSource, ProxyNode).join(
                ProxyNode, ProxyNode.node_id == ProxyNodeSource.node_id
            )
        )
    ).all()
    by_source: dict[tuple[int, str], ProxyNode] = {}
    for source, node in rows:
        by_source[(int(source.subscription_id), str(source.original_name))] = node
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
            )
        )


async def ingest_node_check(
    session: AsyncSession,
    *,
    subscription_id: int,
    results: list[dict],
    now: datetime,
) -> IngestionResult:
    """把一轮体检的存活节点对齐进池账本。

    results 是 test_clash_nodes 的逐节点结果（含 name / alive / exitIp / ms）。
    只处理存活节点——失败不在这里降级，池内 L0/L2 自有状态机；体检的价值是
    「证明它现在能服务生产流量」。
    """
    by_source, by_runtime = await _load_index(session)
    outcome = IngestionResult()
    code = subscription_code(subscription_id)

    for row in results:
        if not row.get("alive"):
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
        state_changed, exit_added = _apply_success(
            node, exit_ip=exit_ip, latency_ms=latency_ms, now=now
        )
        if state_changed:
            outcome.activated += 1
        if exit_added:
            outcome.exit_ips_added += 1
        if state_changed or exit_added:
            _write_observations(
                session, node, exit_ip=exit_ip, latency_ms=latency_ms, now=now
            )
        row["sourceCode"] = code
        row["poolState"] = node.state

    return outcome


async def ingest_ledger(
    session: AsyncSession,
    *,
    now: datetime,
    fresh_hours: int = LEDGER_FRESH_HOURS,
) -> IngestionResult:
    """把已落库的节点体检结论对齐进池账本（覆盖全部订阅）。

    前端点一次体检只覆盖当时内核在跑的那条订阅；clash_nodes 账本里还留着
    其它订阅的最近结论。本函数按来源把它对齐进池，让「每一条已证健康的出口
    节点」都参与容量，而不是只有当前内核那条订阅。
    """
    cutoff = now - timedelta(hours=max(1, int(fresh_hours)))
    by_source, by_runtime = await _load_index(session)
    rows = (
        await session.execute(select(ClashNode).where(ClashNode.status == "ok"))
    ).scalars().all()

    outcome = IngestionResult()
    for row in rows:
        checked_at = row.last_checked_at
        if checked_at is not None and checked_at < cutoff:
            outcome.skipped_stale += 1
            continue
        node = _match(by_source, by_runtime, int(row.subscription_id), str(row.name))
        if node is None:
            continue
        # 池内已有**更新**的 L0 结论时，不用更早的体检结论覆盖它：否则
        # 「L0 判死 → 体检账本再复活」会每拍互相推翻，节点状态永远翻烧饼。
        if (
            node.last_l0_at is not None
            and checked_at is not None
            and node.last_l0_at > checked_at
        ):
            continue
        outcome.matched += 1
        state_changed, exit_added = _apply_success(
            node, exit_ip=row.exit_ip, latency_ms=row.latency_ms, now=now
        )
        if state_changed:
            outcome.activated += 1
        if exit_added:
            outcome.exit_ips_added += 1
        if state_changed or exit_added:
            _write_observations(
                session, node, exit_ip=row.exit_ip, latency_ms=row.latency_ms, now=now
            )
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
    from app.domains.proxypool.models import node_fingerprint
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
        wanted = {
            node_fingerprint(node)
            for node in snap.nodes
            if not is_info_placeholder(str(node.get("name") or ""))
        }
        have = set(
            await session.scalars(
                select(ProxyNode.fingerprint)
                .join(ProxyNodeSource, ProxyNode.node_id == ProxyNodeSource.node_id)
                .where(ProxyNodeSource.subscription_id == int(sub.id))
            )
        )
        if not (wanted - have):
            continue
        await apply_snapshot(session, subscription_id=int(sub.id), snapshot=snap, now=now)
        repaired.append(int(sub.id))
    return tuple(repaired)
