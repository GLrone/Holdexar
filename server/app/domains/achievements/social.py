"""成就批注与社交聚合：achievement_notes 是「锚定到具体成就的批注」唯一事实源
（KV 便利贴墙互不替代、互不迁移）；compare/feed 只读既有账本聚合，不落新表。
首评经 xp_events 给小额外 XP（每成就每账号一次幂等，删除批注不回冲 XP）。
service 的账号口径在调用点延迟导入——集成步同步钩子反向挂载，顶层互引成环。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, and_, delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, WritePriority, get_session_factory, write_session
from app.domains.achievements import level
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState

NOTE_TEXT_MAX = 140  # 上限对齐 KV 便利贴墙 career.WALL_TEXT_MAX
NOTE_MOODS: tuple[str, ...] = ("love", "wow", "hard", "fun")

COMPARE_MAX = 6
COMPARE_DEFAULT = 4
FEED_DEFAULT_LIMIT = 30
FEED_MAX_LIMIT = 100


class AchievementNote(Base):
    __tablename__ = "achievement_notes"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # 与 achievement_defs 同款图标资产名标识
    image_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    content: Mapped[str] = mapped_column(String(140), nullable=False)
    mood: Mapped[str | None] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── 口径与校验 ────────────────────────────────────────────────


def _comment_ref(appid: int, image_name: str) -> str:
    return f"{appid}:{image_name}"


def _clean_note_text(raw: str) -> str:
    body = " ".join(str(raw or "").split())
    if not body:
        raise ValueError("评语不能为空")
    if len(body) > NOTE_TEXT_MAX:
        raise ValueError(f"评语最长 {NOTE_TEXT_MAX} 字")
    return body


async def _resolve_steamid(target: str | None = None) -> str:
    from . import service

    steamid = (target or "").strip()
    if not steamid:
        steamid = await service._primary_steam_id()
    return steamid


def _note_payload(note: AchievementNote, d: AchievementDef | None, game_name: str | None) -> dict:
    return {
        "appid": note.appid,
        "imageName": note.image_name,
        "content": note.content,
        "mood": note.mood,
        "achievementName": (d.name if d and d.name else note.image_name),
        "achievementIcon": d.icon_url if d else None,
        "achievementGrayIcon": d.icon_gray_url if d else None,
        "gameName": game_name or "",
        "createdAt": note.created_at.isoformat() if note.created_at else None,
        "updatedAt": note.updated_at.isoformat() if note.updated_at else None,
    }


_NOTE_JOIN = (
    AchievementDef,
    and_(
        AchievementDef.appid == AchievementNote.appid,
        AchievementDef.image_name == AchievementNote.image_name,
    ),
)
_NOTE_GAME_JOIN = (
    AchievementGame,
    and_(
        AchievementGame.appid == AchievementNote.appid,
        AchievementGame.steamid == AchievementNote.steamid,
    ),
)


# ─── 批注（写评语经 xp_events 给小额外 XP，每成就每账号一次）──────


async def list_notes(target: str | None = None) -> dict:
    """批注墙（最新更新在前），join defs/games 补成就名、图标与游戏名。"""
    steamid = await _resolve_steamid(target)
    if not steamid:
        return {"total": 0, "notes": []}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AchievementNote, AchievementDef, AchievementGame.name)
                .outerjoin(*_NOTE_JOIN)
                .outerjoin(*_NOTE_GAME_JOIN)
                .where(AchievementNote.steamid == steamid)
                .order_by(AchievementNote.updated_at.desc(), AchievementNote.created_at.desc())
            )
        ).all()
    notes = [_note_payload(note, d, game_name) for note, d, game_name in rows]
    return {"total": len(notes), "notes": notes}


async def save_note(
    target: str | None, appid: int, image_name: str, content: str, mood: str | None = None
) -> dict:
    """保存批注（同键重存=编辑）；首评落 XP 流水并检测涨级，编辑重存不发。"""
    steamid = await _resolve_steamid(target)
    if not steamid:
        raise ValueError("未绑定 Steam 账号，无法保存评语")
    body = _clean_note_text(content)
    if mood is not None and mood not in NOTE_MOODS:
        raise ValueError(f"评语心情只能是 {'/'.join(NOTE_MOODS)}")
    image_name = str(image_name or "").strip()
    if not image_name:
        raise ValueError("成就标识缺失")
    appid = int(appid)

    async with write_session(WritePriority.BACKGROUND, label="achievements_note_save") as session:
        prev_level = level.level_for_xp(await level.total_xp(session, steamid))[0]
        now = datetime.utcnow()
        await session.execute(
            sqlite_insert(AchievementNote)
            .values(
                steamid=steamid,
                appid=appid,
                image_name=image_name,
                content=body,
                mood=mood,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["steamid", "appid", "image_name"],
                set_={"content": body, "mood": mood, "updated_at": now},
            )
        )
        streak_days = await level.current_streak_days(session, steamid)
        xp, multiplier = level.compose_xp(level.SOURCE_COMMENT, streak_days=streak_days)
        granted = await level.record_xp_events(
            session,
            steamid,
            [
                {
                    "source": level.SOURCE_COMMENT,
                    "source_ref": _comment_ref(appid, image_name),
                    "xp": xp,
                    "multiplier": multiplier,
                }
            ],
        )
        row = (
            await session.execute(
                select(AchievementNote, AchievementDef, AchievementGame.name)
                .outerjoin(*_NOTE_JOIN)
                .outerjoin(*_NOTE_GAME_JOIN)
                .where(
                    AchievementNote.steamid == steamid,
                    AchievementNote.appid == appid,
                    AchievementNote.image_name == image_name,
                )
            )
        ).first()
        await session.commit()

    if granted:
        # 涨级事实自开事务落库（fact_key 幂等），不占本批注的写事务
        async with get_session_factory()() as session:
            await level.notify_level_up(session, steamid, previous_level=prev_level)
    note, d, game_name = row
    return {
        "note": _note_payload(note, d, game_name),
        "xpGranted": xp if granted else 0,
        "firstForAchievement": bool(granted),
    }


async def remove_note(target: str | None, appid: int, image_name: str) -> bool:
    """删除批注（不存在静默返回 False）；已发 XP 不回冲。"""
    steamid = await _resolve_steamid(target)
    if not steamid:
        return False
    async with write_session(WritePriority.BACKGROUND, label="achievements_note_remove") as session:
        result = await session.execute(
            delete(AchievementNote).where(
                AchievementNote.steamid == steamid,
                AchievementNote.appid == int(appid),
                AchievementNote.image_name == str(image_name),
            )
        )
        await session.commit()
        return bool(result.rowcount)


# ─── 多账号对比（全部读既有账本，不落任何新表）──────────────────


def _parse_steamids(raw: str | None) -> list[str]:
    """逗号清单去重保序，截到上限；空返回空（调用方走缺省账号来源）。"""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in str(raw).split(","):
        sid = part.strip()
        if sid:
            seen.setdefault(sid, None)
    return list(seen)[:COMPARE_MAX]


async def _rarity_buckets(session: AsyncSession, steamid: str) -> list[dict]:
    """已解锁成就稀有度分桶（口径与序对齐 summary.rarityBuckets）。"""
    from . import service

    rows = (
        await session.execute(
            select(AchievementDef.global_percent)
            .join(
                AchievementState,
                and_(
                    AchievementState.appid == AchievementDef.appid,
                    AchievementState.image_name == AchievementDef.image_name,
                ),
            )
            .where(AchievementState.steamid == steamid, AchievementState.achieved.is_(True))
        )
    ).all()
    counts: dict[str, int] = {t: 0 for t, _ in service.RARITY_BANDS}
    counts["common"] = 0
    counts["unknown"] = 0
    for (percent,) in rows:
        counts[service.rarity_tier(percent)] += 1
    return [{"tier": t, "count": counts[t]} for t, _ in service.RARITY_BANDS] + [
        {"tier": "common", "count": counts["common"]},
        {"tier": "unknown", "count": counts["unknown"]},
    ]


async def _challenges_completed(session: AsyncSession, steamid: str) -> int:
    """挑战完成数只读聚合（challenge_progress 是挑战域唯一账本；
    目录模块未交付/表未建时按 0 计，不另立副本）。"""
    try:
        row = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM challenge_progress "
                    "WHERE steamid = :sid AND status IN ('completed', 'claimed')"
                ),
                {"sid": steamid},
            )
        ).scalar()
    except Exception:  # noqa: BLE001 —— 表未建按零计，交付后自动可读
        return 0
    return int(row or 0)


async def _compare_row(session: AsyncSession, steamid: str, roster_entry: dict | None) -> dict:
    unlocked = (
        await session.scalar(
            select(func.count())
            .select_from(AchievementState)
            .where(AchievementState.steamid == steamid, AchievementState.achieved.is_(True))
        )
        or 0
    )
    platinum = (
        await session.scalar(
            select(func.count())
            .select_from(AchievementGame)
            .where(AchievementGame.steamid == steamid, AchievementGame.platinum.is_(True))
        )
        or 0
    )
    playtime = (
        await session.scalar(
            select(func.coalesce(func.sum(AchievementGame.playtime_min), 0)).where(
                AchievementGame.steamid == steamid
            )
        )
        or 0
    )
    total_xp = await level.total_xp(session, steamid)
    lvl, _, _ = level.level_for_xp(total_xp)
    entry = roster_entry or {}
    return {
        "steamid": steamid,
        "personaName": entry.get("personaName") or "",
        "avatarUrl": entry.get("avatarUrl") or "",
        "relation": entry.get("relation") or "",
        "level": lvl,
        "tier": level.level_tier(lvl),
        "totalXp": int(total_xp),
        "unlockedCount": int(unlocked),
        "platinumCount": int(platinum),
        "playtimeMinutes": int(playtime),
        "challengesCompleted": await _challenges_completed(session, steamid),
        "rarityBuckets": await _rarity_buckets(session, steamid),
    }


async def compare(steamids: str | None = None) -> dict:
    """多账号对比；缺省取可切换账号（available_accounts 同源）前 N 个。"""
    from . import service

    roster = {a["steamid"]: a for a in await service.available_accounts()}
    wanted = _parse_steamids(steamids)
    if not wanted:
        wanted = list(roster)[:COMPARE_DEFAULT]
    if not wanted:
        return {"accounts": []}
    async with write_session(WritePriority.BACKGROUND, label="social_compare_baseline") as wsession:
        # 对比读数前先补齐每个账号的存量基线（幂等），未开过等级页的账号不再显示 1 级 0 经验。
        # 基线必须落库后才读：写会话不自动提交，不提交则本次补的基线在读数前被丢弃
        for sid in wanted:
            await level.ensure_baseline(wsession, sid)
        await wsession.commit()
    async with get_session_factory()() as session:
        accounts = [await _compare_row(session, sid, roster.get(sid)) for sid in wanted]
    return {"accounts": accounts}


# ─── 最近解锁动态流（achievement_states join defs，倒序分页）──────


async def recent_unlocks(target: str | None = None, limit: int = FEED_DEFAULT_LIMIT) -> dict:
    """最近解锁流：unlock_time>0 按解锁时刻倒序；稀有度出 rarity_tier 原值。"""
    from . import service

    steamid = await _resolve_steamid(target)
    if not steamid:
        return {"items": []}
    cap = max(1, min(int(limit or FEED_DEFAULT_LIMIT), FEED_MAX_LIMIT))
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AchievementState, AchievementDef, AchievementGame.name)
                .outerjoin(
                    AchievementDef,
                    and_(
                        AchievementDef.appid == AchievementState.appid,
                        AchievementDef.image_name == AchievementState.image_name,
                    ),
                )
                .outerjoin(
                    AchievementGame,
                    and_(
                        AchievementGame.appid == AchievementState.appid,
                        AchievementGame.steamid == AchievementState.steamid,
                    ),
                )
                .where(
                    AchievementState.steamid == steamid,
                    AchievementState.achieved.is_(True),
                    AchievementState.unlock_time > 0,
                )
                .order_by(AchievementState.unlock_time.desc(), AchievementState.image_name)
                .limit(cap)
            )
        ).all()
    items = []
    for st, d, game_name in rows:
        percent = d.global_percent if d else None
        items.append(
            {
                "appid": st.appid,
                "imageName": st.image_name,
                "name": (d.name if d and d.name else st.image_name),
                "iconUrl": d.icon_url if d else None,
                "iconGrayUrl": d.icon_gray_url if d else None,
                "gameName": game_name or "",
                "globalPercent": percent,
                "rarity": service.rarity_tier(percent),
                "unlockTime": int(st.unlock_time or 0),
            }
        )
    return {"items": items}
