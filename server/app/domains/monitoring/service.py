"""monitoring 域服务：来源 → 监控对象 → crawl 资格 的生命周期。

三层语义（资格直接读取来源与排除）：

- **Source**（`monitor_sources`）：谁想监控它。家族愿望单只是来源之一。
- **Monitoring**（`monitor_targets`）：在册身份与生命周期时刻，状态由来源和排除计算。
- **Crawl Queue**：这一轮爬谁、按什么顺序。由 crawl 域按 Monitoring 的
  状态与优先级决定，本域不参与排队。

资格门的判定顺序是固定的：

1. 有 active exclusion → `excluded`（无论来源如何，不进 crawl）
2. 无任何 active source → `released`（留在 Catalog，不进 crawl）
3. 否则 → `active`，priority = 有效来源里的最高优先级

排除与来源优先级不合并成一个数字：优先级只决定队列先后，资格只由
state 表达。新增来源只需在 `SOURCE_PRIORITY` 登记，不改判定逻辑。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import bindparam, delete, func, select

from app.core.database import WritePriority, get_session_factory, write_session
from app.domains.monitoring.models import (
    NON_CRAWL_STATES,
    STATE_ACTIVE,
    STATE_EXCLUDED,
    STATE_RELEASED,
    MonitorExclusion,
    MonitorSource,
    MonitorTarget,
    TARGET_BUNDLE,
    TARGET_GAME,
)

logger = logging.getLogger(__name__)

# 来源优先级：只决定 crawl 队列先后，不决定监控资格。
# 星标关注与家族愿望单同属第一优先级组（关注略先），手动入池 / 已购 /
# 导入 / 榜单属第二优先级——沿用监控条目排序的既有档位。
SOURCE_PRIORITY: dict[str, int] = {
    "favorite": 100,         # 星标关注（follows 直挂）
    "family_wishlist": 95,   # 家族 / 多账号愿望单（wishlist_items.wishlisted）
    "manual": 60,            # 手动入池（add_pool_items 直挂）
    "owned": 40,             # 已购同步（wishlist_items.owned）
    "import": 30,            # 捆绑包导入（bundles/refresh.import_bundle）
    "board": 20,             # 榜单发现源落池（ensure_board_pool 直挂）
    # 外部社区「最受期待」榜落池（admin 侧脚本经 ensure_board_pool(source=...) 推）。
    # 与 board 同档但**必须是独立来源**：board 只代表 Steam 官方榜单，两者共用
    # 一个标签会让榜单语义混入外部清单。
    "anticipated": 20,
}
DEFAULT_SOURCE_PRIORITY = 10

# wishlist_items 的来源布尔列 → monitoring source（多账户同 appid 去重后取并
# 集）。项目自加来源（favorite / manual / board）由各自动作直写监控层，不在
# 账户对账的输入之列。
_WISHLIST_SOURCE_FLAGS: tuple[tuple[str, str], ...] = (
    ("wishlisted", "family_wishlist"),
    ("owned", "owned"),
)

# 账号派生来源：由 wishlist_items（Steam 账户来源数据）对账产生，账号同步时
# 按现状重算。用户显式来源（favorite / manual）与榜单落池（board）各有直接
# 管理者——对账只收敛本集合内的种类，不会把它们洗掉。
DERIVED_SOURCES: frozenset[str] = frozenset({"family_wishlist", "owned"})

# 单次批量同步的 appid 数（SQLite 变量数上限防御）
_SYNC_CHUNK = 400


def _now() -> datetime:
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)


def _priority_of(source: str) -> int:
    return SOURCE_PRIORITY.get(source, DEFAULT_SOURCE_PRIORITY)


# ── 内部：在册身份与生命周期 ──────────────────────────────────


async def _target_states(
    session, target_type: str, target_ids: list[int],
) -> dict[int, tuple[str, int]]:
    rows = await session.execute(
        select(MonitorTarget.target_id, MonitorTarget.state, MonitorTarget.priority).where(
            MonitorTarget.target_type == target_type, MonitorTarget.target_id.in_(target_ids),
        )
    )
    return {int(tid): (state, int(priority)) for tid, state, priority in rows}


async def _ensure_targets(
    session, target_type: str, target_ids: list[int], now: datetime,
) -> dict[int, tuple[str, int]]:
    previous = await _target_states(session, target_type, target_ids)
    missing = [
        {"target_type": target_type, "target_id": tid, "created_at": now, "updated_at": now}
        for tid in target_ids if tid not in previous
    ]
    if missing:
        await session.execute(MonitorTarget.__table__.insert(), missing)
    return previous


async def _record_lifecycle(
    session, target_type: str, target_ids: list[int],
    previous: dict[int, tuple[str, int]], now: datetime,
) -> dict[int, str]:
    """记录原有生命周期时刻；资格和排序本身不落副本。"""
    current = await _target_states(session, target_type, target_ids)
    changes: dict[str, list[dict]] = {}
    for tid, (state, priority) in current.items():
        if previous.get(tid, (STATE_RELEASED, 0)) != (state, priority):
            changes.setdefault(state, []).append({"_target_id": tid})
    for state, records in changes.items():
        column = {STATE_ACTIVE: "activated_at", STATE_RELEASED: "released_at", STATE_EXCLUDED: "excluded_at"}[state]
        await session.execute(
            MonitorTarget.__table__.update().where(
                MonitorTarget.target_type == target_type,
                MonitorTarget.target_id == bindparam("_target_id"),
            ).values(updated_at=now, **{column: now}), records,
        )
    return {tid: state for tid, (state, _) in current.items()}


async def _clear_exclusions(session, target_type: str, target_ids: list[int], now: datetime) -> None:
    await session.execute(MonitorExclusion.__table__.update().where(
        MonitorExclusion.target_type == target_type, MonitorExclusion.target_id.in_(target_ids),
        MonitorExclusion.active.is_(True),
    ).values(active=False, cleared_at=now))


# ── 对外：来源维护 ────────────────────────────────────────────


async def sync_sources(
    target_type: str,
    desired: dict[int, set[str]],
    *,
    managed: set[str] | frozenset[str] | None = None,
    clear_exclusion: bool = False,
) -> dict[int, str]:
    """按给定来源集合幂等重算一批监控对象（多账户去重后的并集）。

    `desired` 里没出现的来源一律停用——但只停用 `managed` 内的来源种类
    （None = 全部）。账号派生来源（`DERIVED_SOURCES`）由账户对账管理；
    用户显式来源（favorite / manual）由各自动作直接挂摘，不被对账覆盖。
    出现但尚未落行的新建。返回 {target_id: 重算后的 state}。
    """
    if not desired:
        return {}
    now = _now()
    priority = WritePriority.INTERACTIVE if clear_exclusion else WritePriority.BACKGROUND
    async with write_session(priority, label="monitoring") as session:
        targets = list(desired)
        previous = await _ensure_targets(session, target_type, targets, now)
        if clear_exclusion:
            await _clear_exclusions(session, target_type, targets, now)
        existing: dict[int, dict[str, MonitorSource]] = {}
        for row in (
            await session.execute(
                select(MonitorSource).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id.in_(targets),
                )
            )
        ).scalars():
            existing.setdefault(int(row.target_id), {})[row.source] = row
        added = []
        for target_id, sources in desired.items():
            sources = {s for s in (sources or set()) if s}
            if managed is not None:
                sources = {s for s in sources if s in managed}
            current = existing.get(target_id, {})
            for src in sources:
                row = current.get(src)
                if row is None:
                    added.append({
                        "target_type": target_type, "target_id": target_id, "source": src,
                        "priority": _priority_of(src), "active": True,
                        "created_at": now, "updated_at": now,
                    })
                elif not row.active:
                    row.active = True
                    row.priority = _priority_of(src)
                    row.updated_at = now
            for src, row in current.items():
                if managed is not None and src not in managed:
                    continue
                if src not in sources and row.active:
                    row.active = False
                    row.updated_at = now
        if added:
            await session.execute(MonitorSource.__table__.insert(), added)
        result = await _record_lifecycle(session, target_type, targets, previous, now)
        await session.commit()
    return result


async def ensure_source(
    target_type: str, target_id: int, source: str, *, clear_exclusion: bool = False,
) -> str:
    """确保某个来源为激活态，**不动该对象的其它来源**。

    与 `attach_source` 的分工：attach 把来源集合收敛成只含这一个（切换来源
    语义）；ensure 只加不减——用户显式动作（关注 / 手动入池）用它，免得
    把 Steam 愿望单等来源顺手洗掉。
    """
    now = _now()
    target_id = int(target_id)
    async with write_session(WritePriority.INTERACTIVE, label="monitoring") as session:
        previous = await _ensure_targets(session, target_type, [target_id], now)
        if clear_exclusion:
            await _clear_exclusions(session, target_type, [target_id], now)
        row = (
            await session.execute(
                select(MonitorSource).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == int(target_id),
                    MonitorSource.source == source,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                MonitorSource(
                    target_type=target_type,
                    target_id=int(target_id),
                    source=source,
                    priority=_priority_of(source),
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        elif not row.active:
            row.active = True
            row.priority = _priority_of(source)
            row.updated_at = now
        state = (await _record_lifecycle(session, target_type, [target_id], previous, now))[target_id]
        await session.commit()
    return state


async def ids_with_source(target_type: str, source: str) -> list[int]:
    """带某个激活来源的对象 id（升序去重）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorSource.target_id)
                .where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.source == source,
                    MonitorSource.active.is_(True),
                )
                .distinct()
                .order_by(MonitorSource.target_id)
            )
        ).all()
    return [int(r[0]) for r in rows]


async def sources_map(
    target_type: str, target_ids: list[int]
) -> dict[int, set[str]]:
    """批量取激活来源集合（列表页派生「来源标记」用，避免逐条查询）。"""
    clean = [int(t) for t in (target_ids or [])]
    if not clean:
        return {}
    out: dict[int, set[str]] = {}
    async with get_session_factory()() as session:
        for i in range(0, len(clean), _SYNC_CHUNK):
            chunk = clean[i : i + _SYNC_CHUNK]
            rows = (
                await session.execute(
                    select(MonitorSource.target_id, MonitorSource.source).where(
                        MonitorSource.target_type == target_type,
                        MonitorSource.target_id.in_(chunk),
                        MonitorSource.active.is_(True),
                    )
                )
            ).all()
            for tid, src in rows:
                out.setdefault(int(tid), set()).add(str(src))
    return out


async def attach_source(target_type: str, target_id: int, source: str) -> str:
    """挂上一个来源。已排除的对象不会因此复活——解除排除是独立动作。"""
    return (await sync_sources(target_type, {int(target_id): {source}}))[int(target_id)]


async def detach_source(target_type: str, target_id: int, source: str) -> str:
    """摘掉一个来源；最后一个来源摘掉后状态转 released。"""
    target_id = int(target_id)
    now = _now()
    async with write_session(WritePriority.INTERACTIVE, label="monitoring") as session:
        previous = await _ensure_targets(session, target_type, [target_id], now)
        row = (
            await session.execute(
                select(MonitorSource).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == int(target_id),
                    MonitorSource.source == source,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.active:
            row.active = False
            row.updated_at = now
        state = (await _record_lifecycle(session, target_type, [target_id], previous, now))[target_id]
        await session.commit()
    return state


async def detach_all_sources(target_type: str, target_id: int) -> str:
    """摘掉全部来源（「停止监控」）：对象留在 Catalog，状态转 released。"""
    target_id = int(target_id)
    now = _now()
    async with write_session(WritePriority.INTERACTIVE, label="monitoring") as session:
        previous = await _ensure_targets(session, target_type, [target_id], now)
        rows = (
            await session.execute(
                select(MonitorSource).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == int(target_id),
                    MonitorSource.active.is_(True),
                )
            )
        ).scalars().all()
        for row in rows:
            row.active = False
            row.updated_at = now
        state = (await _record_lifecycle(session, target_type, [target_id], previous, now))[target_id]
        await session.commit()
    return state


# ── 对外：排除（用户意图，与业务状态正交）────────────────────


async def set_exclusion(
    target_type: str, target_id: int, excluded: bool, reason: str | None = None
) -> str:
    """设置 / 解除排除。解除时行保留（只翻 active + 记 cleared_at）。"""
    target_id = int(target_id)
    async with write_session(WritePriority.INTERACTIVE, label="monitoring") as session:
        now = _now()
        previous = await _ensure_targets(session, target_type, [target_id], now)
        if excluded:
            row = (await session.execute(select(MonitorExclusion).where(
                MonitorExclusion.target_type == target_type,
                MonitorExclusion.target_id == target_id, MonitorExclusion.active.is_(True),
            ))).scalar_one_or_none()
            if row is None:
                session.add(
                    MonitorExclusion(
                        target_type=target_type,
                        target_id=target_id,
                        reason=(reason or "")[:200] or None,
                        active=True,
                        created_at=now,
                    )
                )
            elif reason and row.reason != reason[:200]:
                row.reason = reason[:200]
        else:
            await _clear_exclusions(session, target_type, [target_id], now)
        state = (await _record_lifecycle(session, target_type, [target_id], previous, now))[target_id]
        await session.commit()
    return state


async def is_excluded(target_type: str, target_id: int) -> bool:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(MonitorExclusion.id).where(
                    MonitorExclusion.target_type == target_type,
                    MonitorExclusion.target_id == int(target_id),
                    MonitorExclusion.active.is_(True),
                )
            )
        ).scalar_one_or_none() is not None


async def exclusion_reason(target_type: str, target_id: int) -> str | None:
    """当前 active 排除的 reason；无 active 排除时 None。

    供恢复类动作判定「这条排除是不是自己挂的」——只解除本链路所挂的
    排除，用户显式设置的排除语义不被洗掉。
    """
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(MonitorExclusion.reason).where(
                    MonitorExclusion.target_type == target_type,
                    MonitorExclusion.target_id == int(target_id),
                    MonitorExclusion.active.is_(True),
                )
            )
        ).scalar_one_or_none()


async def track(target_type: str, target_id: int, source: str = "manual") -> str:
    """重新监控：清排除 + 挂来源。排除是资格门，只有显式动作能打开。"""
    tid = int(target_id)
    return (await sync_sources(target_type, {tid: {source}}, clear_exclusion=True))[tid]


async def stop(target_type: str, target_id: int) -> str:
    """停止监控：摘掉全部来源（保留排除语义，Catalog 与价格历史不动）。"""
    return await detach_all_sources(target_type, target_id)


async def forget(target_type: str, target_id: int) -> bool:
    """抹掉对象的监控记录（target 行 + 来源历史），回到「无记录」基线。

    纯星标包取消关注后恢复普通列表展示，不留下 released 移除标记；
    无记录的包仍不参与监控刷新。有激活来源或激活排除时拒绝执行；
    排除历史行保留（排除/解除可回溯）。
    """
    tid = int(target_id)
    async with write_session(WritePriority.INTERACTIVE, label="monitoring") as session:
        active_source = (
            await session.execute(
                select(MonitorSource.id).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == tid,
                    MonitorSource.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        active_exclusion = (
            await session.execute(
                select(MonitorExclusion.id).where(
                    MonitorExclusion.target_type == target_type,
                    MonitorExclusion.target_id == tid,
                    MonitorExclusion.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if active_source is not None or active_exclusion is not None:
            return False
        await session.execute(
            delete(MonitorSource).where(
                MonitorSource.target_type == target_type,
                MonitorSource.target_id == tid,
            )
        )
        await session.execute(
            delete(MonitorTarget).where(
                MonitorTarget.target_type == target_type,
                MonitorTarget.target_id == tid,
            )
        )
        await session.commit()
    return True


# ── 对外：状态查询 ────────────────────────────────────────────


async def state_of(target_type: str, target_id: int) -> str | None:
    """返回 state；无监控记录时返回 None（= 纯 Catalog 对象）。"""
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(MonitorTarget.state).where(
                    MonitorTarget.target_type == target_type,
                    MonitorTarget.target_id == int(target_id),
                )
            )
        ).scalar_one_or_none()


async def states_of(target_type: str, target_ids: list[int]) -> dict[int, str]:
    if not target_ids:
        return {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorTarget.target_id, MonitorTarget.state).where(
                    MonitorTarget.target_type == target_type,
                    MonitorTarget.target_id.in_([int(a) for a in target_ids]),
                )
            )
        ).all()
    return {int(a): s for a, s in rows}


async def sources_of(target_type: str, target_id: int) -> list[str]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorSource.source).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == int(target_id),
                    MonitorSource.active.is_(True),
                )
            )
        ).scalars().all()
    return sorted(rows)


async def active_ids(target_type: str) -> list[int]:
    """有 crawl 资格的对象 id（SQL 侧过滤，不物化 Catalog）。

    excluded / released 都不在此列——资格门在 state 上，调用方无需再判。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorSource.target_id)
                .join(MonitorTarget, (
                    (MonitorTarget.target_type == MonitorSource.target_type)
                    & (MonitorTarget.target_id == MonitorSource.target_id)
                ))
                .where(
                    MonitorSource.target_type == target_type, MonitorSource.active.is_(True),
                    ~select(MonitorExclusion.id).where(
                        MonitorExclusion.target_type == MonitorSource.target_type,
                        MonitorExclusion.target_id == MonitorSource.target_id,
                        MonitorExclusion.active.is_(True),
                    ).exists(),
                )
                .group_by(MonitorSource.target_id)
                .order_by(func.max(MonitorSource.priority).desc(), MonitorSource.target_id)
            )
        ).scalars().all()
    return [int(a) for a in rows]


async def priority_map(target_type: str, target_ids: list[int]) -> dict[int, int]:
    if not target_ids:
        return {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorTarget.target_id, MonitorTarget.priority).where(
                    MonitorTarget.target_type == target_type,
                    MonitorTarget.target_id.in_([int(a) for a in target_ids]),
                )
            )
        ).all()
    return {int(a): int(p or 0) for a, p in rows}


async def blocked_ids(target_type: str) -> set[int]:
    """不进 crawl 的对象 id（released + excluded）；供 bundle 刷新候选集过滤。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorTarget.target_id).where(
                    MonitorTarget.target_type == target_type,
                    MonitorTarget.state.in_(NON_CRAWL_STATES),
                )
            )
        ).scalars().all()
    return {int(a) for a in rows}


async def crawl_order(target_type: str) -> list[int]:
    """这一轮该爬谁、按什么顺序：来源优先级降序 → hot 优先 → id 升序。

    只排 Monitoring 层已有的对象；Catalog 里没有监控记录的对象不在此列。
    """
    ids = await active_ids(target_type)
    if not ids:
        return []
    prio = await priority_map(target_type, ids)
    hot = await _hot_ids(target_type, ids)
    return sorted(ids, key=lambda a: (-prio.get(a, 0), a not in hot, a))


async def _hot_ids(target_type: str, ids: list[int]) -> set[int]:
    """打折中 / 史低的对象（hot 段优先于同优先级组内其余对象）。"""
    if target_type == TARGET_GAME:
        from sqlalchemy import or_

        from app.domains.games.models import Game, GameCurrentPrice

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(Game.appid)
                    .where(
                        Game.appid.in_(ids),
                        or_(
                            Game.hl_flag > 0,
                            Game.appid.in_(
                                select(GameCurrentPrice.appid).where(
                                    GameCurrentPrice.discount_percent > 0
                                )
                            ),
                        ),
                    )
                )
            ).scalars().all()
        return {int(a) for a in rows}
    if target_type == TARGET_BUNDLE:
        from app.domains.games.models import Bundle

        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(Bundle.bundle_id)
                    .where(Bundle.bundle_id.in_(ids), Bundle.is_lowest.is_(True))
                )
            ).scalars().all()
        return {int(a) for a in rows}
    return set()


# ── wishlist → Tracking Source 同步 ───────────────────────────


async def sync_game_sources(appids: list[int]) -> dict[int, str]:
    """按 wishlist_items 现状重算这些 appid 的**账号派生来源**（幂等）。

    单一真相源是 wishlist_items：多账户同一 appid 的来源标记取并集
    （「任一账户仍想要」= 来源有效），无 active 行的 appid 派生来源置空 →
    仅剩用户显式来源时仍为 active。只收敛 `DERIVED_SOURCES`：用户自己挂的
    关注 favorite / 手动入池 manual 不被账号对账改写。
    排除状态不被本函数改写——它只由用户显式操作维护。
    """
    clean: list[int] = []
    seen: set[int] = set()
    for a in appids or []:
        try:
            appid = int(a)
        except (TypeError, ValueError):
            continue
        if appid > 0 and appid not in seen:
            seen.add(appid)
            clean.append(appid)
    if not clean:
        return {}

    from app.domains.wishlist.models import WishlistItem

    desired: dict[int, set[str]] = {}
    async with get_session_factory()() as session:
        for i in range(0, len(clean), _SYNC_CHUNK):
            chunk = clean[i : i + _SYNC_CHUNK]
            cols = [WishlistItem.appid]
            for flag, _src in _WISHLIST_SOURCE_FLAGS:
                cols.append(func.max(func.coalesce(getattr(WishlistItem, flag), 0)))
            rows = (
                await session.execute(
                    select(*cols)
                    .where(
                        WishlistItem.appid.in_(chunk),
                        WishlistItem.active.is_(True),
                    )
                    .group_by(WishlistItem.appid)
                )
            ).all()
            for r in rows:
                appid = int(r[0])
                sources = {
                    src
                    for idx, (_flag, src) in enumerate(_WISHLIST_SOURCE_FLAGS, start=1)
                    if int(r[idx] or 0) == 1
                }
                desired[appid] = sources
    for appid in clean:
        desired.setdefault(appid, set())
    return await sync_sources(TARGET_GAME, desired, managed=DERIVED_SOURCES)


async def recycle_orphan_game_sources() -> int:
    """回收孤儿账号派生来源：active 派生来源已无任何 active wishlist_items
    行支撑的（账户已解绑 / 历史解绑遗留），按空集重算停用。

    复用 `sync_sources`（managed=DERIVED_SOURCES）：用户显式来源
    （favorite / manual / board）不动，失去全部来源的对象转 released。
    返回因回收而退出监控（active → released）的对象数。
    """
    from app.domains.wishlist.models import WishlistItem

    async with get_session_factory()() as session:
        orphan_ids = [
            int(a)
            for a in (
                await session.execute(
                    select(MonitorSource.target_id)
                    .where(
                        MonitorSource.target_type == TARGET_GAME,
                        MonitorSource.source.in_(DERIVED_SOURCES),
                        MonitorSource.active.is_(True),
                        MonitorSource.target_id.not_in(
                            select(WishlistItem.appid).where(
                                WishlistItem.active.is_(True)
                            )
                        ),
                    )
                    .distinct()
                )
            ).scalars().all()
        ]
    if not orphan_ids:
        return 0
    before = await states_of(TARGET_GAME, orphan_ids)
    await sync_sources(TARGET_GAME, {a: set() for a in orphan_ids}, managed=DERIVED_SOURCES)
    after = await states_of(TARGET_GAME, orphan_ids)
    return sum(
        1
        for a in orphan_ids
        if before.get(a) == STATE_ACTIVE and after.get(a) == STATE_RELEASED
    )


# ── 统计（运行验证用）────────────────────────────────────────


async def stats() -> dict:
    """Catalog / Monitoring / Source / Exclusion 的分层计数。"""
    from app.domains.games.models import Bundle, Game

    async with get_session_factory()() as session:
        catalog_game = int(
            (await session.execute(select(func.count()).select_from(Game))).scalar_one()
        )
        catalog_bundle = int(
            (await session.execute(select(func.count()).select_from(Bundle))).scalar_one()
        )
        counts = await session.execute(
            select(MonitorTarget.target_type, MonitorTarget.state, func.count())
            .group_by(MonitorTarget.target_type, MonitorTarget.state)
        )
        states: dict[str, int] = {}
        by_type = {}
        for target_type, state, count in counts:
            states[state] = states.get(state, 0) + int(count)
            by_type[f"{target_type}/{state}"] = int(count)
        sources = {
            str(s): int(n)
            for s, n in (
                await session.execute(
                    select(MonitorSource.source, func.count())
                    .where(MonitorSource.active.is_(True))
                    .group_by(MonitorSource.source)
                )
            ).all()
        }
        exclusions = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(MonitorExclusion)
                    .where(MonitorExclusion.active.is_(True))
                )
            ).scalar_one()
        )
    return {
        "catalog": {"game": catalog_game, "bundle": catalog_bundle},
        "monitoring": {
            "active": states.get(STATE_ACTIVE, 0),
            "released": states.get(STATE_RELEASED, 0),
            "excluded": states.get(STATE_EXCLUDED, 0),
            "total": sum(states.values()),
        },
        "byType": by_type,
        "sources": sources,
        "exclusions": exclusions,
    }
