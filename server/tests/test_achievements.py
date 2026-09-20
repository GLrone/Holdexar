"""achievements 域单元测试：公开页解析、桥接、同步链、汇总与生涯度量（mock 页面与 API）。

覆盖：清单页/个人页解析（中文时间、`_BW` 灰图）、同图复用（复合键）与
名称兜底桥接、三段同步落库、白金/稀有度/最稀有/最近解锁/接近白金汇总、
列表过滤排序搜索、明细排序、无凭证降级，以及生涯域（时长分档 / 活跃热力图 /
偏好画像 / 纪录与里程碑 / 箴言 / 评语墙）的完整口径。
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.achievements import service as achievements_service
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState
from app.domains.games.models import Game
from app.domains.settings import service as settings_service

PRIMARY = "76561198000000001"
# 解锁时间落在近 12 个月窗口内（近 5 天），趋势图末档即有计数
NOW = int(time.time()) - 5 * 86400
THIS_MONTH = datetime.fromtimestamp(NOW)


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(achievements_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.account.models  # noqa: F401
    import app.domains.achievements.models  # noqa: F401
    import app.domains.family.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401 —— tracked_accounts（账号档案兜底源）

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture(autouse=True)
def _primary_sid(monkeypatch):
    async def fake_sid():
        return PRIMARY

    monkeypatch.setattr(achievements_service, "_primary_steam_id", fake_sid)
    monkeypatch.setattr(achievements_service, "_sync_task", None)


@pytest.fixture
def _creds(monkeypatch):
    async def fake_creds(target=None):
        return (target or PRIMARY), [("token", "t")]

    monkeypatch.setattr(achievements_service, "resolve_credentials", fake_creds)


@pytest.fixture
def _no_creds(monkeypatch):
    async def fake_creds(target=None):
        return (target or PRIMARY), []

    monkeypatch.setattr(achievements_service, "resolve_credentials", fake_creds)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认掐断 Web API 明细通道：单测一律不许打真网。

    社区页通道一直是 mock 的，但 API 通道（GetGameAchievements /
    GetPlayerAchievements）若漏 mock，测试会在有网机器上**安静地打真网**。
    需要验证 API 通道的
    用例自行 monkeypatch `_api_try` 注入响应。
    """
    async def fake_try(url, params, creds):
        return None

    monkeypatch.setattr(achievements_service, "_api_try", fake_try)


# ─── 页面夹具（与真实社区页结构逐字段同构）────────────────────

def global_html(appid: int, items: list[tuple]) -> str:
    """items: [(image, name, desc, percent)]"""
    rows = "".join(
        f'<div class="achieveRow ">'
        f'<div class="achieveImgHolder"><img src="https://shared.fastly.steamstatic.com/'
        f'community_assets/images/apps/{appid}/{img}.jpg" width="64"></div>'
        f'<div class="achieveTxtHolder"><div class="achieveFill" style="width: {int(pct)}%"></div>'
        f'<div class="achievePercent">{pct}%</div>'
        f'<div class="achieveTxt"><h3>{name}</h3><h5>{desc}</h5></div>'
        f'<div style="clear: both;"></div></div></div>'
        for img, name, desc, pct in items
    )
    return f'<div id="headerContentLeft"> 总成就: <span class="wt">{len(items)}</span></div>{rows}'


def player_html(appid: int, items: list[tuple]) -> str:
    """items: [(image_with_bw, name, unlock_text_or_None)]"""
    rows = "".join(
        f'<div data-panel="{{}}" role="button" class="achieveRow">'
        f'<div class="achieveImgHolder"><img src="https://shared.fastly.steamstatic.com/'
        f'community_assets/images/apps/{appid}/{img}.jpg"></div>'
        f'<div class="achieveTxtHolder"><div class="achieveTxt"><h3 class="ellipsis">{name}</h3>'
        f'<h5>x</h5></div>'
        + (
            f'<div class="achieveUnlockTime">{unlock}<br/></div>'
            if unlock
            else '<div style="clear: both;"></div>'
        )
        + "</div></div>"
        for img, name, unlock in items
    )
    return rows


# ─── 解析与桥接 ─────────────────────────────────────────────

def test_parse_global_page():
    html = global_html(620, [
        ("SURVIVE_CONTAINER_RIDE", "唤醒闹钟", "人工复活", 74.1),
        ("WAKE_UP", "怪兽", "重新组合 GLaDOS", 0.8),
    ])
    defs = achievements_service.parse_global_page(html, 620)
    assert [d["image_name"] for d in defs] == ["SURVIVE_CONTAINER_RIDE", "WAKE_UP"]
    assert defs[0]["name"] == "唤醒闹钟" and defs[0]["global_percent"] == 74.1
    assert defs[0]["icon_url"].endswith("/620/SURVIVE_CONTAINER_RIDE.jpg")
    assert [d["key"] for d in defs] == ["SURVIVE_CONTAINER_RIDE", "WAKE_UP"]
    assert achievements_service.rarity_tier(defs[1]["global_percent"]) == "ultra"


def test_parse_unlock_time_zh_en():
    p = achievements_service.parse_unlock_time
    assert p("2024 年 11 月 2 日 上午 3:56 解锁") == int(datetime(2024, 11, 2, 3, 56).timestamp())
    assert p("2024 年 11 月 2 日 下午 3:56 解锁") == int(datetime(2024, 11, 2, 15, 56).timestamp())
    assert p("Unlocked 2 Nov, 2024 @ 3:56am") == int(datetime(2024, 11, 2, 3, 56).timestamp())
    assert p("Unlocked 2 Nov, 2024 @ 3:56pm") == int(datetime(2024, 11, 2, 15, 56).timestamp())
    assert p("") == 0


def test_merge_duplicate_icons_and_gray_fallback():
    """同图复用（Bongo 场景）+ 无灰图资产行按名称兜底 + `_BW` 灰图采集。"""
    defs = achievements_service.parse_global_page(global_html(1, [
        ("SHARED", "Bongo Beat 1", "1 beat", 90.8),
        ("SHARED", "Bongo Beat 10", "10 beats", 89.2),
        ("ALONE", "Item Collector", "collect", 23.3),
    ]), 1)
    rows = achievements_service.parse_player_page(player_html(1, [
        ("SHARED_BW", "Bongo Beat 1", None),
        ("SHARED", "Bongo Beat 10", "2024 年 11 月 2 日 上午 3:56 解锁"),
        ("0c3f74f5015862a388cd141002f7d9e4627f4c65", "Item Collector", None),
    ]), 1)
    assert [r["key"] for r in rows] == ["SHARED", "SHARED#1", "0c3f74f5015862a388cd141002f7d9e4627f4c65"]
    assert rows[0]["gray_icon"].endswith("SHARED.jpg")
    merged, unmatched = achievements_service.merge_states(defs, rows)
    assert unmatched == 0
    assert merged["SHARED"]["achieved"] is False
    assert merged["SHARED#1"]["achieved"] is True
    assert merged["ALONE"]["achieved"] is False


# ─── 同步链 ────────────────────────────────────────────────

def _mock_pages(monkeypatch, appid: int, defs_items, player_items):
    async def fake_community(url, params):
        if f"/stats/{appid}/achievements/" in url:
            return global_html(appid, defs_items)
        if f"/stats/{appid}/" in url:
            return player_html(appid, player_items)
        return ""

    monkeypatch.setattr(achievements_service, "_community_get", fake_community)


@pytest.mark.asyncio
async def test_sync_full_chain(db, _creds, monkeypatch):
    async def fake_owned(steamid, creds):
        return [{"appid": 620, "name": "Portal 2", "playtime_forever": 900,
                 "rtime_last_played": NOW - 86400}]

    async def fake_progress(steamid, creds, appids):
        assert appids == [620]
        return {620: {"total": 3, "unlocked": 2}}

    monkeypatch.setattr(achievements_service, "fetch_owned", fake_owned)
    monkeypatch.setattr(achievements_service, "fetch_progress", fake_progress)
    _mock_pages(monkeypatch, 620,
                [("A", "成就一", "描述一", 74.1), ("B", "成就二", "描述二", 0.8),
                 ("C", "成就三", "描述三", 30.0)],
                [("A", "成就一", "2024 年 11 月 2 日 上午 3:56 解锁"),
                 ("B", "成就二", "2024 年 11 月 2 日 上午 4:14 解锁"),
                 ("C_BW", "成就三", None)])

    await achievements_service._sync_all()
    snap = await achievements_service.sync_status()
    assert snap["ok"] is True

    async with db() as session:
        g = (await session.execute(select(AchievementGame))).scalar_one()
        assert (g.playtime_min, g.total_achievements, g.unlocked) == (900, 3, 2)
        assert g.platinum is False
        defs = (await session.execute(
            select(AchievementDef).order_by(AchievementDef.display_order)
        )).scalars().all()
        assert [d.name for d in defs] == ["成就一", "成就二", "成就三"]
        assert defs[2].icon_gray_url is not None
        states = (await session.execute(select(AchievementState))).scalars().all()
        assert len(states) == 3
        assert sum(1 for s in states if s.achieved) == 2


@pytest.mark.asyncio
async def test_sync_no_credential(db, _no_creds):
    with pytest.raises(achievements_service.SyncError):
        await achievements_service._sync_all()


@pytest.mark.asyncio
async def test_start_sync_conflict(db, _creds, monkeypatch):
    import asyncio

    release = asyncio.Event()

    async def slow_sync(steamid=""):
        await release.wait()

    monkeypatch.setattr(achievements_service, "_sync_all", slow_sync)
    await achievements_service.start_sync()
    with pytest.raises(ValueError):
        await achievements_service.start_sync()
    release.set()
    task = achievements_service._sync_task
    if task is not None:
        await task


# ─── Web API 明细通道（官方接口，替代社区页 HTML）──────────────

@pytest.mark.asyncio
async def test_fetch_defs_api_parses_and_keys(db, monkeypatch):
    """定义行：internal_name 落 apiname、图标资产名做行标识、稀有度降序、无图跳过。"""
    async def fake_try(url, params, creds):
        assert url == achievements_service.GAME_ACHIEVEMENTS_URL
        return {"response": {"achievements": [
            {"internal_name": "SHARED_A", "localized_name": "Bongo 1", "localized_desc": "one",
             "icon": "SHARED.jpg", "icon_gray": "SHARED_BW.jpg", "player_percent_unlocked": "90.8"},
            {"internal_name": "SHARED_B", "localized_name": "Bongo 2", "localized_desc": "two",
             "icon": "SHARED.jpg", "icon_gray": "SHARED.jpg", "player_percent_unlocked": "89.2"},
            {"internal_name": "RARE", "localized_name": "稀有", "localized_desc": "rare",
             "icon": "RARE.jpg", "player_percent_unlocked": "0.8"},
            {"internal_name": "NOICON", "localized_name": "无图", "icon": ""},
        ]}}

    monkeypatch.setattr(achievements_service, "_api_try", fake_try)
    defs = await achievements_service.fetch_defs_api(620, [("token", "t")])

    assert [d["apiname"] for d in defs] == ["SHARED_A", "SHARED_B", "RARE"]
    assert [d["key"] for d in defs] == ["SHARED", "SHARED#1", "RARE"]
    assert defs[0]["icon_url"].endswith("/620/SHARED.jpg")
    # 灰图 `_BW` 后缀在行标识里归一，URL 仍是彩图资产
    assert defs[0]["icon_gray_url"].endswith("/620/SHARED.jpg")
    assert defs[2]["global_percent"] == 0.8
    assert achievements_service.rarity_tier(defs[2]["global_percent"]) == "ultra"


def test_merge_states_api_by_apiname(db):
    """解锁态按 apiname 精确对上定义行；对不上的计数（爬虫通道补的定义无 apiname）。"""
    class _Def:
        def __init__(self, apiname, image_name):
            self.apiname = apiname
            self.image_name = image_name

    defs = [_Def("A", "img_a"), _Def("B", "img_b"), _Def("", "img_legacy")]
    merged, unmatched = achievements_service.merge_states_api(defs, {
        "A": {"achieved": True, "unlock_time": 123},
        "B": {"achieved": False, "unlock_time": 0},
        "ZZZ": {"achieved": True, "unlock_time": 1},
    })
    assert set(merged) == {"img_a", "img_b"}
    assert merged["img_a"] == {"achieved": True, "unlock_time": 123, "gray_icon": ""}
    assert unmatched == 1


@pytest.mark.asyncio
async def test_sync_uses_api_channel_when_key_present(db, monkeypatch):
    """有 Key：定义与解锁态都走 Web API，全程不回头抓社区页 HTML。"""
    async def fake_creds(target=None):
        return (target or PRIMARY), [("key", "k"), ("token", "t")]

    async def fake_owned(steamid, creds):
        return [{"appid": 620, "name": "Portal 2", "playtime_forever": 900,
                 "rtime_last_played": NOW - 86400}]

    async def fake_progress(steamid, creds, appids):
        return {620: {"total": 3, "unlocked": 2}}

    def _ach(apiname, image, name, pct):
        return {"internal_name": apiname, "localized_name": name, "localized_desc": "d",
                "icon": f"{image}.jpg", "icon_gray": f"{image}_BW.jpg",
                "player_percent_unlocked": pct}

    async def fake_try(url, params, creds):
        if url == achievements_service.GAME_ACHIEVEMENTS_URL:
            return {"response": {"achievements": [
                _ach("A", "A", "成就一", "74.1"),
                _ach("B", "B", "成就二", "30.0"),
                _ach("C", "C", "成就三", "0.8"),
            ]}}
        if url == achievements_service.PLAYER_ACHIEVEMENTS_URL:
            return {"playerstats": {"achievements": [
                {"apiname": "A", "achieved": 1, "unlocktime": NOW - 3600},
                {"apiname": "B", "achieved": 1, "unlocktime": NOW - 7200},
                {"apiname": "C", "achieved": 0, "unlocktime": 0},
            ]}}
        raise AssertionError(f"未预期的 API：{url}")

    calls = {"community": 0}

    async def fake_community(url, params):
        calls["community"] += 1
        return ""

    monkeypatch.setattr(achievements_service, "resolve_credentials", fake_creds)
    monkeypatch.setattr(achievements_service, "fetch_owned", fake_owned)
    monkeypatch.setattr(achievements_service, "fetch_progress", fake_progress)
    monkeypatch.setattr(achievements_service, "_api_try", fake_try)
    monkeypatch.setattr(achievements_service, "_community_get", fake_community)

    await achievements_service._sync_all()
    assert calls["community"] == 0

    async with db() as session:
        defs = (await session.execute(
            select(AchievementDef).order_by(AchievementDef.display_order)
        )).scalars().all()
        assert [d.apiname for d in defs] == ["A", "B", "C"]
        assert defs[0].icon_gray_url is not None
        states = (await session.execute(select(AchievementState))).scalars().all()
        assert len(states) == 3
        assert sum(1 for s in states if s.achieved) == 2
        g = (await session.execute(select(AchievementGame))).scalar_one()
        assert (g.source, g.unlocked, g.total_achievements) == ("owned", 2, 3)


@pytest.mark.asyncio
async def test_sync_routes_to_requested_account(db, _creds, monkeypatch):
    """`_sync_all(target)` 必须同步**指定账号**而不是主账号（多账号隔离的入口）。"""
    other = "76561198000000002"

    async def fake_owned(steamid, creds):
        assert steamid == other
        return [{"appid": 440, "name": "Team Fortress 2", "playtime_forever": 60,
                 "rtime_last_played": 0}]

    async def fake_progress(steamid, creds, appids):
        assert steamid == other
        return {440: {"total": 10, "unlocked": 1}}

    monkeypatch.setattr(achievements_service, "fetch_owned", fake_owned)
    monkeypatch.setattr(achievements_service, "fetch_progress", fake_progress)
    _mock_pages(monkeypatch, 440, [("A", "成就一", "描述一", 50.0)],
                [("A", "成就一", "2024 年 11 月 2 日 上午 3:56 解锁")])

    await achievements_service._sync_all(other)
    async with db() as session:
        rows = (await session.execute(select(AchievementGame))).scalars().all()
        assert [(r.steamid, r.appid) for r in rows] == [(other, 440)]


@pytest.mark.asyncio
async def test_progress_falls_back_to_keyed_when_token_dead(db, monkeypatch):
    """批量进度通道 401（access_token 失效）时有 Key 就逐款回退，整轮同步不判死。"""
    async def fake_creds(target=None):
        return (target or PRIMARY), [("key", "k"), ("token", "t")]

    async def fake_owned(steamid, creds):
        return [{"appid": 620, "name": "Portal 2", "playtime_forever": 900,
                 "rtime_last_played": 0}]

    calls = {"batch": 0}

    async def dead_batch(steamid, creds, appids):
        calls["batch"] += 1
        raise achievements_service.SyncError("Steam API 鉴权被拒（401/403）")

    async def fake_try(url, params, creds):
        if url == achievements_service.PLAYER_ACHIEVEMENTS_URL:
            return {"playerstats": {"achievements": [
                {"apiname": "A", "achieved": 1, "unlocktime": NOW - 100},
                {"apiname": "B", "achieved": 0, "unlocktime": 0},
            ]}}
        if url == achievements_service.GAME_ACHIEVEMENTS_URL:
            return {"response": {"achievements": [
                {"internal_name": "A", "localized_name": "成就一", "localized_desc": "",
                 "icon": "A.jpg", "player_percent_unlocked": "60.0"},
                {"internal_name": "B", "localized_name": "成就二", "localized_desc": "",
                 "icon": "B.jpg", "player_percent_unlocked": "20.0"},
            ]}}
        raise AssertionError(f"未预期的 API：{url}")

    monkeypatch.setattr(achievements_service, "resolve_credentials", fake_creds)
    monkeypatch.setattr(achievements_service, "fetch_owned", fake_owned)
    monkeypatch.setattr(achievements_service, "fetch_progress", dead_batch)
    monkeypatch.setattr(achievements_service, "_api_try", fake_try)

    await achievements_service._sync_all()

    assert calls["batch"] == 1
    snap = await achievements_service.sync_status()
    assert snap["ok"] is True
    async with db() as session:
        g = (await session.execute(select(AchievementGame))).scalar_one()
        assert (g.total_achievements, g.unlocked, g.source) == (2, 1, "owned")
        states = (await session.execute(select(AchievementState))).scalars().all()
        assert len(states) == 2


@pytest.mark.asyncio
async def test_progress_fallback_skips_recently_synced(db, monkeypatch):
    """逐款兜底不重复点名近一小时内刷过的行（成本线性，重复点名纯浪费）。"""
    calls: list[int] = []

    async def fake_states(steamid, appid, key):
        calls.append(appid)
        return {"A": {"achieved": True, "unlock_time": 1},
                "B": {"achieved": False, "unlock_time": 0}}

    monkeypatch.setattr(achievements_service, "fetch_states_api", fake_states)
    out = await achievements_service.fetch_progress_keyed(
        PRIMARY, [620, 440], "k", skip={620}
    )

    assert calls == [440]
    assert out == {440: {"total": 2, "unlocked": 1}}


@pytest.mark.asyncio
async def test_shared_pool_falls_back_to_family_snapshot(db, monkeypatch):
    """实时共享清单不可得（access_token 失效）→ 用本地家庭库快照枚举候选。"""
    import app.domains.family.models as family_models

    async def dead_groups(token):
        raise RuntimeError("401 Unauthorized")

    async def fake_token():
        return "t"

    monkeypatch.setattr(achievements_service, "_fetch_family_group", dead_groups)
    monkeypatch.setattr(achievements_service, "_local_token", fake_token)

    friend = "76561198000000002"
    async with db() as session:
        session.add(family_models.FamilyGroup(
            steamid=PRIMARY, family_groupid="111", family_name="测试家庭",
            members_json=[{"steamid": PRIMARY}, {"steamid": friend}],
            fetch_ok=True, member_count=2,
        ))
        session.add(family_models.FamilyLibrarySnapshot(
            appid=268910, family_groupid="111", name="Cuphead",
            owners_json=[friend], excluded=False))
        session.add(family_models.FamilyLibrarySnapshot(
            appid=239140, family_groupid="111", name="Dying Light",
            owners_json=[PRIMARY], excluded=False))     # 自己也有 = 已购，不算库外
        session.add(family_models.FamilyLibrarySnapshot(
            appid=999001, family_groupid="111", name="已排除",
            owners_json=[friend], excluded=True))       # 已排除出共享
        await session.commit()

    ids, names = await achievements_service.shared_pool_candidates(PRIMARY)
    assert ids == [268910]
    assert names[268910] == "Cuphead"


@pytest.mark.asyncio
async def test_shared_pool_skips_non_member(db, monkeypatch):
    """目标账号不在该家庭组 → 不借用别人的共享池（隔离）。"""
    import app.domains.family.models as family_models

    async def dead_groups(token):
        raise RuntimeError("401")

    async def fake_token():
        return "t"

    monkeypatch.setattr(achievements_service, "_fetch_family_group", dead_groups)
    monkeypatch.setattr(achievements_service, "_local_token", fake_token)
    other = "76561198000000009"
    async with db() as session:
        session.add(family_models.FamilyGroup(
            steamid=PRIMARY, family_groupid="111",
            members_json=[{"steamid": PRIMARY}], fetch_ok=True, member_count=1,
        ))
        session.add(family_models.FamilyLibrarySnapshot(
            appid=268910, family_groupid="111", name="Cuphead",
            owners_json=["76561198000000002"], excluded=False))
        await session.commit()

    assert await achievements_service.shared_pool_candidates(other) == ([], {})


# ─── 库外（家庭共享等）名册 ─────────────────────────────────

@pytest.mark.asyncio
async def test_sync_collects_external_shared_games(db, _creds, monkeypatch):
    """库外有解锁入册并标 source=shared；未拥有且 0 解锁不入册；失效库外行连解锁态一起清。"""
    async def fake_owned(steamid, creds):
        return [{"appid": 620, "name": "Portal 2", "playtime_forever": 900,
                 "rtime_last_played": NOW - 86400}]

    async def fake_candidates(steamid):
        return [268910, 999001], {268910: "Cuphead", 999001: "从未玩过"}

    async def fake_progress(steamid, creds, appids):
        assert 268910 in appids and 999001 in appids and 620 in appids
        return {620: {"total": 3, "unlocked": 1},
                268910: {"total": 42, "unlocked": 10},
                999001: {"total": 5, "unlocked": 0}}

    monkeypatch.setattr(achievements_service, "fetch_owned", fake_owned)
    monkeypatch.setattr(achievements_service, "shared_pool_candidates", fake_candidates)
    monkeypatch.setattr(achievements_service, "fetch_progress", fake_progress)
    _mock_pages(monkeypatch, 620, [("A", "成就一", "描述一", 74.1)],
                [("A", "成就一", "2024 年 11 月 2 日 上午 3:56 解锁")])

    async with db() as session:
        # 上一轮留下的库外行，已不在共享池里
        session.add(AchievementGame(steamid=PRIMARY, appid=555, name="已不在共享池",
                                    total_achievements=5, unlocked=2, source="shared"))
        session.add(AchievementState(steamid=PRIMARY, appid=555, image_name="X",
                                     achieved=True, unlock_time=NOW))
        # 已购行：任何情况下都不清理
        session.add(AchievementGame(steamid=PRIMARY, appid=730, name="已购保留",
                                    total_achievements=1, unlocked=1, source="owned"))
        await session.commit()

    await achievements_service._sync_all()

    async with db() as session:
        rows = {
            r.appid: r
            for r in (await session.execute(select(AchievementGame))).scalars()
        }
        assert rows[268910].source == "shared" and rows[268910].unlocked == 10
        assert rows[268910].name == "Cuphead"
        assert 999001 not in rows       # 未拥有 + 0 解锁 = 共享池库存，不进殿堂
        assert 555 not in rows          # 失效库外行被清
        assert rows[730].source == "owned"
        left = (await session.execute(
            select(AchievementState).where(AchievementState.appid == 555)
        )).scalars().all()
        assert left == []               # 解锁态随行一起清，不留孤儿


@pytest.mark.asyncio
async def test_prune_external_keeps_zero_unlock_if_still_shared(db, _creds, monkeypatch):
    """仍在共享池但被清到 0 解锁 → 同样出册（库外行只代表「库外但有成就」）。"""
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=268910, name="Cuphead",
                                    total_achievements=42, unlocked=10, source="shared"))
        await session.commit()

    removed = await achievements_service._prune_external(PRIMARY, {268910})
    assert removed == 0  # 还有解锁：保留

    async with db() as session:
        row = (await session.execute(select(AchievementGame))).scalar_one()
        row.unlocked = 0
        await session.commit()

    removed = await achievements_service._prune_external(PRIMARY, {268910})
    assert removed == 1
    async with db() as session:
        assert (await session.execute(select(AchievementGame))).scalars().all() == []


# ─── 多账号隔离 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_account_isolation(db, _creds):
    """两个账号的 KPI / 列表各自成账：显式 steamid 决定读谁，库外单独计数。"""
    other = "76561198000000002"
    async with db() as session:
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="Portal 2",
                                    playtime_min=900, total_achievements=3, unlocked=3,
                                    platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=268910, name="Cuphead",
                                    total_achievements=42, unlocked=10, source="shared"))
        session.add(AchievementGame(steamid=other, appid=440, name="Team Fortress 2",
                                    playtime_min=60, total_achievements=10, unlocked=1))
        session.add(AchievementGame(steamid=other, appid=570, name="Dota 2",
                                    total_achievements=5, unlocked=5, platinum=True))
        await session.commit()

    mine = await achievements_service.get_summary()
    theirs = await achievements_service.get_summary(other)
    assert mine["steamid"] == PRIMARY
    assert mine["gamesWithAchievements"] == 2 and mine["unlockedAchievements"] == 13
    assert (mine["externalGames"], mine["externalUnlocked"], mine["externalPlatinum"]) == (1, 10, 0)
    assert theirs["steamid"] == other
    assert theirs["gamesWithAchievements"] == 2 and theirs["unlockedAchievements"] == 6
    assert theirs["externalGames"] == 0 and theirs["platinum"] == 1

    my_games = await achievements_service.list_games("all", "name")
    assert sorted(g["appid"] for g in my_games["games"]) == [620, 268910]
    assert [g["owned"] for g in my_games["games"] if g["appid"] == 268910] == [False]

    external = await achievements_service.list_games("external", "name")
    assert [g["appid"] for g in external["games"]] == [268910]

    their_games = await achievements_service.list_games("all", "name", "", other)
    assert sorted(g["appid"] for g in their_games["games"]) == [440, 570]
    assert all(g["owned"] for g in their_games["games"])

    detail = await achievements_service.get_game_detail(268910)
    assert detail is not None and detail["owned"] is False and detail["source"] == "shared"


@pytest.mark.asyncio
async def test_accounts_fill_family_profiles(db, monkeypatch):
    """账号清单复用本地档案补齐成员名字/头像：family_groups.members_json 是
    驼峰键（现行写入口径），tracked_accounts 兜底（账户轮转持续维护）。"""
    from app.domains.family.models import FamilyGroup
    from app.domains.wishlist.models import TrackedAccount

    async def fake_list_accounts():
        return [{
            "steam_id": PRIMARY, "persona_name": "主号", "avatar_url": "http://a/p.jpg",
            "is_primary": True, "is_active": True,
        }]

    monkeypatch.setattr(
        achievements_service.account_service, "list_accounts", fake_list_accounts
    )

    async with db() as session:
        session.add(FamilyGroup(steamid=PRIMARY, family_groupid="42", members_json=[
            # 驼峰键：现行快照写入口径
            {"steamid": "76561198000000002", "role": "member",
             "personaName": "玩家甲", "avatarUrl": "http://a/k.jpg"},
            # 两键都缺 → tracked_accounts 补
            {"steamid": "76561198000000003", "role": "member"},
        ]))
        session.add(TrackedAccount(
            steamid="76561198000000003", kinds_json={"wishlist": True, "owned": True},
            persona_name="玩家乙", avatar_url="http://a/f.jpg",
        ))
        await session.commit()

    accounts = {a["steamid"]: a for a in await achievements_service.available_accounts()}
    assert accounts["76561198000000002"]["personaName"] == "玩家甲"
    assert accounts["76561198000000002"]["avatarUrl"] == "http://a/k.jpg"
    assert accounts["76561198000000003"]["personaName"] == "玩家乙"
    assert accounts["76561198000000003"]["avatarUrl"] == "http://a/f.jpg"
    assert accounts[PRIMARY]["relation"] == "bound"
    assert accounts[PRIMARY]["personaName"] == "主号"


# ─── 读取端汇总 ─────────────────────────────────────────────

async def _seed(db):
    async with db() as session:
        session.add(Game(appid=620, name="Portal 2", header_image="https://x/620.jpg"))
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="Portal 2",
                                    playtime_min=900, last_played=NOW - 86400,
                                    total_achievements=3, unlocked=3, platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=440, name="Team Fortress 2",
                                    playtime_min=60, total_achievements=10, unlocked=8))
        session.add(AchievementGame(steamid=PRIMARY, appid=730, name="CS2", playtime_min=10))
        defs = [
            ("A", "成就一", 74.1), ("B", "成就二", 3.0), ("C", "成就三", 0.5),
            ("T1", "TF·一", 60.0), ("T2", "TF·二", 12.0), ("T3", "TF·三", 2.0),
        ]
        for img, name, pct in defs:
            appid = 620 if img in ("A", "B", "C") else 440
            session.add(AchievementDef(appid=appid, image_name=img, name=name,
                                       icon_url=f"https://i/{appid}/{img}.jpg",
                                       global_percent=pct, display_order=0))
        for img, unlock in (("A", NOW - 3600), ("B", NOW - 7200), ("C", NOW - 10800)):
            session.add(AchievementState(steamid=PRIMARY, appid=620, image_name=img,
                                         achieved=True, unlock_time=unlock))
        session.add(AchievementState(steamid=PRIMARY, appid=440, image_name="T1",
                                     achieved=True, unlock_time=NOW - 100))
        session.add(AchievementState(steamid=PRIMARY, appid=440, image_name="T3",
                                     achieved=True, unlock_time=NOW - 200))
        session.add(AchievementState(steamid=PRIMARY, appid=440, image_name="T2",
                                     achieved=False, unlock_time=0))
        await session.commit()


@pytest.mark.asyncio
async def test_summary_aggregates(db, _creds):
    await _seed(db)
    s = await achievements_service.get_summary()
    assert s["platinum"] == 1
    assert s["gamesWithAchievements"] == 2
    assert s["playedGames"] == 3
    assert s["totalAchievements"] == 13
    assert s["unlockedAchievements"] == 11
    assert s["completionRate"] == 84.6
    assert s["totalPlaytimeMin"] == 970

    assert [p["appid"] for p in s["platinums"]] == [620]
    assert s["platinums"][0]["date"] == NOW - 3600
    assert s["platinums"][0]["headerImage"] == "https://x/620.jpg"

    assert s["rarest"][0]["imageName"] == "C" and s["rarest"][0]["globalPercent"] == 0.5
    assert [r["imageName"] for r in s["rarest"][:3]] == ["C", "T3", "B"]
    assert s["recentUnlocks"][0]["imageName"] == "T1"

    assert [g["appid"] for g in s["nearCompletion"]] == [440]
    assert s["nearCompletion"][0]["remaining"] == 2

    buckets = {b["tier"]: b["count"] for b in s["rarityBuckets"]}
    # 已获得成就分档：C 0.5→传说 / B、T3 →极稀有 / A、T1 →常见（T2 未解锁不计）
    assert buckets["ultra"] == 1 and buckets["very_rare"] == 2
    assert buckets["rare"] == 0 and buckets["common"] == 2
    assert len(s["unlockTimeline"]) == 24
    today = datetime.now()
    assert s["unlockTimeline"][-1]["month"] == f"{today.year:04d}-{today.month:02d}"
    seed_month = f"{THIS_MONTH.year:04d}-{THIS_MONTH.month:02d}"
    assert next(t["count"] for t in s["unlockTimeline"] if t["month"] == seed_month) == 5


@pytest.mark.asyncio
async def test_summary_empty_without_account(db, monkeypatch):
    async def no_sid():
        return ""

    monkeypatch.setattr(achievements_service, "_primary_steam_id", no_sid)
    s = await achievements_service.get_summary()
    assert s["hasCredential"] is False
    assert s["platinum"] == 0 and s["platinums"] == []
    assert s["rarityBuckets"] == []


@pytest.mark.asyncio
async def test_list_games_filters_and_sorts(db):
    await _seed(db)
    trophy = await achievements_service.list_games(filter_="trophy")
    assert [g["appid"] for g in trophy["games"]] == [620, 440]
    platinum = await achievements_service.list_games(filter_="platinum")
    assert [g["appid"] for g in platinum["games"]] == [620]
    progress = await achievements_service.list_games(filter_="progress")
    assert [g["appid"] for g in progress["games"]] == [440]
    everything = await achievements_service.list_games(filter_="all", sort_="name")
    assert [g["appid"] for g in everything["games"]] == [730, 620, 440]
    searched = await achievements_service.list_games(filter_="all", q="fortress")
    assert [g["appid"] for g in searched["games"]] == [440]
    assert trophy["games"][0]["remaining"] == 0
    assert trophy["games"][1]["remaining"] == 2


@pytest.mark.asyncio
async def test_game_detail_order_and_payload(db):
    await _seed(db)
    detail = await achievements_service.get_game_detail(440)
    assert detail is not None
    assert detail["unlocked"] == 8 and detail["platinum"] is False
    names = [a["imageName"] for a in detail["achievements"]]
    assert names == ["T1", "T3", "T2"]
    assert detail["achievements"][1]["rarity"] == "very_rare"
    assert detail["achievements"][2]["achieved"] is False
    assert await achievements_service.get_game_detail(999) is None


@pytest.mark.asyncio
async def test_platinum_detail_has_perfect_date(db):
    await _seed(db)
    detail = await achievements_service.get_game_detail(620)
    assert detail["platinum"] is True
    assert detail["perfectDate"] == NOW - 3600


# ─── 游戏生涯度量（career）────────────────────────────────────
#
# career 模块从 `app.core.database` 直接 import 了 get_session_factory，
# 也直接持有 service.resolve_credentials 的引用（不是每次现查模块属性），
# 所以 db / _creds 两个 fixture 在 service 命名空间里打的补丁管不到它——
# 必须在 career 命名空间里再打一次，否则测试会去连真实库。

CAREER_DAYS_EARLY = 45  # 早期解锁日（相对今天）
CAREER_DAYS_LATE = 15   # 晚期解锁日；两者相差 30 天，用于最长空窗与前后的通关跨度


def _ts(days_ago: int, hour: int, minute: int = 0) -> int:
    """指定「N 天前的 hh:mm（本机时区）」的 epoch 秒。"""
    base = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int((base - timedelta(days=days_ago)).timestamp())


@pytest.fixture
def career(monkeypatch, db):
    """career 模块 + 会话工厂/凭证补丁（见上方注释）。"""
    import app.domains.achievements.career as career_module

    async def fake_creds(target=None):
        return PRIMARY, [("token", "t")]

    monkeypatch.setattr(career_module, "get_session_factory", lambda: db)
    monkeypatch.setattr(career_module, "resolve_credentials", fake_creds)
    return career_module


async def _seed_career(db):
    """三款游戏的生涯样本：一款白金（6/6 成就、5 小时通关跨度）、
    一款未完（5/10，跨度 30 天）、一款开了没碰成就（10 分钟）。

    时间刻意跨两天：早期 6 枚集中在 03:00–08:00，晚期 5 枚在 10:00–12:00，
    这样连续天数、最长空窗、时段直方图、星期×时段矩阵同时有可断言的值。
    """
    year = datetime.now().year
    async with db() as session:
        session.add(Game(appid=620, name="Portal 2", header_image="https://x/620.jpg",
                         genres="动作,冒险", developers=["Valve"], publishers=["Valve"],
                         release_date="2011-04-19", series_id="Portal",
                         chinese_support="official", positive_rate=9800, min_cny_fen=3700))
        session.add(Game(appid=440, name="Team Fortress 2", genres="动作",
                         developers=["Valve"], publishers=["Valve"],
                         release_date="2007-10-10", positive_rate=9000))
        session.add(Game(appid=730, name="CS2", release_date=f"{year}-01-15",
                         developers=["Valve"], publishers=["Valve"], chinese_support="official"))
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="Portal 2",
                                    playtime_min=900, last_played=_ts(CAREER_DAYS_EARLY, 8),
                                    total_achievements=6, unlocked=6, platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=440, name="Team Fortress 2",
                                    playtime_min=60, last_played=_ts(CAREER_DAYS_LATE, 12),
                                    total_achievements=10, unlocked=5))
        session.add(AchievementGame(steamid=PRIMARY, appid=730, name="CS2", playtime_min=10))

        # (appid, 图名, 成就名, 全服占比, 描述)
        defs = [
            (620, "P1", "首关", 0.5, "只用一把传送门枪通关全程"),
            (620, "P2", "速通", 3.0, "在二十分钟内完成任意一章"),
            (620, "P3", "收藏家", 12.0, "集齐全部隐藏磁贴"),
            (620, "P4", "工程师", 30.0, "不搭建任何炮塔通过测试室"),
            (620, "P5", "友情提示", 60.0, "把同伴方块带回终点"),
            (620, "P6", "静音", 74.1, ""),
            (440, "T1", "医生", 2.0, "单局治疗量突破一万点"),
            (440, "T2", "爆破手", 6.0, "用粘弹一次炸掉三个敌人"),
            (440, "T3", "工程师", 15.0, "用步哨守住最后一道防线"),
            (440, "T4", "侦察兵", 40.0, ""),
            (440, "T5", "间谍", 55.0, ""),
        ]
        for appid, img, name, pct, desc in defs:
            session.add(AchievementDef(appid=appid, image_name=img, name=name,
                                       description=desc, icon_url=f"https://i/{appid}/{img}.jpg",
                                       global_percent=pct, display_order=0))
        early = [_ts(CAREER_DAYS_EARLY, h) for h in (3, 4, 5, 6, 7, 8)]
        # 440 的 T1 落在早期日 09:00，其余在晚期日 —— 该游戏的通关跨度因此是
        # 「30 天 + 3 小时」，与 620 的 5 小时形成最快/最慢两端
        late = [_ts(CAREER_DAYS_EARLY, 9), _ts(CAREER_DAYS_LATE, 10, 0),
                _ts(CAREER_DAYS_LATE, 10, 30), _ts(CAREER_DAYS_LATE, 11, 0),
                _ts(CAREER_DAYS_LATE, 12, 0)]
        for img, unlock in zip(("P1", "P2", "P3", "P4", "P5", "P6"), early):
            session.add(AchievementState(steamid=PRIMARY, appid=620, image_name=img,
                                         achieved=True, unlock_time=unlock))
        for img, unlock in zip(("T1", "T2", "T3", "T4", "T5"), late):
            session.add(AchievementState(steamid=PRIMARY, appid=440, image_name=img,
                                         achieved=True, unlock_time=unlock))
        await session.commit()


def test_playtime_bucket_boundaries(career):
    """分档边界：曾经写成左闭右闭导致「所有游戏都落第一档」（449/0/0/0/0/0）。"""
    assert [career._bucket_index(m) for m in (0, 1, 59, 60, 299, 300, 1199, 1200, 2999, 3000, 99999)] == [
        0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5
    ]


def test_genre_family_table_is_total(career):
    """族表必须覆盖 Steam 类型取值域：漏一个类型，那一票时长就从画像里静默消失。"""
    domain = {
        "动作", "冒险", "独立", "角色扮演", "模拟", "休闲", "策略", "抢先体验",
        "大型多人在线", "体育", "竞速", "免费开玩", "设计和插画", "动画制作和建模",
        "实用工具", "教育", "网络出版", "游戏开发", "视频制作", "照片编辑",
    }
    missing = {g for g in domain if g not in career.FAMILY_OF}
    assert missing == set(), f"未归族的类型：{missing}"
    # 每个族 id 必须是 ASCII（前端 TS 里写不了中文字面量，只认族 id）
    assert all(f.isascii() for f in career.GENRE_FAMILIES)
    assert all(g not in career.FAMILY_OF for g in ("裸露", ""))


@pytest.mark.asyncio
async def test_career_playtime_and_trophy(career, db):
    await _seed_career(db)
    c = await career.get_career()

    p = c["playtime"]
    assert p["totalMin"] == 970 and p["playedGames"] == 3
    assert p["avgMin"] == 323 and p["medianMin"] == 60 and p["maxMin"] == 900
    assert p["maxGame"]["appid"] == 620
    # 900 分在 [300,1200) / 60 分在 [60,300) / 10 分在 [1,60)
    assert p["histogram"] == [0, 1, 1, 1, 0, 0]
    assert p["over10h"] == 1 and p["over20h"] == 0 and p["idleGames"] == 1
    assert p["untouchedGames"] == 0

    t = c["trophy"]
    assert t["total"] == 16 and t["unlocked"] == 11
    assert t["rate"] == 68.8 and t["gamesWithAchievements"] == 2
    assert t["perfect"] == 1 and t["platinumRate"] == 50.0
    # 极稀有 = ultra + very_rare = 1 + 2（6.0% 已落 rare 档，档界是 <5%）
    assert t["rareCount"] == 3 and t["rareShare"] == 27.3
    assert t["avgRarity"] == 27.1
    assert t["perHour"] == 0.68
    assert t["rarity"] | {} == {"ultra": 1, "very_rare": 2, "rare": 3,
                                "uncommon": 2, "common": 3, "unknown": 0}
    assert c["completedGames"] == 1


@pytest.mark.asyncio
async def test_career_activity_heatmap(career, db):
    await _seed_career(db)
    a = (await career.get_career())["activity"]

    assert a["activeDays"] == 2 and a["totalUnlocks"] == 11
    assert a["longestStreak"] == 1 and a["currentStreak"] == 1
    assert a["maxGapDays"] == 30
    assert a["spanDays"] == 31
    # 早期日 6+1=7 枚，晚期日 4 枚 → 最忙日落在早期日（也就是首日）
    assert a["busiestDay"]["count"] == 7 and a["busiestDay"]["date"] == a["firstDate"]

    # 时段直方图：03–09 各 1 枚，10 点 2 枚，11/12 点各 1 枚
    hh = a["hourHistogram"]
    assert len(hh) == 24 and sum(hh) == 11
    assert [hh[h] for h in (3, 4, 5, 6, 7, 8, 9)] == [1] * 7
    assert [hh[h] for h in (10, 11, 12)] == [2, 1, 1]
    assert a["nightUnlocks"] == 2 and a["morningUnlocks"] == 4 and a["dayUnlocks"] == 5
    assert a["eveningUnlocks"] == 0

    # 星期×时段矩阵：168 格，星期为主序，总数与直方图一致
    hw = a["hourWeekday"]
    assert len(hw) == 168 and sum(hw) == 11
    assert sum(a["weekdayHistogram"]) == 11
    assert a["weekendUnlocks"] == a["weekdayHistogram"][5] + a["weekdayHistogram"][6]

    # 月度是稀疏表，前端按最早→最晚补齐；两日口径下总数必须守恒
    assert sum(m["count"] for m in a["monthly"]) == 11
    assert sum(y["unlocks"] for y in (await career.get_career())["yearly"]) == 11
    # 首枚解锁 = 最稀有那条（0.5%）
    assert a["firstUnlock"]["appid"] == 620 and a["firstUnlock"]["globalPercent"] == 0.5


@pytest.mark.asyncio
async def test_career_records_milestones_quotes(career, db):
    await _seed_career(db)
    c = await career.get_career()
    r = c["records"]

    # 620：6 枚解锁跨 5 小时（03:00–08:00）；440：5 枚跨「30 天 + 3 小时」
    assert r["fastestComplete"]["appid"] == 620 and r["fastestComplete"]["spanMin"] == 300
    assert r["fastestComplete"]["unlocks"] == 6
    assert r["slowestComplete"]["appid"] == 440
    assert r["slowestComplete"]["spanMin"] == 30 * 24 * 60 + 3 * 60
    assert r["slowestComplete"]["unlocks"] == 5
    assert r["marathonDay"]["count"] == 7
    assert r["busiestHour"] == 10
    assert r["mostUnlocksGame"]["appid"] == 620 and r["mostUnlocksGame"]["unlocked"] == 6
    assert r["biggestPlatinum"]["appid"] == 620 and r["biggestPlatinum"]["total"] == 6

    # 里程碑：第 1 枚 + 第 10 枚 + 首枚稀有（0.5% 那条最早，与第 1 枚同一时刻）
    ms = c["milestones"]
    assert len(ms) == 3
    assert sorted(m["index"] for m in ms) == [0, 1, 10]
    rare_node = next(m for m in ms if m["kind"] == "rarest")
    assert rare_node["globalPercent"] == 0.5 and rare_node["appid"] == 620
    first_node = next(m for m in ms if m["index"] == 1)
    assert first_node["kind"] == "count" and first_node["appid"] == 620
    assert first_node["name"] == "首关"
    tenth_node = next(m for m in ms if m["index"] == 10)
    assert tenth_node["appid"] == 440
    assert [m["at"] for m in ms] == sorted(m["at"] for m in ms)

    quotes = c["quotes"]
    # 描述短于 8 字 / 为空的不入选；"工程师" 在 620 与 440 上同名且描述不同，均保留
    assert len(quotes) == 8
    assert quotes[0]["globalPercent"] == 0.5 and quotes[0]["gameName"] == "Portal 2"
    assert [q["globalPercent"] for q in quotes] == sorted(q["globalPercent"] for q in quotes)
    assert all(len(q["text"]) >= 8 for q in quotes)


@pytest.mark.asyncio
async def test_career_taste_and_library(career, db):
    await _seed_career(db)
    c = await career.get_career()
    taste = c["taste"]

    # 类型按「一游戏多类型时均分时长」计：动作 450+60=510，冒险 450
    genres = {g["genre"]: g for g in taste["genres"]}
    assert set(genres) == {"动作", "冒险"}
    assert [g["genre"] for g in taste["genres"]] == ["动作", "冒险"]
    assert genres["动作"]["playtimeMin"] == 510 and genres["动作"]["games"] == 2
    assert genres["冒险"]["playtimeMin"] == 450 and genres["冒险"]["platinum"] == 1

    # 风格族（前端画像词云只认这些 ASCII 族 id；中文归类只做在后端）
    fams = {f["family"]: f for f in taste["families"]}
    assert set(fams) == {"action", "adventure"}
    assert fams["action"]["playtimeMin"] == 510 and fams["action"]["games"] == 2
    assert fams["adventure"]["playtimeMin"] == 450 and fams["adventure"]["platinum"] == 1

    # 厂牌/系列按 name 排序（曾误用 genre 键导致 KeyError）
    assert taste["developers"][0]["name"] == "Valve"
    assert taste["developers"][0]["playtimeMin"] == 970 and taste["developers"][0]["games"] == 3
    assert taste["publishers"][0]["name"] == "Valve"
    assert taste["series"][0]["name"] == "Portal" and taste["series"][0]["playtimeMin"] == 900

    assert [d["decade"] for d in taste["decades"]] == ["2000s", "2010s", "2020s"]
    assert taste["chineseGames"] == 2
    assert taste["freshGames"] == 1
    assert taste["avgReleaseYear"] == (2007 + 2011 + datetime.now().year) // 3
    assert taste["oldestGame"]["appid"] == 440 and taste["oldestGame"]["releaseDate"] == "2007-10-10"
    assert taste["newestGame"]["appid"] == 730

    lib = c["library"]
    assert lib["valueFen"] == 3700 and lib["pricedGames"] == 1
    assert lib["costPerHourFen"] == round(3700 / (970 / 60), 1)
    # positive_rate 是万分比，出参统一成百分数
    assert lib["avgPositiveRate"] == 94.0
    assert lib["topValue"]["appid"] == 620 and lib["topValue"]["positiveRate"] == 98.0


@pytest.mark.asyncio
async def test_career_spotlight_unfinished_dormant(career, db):
    await _seed_career(db)
    c = await career.get_career()

    assert [g["appid"] for g in c["spotlight"]] == [620, 440, 730]
    # 未完待续：0 < unlocked < total，缺口小的在前
    assert c["unfinishedCount"] == 1
    assert [g["appid"] for g in c["unfinished"]] == [440]
    assert c["unfinished"][0]["remaining"] == 5
    # 尘封角落：有实际时长且有最后启动时间，早者在前
    assert [g["appid"] for g in c["dormant"]] == [620, 440]
    assert c["dormant"][0]["lastPlayed"] < c["dormant"][1]["lastPlayed"]
    # 440 没有 header_image，走 Steam CDN 兜底地址
    assert "440" in c["spotlight"][1]["headerImage"]


@pytest.mark.asyncio
async def test_career_empty_without_account(career, monkeypatch):
    async def no_sid(target=None):
        return "", []

    monkeypatch.setattr(career, "resolve_credentials", no_sid)
    c = await career.get_career()
    assert c["hasCredential"] is False
    assert c["playtime"]["totalMin"] == 0
    assert c["trophy"]["unlocked"] == 0 and c["trophy"]["avgRarity"] == 0.0
    assert c["activity"]["hourWeekday"] == [] and c["activity"]["monthly"] == []
    assert c["milestones"] == [] and c["quotes"] == []
    assert c["spotlight"] == [] and c["records"]["fastestComplete"] is None
    assert c["library"]["valueFen"] == 0
    assert c["taste"]["genres"] == []
    assert c["series"] == {"rows": [], "seriesTotal": 0, "taggedTotal": 0, "perfected": 0}


# ─── 系列进度（career.series）──────────────────────────────────
#
# 独立种子：系列聚合要验的是「拥有/已玩/白金/完成度/展示名/排序/门槛」，
# 塞进 _seed_career 会把那边已断言的时长与成就账全打乱。

async def _seed_series(db):
    async with db() as session:
        # 同一系列两款：一款白金（6/6），一款刷了一半（5/10）→ 有 nextGame
        session.add(Game(appid=620, name="Portal 2", series_id="Portal"))
        session.add(Game(appid=440, name="Team Fortress 2", series_id="Portal"))
        session.add(AchievementGame(steamid=PRIMARY, appid=620, name="Portal 2",
                                    playtime_min=900, total_achievements=6, unlocked=6,
                                    platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=440, name="Team Fortress 2",
                                    playtime_min=60, total_achievements=10, unlocked=5))
        # 有系列但一款没碰（进度 0、无 nextGame、封面取任一款）
        session.add(Game(appid=800, name="Dota One", series_id="Dota"))
        session.add(Game(appid=801, name="Dota Two", series_id="Dota"))
        session.add(AchievementGame(steamid=PRIMARY, appid=800, name="Dota One", playtime_min=0))
        session.add(AchievementGame(steamid=PRIMARY, appid=801, name="Dota Two", playtime_min=0))
        # 单款系列：不该进 rows，但要计入 taggedTotal
        session.add(Game(appid=802, name="Solo", series_id="Solo"))
        session.add(AchievementGame(steamid=PRIMARY, appid=802, name="Solo", playtime_min=0))
        # 全白金系列 + 中文展示名（成员展示名的公共汉字前缀 ≥2 字才启用）
        session.add(Game(appid=810, name="英雄传说：闪之轨迹I", series_id="Legend Of Heroes"))
        session.add(Game(appid=811, name="英雄传说：闪之轨迹II", series_id="Legend Of Heroes"))
        session.add(AchievementGame(steamid=PRIMARY, appid=810, name="英雄传说：闪之轨迹I",
                                    playtime_min=100, total_achievements=5, unlocked=5,
                                    platinum=True))
        session.add(AchievementGame(steamid=PRIMARY, appid=811, name="英雄传说：闪之轨迹II",
                                    playtime_min=50, total_achievements=5, unlocked=5,
                                    platinum=True))
        await session.commit()


@pytest.mark.asyncio
async def test_career_series_progress(career, db):
    await _seed_series(db)
    s = (await career.get_career())["series"]

    # 门槛：单款系列不计入 rows，但仍计入「有系列标记」
    assert s["taggedTotal"] == 4
    assert s["seriesTotal"] == 3
    assert s["perfected"] == 1          # 英雄传说（2 款全白金）
    assert [r["seriesId"] for r in s["rows"]] == ["Legend Of Heroes", "Portal", "Dota"]

    # 展示名：成员无公共汉字前缀 → 回落标识；有 → 用公共前缀
    assert s["rows"][0]["name"] == "英雄传说闪之轨迹"
    assert s["rows"][1]["name"] == "Portal"

    duo = s["rows"][0]
    assert duo["owned"] == 2 and duo["played"] == 2 and duo["platinum"] == 2
    assert duo["completed"] == 2 and duo["progress"] == 1.0
    assert duo["topGame"]["appid"] == 810          # 100 分 > 50 分
    assert duo["nextGame"] is None                 # 没有 0<unlocked<total 的成员

    portal = s["rows"][1]
    assert portal["owned"] == 2 and portal["played"] == 2 and portal["platinum"] == 1
    assert portal["completed"] == 1
    assert portal["unlocked"] == 11 and portal["total"] == 16
    # 进度条口径只算已玩成员（两款都已玩，故与整体口径一致）
    assert portal["playedUnlocked"] == 11 and portal["playedTotal"] == 16
    assert abs(portal["progress"] - 0.6875) < 0.001
    assert portal["topGame"]["appid"] == 620
    assert portal["nextGame"]["appid"] == 440 and portal["nextGame"]["remaining"] == 5
    assert portal["nextGame"]["total"] == 10 and portal["nextGame"]["unlocked"] == 5

    dota = s["rows"][2]
    assert dota["played"] == 0 and dota["playedTotal"] == 0 and dota["progress"] == 0.0
    assert dota["nextGame"] is None
    assert dota["topGame"]["appid"] in (800, 801)
    # 无 header_image 时封面走 Steam CDN 兜底地址（不为空）
    assert "800" in dota["topGame"]["headerImage"] or "801" in dota["topGame"]["headerImage"]


@pytest.mark.asyncio
async def test_career_series_sorted_by_engagement(career, db):
    await _seed_series(db)
    rows = (await career.get_career())["series"]["rows"]
    # 排序键 (已玩, 白金, 时长) 全降序：没碰过的大系列不该压过在刷的
    keys = [(-r["played"], -r["platinum"], -r["playtimeMin"]) for r in rows]
    assert keys == sorted(keys)
    assert rows[0]["played"] >= rows[-1]["played"]


# ─── 评语墙（settings KV 持久化）────────────────────────────────

@pytest.mark.asyncio
async def test_wall_add_list_remove(career):
    assert await career.list_wall() == []

    notes = await career.add_wall("  生涯   第一条   ")
    assert len(notes) == 1
    # 内部空白折叠成单空格
    assert notes[0]["text"] == "生涯 第一条"
    assert len(notes[0]["id"]) == 12 and notes[0]["at"] > 0

    second = await career.add_wall("第二条")
    assert [n["text"] for n in second] == ["生涯 第一条", "第二条"]
    # 读回走的是同一持久化通道（settings KV），不是内存副本
    assert [n["text"] for n in await career.list_wall()] == ["生涯 第一条", "第二条"]

    left = await career.remove_wall(second[0]["id"])
    assert [n["text"] for n in left] == ["第二条"]
    assert [n["text"] for n in await career.list_wall()] == ["第二条"]
    # 删不存在的 id 是幂等空操作
    assert [n["text"] for n in await career.remove_wall("nope")] == ["第二条"]


@pytest.mark.asyncio
async def test_wall_rejects_blank_and_overlong(career):
    with pytest.raises(ValueError):
        await career.add_wall("   ")
    with pytest.raises(ValueError):
        await career.add_wall("")
    with pytest.raises(ValueError):
        await career.add_wall("字" * (career.WALL_TEXT_MAX + 1))
    # 正好到上限要收
    ok = await career.add_wall("字" * career.WALL_TEXT_MAX)
    assert len(ok) == 1 and len(ok[0]["text"]) == career.WALL_TEXT_MAX


@pytest.mark.asyncio
async def test_wall_caps_and_drops_oldest(career):
    for i in range(career.WALL_MAX + 5):
        notes = await career.add_wall(f"第 {i} 条")
    assert len(notes) == career.WALL_MAX
    assert notes[0]["text"] == "第 5 条"          # 最旧的 5 条被丢
    assert notes[-1]["text"] == f"第 {career.WALL_MAX + 4} 条"
    assert len(await career.list_wall()) == career.WALL_MAX