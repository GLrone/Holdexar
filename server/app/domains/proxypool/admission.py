"""订阅级生产准入：CANDIDATE 只落快照，ACTIVE 才进 Registry。

语义边界（已锁定，别扩）：

- 准入是**订阅（入口）级**的编排角色。它**不是**「当前内核在跑哪条订阅」，**不是**
  Runtime `GLOBAL` 的选择，也**不是** `ProxyNode.state`（节点级健康证据）。
- `CANDIDATE`：`bootstrap.sync_subscriptions` 抓取 + 落快照后**停止**——不调用
  `apply_snapshot`，因此不产生 `ProxyNode`、不进 eligible / pool、
  不改 `pool_signature`、不触发 rebuild、不进 health，也不进作业台账的来源事实。
- `ACTIVE`：保持既有行为（`persist_snapshot → apply_snapshot`）。
- `deprecated` 与本角色**完全独立**：不互相转换、不互相跟随（前者是旧来源的可用性
  弃用，后者是新来源的准入结论）。

晋升（CANDIDATE → ACTIVE）**不是只改字段**：必须同事务完成
「读该订阅最近一次成功快照 → `apply_snapshot` → 置 `ADMISSION_ACTIVE`」，
否则会出现「库说 ACTIVE、Registry 里没有来源」的中间事实。
没有成功快照就**不允许**晋升——不重抓、不用失败快照、不用旧缓存、不猜数据。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log_event
from app.domains.proxies.models import (
    ADMISSION_ACTIVE,
    ADMISSION_CANDIDATE,
    ProxySubscription,
)
from app.domains.proxies.subscription_secret import UNREADABLE_MESSAGE, open_url
from app.domains.proxypool.models import (
    HealthObservation,
    ProxyNode,
    SubscriptionSnapshot,
    node_fingerprint,
    subscription_tag,
)
from app.domains.proxypool.registry import apply_snapshot, lock_subscription_mutation
from app.domains.proxypool.scheduling import request_rebuild
from app.domains.proxypool.subscription import (
    SnapshotRecoveryError,
    latest_snapshot,
)

logger = logging.getLogger(__name__)


class PromotionError(RuntimeError):
    """晋升不成立：订阅不存在 / 非 clash / 没有成功快照 / 快照不可恢复。"""


@dataclass(frozen=True)
class PromotionResult:
    """一次晋升的账目（`promoted=False` = 本来就是 ACTIVE，幂等空转）。"""

    subscription_id: int
    promoted: bool
    detail: str
    snapshot_sha256: str | None
    applied_nodes: int


def is_admitted(sub: ProxySubscription) -> bool:
    """生产准入判定。

    `NULL`（ALTER 之前的遗留行读不到值时）一律按 **ACTIVE**：兼容旧数据，
    绝不因为本切片把历史订阅降级成候选。
    """
    return (sub.admission_status or ADMISSION_ACTIVE) == ADMISSION_ACTIVE


@dataclass(frozen=True)
class ExitResult:
    """一次退出生产池的账目（`exited=False` = 本来就不在池里，幂等空转）。"""

    subscription_id: int
    exited: bool
    detail: str
    removed_sources: int
    degraded_nodes: int = 0


async def exit_from_production(
    session: AsyncSession,
    *,
    subscription_id: int,
    now: datetime,
) -> ExitResult:
    """把一个 ACTIVE 订阅撤出生产池：置回 CANDIDATE，并移除它的全部来源行。

    **只 flush，不 commit**：与晋升同一套事务约定，提交归调用方。

    事务顺序（不变量 `CANDIDATE ⇒ 它提供的节点行已全部删除`）：

        取写串行化边界 → 读订阅行 → 删本订阅节点行 → 置 CANDIDATE

    节点跟着订阅走，故本订阅提供的节点行**随退出物理删除**（连同其健康观测）。
    别的订阅的节点不受影响——它们的归属与身份都独立。
    """
    await lock_subscription_mutation(session, subscription_id)

    sub = await session.get(ProxySubscription, subscription_id)
    if sub is None:
        raise PromotionError(f"订阅不存在：{subscription_id}")
    if sub.kind != "clash":
        raise PromotionError(f"订阅 {subscription_id} 不是 clash 订阅，没有生产准入语义")
    if not is_admitted(sub):
        return ExitResult(subscription_id, False, "本来就不在池里", 0)

    from app.domains.proxypool.bootstrap import pool_signature

    before_sig = await pool_signature(session)
    source_node_ids = set(await session.scalars(
        select(ProxyNode.node_id)
        .where(ProxyNode.subscription_id == subscription_id)
    ))
    removed = len(source_node_ids)
    if source_node_ids:
        await session.execute(
            delete(HealthObservation)
            .where(HealthObservation.node_id.in_(source_node_ids))
        )
        await session.execute(
            delete(ProxyNode).where(ProxyNode.subscription_id == subscription_id)
        )
    sub.admission_status = ADMISSION_CANDIDATE
    await session.flush()

    if await pool_signature(session) != before_sig:
        request_rebuild()
        log_event(
            logger,
            f"订阅 {subscription_id} 退出生产池，合格集变化已请求重建",
            detail={"订阅编号": subscription_id, "删除节点行数": removed},
        )
    log_event(
        logger,
        f"订阅 {subscription_id} 退出生产池，置回候选",
        detail={"订阅编号": subscription_id, "删除节点行数": removed},
    )
    return ExitResult(subscription_id, True, "已退出生产池（置回 CANDIDATE）",
                      removed, removed)


async def promote_to_active(
    session: AsyncSession,
    *,
    subscription_id: int,
    data_dir: Path,
    now: datetime,
) -> PromotionResult:
    """把一个 CANDIDATE 订阅晋升为 ACTIVE，并把它最近一次成功快照 apply 进 Registry。

    **只 flush，不 commit**：状态与来源行必须落在**调用方的同一个事务**里
    （见模块 docstring 的中间事实说明）。调用方负责 `commit()` 或 `rollback()`。

    事务顺序（不变量 `ACTIVE ⇒ Registry 已应用当前 URL 的最新成功快照` 的关键）：

        取写串行化边界 → 读订阅行 → 读当前 URL 的最新成功快照 → apply → 置 ACTIVE

    **边界必须在读之前**：否则并发 sync 可能在"读完快照"与"apply"之间落盘一份更新的
    成功快照，让本次 apply 被「最新成功快照胜」守卫跳过（`skipped_stale`）——那时若仍
    写 ACTIVE，就会出现 `ACTIVE + Registry 无来源`。
    除边界本身，还有一道兜底：`skipped_stale` 一律**拒绝晋升**（保持 CANDIDATE）。
    """
    await lock_subscription_mutation(session, subscription_id)

    sub = await session.get(ProxySubscription, subscription_id)
    if sub is None:
        raise PromotionError(f"订阅不存在：{subscription_id}")
    if sub.kind != "clash":
        raise PromotionError(f"订阅 {subscription_id} 不是 clash 订阅，没有生产准入语义")
    if is_admitted(sub):
        return PromotionResult(subscription_id, False, "已是 ACTIVE", None, 0)

    url = open_url(sub.url)
    if not url:
        raise PromotionError(UNREADABLE_MESSAGE)
    try:
        snap = await latest_snapshot(session, subscription_id, url=url)
    except SnapshotRecoveryError as exc:
        raise PromotionError(f"最近一次成功快照不可恢复，拒绝晋升：{exc}") from exc
    if snap is None:
        any_ok = await session.scalar(
            select(func.count())
            .select_from(SubscriptionSnapshot)
            .where(
                SubscriptionSnapshot.subscription_id == subscription_id,
                SubscriptionSnapshot.status == "OK",
            )
        )
        if any_ok:
            raise PromotionError(
                "当前订阅 URL 还没有成功快照（换链接后必须重新成功抓取），拒绝晋升"
            )
        raise PromotionError(
            "没有成功快照，拒绝晋升（不重新抓取、不用失败快照或旧缓存）"
        )

    from app.domains.proxypool.bootstrap import pool_signature

    before_sig = await pool_signature(session)

    tag = subscription_tag(url)
    result = await apply_snapshot(
        session, subscription_id=subscription_id, snapshot=snap, now=now, tag=tag
    )
    if result.skipped_stale:
        raise PromotionError(
            "最近一次成功快照在晋升过程中已被更新的成功快照取代，拒绝晋升（保持 CANDIDATE）"
        )
    sub.admission_status = ADMISSION_ACTIVE
    await session.flush()

    applied = len({node_fingerprint(dict(n), tag) for n in snap.nodes})
    if await pool_signature(session) != before_sig:
        request_rebuild()
        log_event(
            logger,
            f"订阅 {subscription_id} 晋升后合格集变化，已请求重建",
            detail={"订阅编号": subscription_id},
        )
    log_event(
        logger,
        f"订阅 {subscription_id} 晋升为生产订阅，快照已应用",
        tag="成功",
        detail={
            "订阅编号": subscription_id,
            "快照": snap.sha256[:10],
            "对齐节点数": applied,
            "新建节点数": len(result.created_nodes),
            "刷新来源数": len(result.refreshed_nodes),
            "移除来源数": len(result.removed_sources),
        },
    )
    return PromotionResult(
        subscription_id, True, "晋升完成（快照已进 Registry）", snap.sha256, applied
    )


AUTO_ADMIT_MIN_ALIVE = 1


async def auto_admit_healthy_candidate(
    session: AsyncSession,
    *,
    subscription_id: int,
    data_dir: Path,
    now: datetime,
) -> PromotionResult | None:
    """候选订阅的健康自动转正：订阅最近一次体检有可用节点（≥ AUTO_ADMIT_MIN_ALIVE）
    就直接晋升进池；体检无可用节点（或还没体检过）的订阅保持候选。

    复用 `promote_to_active`（同一事务语义：只 flush 不 commit，由调用方提交）；
    晋升失败（快照未就绪 / 换链接后当前 URL 还没重抓成功等）时保持候选原样返回
    `None`——快照与状态不能被半截转正污染，等下一次体检 / 同步再试。
    """
    sub = await session.get(ProxySubscription, subscription_id)
    if sub is None or is_admitted(sub):
        return None
    stats = sub.last_stats if isinstance(sub.last_stats, Mapping) else {}
    try:
        alive = int(stats.get("alive") or 0)
    except (TypeError, ValueError):
        alive = 0
    if alive < AUTO_ADMIT_MIN_ALIVE:
        return None
    try:
        return await promote_to_active(
            session, subscription_id=subscription_id, data_dir=data_dir, now=now
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger,
            f"订阅 {subscription_id} 体检健康但暂不能转正，保持候选",
            tag="未完成",
            level=logging.WARNING,
            detail={
                "订阅编号": subscription_id,
                "可用节点数": alive,
                "原因": str(exc),
            },
        )
        return None