"""P1.3-A：Snapshot → Registry —— 只登记事实。

本模块只回答一个问题：**「谁还在提供这个节点？」**
- 「这个节点还能不能用？」归健康模块（探测、失败计数、退休阈值，本模块一概不碰）
- 「该把哪些节点给 Mihomo？」归池生成（`state` 是它的输入，不是它的职责）

三条硬规则：

1. **来源整体替换，不读 Diff**：本订阅的行按「本次快照的指纹集合」整体对齐——
   新指纹插入、仍在的刷新、已不再提供的删除。`apply_snapshot` 的签名里没有
   `Diff`：diff 是**有状态的**（语义依赖上一份基线被正确持久化），一次回滚就会让
   误差固化并逐轮累积；快照替换是无状态的，每轮从完整事实重算。快照内的重复
   指纹按**集合**去重（`name` 不参与身份，同一线路挂两个名字天然撞同一指纹），
   首次出现的写法胜出——它是数据事实，不该以 `UNIQUE` 异常收场。
2. **身份不重铸**：`runtime_name` 只在节点首次登记时铸造，此后任何刷新都不重算——
   重算会让已绑定的 lane / 池文件条目指向一个不存在的名字。第二名提供它的订阅
   直接复用该名字。
3. **状态只在一个时点动作**：仅当某节点的来源**全部**流失时，才向状态机要一次
   判定（`probe_ok=None`，即"没有新的健康证据"）。这落在已封板的语义上：
   `NEW/DEAD → DEAD`、`ACTIVE → STALE`、`STALE → STALE`、`RETIRED → RETIRED`。
   仍被提供时**不重新判定**——`NEW` 要保持 `NEW`，直到健康模块给出证据。

事务边界：本函数只 `flush()`，**不 commit**——与 `persist_snapshot` 同一约定，
调用方拥有事务（后续池发布需要把账本行与来源替换放在同一个事务里）。

**并发边界（同一订阅的 Snapshot → Registry mutation 串行化）**：函数开头对该订阅的
来源行发一条**同值 UPDATE**——它不改任何数据，只为在本事务里**先取 SQLite 的写锁**，
再开始读「现有来源 / 已用名字」。SQLite 单写者模型下，第二个 apply 的读改写就无法
插进第一个的读与写之间：否则后者会基于陈旧读算出**空删除集**，把两份快照的来源
并集成既成事实（曾真实复现：{1,2} 与 {3,4} 交错 → 最终 {1,2,3,4}）。

边界选在**写事务**而不是进程内锁，理由是：事务与提交归调用方（调度器 job 与 HTTP
请求各自 commit），锁必须覆盖提交才成立；写事务天然覆盖到提交，且跨连接、跨进程
同样成立。写锁窗口本身与改动前一致（从本函数的第一次写到调用方提交），只是把
「第一次写」提前到「读之前」——毫秒级，不新增阻塞面。

写锁只保证**不并集**，不保证**次序**，所以另有第二条规则：**最新成功快照胜**
——「最新」= **成功落库顺序**（`SubscriptionSnapshot.id` 最大者，与
`latest_snapshot()` / `retention` 同一口径）；输入比它更旧时整体跳过
（`skipped_stale`）。否则两轮并发同步里「先抓到的后到」会让 Registry 退回旧内容，
而订阅的 `snapshot_sha256` / `snapshot_version` 投影却指向更新的那份，两边对不上。
（**不以 `fetched_at` 为序**：抓取时刻与落库顺序在慢请求下会给出相反结论。）

`node_id` 直接取 `fingerprint`：身份就是整份配置的哈希，`node_id` 只是这一行的
句柄，另造一个截断值等于多开一个碰撞域，没有任何好处。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import (
    ProxyNode,
    ProxyNodeSource,
    SubscriptionSnapshot,
    make_runtime_name,
    node_fingerprint,
)
from app.domains.proxypool.state import NODE_NEW, evaluate_node_state
from app.domains.proxypool.subscription import Snapshot


@dataclass(frozen=True)
class ApplyResult:
    """一次登记的账目摘要（供日志与测试断言，不参与任何状态决策）。"""

    created_nodes: tuple[str, ...] = ()
    refreshed_nodes: tuple[str, ...] = ()
    removed_sources: tuple[str, ...] = ()
    # (node_id, 原状态, 新状态)：只包含本次真的发生变化的
    state_changes: tuple[tuple[str, str, str], ...] = ()
    # 输入快照比已落库的最新成功快照更旧 → 整体跳过（见「顺序语义」注释）
    skipped_stale: bool = False


async def lock_subscription_mutation(session: AsyncSession, subscription_id: int) -> None:
    """取该订阅 Registry 变更的**串行化边界**（SQLite 写锁），不改任何数据。

    同值 UPDATE 只为在本事务里开写事务：SQLite 单写者下，之后任何写者都要等到本
    事务提交。**要求"读快照 → 写 Registry"整体原子的调用方，必须在读之前先调它**
    ——否则会出现：promote 读到 Snapshot A → 并发 sync 落盘更新的 B → promote 的 A
    被「最新成功快照胜」守卫跳过 → 却仍写 ACTIVE ⇒ ACTIVE + Registry 空。

    在同一事务里重复调用是幂等的（第二条 UPDATE 命中同一写事务）。
    """
    await session.execute(
        update(ProxyNodeSource)
        .where(ProxyNodeSource.subscription_id == subscription_id)
        .values(last_seen=ProxyNodeSource.last_seen)
        .execution_options(synchronize_session=False)
    )


async def _minted_names(session: AsyncSession, subscription_id: int) -> set[str]:
    """本订阅铸过的名字。

    必须包含**来源已流失但节点仍存在**的那些：它们的 `runtime_name` 仍被占用，
    若漏掉就会给新节点铸出同名（撞全局唯一约束，或静默覆盖别人的名字）。
    `runtime_name` 形如 `<订阅>|<名>`，故按前缀筛即完整覆盖本订阅。
    """
    rows = await session.execute(
        select(ProxyNode.runtime_name).where(
            ProxyNode.runtime_name.like(f"{subscription_id}|%")
        )
    )
    return {name for (name,) in rows.all()}


async def _sources_of(session: AsyncSession, subscription_id: int) -> dict[str, str]:
    """本订阅现有来源 → {指纹: node_id}。"""
    rows = await session.execute(
        select(ProxyNodeSource.node_id, ProxyNode.fingerprint)
        .join(ProxyNode, ProxyNode.node_id == ProxyNodeSource.node_id)
        .where(ProxyNodeSource.subscription_id == subscription_id)
    )
    return {fingerprint: node_id for node_id, fingerprint in rows.all()}


async def apply_snapshot(
    session: AsyncSession,
    *,
    subscription_id: int,
    snapshot: Snapshot,
    now: datetime,
) -> ApplyResult:
    """把订阅 `subscription_id` 的来源证据整体对齐到 `snapshot` 的事实。"""
    # 串行化边界：先取写锁，再读（见模块 docstring「并发边界」）。同值 UPDATE 不改数据。
    await lock_subscription_mutation(session, subscription_id)

    # 顺序语义：**最新成功快照胜**，判据必须与 `latest_snapshot()` **同源**——
    # 「成功落库顺序」= `SubscriptionSnapshot.id` 最大者（`latest_snapshot` 与
    # `retention` 都是按 id desc 取的）。
    # **不能用 `fetched_at`**：慢请求会"先抓取、后落库"（A 抓取早但落库晚），
    # 那时 id 判据说 A 最新、fetched_at 判据说 A 更旧 → A 被跳过，而且
    # `latest_snapshot()` 永远返回 A ⇒ 晋升会永久失败直到下一轮同步。
    # 找不到对应行（调用方直接 apply 一份尚未落库的快照，如单测）→ 不做陈旧判定。
    incoming_id = await session.scalar(
        select(func.max(SubscriptionSnapshot.id)).where(
            SubscriptionSnapshot.subscription_id == subscription_id,
            SubscriptionSnapshot.status == "OK",
            SubscriptionSnapshot.sha256 == snapshot.sha256,
        )
    )
    if incoming_id is not None:
        newest_id = await session.scalar(
            select(func.max(SubscriptionSnapshot.id)).where(
                SubscriptionSnapshot.subscription_id == subscription_id,
                SubscriptionSnapshot.status == "OK",
            )
        )
        if newest_id is not None and incoming_id < newest_id:
            return ApplyResult((), (), (), (), skipped_stale=True)

    desired: dict[str, Mapping] = {}
    for node in snapshot.nodes:
        desired.setdefault(node_fingerprint(node), node)
    desired_fp = set(desired)

    existing = await _sources_of(session, subscription_id)
    taken = await _minted_names(session, subscription_id)

    created: list[str] = []
    refreshed: list[str] = []
    touched: set[str] = set()

    for fingerprint, node in desired.items():
        original_name = str(node.get("name") or "")

        if fingerprint in existing:
            node_id = existing[fingerprint]
            source = (
                await session.execute(
                    select(ProxyNodeSource).where(
                        ProxyNodeSource.subscription_id == subscription_id,
                        ProxyNodeSource.node_id == node_id,
                    )
                )
            ).scalar_one()
            source.original_name = original_name
            source.last_seen = now
            refreshed.append(fingerprint)
            touched.add(node_id)
            continue

        # 新来源：节点身份可能已由别的订阅登记过——那就复用，不重铸任何东西
        row = (
            await session.execute(
                select(ProxyNode).where(ProxyNode.fingerprint == fingerprint)
            )
        ).scalar_one_or_none()
        if row is None:
            runtime_name = make_runtime_name(subscription_id, original_name, taken)
            taken.add(runtime_name)
            row = ProxyNode(
                node_id=fingerprint,
                fingerprint=fingerprint,
                runtime_name=runtime_name,
                proxy_type=str(node.get("type") or ""),
                server=node.get("server"),
                normalized_config=dict(node),
                state=NODE_NEW,
                first_seen=now,
                last_seen=now,
                last_source_seen=now,
            )
            session.add(row)
            created.append(fingerprint)

        session.add(
            ProxyNodeSource(
                node_id=row.node_id,
                subscription_id=subscription_id,
                original_name=original_name,
                first_seen=now,
                last_seen=now,
            )
        )
        touched.add(row.node_id)

    # 本订阅已不再提供的来源：删掉（来源是「当前事实」，不是历史台账）
    removed = [fp for fp in existing if fp not in desired_fp]
    if removed:
        removed_ids = [existing[fp] for fp in removed]
        await session.execute(
            delete(ProxyNodeSource).where(
                ProxyNodeSource.subscription_id == subscription_id,
                ProxyNodeSource.node_id.in_(removed_ids),
            )
        )
        touched.update(removed_ids)

    await session.flush()

    changes: list[tuple[str, str, str]] = []
    for node_id in sorted(touched):
        row = (
            await session.execute(select(ProxyNode).where(ProxyNode.node_id == node_id))
        ).scalar_one()
        remaining = await session.scalar(
            select(func.count())
            .select_from(ProxyNodeSource)
            .where(ProxyNodeSource.node_id == node_id)
        )
        if remaining:
            # last_source_seen 的语义是「全部来源 last_seen 的最大值」——本轮本订阅
            # 撤掉、别的订阅没刷新时绝不能写 now：那会把节点的"最后见到"时刻凭空
            # 推到本轮，STALE 候选按新鲜度筛选会因此误判。
            latest = await session.scalar(
                select(func.max(ProxyNodeSource.last_seen)).where(
                    ProxyNodeSource.node_id == node_id
                )
            )
            row.last_source_seen = latest or now
            continue
        target = evaluate_node_state(
            row.state,
            source_seen=False,
            probe_ok=None,
            consecutive_failures=row.consecutive_failures or 0,
        )
        if target != row.state:
            changes.append((node_id, row.state, target))
            row.state = target

    await session.flush()
    return ApplyResult(
        created_nodes=tuple(created),
        refreshed_nodes=tuple(refreshed),
        removed_sources=tuple(removed),
        state_changes=tuple(changes),
    )