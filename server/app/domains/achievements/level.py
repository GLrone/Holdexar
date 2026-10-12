"""等级与经验账本：xp_events 是 XP 流水的唯一事实源，等级/段位纯函数派生不落表。

记账入口 record_xp_events（批量幂等、事务归调用方）；基线回填 ensure_baseline
（每账号一次幂等哨兵）；读数组装 level_profile 只出数值，称号与档位文案由前端词典译。
service/career 的口径函数（rarity_tier / _streaks / _bucket_index）在调用点延迟导入——
集成步 service 同步钩子会反向 import 本模块，顶层互引成环。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint, func, select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, WritePriority, get_session_factory, write_session
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState

# xp_events.source 枚举（枚举外值在记账入口拒收）
SOURCE_UNLOCK = "achievement_unlock"
SOURCE_PLATINUM = "platinum"
SOURCE_BUCKET = "playtime_bucket"
SOURCE_STREAK_DAY = "streak_day"
SOURCE_CHALLENGE = "challenge_claim"
SOURCE_COMMENT = "comment"
SOURCE_BASELINE = "baseline"
XP_SOURCES: frozenset[str] = frozenset(
    {
        SOURCE_UNLOCK,
        SOURCE_PLATINUM,
        SOURCE_BUCKET,
        SOURCE_STREAK_DAY,
        SOURCE_CHALLENGE,
        SOURCE_COMMENT,
        SOURCE_BASELINE,
    }
)

# 基数表；challenge_claim 的奖励是挑战目录定值、baseline 为一次性折算，不走基数
XP_BASE: dict[str, int] = {
    SOURCE_UNLOCK: 15,
    SOURCE_PLATINUM: 200,
    SOURCE_BUCKET: 40,
    SOURCE_STREAK_DAY: 20,
    SOURCE_COMMENT: 10,
}
# 稀有度倍率（tier 判定复用 service.rarity_tier，切点不另立第二套口径）
RARITY_MULTIPLIERS: dict[str, float] = {
    "common": 1.0,
    "uncommon": 1.25,
    "rare": 1.5,
    "very_rare": 2.0,
    "ultra": 3.0,
}
# 连击倍率按 career 口径当前连续解锁天数取档；结算定值与存量折算不追认加成
STREAK_BANDS: tuple[tuple[int, float], ...] = ((2, 1.1), (7, 1.25), (14, 1.5), (30, 2.0))
STREAK_EXEMPT: frozenset[str] = frozenset({SOURCE_CHALLENGE, SOURCE_BASELINE})

# 等级里程碑只做节点预览，不发 XP
MILESTONE_LEVELS: tuple[int, ...] = (10, 25, 50, 75, 100, 150, 200)
# 段位起点（前四档对齐奖杯陈列件四档视觉，legend 复用 platinum 件）
TIER_STEPS: tuple[tuple[str, int], ...] = (
    ("bronze", 1),
    ("silver", 10),
    ("gold", 20),
    ("platinum", 40),
    ("legend", 70),
)
CURVE_PREVIEW_LEVELS = 6
RECENT_EVENTS_LIMIT = 10
BASELINE_NOTE = "baseline from unlocks/platinum/playtime buckets"


class XpEvent(Base):
    __tablename__ = "xp_events"
    __table_args__ = (
        UniqueConstraint("steamid", "source", "source_ref", name="uq_xp_events_sid_src_ref"),
        Index("ix_xp_events_sid_created", "steamid", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    steamid: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    # 幂等键：与 (steamid, source) 合组唯一，语义按 source 各有定义（unlock=appid:image_name 等）
    source_ref: Mapped[str] = mapped_column(String(220), nullable=False)
    xp: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 机读备注（appid:image_name 等），不进任何 UI 文案
    note: Mapped[str | None] = mapped_column(String(220))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ─── 曲线纯函数（xp = ceil(30·n^1.5)，绝对增量恒为正）─────────────────


def xp_for_level(n: int) -> int:
    """从 n 级升 n+1 级所需 XP；n≥1。"""
    if n < 1:
        raise ValueError("level starts at 1")
    return math.ceil(30.0 * n**1.5)


def total_xp_for_level(level: int) -> int:
    """到达 level 级的累计 XP（level 1 = 0）。"""
    return sum(xp_for_level(n) for n in range(1, max(1, level)))


def level_for_xp(xp: int) -> tuple[int, int, int]:
    """累计 XP → (level, 本级已入, 升下一级所需)。"""
    level = 1
    into = int(xp)
    cost = xp_for_level(1)
    while into >= cost:
        into -= cost
        level += 1
        cost = xp_for_level(level)
    return level, into, cost


# 任务书口径别名（挑战/社交模块与前端消费方按名取用）
level_from_xp = level_for_xp


def level_tier(level: int) -> str:
    tier = TIER_STEPS[0][0]
    for name, start in TIER_STEPS:
        if level >= start:
            tier = name
    return tier


def next_tier(level: int) -> str | None:
    for name, start in TIER_STEPS:
        if level < start:
            return name
    return None


def tier_progress(level: int) -> float | None:
    """段位内进度 0~1（按级数线性）；legend 无下一档返回 None。"""
    idx = max(i for i, (_, start) in enumerate(TIER_STEPS) if level >= start)
    start = TIER_STEPS[idx][1]
    if idx + 1 >= len(TIER_STEPS):
        return None
    next_start = TIER_STEPS[idx + 1][1]
    return min(1.0, max(0.0, (level - start) / (next_start - start)))


# ─── 倍率与取整 ────────────────────────────────────────────────


def round_half_up(x: float) -> int:
    """四舍五入取整（Python 内建 round 是银行家舍入，奖励口径不用它）。"""
    return math.floor(float(x) + 0.5)


def rarity_multiplier(percent: float | None) -> float:
    from .service import rarity_tier

    if percent is None:
        return 1.0  # unknown 不惩罚不奖励
    return RARITY_MULTIPLIERS[rarity_tier(percent)]


def streak_multiplier(streak_days: int) -> float:
    mult = 1.0
    for lower, band in STREAK_BANDS:
        if streak_days >= lower:
            mult = band
    return mult


def playtime_bucket_index(minutes: int) -> int:
    """时长桶序 0..5（复用 career 的桶表切点；0 档不计 XP）。"""
    from .career import _bucket_index

    return _bucket_index(int(minutes or 0))


def compose_xp(
    source: str, rarity_percent: float | None = None, streak_days: int = 0
) -> tuple[int, float]:
    """按基数×倍率合成一条事件的 (实发 xp, 合计 multiplier)。

    稀有度倍率只作用于 achievement_unlock；连击倍率不作用于 challenge_claim/baseline。
    challenge_claim/baseline 无基数（目录定值/折算合计），由调用方给成品 xp。
    """
    base = XP_BASE[source]  # 无基数来源走成品 xp，KeyError 即调用方误用
    mult = 1.0
    if source == SOURCE_UNLOCK:
        mult *= rarity_multiplier(rarity_percent)
    if source not in STREAK_EXEMPT:
        mult *= streak_multiplier(streak_days)
    return round_half_up(base * mult), round(mult, 4)


# ─── 记账（批量幂等，事务归调用方）────────────────────────────────


async def record_xp_events(
    session: AsyncSession, steamid: str, entries: Iterable[dict[str, Any]]
) -> int:
    """批量落 XP 流水，返回实际新增行数；不提交（调用方决定事务边界）。

    幂等双保险：先按 (source, source_ref) 过滤已存在键，写入侧
    on_conflict_do_nothing 兜住并发竞态。entry 键：source/source_ref 必填，
    xp 实发值必填（合成口径见 compose_xp），multiplier 缺省 1.0，note 机读。
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        source = str(entry["source"])
        if source not in XP_SOURCES:
            raise ValueError(f"unknown xp source: {source}")
        rows.append(
            {
                "steamid": steamid,
                "source": source,
                "source_ref": str(entry["source_ref"]),
                "xp": int(entry["xp"]),
                "multiplier": float(entry.get("multiplier", 1.0)),
                "note": entry.get("note"),
            }
        )
    if not rows:
        return 0
    pairs = {(r["source"], r["source_ref"]) for r in rows}
    existing = set(
        (
            await session.execute(
                select(XpEvent.source, XpEvent.source_ref).where(
                    XpEvent.steamid == steamid,
                    tuple_(XpEvent.source, XpEvent.source_ref).in_(pairs),
                )
            )
        ).all()
    )
    fresh = [r for r in rows if (r["source"], r["source_ref"]) not in existing]
    if not fresh:
        return 0
    # 单语句多行 VALUES + OR IGNORE：rowcount 即实际新增数，同批重复也精确
    stmt = sqlite_insert(XpEvent).values(fresh).on_conflict_do_nothing(
        index_elements=["steamid", "source", "source_ref"]
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def total_xp(session: AsyncSession, steamid: str) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(XpEvent.xp), 0)).where(XpEvent.steamid == steamid)
        )
    )


async def existing_source_refs(session: AsyncSession, steamid: str, source: str) -> set[str]:
    """某来源已落账的 source_ref 全集（增量判定用：无事件即新增量）。"""
    rows = (
        await session.execute(
            select(XpEvent.source_ref).where(XpEvent.steamid == steamid, XpEvent.source == source)
        )
    ).all()
    return {r[0] for r in rows}


# ─── 基线回填（一次性幂等）───────────────────────────────────────


async def ensure_baseline(session: AsyncSession, steamid: str) -> dict | None:
    """存量折算基线：该账号无任何 XP 事件时落一条 baseline（幂等哨兵）。

    折算 = Σ已解锁 round(15×稀有度倍率) + 白金数×200 + Σ每游戏桶序(1..5)×40；
    不含 streak_day（历史连击属存量荣誉不回溯）。返回落账事件 dict，已有记录返回 None。
    """
    has_any = await session.scalar(
        select(XpEvent.id).where(XpEvent.steamid == steamid).limit(1)
    )
    if has_any is not None:
        return None

    unlock_rows = (
        await session.execute(
            select(AchievementState.appid, AchievementState.image_name, AchievementDef.global_percent)
            .outerjoin(
                AchievementDef,
                (AchievementDef.appid == AchievementState.appid)
                & (AchievementDef.image_name == AchievementState.image_name),
            )
            .where(AchievementState.steamid == steamid, AchievementState.achieved.is_(True))
        )
    ).all()
    game_rows = (
        await session.execute(
            select(AchievementGame.playtime_min, AchievementGame.platinum).where(
                AchievementGame.steamid == steamid
            )
        )
    ).all()

    xp = sum(round_half_up(XP_BASE[SOURCE_UNLOCK] * rarity_multiplier(p)) for _, _, p in unlock_rows)
    xp += sum(1 for _, platinum in game_rows if platinum) * XP_BASE[SOURCE_PLATINUM]
    xp += sum(
        playtime_bucket_index(minutes) * XP_BASE[SOURCE_BUCKET]
        for minutes, _ in game_rows
        if playtime_bucket_index(minutes) > 0
    )

    entry = {
        "source": SOURCE_BASELINE,
        "source_ref": "v1",
        "xp": xp,
        "multiplier": 1.0,
        "note": BASELINE_NOTE,
    }
    await record_xp_events(session, steamid, [entry])
    return entry


# ─── career 口径派生（连击日）────────────────────────────────────


async def unlock_days(session: AsyncSession, steamid: str) -> list:
    """有解锁时刻的日期升序去重（career._streaks 的输入口径）。"""
    from datetime import date as date_cls

    stamps = (
        await session.execute(
            select(AchievementState.unlock_time).where(
                AchievementState.steamid == steamid,
                AchievementState.achieved.is_(True),
                AchievementState.unlock_time > 0,
            )
        )
    ).scalars().all()
    return sorted({datetime.fromtimestamp(ts).date() for ts in stamps if ts})


async def current_streak_days(session: AsyncSession, steamid: str) -> int:
    """当前连续解锁天数（career._streaks 同一口径）。"""
    from .career import _streaks

    current, _, _ = _streaks(await unlock_days(session, steamid))
    return int(current)


async def notify_level_up(session: AsyncSession, steamid: str, previous_level: int) -> bool:
    """落账后检测涨级并发岛上事实（fact_key 幂等，重复调用不重发）。返回是否新发。"""
    from app.domains.notifications.facts import record_fact

    new_level, _, _ = level_for_xp(await total_xp(session, steamid))
    if new_level <= previous_level:
        return False
    return bool(
        await record_fact(
            source="achievements",
            kind="level_up",
            fact_key=f"{steamid}:level:{new_level}",
            data={"fromLevel": int(previous_level), "toLevel": int(new_level)},
        )
    )


# ─── 读数组装 ────────────────────────────────────────────────


def _curve_preview(total: int, level: int) -> list[dict]:
    rows = []
    cum = total
    cost = xp_for_level(level)
    for step in range(CURVE_PREVIEW_LEVELS):
        target = level + 1 + step
        cum += cost
        rows.append({"level": target, "costXp": cost, "cumulativeXp": cum})
        cost = xp_for_level(target)
    return rows


def _milestones(total: int) -> list[dict]:
    return [
        {
            "level": m,
            "tier": level_tier(m),
            "reached": total >= total_xp_for_level(m),
        }
        for m in MILESTONE_LEVELS
    ]


async def _active_bonuses(session: AsyncSession, steamid: str) -> list[dict]:
    days = await current_streak_days(session, steamid)
    if days < STREAK_BANDS[0][0]:
        return []
    return [{"kind": "streak", "multiplier": streak_multiplier(days), "streakDays": days}]


async def _recent_events(session: AsyncSession, steamid: str) -> list[dict]:
    rows = (
        await session.execute(
            select(XpEvent)
            .where(XpEvent.steamid == steamid)
            .order_by(XpEvent.created_at.desc(), XpEvent.id.desc())
            .limit(RECENT_EVENTS_LIMIT)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "source": r.source,
            "xp": r.xp,
            "multiplier": r.multiplier,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def _empty_profile(steamid: str) -> dict:
    """无主账号时的合法空形状（曲线/里程碑仍可由纯函数给出）。"""
    level, into, next_cost = level_for_xp(0)
    return {
        "steamid": steamid,
        "level": level,
        "tier": level_tier(level),
        "nextTier": next_tier(level),
        "tierProgress": tier_progress(level),
        "totalXp": 0,
        "xpIntoLevel": into,
        "xpForNextLevel": next_cost,
        "xpRemaining": next_cost - into,
        "levelProgress": 0.0,
        "lastLevelCost": 0,
        "nextCostGrowth": 0.0,
        "curvePreview": _curve_preview(0, level),
        "milestones": _milestones(0),
        "activeBonuses": [],
        "recentEvents": [],
    }


async def _assemble_profile(session: AsyncSession, steamid: str) -> dict:
    total = await total_xp(session, steamid)
    level, into, next_cost = level_for_xp(total)
    last_cost = xp_for_level(level - 1) if level > 1 else 0
    return {
        "steamid": steamid,
        "level": level,
        "tier": level_tier(level),
        "nextTier": next_tier(level),
        "tierProgress": tier_progress(level),
        "totalXp": total,
        "xpIntoLevel": into,
        "xpForNextLevel": next_cost,
        "xpRemaining": next_cost - into,
        "levelProgress": round(into / next_cost, 3) if next_cost else 0.0,
        "lastLevelCost": last_cost,
        "nextCostGrowth": round((next_cost - last_cost) / last_cost, 3) if last_cost else 0.0,
        "curvePreview": _curve_preview(total, level),
        "milestones": _milestones(total),
        "activeBonuses": await _active_bonuses(session, steamid),
        "recentEvents": await _recent_events(session, steamid),
    }


async def level_profile(target: str | None = None) -> dict:
    """等级读数；`target` 为空取主账号。首调触发基线回填（先到先做，幂等）。"""
    from . import service

    steamid = (target or "").strip()
    if not steamid:
        steamid = await service._primary_steam_id()
    if not steamid:
        return _empty_profile("")

    async with get_session_factory()() as session:
        has_any = await session.scalar(
            select(XpEvent.id).where(XpEvent.steamid == steamid).limit(1)
        )
    if has_any is None:
        async with write_session(WritePriority.BACKGROUND, label="level_baseline") as wsession:
            await ensure_baseline(wsession, steamid)
            await wsession.commit()

    async with get_session_factory()() as session:
        return await _assemble_profile(session, steamid)
