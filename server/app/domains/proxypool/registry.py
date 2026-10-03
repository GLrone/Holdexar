"""节点来源账本（Snapshot → Registry）：只回答「谁还在提供这个节点」——
健康归 health 模块，池生成归 state。硬规则：①来源按本次快照的指纹集合
整体替换（不用有状态 diff，快照内重复指纹按集合去重）；②runtime_name
首次登记时铸造，此后任何刷新不重铸；③仅当来源全部流失才向状态机要一次
判定（probe_ok=None），仍被提供时不重判。

并发边界：apply 开头对该订阅来源行发同值 UPDATE 先取 SQLite 写锁——两份
快照的读改写无法交错（否则陈旧读算出空删除集，并集成既成事实）；写事务
覆盖到调用方提交，跨连接/进程成立。次序由「最新成功快照胜」（snapshot id
最大；更旧输入 skipped_stale，不以 fetched_at 为序）。事务归调用方：本模块
只 flush 不 commit。node_id 直接取 fingerprint。
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
    is_info_placeholder,
    make_runtime_name,
    node_fingerprint,
    subscription_code,
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


async def _inheritable_exit_ip(session: AsyncSession, node: Mapping) -> str | None:
    """旧化身**已知的出口 IP**：机场频繁改写节点条目（重排序 / 换入口服务器 /
    换内容版本号 `#N`），指纹与端点键都可能变，但**节点名主干**（去掉版本后缀）
    是机场自己的节点身份——同一主干的新化身继承旧化身的出口身份，避免每轮
    内容更新后池子裸探一轮、空窗期内爬取全拒。端点键相同也认（改名的情形）。

    只认**最近 seen** 的同主干化身；同主干都没有已知出口时返回 `None`（交给 L1 正常
    探测）。这是把已知事实带过内容更替，出口是否仍成立由 L1 持续复核。"""
    name = str(node.get("name") or "")
    base = _name_base(name)
    server = node.get("server")
    endpoint = (str(node.get("type") or ""), str(server or ""), str(node.get("port") or ""))
    if not base and not all(endpoint):
        return None
    rows = await session.execute(
        select(ProxyNode).order_by(ProxyNode.last_seen.desc())
    )
    fallback: str | None = None
    for cand in rows.scalars():
        if not cand.exit_ip:
            continue
        cand_base = _name_base(str((cand.normalized_config or {}).get("name") or ""))
        cand_endpoint = (
            str(cand.proxy_type or ""),
            str(cand.server or ""),
            str((cand.normalized_config or {}).get("port") or ""),
        )
        if base and cand_base == base:
            return cand.exit_ip
        if fallback is None and all(endpoint) and cand_endpoint == endpoint:
            fallback = cand.exit_ip
    return fallback


def _name_base(name: str) -> str:
    """节点名主干：去掉机场内容版本后缀（`…#23` → `…`）与空白。"""
    import re

    return re.sub(r"#\d+\s*$", "", name).strip()


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
        # 信息占位节点（旧快照重放仍可能携带）不进 Registry——它们不是代理端点
        if is_info_placeholder(str(node.get("name") or "")):
            continue
        desired.setdefault(node_fingerprint(node), node)
    desired_fp = set(desired)

    existing = await _sources_of(session, subscription_id)
    taken = await _minted_names(session, subscription_id)
    # 本订阅的特征码：写进每一条来源关联，体检结果据此对齐回池账本
    code = subscription_code(subscription_id)

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
            source.source_code = code
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
                # 端点身份相同的旧节点携带着**已知的出口身份**：机场改名单 /
                # 换内容版本号（指纹变、type|server|port 不变）时把 exit_ip 继承
                # 过来——否则每次内容更新都要裸探一轮，空窗期内爬取全部被拒
                exit_ip=await _inheritable_exit_ip(session, node),
            )
            session.add(row)
            created.append(fingerprint)

        session.add(
            ProxyNodeSource(
                node_id=row.node_id,
                subscription_id=subscription_id,
                original_name=original_name,
                source_code=code,
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