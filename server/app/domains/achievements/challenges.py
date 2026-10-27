"""挑战中心：challenge_progress 是挑战进度唯一账本，目录为代码常量（无外键）。

进度只读既有账本（成就快照 / 生涯连击口径 / 批注表 / 可切换账号清单）幂等重算，
批量 upsert 落行、target 直接读目录、status 只进不退；领取经 level.record_xp_events
发目录定值，status 守卫与 xp_events 唯一键双保险防重。service 的账号口径在调用点
延迟导入——集成步同步钩子会反向 import 本模块，顶层互引成环。
"""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import DateTime, Integer, String, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, WritePriority, get_session_factory, write_session
from app.domains.achievements.models import AchievementGame, AchievementState

from . import level

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_CLAIMED = "claimed"

# 档位奖励定值（铜 / 银 / 金）
REWARD_BY_TIER: dict[str, int] = {"bronze": 80, "silver": 200, "gold": 500}

# 进度来源全集（每项只读既有账本，禁止另立口径）
PROGRESS_SOURCES: frozenset[str] = frozenset(
    {
        "states_count",
        "platinum_count",
        "full_clear_games",
        "games_with_unlocks",
        "playtime_sum",
        "bucket_crossovers",
        "streak_current",
        "notes_count",
        "social_accounts_unlocked",
        "social_accounts_unlocked10",
    }
)


class ChallengeSpec(NamedTuple):
    id: str
    tier: str
    category: str
    target: int
    progress_source: str
    reward_xp: int


# 挑战目录；概念名文案由前端按 id 取词典，后端只出 id 与数值
CHALLENGE_CATALOG: tuple[ChallengeSpec, ...] = (
    ChallengeSpec("first_trophy", "bronze", "milestone", 1, "states_count", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("hunter_twelve", "bronze", "milestone", 12, "states_count", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("hunter_fifty", "silver", "milestone", 50, "states_count", REWARD_BY_TIER["silver"]),
    ChallengeSpec("hunter_hundred", "gold", "milestone", 100, "states_count", REWARD_BY_TIER["gold"]),
    ChallengeSpec("streak_three", "bronze", "streak", 3, "streak_current", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("streak_seven", "silver", "streak", 7, "streak_current", REWARD_BY_TIER["silver"]),
    ChallengeSpec("streak_thirty", "gold", "streak", 30, "streak_current", REWARD_BY_TIER["gold"]),
    ChallengeSpec("platinum_first", "bronze", "platinum", 1, "platinum_count", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("platinum_five", "silver", "platinum", 5, "platinum_count", REWARD_BY_TIER["silver"]),
    ChallengeSpec("platinum_twelve", "gold", "platinum", 12, "platinum_count", REWARD_BY_TIER["gold"]),
    ChallengeSpec("breadth_twenty", "bronze", "collection", 20, "games_with_unlocks", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("full_clear_first", "silver", "collection", 1, "full_clear_games", REWARD_BY_TIER["silver"]),
    ChallengeSpec("full_clear_triple", "gold", "collection", 3, "full_clear_games", REWARD_BY_TIER["gold"]),
    ChallengeSpec("hours_hundred", "silver", "cumulative", 6000, "playtime_sum", REWARD_BY_TIER["silver"]),
    ChallengeSpec("hours_thousand", "gold", "cumulative", 60000, "playtime_sum", REWARD_BY_TIER["gold"]),
    ChallengeSpec("buckets_ten", "silver", "cumulative", 10, "bucket_crossovers", REWARD_BY_TIER["silver"]),
    ChallengeSpec("note_first", "bronze", "comment", 1, "notes_count", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("note_gallery", "silver", "comment", 25, "notes_count", REWARD_BY_TIER["silver"]),
    ChallengeSpec("social_duo", "bronze", "social", 2, "social_accounts_unlocked", REWARD_BY_TIER["bronze"]),
    ChallengeSpec("social_trio", "gold", "social", 3, "social_accounts_unlocked10", REWARD_BY_TIER["gold"]),
)
CATALOG_BY_ID: dict[str, ChallengeSpec] = {spec.id: spec for spec in CHALLENGE_CATALOG}


class ChallengeProgress(Base):
    __tablename__ = "challenge_progress"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=STATUS_ACTIVE)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ─── 进度度量（全部只读既有账本）─────────────────────────────────


async def _notes_count(session: AsyncSession, steamid: str) -> int:
    """批注账本只读计数（批注表归社交模块，表未建时评语类挑战按 0 计）。"""
    try:
        row = (
            await session.execute(
                text("SELECT COUNT(*) FROM achievement_notes WHERE steamid = :sid"),
                {"sid": steamid},
            )
        ).scalar()
    except Exception:  # noqa: BLE001 —— 表未建按零计，社交模块交付后自动可读
        return 0
    return int(row or 0)


async def _measure(session: AsyncSession, steamid: str) -> dict[str, int]:
    """一次读会话算齐全部进度来源。"""
    from . import service

    states_count = int(
        await session.scalar(
            select(func.count())
            .select_from(AchievementState)
            .where(AchievementState.steamid == steamid, AchievementState.achieved.is_(True))
        )
        or 0
    )
    game_rows = (
        await session.execute(
            select(
                AchievementGame.playtime_min,
                AchievementGame.total_achievements,
                AchievementGame.unlocked,
                AchievementGame.platinum,
            ).where(AchievementGame.steamid == steamid)
        )
    ).all()

    # 社交类来源：可切换账号清单（已绑 + 家庭成员）里有解锁记录的账号数，只读不回写
    roster = await service.available_accounts()
    roster_ids = [a["steamid"] for a in roster if a.get("steamid")]
    unlock_counts: dict[str, int] = {}
    if roster_ids:
        rows = (
            await session.execute(
                select(AchievementState.steamid, func.count())
                .where(AchievementState.steamid.in_(roster_ids), AchievementState.achieved.is_(True))
                .group_by(AchievementState.steamid)
            )
        ).all()
        unlock_counts = {sid: int(n) for sid, n in rows}

    return {
        "states_count": states_count,
        "platinum_count": sum(1 for r in game_rows if r.platinum),
        "full_clear_games": sum(
            1 for r in game_rows if r.total_achievements > 0 and r.unlocked >= r.total_achievements
        ),
        "games_with_unlocks": sum(1 for r in game_rows if r.unlocked > 0),
        # shared 行时长恒 0（models 口径），求和天然不含库外时长
        "playtime_sum": sum(int(r.playtime_min or 0) for r in game_rows),
        "bucket_crossovers": sum(
            idx for r in game_rows if (idx := level.playtime_bucket_index(r.playtime_min)) > 0
        ),
        "streak_current": await level.current_streak_days(session, steamid),
        "notes_count": await _notes_count(session, steamid),
        "social_accounts_unlocked": sum(1 for sid in roster_ids if unlock_counts.get(sid, 0) >= 1),
        "social_accounts_unlocked10": sum(1 for sid in roster_ids if unlock_counts.get(sid, 0) >= 10),
    }


# ─── 幂等重算与读数组装 ────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _idle_rows() -> list[dict]:
    """无账号时的零进度读数（不落库）。"""
    return [
        {
            "challengeId": spec.id,
            "progress": 0,
            "target": spec.target,
            "status": STATUS_ACTIVE,
            "completedAt": None,
            "claimedAt": None,
            "updatedAt": None,
        }
        for spec in CHALLENGE_CATALOG
    ]


async def recompute_challenges(target: str | None = None) -> list[dict]:
    """幂等重算：度量账本 → 批量 upsert → 新完成项落岛上事实；返回目录序行读数。

    completed/claimed 行保留既有状态与时刻（status 只进不退）；target 直接读目录。
    """
    from . import service

    steamid = (target or "").strip()
    if not steamid:
        steamid = await service._primary_steam_id()
    if not steamid:
        return _idle_rows()

    async with get_session_factory()() as session:
        measured = await _measure(session, steamid)
        existing = {
            row.challenge_id: row
            for row in (
                await session.execute(
                    select(ChallengeProgress).where(ChallengeProgress.steamid == steamid)
                )
            ).scalars()
        }

    now = datetime.utcnow()
    upserts: list[dict] = []
    snapshot: list[dict] = []
    newly_completed: list[ChallengeSpec] = []
    for spec in CHALLENGE_CATALOG:
        progress = int(measured.get(spec.progress_source, 0))
        prev = existing.get(spec.id)
        claimed_at = prev.claimed_at if prev is not None else None
        if prev is not None and prev.status in (STATUS_COMPLETED, STATUS_CLAIMED):
            status = prev.status
            completed_at = prev.completed_at
        elif progress >= spec.target:
            status = STATUS_COMPLETED
            completed_at = (prev.completed_at if prev is not None else None) or now
            newly_completed.append(spec)
        else:
            status = STATUS_ACTIVE
            completed_at = None
        upserts.append(
            {
                "steamid": steamid,
                "challenge_id": spec.id,
                "progress": progress,
                "status": status,
                "completed_at": completed_at,
                "claimed_at": claimed_at,
                "updated_at": now,
            }
        )
        snapshot.append(
            {
                "challengeId": spec.id,
                "progress": progress,
                "target": spec.target,
                "status": status,
                "completedAt": _iso(completed_at),
                "claimedAt": _iso(claimed_at),
                "updatedAt": _iso(now),
            }
        )

    insert_stmt = sqlite_insert(ChallengeProgress).values(upserts)
    async with write_session(WritePriority.BACKGROUND, label="challenge_recalc") as wsession:
        await wsession.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["steamid", "challenge_id"],
                set_={
                    "progress": insert_stmt.excluded.progress,
                    "status": insert_stmt.excluded.status,
                    "completed_at": insert_stmt.excluded.completed_at,
                    "updated_at": insert_stmt.excluded.updated_at,
                },
            )
        )
        await wsession.commit()

    if newly_completed:
        from app.domains.notifications.facts import record_fact

        for spec in newly_completed:
            await record_fact(
                source="achievements",
                kind="challenge_completed",
                fact_key=f"{steamid}:challenge:{spec.id}",
                data={"challengeId": spec.id, "rewardXp": spec.reward_xp},
            )
    return snapshot


def _assemble(steamid: str, rows: list[dict]) -> dict:
    """行读数 + 目录常量 → 端点读数（completed 含已领取，claimable=完成未领）。"""
    completed = 0
    claimed = 0
    items: list[dict] = []
    for row in rows:
        spec = CATALOG_BY_ID[row["challengeId"]]
        status = row["status"]
        if status in (STATUS_COMPLETED, STATUS_CLAIMED):
            completed += 1
        if status == STATUS_CLAIMED:
            claimed += 1
        items.append(
            {
                "challengeId": spec.id,
                "tier": spec.tier,
                "category": spec.category,
                "target": spec.target,
                "progress": row["progress"],
                "rewardXp": spec.reward_xp,
                "status": status,
                "claimable": status == STATUS_COMPLETED,
                "completedAt": row["completedAt"],
                "claimedAt": row["claimedAt"],
                "updatedAt": row["updatedAt"],
            }
        )
    return {
        "steamid": steamid,
        "challenges": items,
        "summary": {
            "total": len(CHALLENGE_CATALOG),
            "completed": completed,
            "claimed": claimed,
            "claimable": completed - claimed,
        },
    }


async def challenges_overview(target: str | None = None) -> dict:
    """挑战列表读数（GET 即触发一次幂等重算，目录变更自动跟上）。"""
    from . import service

    steamid = (target or "").strip()
    if not steamid:
        steamid = await service._primary_steam_id()
    if not steamid:
        return _assemble("", _idle_rows())
    return _assemble(steamid, await recompute_challenges(steamid))


# ─── 领取（status 守卫 + xp_events 唯一键双保险）──────────────────


async def claim(target: str | None, challenge_id: str) -> dict:
    """领取已完成挑战的目录定值奖励；同事务发奖 + status→claimed，涨级落岛上事实。"""
    from . import service

    spec = CATALOG_BY_ID.get(challenge_id)
    if spec is None:
        raise LookupError(f"unknown challenge: {challenge_id}")
    steamid = (target or "").strip()
    if not steamid:
        steamid = await service._primary_steam_id()
    if not steamid:
        raise ValueError("暂无可领取的挑战")

    await recompute_challenges(steamid)

    async with write_session(WritePriority.BACKGROUND, label="challenge_claim") as wsession:
        row = (
            await wsession.execute(
                select(ChallengeProgress).where(
                    ChallengeProgress.steamid == steamid,
                    ChallengeProgress.challenge_id == challenge_id,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.status == STATUS_ACTIVE:
            raise ValueError("挑战尚未完成，完成后才能领取")
        if row.status == STATUS_CLAIMED:
            raise ValueError("该挑战奖励已领取，不能重复领取")

        previous_level = level.level_for_xp(await level.total_xp(wsession, steamid))[0]
        granted = await level.record_xp_events(
            wsession,
            steamid,
            [
                {
                    "source": level.SOURCE_CHALLENGE,
                    "source_ref": challenge_id,
                    "xp": spec.reward_xp,
                    "multiplier": 1.0,
                    "note": challenge_id,
                }
            ],
        )
        if not granted:
            # 状态守卫被并发穿透时由 xp_events 唯一键兜底：不发奖、不翻状态
            raise ValueError("该挑战奖励已领取，不能重复领取")
        total_after = await level.total_xp(wsession, steamid)
        current_level = level.level_for_xp(total_after)[0]
        now = datetime.utcnow()
        row.status = STATUS_CLAIMED
        row.claimed_at = now
        row.updated_at = now
        await wsession.commit()

    async with get_session_factory()() as session:
        await level.notify_level_up(session, steamid, previous_level=previous_level)

    return {
        "challengeId": challenge_id,
        "status": STATUS_CLAIMED,
        "xpGranted": spec.reward_xp,
        "totalXp": int(total_after),
        "level": {
            "previous": previous_level,
            "current": current_level,
            "leveledUp": current_level > previous_level,
        },
    }


# 集成步同步钩子口径别名
recalc = recompute_challenges
