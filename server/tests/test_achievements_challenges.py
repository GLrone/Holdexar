"""挑战中心测试：目录完整性 / 各来源进度（含 shared 时长与批注、社交口径）/
重算幂等与 target 回填 / 流转与领取落账 / 重复领取双保险 / 完成与涨级事实幂等 /
summary 计数 / 空账本与空账号 / 路由契约。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, write_session
from app.domains.achievements import challenges as challenges_mod
from app.domains.achievements import level as level_mod
from app.domains.achievements import service as achievements_service
from app.domains.achievements.challenges import (
    CHALLENGE_CATALOG,
    CATALOG_BY_ID,
    PROGRESS_SOURCES,
    REWARD_BY_TIER,
)
from app.domains.achievements.challenges_router import router as challenges_router
from app.domains.achievements.level import XpEvent
from app.domains.achievements.models import AchievementGame, AchievementState
from app.domains.achievements.social import AchievementNote
from app.domains.notifications.models import FactNotice

PRIMARY = "76561198000000001"
OTHER = "76561198000000002"

SUMMARY_KEYS = {"total", "completed", "claimed", "claimable"}
ITEM_KEYS = {
    "challengeId", "tier", "category", "target", "progress", "rewardXp",
    "status", "claimable", "completedAt", "claimedAt", "updatedAt",
}


@pytest.fixture(autouse=True)
def _primary_sid(monkeypatch):
    async def fake_sid():
        return PRIMARY

    monkeypatch.setattr(achievements_service, "_primary_steam_id", fake_sid)


@pytest.fixture(autouse=True)
def _no_social_roster(monkeypatch):
    async def empty_roster():
        return []

    monkeypatch.setattr(achievements_service, "available_accounts", empty_roster)


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()


async def _row(db_factory, challenge_id: str):
    async with db_factory() as session:
        return (
            await session.execute(
                select(challenges_mod.ChallengeProgress).where(
                    challenges_mod.ChallengeProgress.steamid == PRIMARY,
                    challenges_mod.ChallengeProgress.challenge_id == challenge_id,
                )
            )
        ).scalar_one()


# ─── 目录常量 ────────────────────────────────────────────────


def test_catalog_integrity():
    assert len(CHALLENGE_CATALOG) >= 18
    ids = [spec.id for spec in CHALLENGE_CATALOG]
    assert len(set(ids)) == len(ids)
    assert {spec.tier for spec in CHALLENGE_CATALOG} == {"bronze", "silver", "gold"}
    assert {spec.category for spec in CHALLENGE_CATALOG} == {
        "milestone", "cumulative", "streak", "collection", "platinum", "comment", "social",
    }
    for spec in CHALLENGE_CATALOG:
        assert spec.reward_xp == REWARD_BY_TIER[spec.tier]
        assert spec.progress_source in PROGRESS_SOURCES
        assert spec.target >= 1
    assert set(CATALOG_BY_ID) == set(ids)


# ─── 幂等重算 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recompute_idempotent_and_target_follows_catalog(db):
    async with db() as session:
        for i in range(5):
            session.add(
                AchievementState(
                    steamid=PRIMARY, appid=620, image_name=f"A_{i}",
                    achieved=True, unlock_time=1760000000 + i,
                )
            )
        await session.commit()

    first = await challenges_mod.recompute_challenges(PRIMARY)
    second = await challenges_mod.recompute_challenges(PRIMARY)
    keys = ("challengeId", "progress", "target", "status", "completedAt", "claimedAt")
    assert [{k: r[k] for k in keys} for r in first] == [{k: r[k] for k in keys} for r in second]
    assert [r["target"] for r in second] == [spec.target for spec in CHALLENGE_CATALOG]
    by_id = {r["challengeId"]: r for r in second}
    assert by_id["first_trophy"]["progress"] == 5
    assert by_id["first_trophy"]["status"] == "completed" and by_id["first_trophy"]["completedAt"]
    assert by_id["hunter_twelve"]["progress"] == 5 and by_id["hunter_twelve"]["status"] == "active"
    async with db() as session:
        rows = (await session.execute(select(challenges_mod.ChallengeProgress))).scalars().all()
    assert len(rows) == len(CHALLENGE_CATALOG)


# ─── 各进度来源 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_sources_from_states_and_games(db):
    async with db() as session:
        session.add(AchievementState(steamid=PRIMARY, appid=620, image_name="S_A", achieved=True, unlock_time=1760000000))
        session.add(AchievementState(steamid=PRIMARY, appid=620, image_name="S_B", achieved=True, unlock_time=1760000001))
        session.add(AchievementState(steamid=PRIMARY, appid=440, image_name="S_C", achieved=True, unlock_time=1760000002))
        session.add(AchievementState(steamid=PRIMARY, appid=440, image_name="S_LOCKED", achieved=False, unlock_time=0))
        # 620 满盘白金 500 分钟；440 半盘 59 分钟；728 库外 shared（时长恒 0）
        session.add(AchievementGame(steamid=PRIMARY, appid=620, playtime_min=500, total_achievements=2, unlocked=2, platinum=True, source="owned"))
        session.add(AchievementGame(steamid=PRIMARY, appid=440, playtime_min=59, total_achievements=10, unlocked=3, platinum=False, source="owned"))
        session.add(AchievementGame(steamid=PRIMARY, appid=728, playtime_min=0, total_achievements=8, unlocked=0, platinum=False, source="shared"))
        await session.commit()

    rows = {r["challengeId"]: r for r in await challenges_mod.recompute_challenges(PRIMARY)}
    assert rows["first_trophy"]["progress"] == 3 and rows["first_trophy"]["status"] == "completed"
    assert rows["hunter_twelve"]["progress"] == 3 and rows["hunter_twelve"]["status"] == "active"
    assert rows["platinum_first"]["progress"] == 1 and rows["platinum_first"]["status"] == "completed"
    assert rows["platinum_five"]["progress"] == 1 and rows["platinum_five"]["status"] == "active"
    assert rows["breadth_twenty"]["progress"] == 2 and rows["breadth_twenty"]["status"] == "active"
    assert rows["full_clear_first"]["progress"] == 1 and rows["full_clear_first"]["status"] == "completed"
    assert rows["full_clear_triple"]["progress"] == 1 and rows["full_clear_triple"]["status"] == "active"
    assert rows["hours_hundred"]["progress"] == 559  # 500 + 59 + 0（shared 行不计）
    assert rows["buckets_ten"]["progress"] == 4  # 500 分钟→桶3、59 分钟→桶1、0→桶0 不计


@pytest.mark.asyncio
async def test_progress_source_streak_current(db):
    noon = int(datetime(2026, 10, 10, 12, 0, 0).timestamp())
    async with db() as session:
        for offset in (0, 86400, 172800):
            session.add(
                AchievementState(
                    steamid=PRIMARY, appid=620, image_name=f"D_{offset}",
                    achieved=True, unlock_time=noon + offset,
                )
            )
        await session.commit()
    rows = {r["challengeId"]: r for r in await challenges_mod.recompute_challenges(PRIMARY)}
    assert rows["streak_three"]["progress"] == 3 and rows["streak_three"]["status"] == "completed"
    assert rows["streak_seven"]["progress"] == 3 and rows["streak_seven"]["status"] == "active"


@pytest.mark.asyncio
async def test_progress_sources_notes_and_social(db, monkeypatch):
    async with db() as session:
        session.add(AchievementNote(steamid=PRIMARY, appid=620, image_name="N_A", content="第一条", mood="love"))
        session.add(AchievementNote(steamid=PRIMARY, appid=620, image_name="N_B", content="第二条", mood="wow"))
        session.add(AchievementNote(steamid=PRIMARY, appid=440, image_name="N_C", content="第三条"))
        session.add(AchievementState(steamid=PRIMARY, appid=620, image_name="P_1", achieved=True, unlock_time=1760000000))
        await session.commit()

    async def roster_with_family():
        return [{"steamid": PRIMARY}, {"steamid": OTHER}]

    monkeypatch.setattr(achievements_service, "available_accounts", roster_with_family)
    async with db() as session:
        session.add_all(
            AchievementState(
                steamid=OTHER, appid=440, image_name=f"O_{i}",
                achieved=True, unlock_time=1760000100 + i,
            )
            for i in range(12)
        )
        await session.commit()

    rows = {r["challengeId"]: r for r in await challenges_mod.recompute_challenges(PRIMARY)}
    assert rows["note_first"]["progress"] == 3 and rows["note_first"]["status"] == "completed"
    assert rows["note_gallery"]["progress"] == 3 and rows["note_gallery"]["status"] == "active"
    assert rows["social_duo"]["progress"] == 2 and rows["social_duo"]["status"] == "completed"
    assert rows["social_trio"]["progress"] == 1 and rows["social_trio"]["status"] == "active"


# ─── 流转与领取落账 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_active_completed_claimed(db):
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()

    overview = await challenges_mod.challenges_overview(PRIMARY)
    item = {c["challengeId"]: c for c in overview["challenges"]}["platinum_first"]
    assert item["status"] == "completed" and item["claimable"] is True
    assert item["completedAt"] and item["claimedAt"] is None
    assert (item["tier"], item["category"], item["rewardXp"]) == ("bronze", "platinum", 80)

    result = await challenges_mod.claim(None, "platinum_first")
    assert result["challengeId"] == "platinum_first"
    assert result["status"] == "claimed" and result["xpGranted"] == 80
    assert result["level"] == {"previous": 1, "current": 2, "leveledUp": True}

    overview2 = await challenges_mod.challenges_overview(PRIMARY)
    item2 = {c["challengeId"]: c for c in overview2["challenges"]}["platinum_first"]
    assert item2["status"] == "claimed" and item2["claimable"] is False and item2["claimedAt"]

    async with db() as session:
        events = (
            await session.execute(
                select(XpEvent).where(
                    XpEvent.steamid == PRIMARY, XpEvent.source == "challenge_claim"
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].source_ref == "platinum_first" and events[0].xp == 80
        assert await level_mod.total_xp(session, PRIMARY) == result["totalXp"] == 80


@pytest.mark.asyncio
async def test_claim_rejections_keep_ledger_intact(db):
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()
    await challenges_mod.recompute_challenges(PRIMARY)
    result = await challenges_mod.claim(None, "platinum_first")

    with pytest.raises(ValueError):
        await challenges_mod.claim(None, "platinum_first")  # 已领取
    with pytest.raises(ValueError):
        await challenges_mod.claim(None, "hunter_hundred")  # 未完成
    async with db() as session:
        assert await level_mod.existing_source_refs(session, PRIMARY, "challenge_claim") == {
            "platinum_first"
        }
        assert await level_mod.total_xp(session, PRIMARY) == result["totalXp"]
    assert (await _row(db, "hunter_hundred")).status == "active"


@pytest.mark.asyncio
async def test_claim_second_guard_on_xp_unique_key(db):
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()
    await challenges_mod.recompute_challenges(PRIMARY)
    # 状态守卫被穿透形态：奖励流水已在、行状态仍停在 completed
    async with write_session() as wsession:
        await level_mod.record_xp_events(
            wsession,
            PRIMARY,
            [{"source": "challenge_claim", "source_ref": "platinum_first", "xp": 80}],
        )
        await wsession.commit()

    with pytest.raises(ValueError):
        await challenges_mod.claim(None, "platinum_first")
    row = await _row(db, "platinum_first")
    assert row.status == "completed" and row.claimed_at is None
    async with db() as session:
        assert await level_mod.existing_source_refs(session, PRIMARY, "challenge_claim") == {
            "platinum_first"
        }


# ─── 事实通知与 summary ────────────────────────────────────────


@pytest.mark.asyncio
async def test_challenge_completed_fact_fires_once(db):
    async with db() as session:
        session.add(AchievementState(steamid=PRIMARY, appid=620, image_name="F_1", achieved=True, unlock_time=1760000000))
        await session.commit()

    await challenges_mod.recompute_challenges(PRIMARY)
    async with db() as session:
        facts = (
            await session.execute(select(FactNotice).where(FactNotice.kind == "challenge_completed"))
        ).scalars().all()
    assert len(facts) == 1
    assert facts[0].fact_key == f"{PRIMARY}:challenge:first_trophy"
    assert facts[0].data == {"challengeId": "first_trophy", "rewardXp": 80}

    await challenges_mod.recompute_challenges(PRIMARY)
    async with db() as session:
        again = (
            await session.execute(select(FactNotice).where(FactNotice.kind == "challenge_completed"))
        ).scalars().all()
    assert len(again) == 1


@pytest.mark.asyncio
async def test_claim_crossing_level_fires_level_up_fact_once(db):
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()
    await challenges_mod.recompute_challenges(PRIMARY)
    result = await challenges_mod.claim(None, "platinum_first")
    assert result["level"] == {"previous": 1, "current": 2, "leveledUp": True}

    async with db() as session:
        facts = (
            await session.execute(select(FactNotice).where(FactNotice.kind == "level_up"))
        ).scalars().all()
    assert len(facts) == 1
    assert facts[0].data == {"fromLevel": 1, "toLevel": 2}

    with pytest.raises(ValueError):
        await challenges_mod.claim(None, "platinum_first")
    async with db() as session:
        facts2 = (
            await session.execute(select(FactNotice).where(FactNotice.kind == "level_up"))
        ).scalars().all()
    assert len(facts2) == 1


@pytest.mark.asyncio
async def test_summary_counts_mixed_states(db):
    async with db() as session:
        session.add(AchievementState(steamid=PRIMARY, appid=620, image_name="M_1", achieved=True, unlock_time=1760000000))
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()

    overview = await challenges_mod.challenges_overview(PRIMARY)
    assert overview["summary"] == {"total": 20, "completed": 2, "claimed": 0, "claimable": 2}
    await challenges_mod.claim(None, "platinum_first")
    overview2 = await challenges_mod.challenges_overview(PRIMARY)
    assert overview2["summary"] == {"total": 20, "completed": 2, "claimed": 1, "claimable": 1}


# ─── 空形态 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_ledger_all_active(db):
    overview = await challenges_mod.challenges_overview(PRIMARY)
    assert overview["steamid"] == PRIMARY
    assert set(overview["summary"]) == SUMMARY_KEYS
    assert overview["summary"] == {
        "total": len(CHALLENGE_CATALOG), "completed": 0, "claimed": 0, "claimable": 0,
    }
    for item in overview["challenges"]:
        assert set(item) == ITEM_KEYS
        assert item["status"] == "active" and item["progress"] == 0 and item["claimable"] is False
        assert item["completedAt"] is None and item["claimedAt"] is None
        assert item["target"] == CATALOG_BY_ID[item["challengeId"]].target


@pytest.mark.asyncio
async def test_no_account_returns_idle_shape(db, monkeypatch):
    async def no_sid():
        return ""

    monkeypatch.setattr(achievements_service, "_primary_steam_id", no_sid)
    overview = await challenges_mod.challenges_overview("")
    assert overview["steamid"] == "" and len(overview["challenges"]) == len(CHALLENGE_CATALOG)
    assert overview["summary"]["claimable"] == 0
    with pytest.raises(ValueError):
        await challenges_mod.claim("", "first_trophy")
    async with db() as session:
        assert (await session.execute(select(challenges_mod.ChallengeProgress))).scalars().all() == []


# ─── 路由契约 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_challenges_router_contract(db):
    app = FastAPI()
    app.include_router(challenges_router, prefix="/api/v1")
    client = TestClient(app)

    resp = client.get("/api/v1/achievements/challenges")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["steamid"] == PRIMARY
    assert body["summary"]["total"] == len(CHALLENGE_CATALOG)
    assert {c["challengeId"] for c in body["challenges"]} == set(CATALOG_BY_ID)
    assert set(body["challenges"][0]) == ITEM_KEYS
    # GET 即触发重算落行
    async with get_session_factory()() as session:
        rows = (await session.execute(select(challenges_mod.ChallengeProgress))).scalars().all()
    assert len(rows) == len(CHALLENGE_CATALOG)

    assert client.post("/api/v1/achievements/challenges/nope/claim").status_code == 404
    uncompleted = client.post("/api/v1/achievements/challenges/first_trophy/claim")
    assert uncompleted.status_code == 409

    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, platinum=True))
        await session.commit()
    assert client.get("/api/v1/achievements/challenges").status_code == 200
    ok = client.post("/api/v1/achievements/challenges/platinum_first/claim")
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["challengeId"] == "platinum_first" and payload["xpGranted"] == 80
    assert payload["status"] == "claimed" and payload["level"]["leveledUp"] is True
    assert client.post("/api/v1/achievements/challenges/platinum_first/claim").status_code == 409
