"""订阅级生产准入：CANDIDATE 只落快照，ACTIVE 才进 Registry。

语义边界（已锁定，别扩）：

- 准入是**订阅（入口）级**的编排角色。它**不是**「当前内核在跑哪条订阅」，**不是**
  Runtime `GLOBAL` 的选择，也**不是** `ProxyNode.state`（节点级健康证据）。
- `CANDIDATE`：`bootstrap.sync_subscriptions` 抓取 + 落快照后**停止**——不调用
  `apply_snapshot`，因此不产生 `ProxyNode` / `ProxyNodeSource`、不进 eligible / pool、
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxies.models import (
    ADMISSION_ACTIVE,
    ADMISSION_CANDIDATE,
    ProxySubscription,
)
from app.domains.proxypool.models import SubscriptionSnapshot, node_fingerprint
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
    # 本次晋升按快照对齐的**节点数**（去重后的指纹数）。注意它包含"给已有节点
    # 新增一个来源"的情形——那种情况 `ApplyResult.created/refreshed` 都是 0
    # （`created` 只算新建的节点行、`refreshed` 只算本订阅原本已有的来源），
    # 直接拿它们当"晋升生效了几个节点"会误读成 0。
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


async def exit_from_production(
    session: AsyncSession,
    *,
    subscription_id: int,
) -> ExitResult:
    """把一个 ACTIVE 订阅撤出生产池：置回 CANDIDATE，并移除它的全部来源行。

    **只 flush，不 commit**：与晋升同一套事务约定，提交归调用方。

    事务顺序（不变量 `CANDIDATE ⇒ 它的来源行已全部移除`）：

        取写串行化边界 → 读订阅行 → 删本订阅来源行 → 置 CANDIDATE

    `ProxyNode` 身份账本一行不动：没有任何当前来源的节点由合格集口径自然退出池，
    仍被别的订阅提供的节点不受影响。退出是**可逆**的——以后要重新进池再走一次
    Promote（用当前 URL 的最新成功快照），不物理删除订阅行。
    """
    await lock_subscription_mutation(session, subscription_id)

    sub = await session.get(ProxySubscription, subscription_id)
    if sub is None:
        raise PromotionError(f"订阅不存在：{subscription_id}")
    if sub.kind != "clash":
        raise PromotionError(f"订阅 {subscription_id} 不是 clash 订阅，没有生产准入语义")
    if not is_admitted(sub):
        return ExitResult(subscription_id, False, "本来就不在池里", 0)

    # 退出会改 eligible 集合 → 用**既有规则**判断要不要重建（与同步/晋升同一条判据）。
    # 函数内 import：`bootstrap` 反向依赖本模块的 `is_admitted`，模块级会成环。
    from app.domains.proxypool.bootstrap import pool_signature
    from app.domains.proxypool.models import ProxyNodeSource

    before_sig = await pool_signature(session)
    removed = int(await session.scalar(
        select(func.count())
        .select_from(ProxyNodeSource)
        .where(ProxyNodeSource.subscription_id == subscription_id)
    ) or 0)
    await session.execute(
        delete(ProxyNodeSource).where(ProxyNodeSource.subscription_id == subscription_id)
    )
    sub.admission_status = ADMISSION_CANDIDATE
    await session.flush()

    if await pool_signature(session) != before_sig:
        request_rebuild()
        logger.info(
            "[准入] 订阅 %s 退出生产池：移除来源 %d 行，eligible 变化 → request_rebuild()",
            subscription_id, removed,
        )
    logger.info("[准入] 订阅 %s 退出生产池（置回 CANDIDATE，来源行 %d）", subscription_id, removed)
    return ExitResult(subscription_id, True, "已退出生产池（置回 CANDIDATE）", removed)


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
    # ① 先取边界（写锁），此后到本事务提交为止不会有更新的快照落库
    await lock_subscription_mutation(session, subscription_id)

    # ② 边界内读：状态与快照都取"锁到手时"的最新事实
    sub = await session.get(ProxySubscription, subscription_id)
    if sub is None:
        raise PromotionError(f"订阅不存在：{subscription_id}")
    if sub.kind != "clash":
        raise PromotionError(f"订阅 {subscription_id} 不是 clash 订阅，没有生产准入语义")
    if is_admitted(sub):
        return PromotionResult(subscription_id, False, "已是 ACTIVE", None, 0)

    url = str(sub.url)
    try:
        snap = await latest_snapshot(session, subscription_id, url=url)
    except SnapshotRecoveryError as exc:
        # 行在但字节丢失 / sha 对不上 / 解不出来：fail-closed，不晋升
        raise PromotionError(f"最近一次成功快照不可恢复，拒绝晋升：{exc}") from exc
    if snap is None:
        # 区分两种"没有可用快照"：完全没抓成功过 / 抓成功过但不是**当前 URL** 的
        # （换链接后旧链接的快照不得顶替——那会让 Registry 与订阅 URL 对不上）
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

    # 晋升会改 eligible 集合 → 用**既有规则**判断要不要重建：
    # 与同步路径同一条判据（eligible 集合/签名是否变化），不另立第二套。
    # 函数内 import：`bootstrap` 反向依赖本模块的 `is_admitted`，模块级会成环。
    from app.domains.proxypool.bootstrap import pool_signature

    before_sig = await pool_signature(session)

    result = await apply_snapshot(
        session, subscription_id=subscription_id, snapshot=snap, now=now
    )
    if result.skipped_stale:
        # 兜底（边界内理论上不会发生）：手上有更新的成功快照时，绝不允许把 ACTIVE
        # 写下去——那正是「ACTIVE + Registry 无来源」。保持 CANDIDATE，由下一轮
        # 同步/晋升重新按最新快照来。
        raise PromotionError(
            "最近一次成功快照在晋升过程中已被更新的成功快照取代，拒绝晋升（保持 CANDIDATE）"
        )
    sub.admission_status = ADMISSION_ACTIVE
    await session.flush()

    applied = len({node_fingerprint(dict(n)) for n in snap.nodes})
    if await pool_signature(session) != before_sig:
        request_rebuild()
        logger.info(
            "[准入] 订阅 %s 晋升后 eligible 集合变化 → request_rebuild()（由既有重建链消费）",
            subscription_id,
        )
    logger.info(
        "[准入] 订阅 %s 晋升 ACTIVE：apply 快照 %s（对齐节点 %d；新建 %d / 刷新 %d / 撤来源 %d）",
        subscription_id,
        snap.sha256[:10],
        applied,
        len(result.created_nodes),
        len(result.refreshed_nodes),
        len(result.removed_sources),
    )
    return PromotionResult(
        subscription_id, True, "晋升完成（快照已进 Registry）", snap.sha256, applied
    )
