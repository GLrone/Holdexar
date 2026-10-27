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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import (
    ProxyNode,
    SubscriptionSnapshot,
    is_info_placeholder,
    make_runtime_name,
    node_fingerprint,
)
from app.domains.proxypool.state import (
    NODE_DEAD,
    NODE_NEW,
    NODE_STALE,
    apply_transition,
    evaluate_node_state,
)
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
        update(ProxyNode)
        .where(ProxyNode.subscription_id == subscription_id)
        .values(last_source_seen=ProxyNode.last_source_seen)
        .execution_options(synchronize_session=False)
    )


async def _minted_names(session: AsyncSession, subscription_id: int) -> set[str]:
    """本订阅铸过的名字。

    `runtime_name` 形如 `<订阅>|<名>`，按前缀筛即覆盖本订阅；已不再提供但行仍在
    的节点其名字同样被占用，漏掉会给新节点铸出同名（撞唯一约束或静默覆盖）。
    """
    rows = await session.execute(
        select(ProxyNode.runtime_name).where(
            ProxyNode.runtime_name.like(f"{subscription_id}|%")
        )
    )
    return {name for (name,) in rows.all()}


async def _exit_ip_indexes(
    session: AsyncSession,
) -> tuple[dict[str, str], dict[tuple[str, str, str], str]]:
    """(名字主干→出口IP) 与 (端点键→出口IP) 两张索引，一次按 `last_seen` 降序
    扫描构建：`setdefault` 保留首个写入 = 最近 seen 的化身；`exit_ip` 空行不进
    索引。批量登记时构建一次供全部新建节点共享，避免每个新指纹各做一次全表
    扫描把写闸按满。"""
    rows = await session.execute(select(ProxyNode).order_by(ProxyNode.last_seen.desc()))
    by_base: dict[str, str] = {}
    by_endpoint: dict[tuple[str, str, str], str] = {}
    for cand in rows.scalars():
        if not cand.exit_ip:
            continue
        base = _name_base(str((cand.normalized_config or {}).get("name") or ""))
        if base:
            by_base.setdefault(base, cand.exit_ip)
        endpoint = (
            str(cand.proxy_type or ""),
            str(cand.server or ""),
            str((cand.normalized_config or {}).get("port") or ""),
        )
        if all(endpoint):
            by_endpoint.setdefault(endpoint, cand.exit_ip)
    return by_base, by_endpoint


def _inheritable_exit_ip(
    indexes: tuple[dict[str, str], dict[tuple[str, str, str], str]],
    node: Mapping,
) -> str | None:
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
    by_base, by_endpoint = indexes
    if base and base in by_base:
        return by_base[base]
    if all(endpoint):
        return by_endpoint.get(endpoint)
    return None


def _name_base(name: str) -> str:
    """节点名主干：去掉机场内容版本后缀（`…#23` → `…`）与空白。"""
    import re

    return re.sub(r"#\d+\s*$", "", name).strip()


async def settle_nodes_after_source_change(
    session: AsyncSession, node_ids: Iterable[str], *, now: datetime
) -> tuple[tuple[str, str, str], ...]:
    """来源变动后的节点收尾（快照对齐 / 退出 / 删除路径共用，唯一实现）：

    仍归属订阅（`subscription_id` 非空）→ 推进 `last_source_seen`；归属被解除
    （订阅撤掉它、退出生产池或被删）→ 向状态机要一次判定（`probe_ok=None`）。
    只 flush，不 commit。
    """
    changes: list[tuple[str, str, str]] = []
    for node_id in sorted(node_ids):
        row = (
            await session.execute(select(ProxyNode).where(ProxyNode.node_id == node_id))
        ).scalar_one()
        if row.subscription_id is not None:
            row.last_source_seen = now
            # 来源仍在提供却停在 DEAD 的节点必须放出来重参选：它已经掉出池文件，
            # 池内核因此不认识它，体检只能拿到「名字没定位到（404）」，而 404 又
            # 坐实 DEAD —— 订阅每轮刷新都在提供它，状态却永远翻不回来。这里按
            # 「订阅还在给这个节点」这条来源证据把它放回 STALE（池成员，可被体检
            # 找到），由下一轮体检用真实结果决定 ACTIVE 还是继续 DEAD；不直接判
            # ACTIVE，避免未经体检就把流量放给一个刚失败过的出口。
            if row.state == NODE_DEAD:
                # 直接走合法转移表：evaluate_node_state 在 probe_ok=None 时
                # 刻意不让 DEAD 凭来源证据跃回运行集（避免未测节点进池），而这里
                # 正是要给一次重测机会——目标是 STALE（池成员）而非 ACTIVE，
                # 能否真正回 ACTIVE 交给下一轮体检的真实结果裁决。
                row.state = apply_transition(NODE_DEAD, NODE_STALE)
                row.consecutive_failures = 0
                changes.append((node_id, NODE_DEAD, NODE_STALE))
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
    return tuple(changes)


async def apply_snapshot(
    session: AsyncSession,
    *,
    subscription_id: int,
    snapshot: Snapshot,
    now: datetime,
    tag: str = "",
) -> ApplyResult:
    """把订阅 `subscription_id` 的来源证据整体对齐到 `snapshot` 的事实。

    `tag` 是该订阅的标识（`subscription_tag(url)`），进节点指纹——同一份配置
    由两个订阅提供时是两个节点。不传则退化为纯配置哈希（仅测试造数用）。
    """
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

    return await _apply_nodes(session, subscription_id=subscription_id, nodes=snapshot.nodes,
                              now=now, tag=tag)


async def _apply_nodes(
    session: AsyncSession, *, subscription_id: int, nodes: Iterable[Mapping],
    now: datetime, tag: str, restore_only: bool = False,
) -> ApplyResult:
    """登记节点身份；缓存恢复只补缺口，不刷新健康或替换当前来源集合。"""
    desired: dict[str, Mapping] = {}
    for node in nodes:
        # 信息占位节点（旧快照重放仍可能携带）不进 Registry——它们不是代理端点
        if is_info_placeholder(str(node.get("name") or "")):
            continue
        desired.setdefault(node_fingerprint(node, tag), node)
    desired_fp = set(desired)

    rows = (await session.execute(select(ProxyNode).where(
        (ProxyNode.subscription_id == subscription_id) | ProxyNode.node_id.in_(desired_fp)
    ))).scalars().all()
    existing = {row.node_id for row in rows if row.subscription_id == subscription_id}
    by_id = {row.node_id: row for row in rows}
    taken = await _minted_names(session, subscription_id)

    created: list[str] = []
    refreshed: list[str] = []
    touched: set[str] = set()
    # 出口继承索引：首个新建节点用到时才构建一次（纯刷新轮零成本），整批换
    # 指纹的场景共享同一份，避免每个新指纹各做一次全表扫描
    inherit_indexes: tuple[dict[str, str], dict[tuple[str, str, str], str]] | None = None

    for fingerprint, node in desired.items():
        original_name = str(node.get("name") or "")

        row = by_id.get(fingerprint)
        if row is not None:
            if restore_only:
                continue
            if row.subscription_id not in (None, subscription_id):
                raise ValueError("节点身份已归属其他订阅，未覆盖来源")
            row.subscription_id = subscription_id
            row.original_name = original_name
            row.normalized_config = dict(node)
            row.last_source_seen = now
            refreshed.append(fingerprint)
            touched.add(fingerprint)
            continue

        # 指纹含订阅标识 ⇒ 同订阅内重建的节点必是同一行；查不到就是新节点
        runtime_name = make_runtime_name(subscription_id, original_name, taken)
        taken.add(runtime_name)
        if inherit_indexes is None:
            inherit_indexes = await _exit_ip_indexes(session)
        row = ProxyNode(
            node_id=fingerprint,
            runtime_name=runtime_name,
            subscription_id=subscription_id,
            original_name=original_name,
            proxy_type=str(node.get("type") or ""),
            server=node.get("server"),
            normalized_config=dict(node),
            state=NODE_NEW,
            first_seen=now,
            last_seen=now,
            last_source_seen=None if restore_only else now,
            # 端点身份相同的旧节点携带着**已知的出口身份**：机场改名单 /
            # 换内容版本号（指纹变、type|server|port 不变）时把 exit_ip 继承
            # 过来——否则每次内容更新都要裸探一轮，空窗期内爬取全部被拒
            exit_ip=_inheritable_exit_ip(inherit_indexes, node),
        )
        session.add(row)
        created.append(fingerprint)
        touched.add(row.node_id)

    # 本订阅已不再提供的节点：解除归属（挂空订阅，由状态机按来源流失结算）。
    # 不删行——行是身份台账，且同一节点可能在别的订阅下另有自己的行。
    removed = [] if restore_only else [fp for fp in existing if fp not in desired_fp]
    if removed:
        for fingerprint in removed:
            by_id[fingerprint].subscription_id = None
        touched.update(removed)

    await session.flush()

    changes = () if restore_only else await settle_nodes_after_source_change(session, touched, now=now)
    return ApplyResult(
        created_nodes=tuple(created),
        refreshed_nodes=tuple(refreshed),
        removed_sources=tuple(removed),
        state_changes=tuple(changes),
    )
