"""monitoring 域服务：来源 → 监控对象 → crawl 资格 的生命周期。

三层语义（互相不可互相推导）：

- **Source**（`monitor_sources`）：谁想监控它。家族愿望单只是来源之一。
- **Monitoring**（`monitor_targets`）：当前是否处于监控中，状态为
  active / released / excluded。
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

from sqlalchemy import func, select

from app.core.database import get_session_factory
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
    "favorite": 100,         # 星标关注（wishlist_items.manual）
    "family_wishlist": 95,   # 家族 / 多账号愿望单（wishlist_items.wishlisted）
    "manual": 60,            # 手动入池 / 任务页导入（wishlist_items.manual_pool）
    "owned": 40,             # 已购同步（wishlist_items.owned）
    "import": 30,            # 捆绑包导入（bundles/refresh.import_bundle）
    "board": 20,             # 榜单发现源落池（wishlist_items.board_pool）
}
DEFAULT_SOURCE_PRIORITY = 10

# wishlist_items 的来源布尔列 → monitoring source（多账户同 appid 去重后取并集）
_WISHLIST_SOURCE_FLAGS: tuple[tuple[str, str], ...] = (
    ("manual", "favorite"),
    ("wishlisted", "family_wishlist"),
    ("manual_pool", "manual"),
    ("owned", "owned"),
    ("board_pool", "board"),
)

# 账号派生来源：由 wishlist_items（Steam 账户来源数据）对账产生，账号同步时
# 按现状重算。用户显式来源（favorite / manual）与它们分属两个管理者——
# 对账只收敛本集合内的种类，不会把用户自己挂的关注/手动入池洗掉。
DERIVED_SOURCES: frozenset[str] = frozenset({"family_wishlist", "owned", "board"})

# 单次批量同步的 appid 数（SQLite 变量数上限防御）
_SYNC_CHUNK = 400


def _now() -> datetime:
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)


def _priority_of(source: str) -> int:
    return SOURCE_PRIORITY.get(source, DEFAULT_SOURCE_PRIORITY)


# ── 内部：行维护 + 状态重算 ────────────────────────────────────


async def _ensure_target(session, target_type: str, target_id: int) -> MonitorTarget:
    row = (
        await session.execute(
            select(MonitorTarget).where(
                MonitorTarget.target_type == target_type,
                MonitorTarget.target_id == target_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        now = _now()
        row = MonitorTarget(
            target_type=target_type,
            target_id=target_id,
            state=STATE_RELEASED,
            priority=0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
    return row


def _next_state(has_source: bool, excluded: bool) -> str:
    if excluded:
        return STATE_EXCLUDED
    return STATE_ACTIVE if has_source else STATE_RELEASED


def _apply_state(row: MonitorTarget, state: str, priority: int, now: datetime) -> None:
    if row.state == state and row.priority == priority:
        return
    row.state = state
    row.priority = priority
    row.updated_at = now
    if state == STATE_ACTIVE:
        row.activated_at = now
    elif state == STATE_RELEASED:
        row.released_at = now
    elif state == STATE_EXCLUDED:
        row.excluded_at = now


async def _recompute(session, target_type: str, target_id: int) -> str:
    """按「排除门 → 来源 → 优先级」重算一个监控对象的状态。"""
    now = _now()
    excluded = (
        await session.execute(
            select(MonitorExclusion.id).where(
                MonitorExclusion.target_type == target_type,
                MonitorExclusion.target_id == target_id,
                MonitorExclusion.active.is_(True),
            )
        )
    ).scalar_one_or_none() is not None

    best = (
        await session.execute(
            select(func.max(MonitorSource.priority)).where(
                MonitorSource.target_type == target_type,
                MonitorSource.target_id == target_id,
                MonitorSource.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    has_source = best is not None

    row = await _ensure_target(session, target_type, target_id)
    _apply_state(row, _next_state(has_source, excluded), int(best or 0), now)
    return row.state


# ── 对外：来源维护 ────────────────────────────────────────────


async def sync_sources(
    target_type: str,
    desired: dict[int, set[str]],
    *,
    managed: set[str] | frozenset[str] | None = None,
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
    result: dict[int, str] = {}
    async with get_session_factory()() as session:
        targets = list(desired)
        existing = {
            (int(r.target_id), r.source): r
            for r in (
                await session.execute(
                    select(MonitorSource).where(
                        MonitorSource.target_type == target_type,
                        MonitorSource.target_id.in_(targets),
                    )
                )
            )
            .scalars()
            .all()
        }
        for target_id, sources in desired.items():
            sources = {s for s in (sources or set()) if s}
            if managed is not None:
                sources = {s for s in sources if s in managed}
            current = {
                src: row for (tid, src), row in existing.items() if tid == target_id
            }
            for src in sources:
                row = current.get(src)
                if row is None:
                    session.add(
                        MonitorSource(
                            target_type=target_type,
                            target_id=target_id,
                            source=src,
                            priority=_priority_of(src),
                            active=True,
                            created_at=now,
                            updated_at=now,
                        )
                    )
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
            result[target_id] = await _recompute(session, target_type, target_id)
        await session.commit()
    return result


async def ensure_source(target_type: str, target_id: int, source: str) -> str:
    """确保某个来源为激活态，**不动该对象的其它来源**。

    与 `attach_source` 的分工：attach 把来源集合收敛成只含这一个（切换来源
    语义）；ensure 只加不减——用户显式动作（关注 / 手动入池）用它，免得
    把 Steam 愿望单等来源顺手洗掉。
    """
    now = _now()
    async with get_session_factory()() as session:
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
        state = await _recompute(session, target_type, int(target_id))
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
    async with get_session_factory()() as session:
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
            row.updated_at = _now()
        state = await _recompute(session, target_type, int(target_id))
        await session.commit()
    return state


async def detach_all_sources(target_type: str, target_id: int) -> str:
    """摘掉全部来源（「停止监控」）：对象留在 Catalog，状态转 released。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(MonitorSource).where(
                    MonitorSource.target_type == target_type,
                    MonitorSource.target_id == int(target_id),
                    MonitorSource.active.is_(True),
                )
            )
        ).scalars().all()
        now = _now()
        for row in rows:
            row.active = False
            row.updated_at = now
        state = await _recompute(session, target_type, int(target_id))
        await session.commit()
    return state


# ── 对外：排除（用户意图，与业务状态正交）────────────────────


async def set_exclusion(
    target_type: str, target_id: int, excluded: bool, reason: str | None = None
) -> str:
    """设置 / 解除排除。解除时行保留（只翻 active + 记 cleared_at）。"""
    target_id = int(target_id)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(MonitorExclusion).where(
                    MonitorExclusion.target_type == target_type,
                    MonitorExclusion.target_id == target_id,
                    MonitorExclusion.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        now = _now()
        if excluded:
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
        elif row is not None:
            row.active = False
            row.cleared_at = now
        state = await _recompute(session, target_type, target_id)
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


async def track(target_type: str, target_id: int, source: str = "manual") -> str:
    """重新监控：清排除 + 挂来源。排除是资格门，只有显式动作能打开。"""
    await set_exclusion(target_type, target_id, False)
    return await attach_source(target_type, target_id, source)


async def stop(target_type: str, target_id: int) -> str:
    """停止监控：摘掉全部来源（保留排除语义，Catalog 与价格历史不动）。"""
    return await detach_all_sources(target_type, target_id)


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
                select(MonitorTarget.target_id)
                .where(
                    MonitorTarget.target_type == target_type,
                    MonitorTarget.state == STATE_ACTIVE,
                )
                .order_by(MonitorTarget.priority.desc(), MonitorTarget.target_id)
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


async def sync_all_game_sources() -> int:
    """全库对账：wishlist_items 里出现过的 appid 全部重算一遍。

    兜底入口（迁移漏跑或监控表被清空的库按需手工调用）；账号同步与池内增删
    走 `sync_game_sources` 按 appid 增量对账。
    """
    from app.domains.wishlist.models import WishlistItem

    async with get_session_factory()() as session:
        appids = [
            int(a)
            for a in (
                await session.execute(select(WishlistItem.appid).distinct())
            ).scalars().all()
        ]
    if not appids:
        return 0
    await sync_game_sources(appids)
    return len(appids)


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
        states = {
            str(s): int(n)
            for s, n in (
                await session.execute(
                    select(MonitorTarget.state, func.count()).group_by(MonitorTarget.state)
                )
            ).all()
        }
        by_type = {
            f"{t}/{s}": int(n)
            for t, s, n in (
                await session.execute(
                    select(
                        MonitorTarget.target_type, MonitorTarget.state, func.count()
                    ).group_by(MonitorTarget.target_type, MonitorTarget.state)
                )
            ).all()
        }
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
