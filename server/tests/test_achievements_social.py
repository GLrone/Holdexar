"""achievements 社交面测试：批注幂等与首评 XP、多账号对比聚合、最近解锁
动态流排序、路由校验与 KV 便利贴墙回归。"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.achievements import career, level as level_mod
from app.domains.achievements import service as achievements_service
from app.domains.achievements import social as social_mod
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState
from app.domains.achievements.social_router import router as social_router

PRIMARY = "76561198000000001"
OTHER = "76561198000000002"
THIRD = "76561198000000003"


@pytest.fixture(autouse=True)
def _primary_sid(monkeypatch):
    async def fake_sid():
        return PRIMARY

    monkeypatch.setattr(achievements_service, "_primary_steam_id", fake_sid)
    monkeypatch.setattr(achievements_service, "_sync_task", None)


@pytest.fixture
def db():
    return get_session_factory()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(social_router, prefix="/api/v1")
    return TestClient(app)


# ─── 批注：首评 XP 与幂等 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_first_save_awards_comment_xp(db):
    async with db() as session:
        session.add(AchievementDef(appid=620, image_name="IMG_A", global_percent=12.0))
        session.add(
            AchievementState(steamid=PRIMARY, appid=620, image_name="IMG_A", achieved=True, unlock_time=1760000000)
        )
        await session.commit()

    result = await social_mod.save_note(None, 620, "IMG_A", "很好玩的一枚", "love")
    assert result["firstForAchievement"] is True and result["xpGranted"] == 10
    assert result["note"]["content"] == "很好玩的一枚" and result["note"]["mood"] == "love"

    async with db() as session:
        row = (await session.execute(select(level_mod.XpEvent).where(level_mod.XpEvent.source == "comment"))).scalars().one()
        assert (row.steamid, row.source_ref, row.xp, row.multiplier) == (PRIMARY, "620:IMG_A", 10, 1.0)


@pytest.mark.asyncio
async def test_note_second_save_and_delete_recreate_xp_semantics(db):
    await social_mod.save_note(None, 620, "IMG_A", "第一版", None)
    second = await social_mod.save_note(None, 620, "IMG_A", "第二版", "wow")
    # 编辑重存不发 XP，文案与心情已更新
    assert second["xpGranted"] == 0 and second["firstForAchievement"] is False
    assert second["note"]["content"] == "第二版" and second["note"]["mood"] == "wow"
    async with db() as session:
        assert len((await session.execute(select(level_mod.XpEvent).where(level_mod.XpEvent.source == "comment"))).all()) == 1

    assert await social_mod.remove_note(None, 620, "IMG_A") is True
    again = await social_mod.save_note(None, 620, "IMG_A", "重建", None)
    # 唯一键语义：删除批注不回冲 XP，重建同 source_ref 永不重发
    assert again["xpGranted"] == 0 and again["firstForAchievement"] is False
    async with db() as session:
        assert len((await session.execute(select(level_mod.XpEvent).where(level_mod.XpEvent.source == "comment"))).all()) == 1
    assert await social_mod.remove_note(None, 620, "IMG_A") is True
    assert await social_mod.remove_note(None, 620, "IMG_A") is False


@pytest.mark.asyncio
async def test_note_validation_rejects(db):
    for args in (
        (None, 620, "IMG_A", "   ", None),
        (None, 620, "IMG_A", "字" * 141, None),
        (None, 620, "IMG_A", "ok", "angry"),
        (None, 620, "  ", "ok", None),
    ):
        with pytest.raises(ValueError):
            await social_mod.save_note(*args)


@pytest.mark.asyncio
async def test_notes_list_join_fields_and_fallback(db):
    async with db() as session:
        session.add(
            AchievementDef(appid=620, image_name="HAS", name="高手", icon_url="i.png", icon_gray_url="g.png", global_percent=10.0)
        )
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="传送门"))
        await session.commit()
    await social_mod.save_note(None, 620, "HAS", "有档案", "fun")
    await social_mod.save_note(None, 999, "NO_DEF", "无档案", None)

    listing = await social_mod.list_notes(None)
    assert listing["total"] == 2
    rows = {n["imageName"]: n for n in listing["notes"]}
    full = rows["HAS"]
    assert full["achievementName"] == "高手"
    assert full["achievementIcon"] == "i.png" and full["achievementGrayIcon"] == "g.png"
    assert full["gameName"] == "传送门" and full["mood"] == "fun"
    assert full["createdAt"] and full["updatedAt"]
    bare = rows["NO_DEF"]
    assert bare["achievementName"] == "NO_DEF"
    assert bare["achievementIcon"] is None and bare["gameName"] == ""


# ─── 多账号对比 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_aggregates_from_ledgers(db):
    async with db() as session:
        for img, pct, ach, ts in (
            ("U", 0.8, True, 1760000000),
            ("R", 10.0, True, 1760000100),
            ("C", 80.0, True, 1760000200),
            ("L", 5.0, False, 0),
        ):
            session.add(AchievementDef(appid=620, image_name=img, global_percent=pct))
            session.add(
                AchievementState(steamid=PRIMARY, appid=620, image_name=img, achieved=ach, unlock_time=ts)
            )
        session.add(AchievementGame(steamid=PRIMARY, appid=620, playtime_min=600, total_achievements=1, unlocked=1))
        session.add(AchievementDef(appid=440, image_name="O1", global_percent=90.0))
        session.add(AchievementState(steamid=OTHER, appid=440, image_name="O1", achieved=True, unlock_time=1760000050))
        await level_mod.record_xp_events(session, PRIMARY, [{"source": "comment", "source_ref": "620:U", "xp": 40}])
        # 挑战账本替身（列同构 challenge_progress；表属挑战域唯一账本，此处只读验证）
        await session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS challenge_progress ("
                "steamid VARCHAR(20) NOT NULL, challenge_id VARCHAR(40) NOT NULL, "
                "progress INTEGER NOT NULL DEFAULT 0, "
                "status VARCHAR(12) NOT NULL DEFAULT 'active', completed_at DATETIME, "
                "claimed_at DATETIME, updated_at DATETIME, "
                "PRIMARY KEY (steamid, challenge_id))"
            )
        )
        await session.execute(
            text(
                "INSERT INTO challenge_progress (steamid, challenge_id, progress, status, updated_at) VALUES "
                "(:p, 'a', 1, 'completed', '2026-01-01 00:00:00'), (:p, 'b', 1, 'claimed', '2026-01-01 00:00:00'), "
                "(:p, 'c', 0, 'active', '2026-01-01 00:00:00'), (:o, 'a', 1, 'active', '2026-01-01 00:00:00')"
            ),
            {"p": PRIMARY, "o": OTHER},
        )
        await session.commit()

    result = await social_mod.compare(steamids=f"{PRIMARY},{OTHER},{THIRD}")
    rows = {a["steamid"]: a for a in result["accounts"]}
    p = rows[PRIMARY]
    assert p["level"] == 2 and p["tier"] == "bronze" and p["totalXp"] == 40
    assert p["unlockedCount"] == 3 and p["platinumCount"] == 1 and p["playtimeMinutes"] == 600
    # 1 completed + 1 claimed 计完成，active 不计
    assert p["challengesCompleted"] == 2
    assert p["rarityBuckets"] == [
        {"tier": "ultra", "count": 1},
        {"tier": "very_rare", "count": 0},
        {"tier": "rare", "count": 1},
        {"tier": "uncommon", "count": 0},
        {"tier": "common", "count": 1},
        {"tier": "unknown", "count": 0},
    ]
    o = rows[OTHER]
    # compare 会为无经验记录的账号先补齐存量基线：1 条 common 解锁 = round(15×1.0)
    assert o["level"] == 1 and o["totalXp"] == 15 and o["unlockedCount"] == 1
    assert o["platinumCount"] == 0 and o["playtimeMinutes"] == 0 and o["challengesCompleted"] == 0
    assert o["rarityBuckets"][4] == {"tier": "common", "count": 1}
    t = rows[THIRD]
    assert t["unlockedCount"] == 0 and t["level"] == 1 and t["totalXp"] == 0


@pytest.mark.asyncio
async def test_compare_defaults_and_cap(monkeypatch):
    roster = [
        {"steamid": f"7656119800000000{i}", "personaName": f"n{i}", "avatarUrl": "", "relation": "bound"}
        for i in range(1, 8)
    ]

    async def fake_accounts():
        return roster

    monkeypatch.setattr(achievements_service, "available_accounts", fake_accounts)

    defaulted = await social_mod.compare()
    assert [a["steamid"] for a in defaulted["accounts"]] == [f"7656119800000000{i}" for i in range(1, 5)]

    capped = await social_mod.compare(steamids="s1, s2,s3,s4,s5,s6,s7,s8")
    assert len(capped["accounts"]) == 6
    assert capped["accounts"][0]["steamid"] == "s1"
    assert capped["accounts"][0]["personaName"] == ""


# ─── 最近解锁动态流 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_ordering_limit_and_fields(db):
    async with db() as session:
        for img, pct, ach, ts in (
            ("F1", 0.5, True, 1760000300),
            ("F2", 30.0, True, 1760000100),
            ("F3", 2.0, True, 1760000200),
            ("F0", 60.0, True, 0),
            ("F9", 1.0, False, 1760000900),
        ):
            session.add(
                AchievementDef(
                    appid=620,
                    image_name=img,
                    name=f"def-{img}",
                    icon_url=f"https://icon/{img}.jpg",
                    icon_gray_url=f"https://icon/{img}_bw.jpg",
                    global_percent=pct,
                )
            )
            session.add(
                AchievementState(steamid=PRIMARY, appid=620, image_name=img, achieved=ach, unlock_time=ts)
            )
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="Portal"))
        await session.commit()

    feed = await social_mod.recent_unlocks(None)
    # unlock_time 降序；unlock_time=0 与未解锁不入流
    assert [i["imageName"] for i in feed["items"]] == ["F1", "F3", "F2"]
    top = feed["items"][0]
    assert top["name"] == "def-F1"
    assert top["iconUrl"].endswith("F1.jpg") and top["iconGrayUrl"].endswith("F1_bw.jpg")
    assert top["gameName"] == "Portal" and top["globalPercent"] == 0.5
    assert top["rarity"] == "ultra" and top["unlockTime"] == 1760000300 and top["appid"] == 620

    limited = await social_mod.recent_unlocks(None, limit=1)
    assert [i["imageName"] for i in limited["items"]] == ["F1"]
    # limit<=0 视为缺省（30），非钳 1
    defaulted = await social_mod.recent_unlocks(None, limit=0)
    assert len(defaulted["items"]) == 3


# ─── KV 便利贴墙回归（两本账本互不影响）───────────────────────


@pytest.mark.asyncio
async def test_career_wall_unaffected_by_notes(db):
    await career.add_wall("便利贴一条")
    await social_mod.save_note(None, 620, "IMG_A", "评语一条", None)
    wall = await career.list_wall()
    assert len(wall) == 1 and wall[0]["text"] == "便利贴一条"
    assert (await social_mod.list_notes(None))["total"] == 1
    await career.remove_wall(wall[0]["id"])
    assert await career.list_wall() == []
    assert (await social_mod.list_notes(None))["total"] == 1


# ─── 路由层冒烟 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_social_routes_smoke(db):
    client = _client()
    assert client.get("/api/v1/achievements/notes").status_code == 200
    assert client.get("/api/v1/achievements/compare").status_code == 200
    assert client.get("/api/v1/achievements/feed").status_code == 200

    ok = client.post(
        "/api/v1/achievements/notes",
        json={"appid": 620, "imageName": "S1", "content": "好评", "mood": "wow"},
    )
    assert ok.status_code == 200 and ok.json()["xpGranted"] == 10
    listed = client.get("/api/v1/achievements/notes").json()
    assert listed["total"] == 1 and listed["notes"][0]["imageName"] == "S1"

    assert client.delete("/api/v1/achievements/notes/620/S1").json()["removed"] is True
    assert client.delete("/api/v1/achievements/notes/620/S1").json()["removed"] is False

    bad = client.post(
        "/api/v1/achievements/notes",
        json={"appid": 620, "imageName": "S1", "content": "x" * 141, "mood": None},
    )
    assert bad.status_code == 400
