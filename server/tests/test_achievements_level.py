"""等级与经验账本测试：曲线单调性/指数、边界与段位、基线幂等、倍率记账、
涨级事实、GET /level 路由（首调基线）与空账号形状。"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, write_session
from app.domains.achievements import level as level_mod
from app.domains.achievements import service as achievements_service
from app.domains.achievements.level_router import router as level_router
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState
from app.domains.notifications.models import FactNotice

PRIMARY = "76561198000000001"
OTHER = "76561198000000002"


@pytest.fixture(autouse=True)
def _primary_sid(monkeypatch):
    async def fake_sid():
        return PRIMARY

    monkeypatch.setattr(achievements_service, "_primary_steam_id", fake_sid)
    monkeypatch.setattr(achievements_service, "_sync_task", None)


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()


async def _seed_defs_states(session, sid: str, rows: list[tuple]) -> None:
    """rows: [(appid, image_name, global_percent, achieved, unlock_time)]"""
    for appid, img, pct, achieved, unlock_time in rows:
        session.add(AchievementDef(appid=appid, image_name=img, global_percent=pct))
        session.add(
            AchievementState(
                steamid=sid, appid=appid, image_name=img,
                achieved=achieved, unlock_time=unlock_time,
            )
        )
    await session.commit()


async def _count(session, source: str | None = None, sid: str = PRIMARY) -> int:
    stmt = select(level_mod.XpEvent.id).where(level_mod.XpEvent.steamid == sid)
    if source is not None:
        stmt = stmt.where(level_mod.XpEvent.source == source)
    return len((await session.execute(stmt)).all())


# ─── 曲线纯函数 ───────────────────────────────────────────────


def test_curve_strictly_increasing_and_n1_cost():
    costs = [level_mod.xp_for_level(n) for n in range(1, 301)]
    assert all(b > a for a, b in zip(costs, costs[1:]))
    assert costs[0] == 30


def test_curve_exponent_growth_ratio():
    for n in range(1, 299):
        ratio = level_mod.xp_for_level(n + 1) / level_mod.xp_for_level(n)
        expected = ((n + 1) / n) ** 1.5
        assert abs(ratio - expected) < 0.02, n


def test_level_for_xp_boundaries():
    assert level_mod.xp_for_level(1) == 30
    assert level_mod.total_xp_for_level(1) == 0
    assert level_mod.total_xp_for_level(2) == 30
    assert level_mod.total_xp_for_level(3) == 115
    assert level_mod.level_for_xp(0) == (1, 0, 30)
    assert level_mod.level_for_xp(29) == (1, 29, 30)
    assert level_mod.level_for_xp(30) == (2, 0, 85)
    assert level_mod.level_for_xp(114) == (2, 84, 85)
    assert level_mod.level_for_xp(115) == (3, 0, 156)
    assert level_mod.level_from_xp(0) == level_mod.level_for_xp(0)


def test_tier_boundaries():
    assert level_mod.level_tier(9) == "bronze" and level_mod.level_tier(10) == "silver"
    assert level_mod.level_tier(19) == "silver" and level_mod.level_tier(20) == "gold"
    assert level_mod.level_tier(39) == "gold" and level_mod.level_tier(40) == "platinum"
    assert level_mod.level_tier(69) == "platinum" and level_mod.level_tier(70) == "legend"
    assert level_mod.next_tier(13) == "gold" and level_mod.next_tier(70) is None
    assert level_mod.tier_progress(13) == pytest.approx(0.3)
    assert level_mod.tier_progress(70) is None


def test_streak_multiplier_bands():
    assert [level_mod.streak_multiplier(d) for d in (1, 2, 6, 7, 13, 14, 29, 30, 45)] == [
        1.0, 1.1, 1.1, 1.25, 1.25, 1.5, 1.5, 2.0, 2.0,
    ]


def test_unlock_batch_cover_is_selected_only_for_one_game():
    same_game = [{"appid": 620}, {"appid": 620}]
    mixed_games = [{"appid": 620}, {"appid": 440}]
    missing_game = [{"appid": 620}, {}]

    assert achievements_service._single_unlock_appid(same_game) == 620
    assert achievements_service._single_unlock_appid(mixed_games) is None
    assert achievements_service._single_unlock_appid(missing_game) is None


# ─── 记账与基线 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_and_record_unlock_with_multipliers(db):
    xp, mult = level_mod.compose_xp("achievement_unlock", rarity_percent=0.5, streak_days=30)
    assert (xp, mult) == (90, 6.0)  # 15 × ultra 3.0 × 连猎 2.0
    async with db() as session:
        inserted = await level_mod.record_xp_events(
            session, PRIMARY,
            [{"source": "achievement_unlock", "source_ref": "620:WAKE_UP", "xp": xp, "multiplier": mult}],
        )
        await session.commit()
        assert inserted == 1
        row = (await session.execute(select(level_mod.XpEvent))).scalars().one()
        assert (row.xp, row.multiplier, row.source_ref) == (90, 6.0, "620:WAKE_UP")
        # 唯一键重复写被忽略
        again = await level_mod.record_xp_events(
            session, PRIMARY,
            [{"source": "achievement_unlock", "source_ref": "620:WAKE_UP", "xp": 999}],
        )
        assert again == 0
        assert await _count(session, "achievement_unlock") == 1


@pytest.mark.asyncio
async def test_playtime_bucket_boundaries_and_sources(db):
    assert [level_mod.playtime_bucket_index(m) for m in (0, 1, 59, 60, 299, 300, 1199, 1200, 2999, 3000)] == [
        0, 1, 1, 2, 2, 3, 3, 4, 4, 5,
    ]
    async with db() as session:
        await level_mod.record_xp_events(
            session, PRIMARY,
            [
                {"source": "platinum", "source_ref": "620", "xp": 200},
                {"source": "streak_day", "source_ref": "2026-10-12", "xp": 25, "multiplier": 1.25},
                # 同日重复（第 N 天重复落）被唯一键拦下
                {"source": "streak_day", "source_ref": "2026-10-12", "xp": 25, "multiplier": 1.25},
            ],
        )
        await session.commit()
        assert await _count(session, "platinum") == 1
        assert await _count(session, "streak_day") == 1


@pytest.mark.asyncio
async def test_fixed_value_sources_and_unknown_source_rejected(db):
    async with db() as session:
        assert await level_mod.record_xp_events(
            session, PRIMARY,
            [
                {"source": "challenge_claim", "source_ref": "platinum_first", "xp": 80},
                {"source": "comment", "source_ref": "620:WAKE_UP", "xp": 10},
            ],
        ) == 2
        await session.commit()
        assert await _count(session, "challenge_claim") == 1
        assert await _count(session, "comment") == 1
        with pytest.raises(ValueError):
            await level_mod.record_xp_events(
                session, PRIMARY,
                [{"source": "free_xp", "source_ref": "x", "xp": 5}],
            )


@pytest.mark.asyncio
async def test_baseline_idempotent_and_per_account(db):
    async with db() as session:
        await _seed_defs_states(
            session, PRIMARY,
            [
                (620, "ULTRA_ACH", 0.8, True, 1760000000),   # ultra ×3 → 45
                (620, "COMMON_ACH", 74.1, True, 1760000100),  # common ×1 → 15
                (620, "LOCKED_ACH", 12.0, False, 0),          # 未解锁不计
            ],
        )
        session.add(AchievementGame(steamid=PRIMARY, appid=620, playtime_min=500, platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=440, playtime_min=0, platinum=False))
        await session.commit()

    async with write_session() as session:
        first = await level_mod.ensure_baseline(session, PRIMARY)
        await session.commit()
    # 45 + 15 + 200（白金）+ 120（500 分钟=桶 3 ×40）= 380
    assert first == {"source": "baseline", "source_ref": "v1", "xp": 380,
                     "multiplier": 1.0, "note": level_mod.BASELINE_NOTE}
    async with write_session() as session:
        second = await level_mod.ensure_baseline(session, PRIMARY)
        await session.commit()
        assert second is None
        assert await _count(session, "baseline") == 1
        # 另一账号独立回填，互不串：空账号落 0 XP 哨兵
        other = await level_mod.ensure_baseline(session, OTHER)
        await session.commit()
        assert other["xp"] == 0
        assert await _count(session, "baseline", sid=OTHER) == 1
        assert await _count(session, "baseline", sid=PRIMARY) == 1


# ─── 涨级事实与读数 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_level_up_fact_fires_once(db):
    async with db() as session:
        await _seed_defs_states(
            session, PRIMARY,
            [(620, f"ACH_{i}", 0.8, True, 1760000000 + i) for i in range(30)],
        )
        await session.commit()

    async with write_session() as session:
        baseline = await level_mod.ensure_baseline(session, PRIMARY)
        await session.commit()
    assert baseline["xp"] == 30 * 45  # 30 枚 ultra × 45

    async with db() as session:
        fired = await level_mod.notify_level_up(session, PRIMARY, previous_level=1)
        assert fired is True
        assert await level_mod.notify_level_up(session, PRIMARY, previous_level=1) is False
    async with db() as session:
        facts = (await session.execute(select(FactNotice))).scalars().all()
    assert len(facts) == 1
    assert facts[0].source == "achievements" and facts[0].kind == "level_up"
    assert facts[0].data == {"fromLevel": 1, "toLevel": level_mod.level_for_xp(1350)[0]}

    # 已在当级的二次同步不重发
    current = level_mod.level_for_xp(1350)[0]
    async with db() as session:
        assert await level_mod.notify_level_up(session, PRIMARY, previous_level=current) is False
    async with db() as session:
        facts = (await session.execute(select(FactNotice))).scalars().all()
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_get_level_route_backfills_baseline_once():
    app = FastAPI()
    app.include_router(level_router, prefix="/api/v1")
    client = TestClient(app)

    resp = client.get("/api/v1/achievements/level")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["steamid"] == PRIMARY and body["level"] == 1 and body["totalXp"] == 0
    assert body["xpForNextLevel"] == 30 and body["tier"] == "bronze"
    assert len(body["curvePreview"]) == 6 and len(body["milestones"]) == 7
    assert body["activeBonuses"] == []

    async with get_session_factory()() as session:
        assert await _count(session, "baseline") == 1

    assert client.get("/api/v1/achievements/level").json()["totalXp"] == 0
    async with get_session_factory()() as session:
        assert await _count(session, "baseline") == 1


@pytest.mark.asyncio
async def test_profile_shape_for_empty_account(db):
    profile = await level_mod.level_profile()
    assert profile["steamid"] == PRIMARY
    assert profile["level"] == 1 and profile["totalXp"] == 0 and profile["xpIntoLevel"] == 0
    assert profile["xpForNextLevel"] == 30 and profile["xpRemaining"] == 30
    assert profile["lastLevelCost"] == 0 and profile["nextCostGrowth"] == 0.0
    assert profile["tier"] == "bronze" and profile["nextTier"] == "silver"
    assert [m["level"] for m in profile["milestones"]] == [10, 25, 50, 75, 100, 150, 200]
    assert all(m["reached"] is False for m in profile["milestones"])
    assert profile["activeBonuses"] == []
    assert [e["source"] for e in profile["recentEvents"]] == ["baseline"]
