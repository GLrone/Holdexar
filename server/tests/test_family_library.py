"""family 域单元测试：家庭库聚合逻辑（mock Steam API，不触真实网络）。

覆盖：共享库 ∪ 成员已购合并 / presence 与 ownerCount / 游玩时长聚合 /
本地 games 表元数据与 CN 价补齐 / 缓存失效 / 多账号家庭组同步 /
成员地区判定链（手动 > 钱包结算区 > 资料国家 > 未设置）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.family import service as family_service
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(family_service, "get_session_factory", lambda: factory)
    # family 经 account_service.get_primary_cookies() 读表：
    # 模块级 factory 引用必须单独打桩，否则直读生产库
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    # miniprofile 头像兜底默认静默空（防单测触网）；验证头像链路的用例自行覆盖
    async def _no_preview(sid):
        return {"personaName": "", "avatarUrl": ""}
    monkeypatch.setattr(family_service, "_persona_preview", _no_preview)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _reset_family_cache():
    """家庭库缓存/刷新账本是模块级状态：跨用例重置，防顺序耦合。"""
    family_service._LIBRARY_CACHE = {}
    family_service._LIBRARY_REFRESHING = set()
    yield
    family_service._LIBRARY_CACHE = {}
    family_service._LIBRARY_REFRESHING = set()


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.account.models  # noqa: F401
    import app.domains.alerts.models  # noqa: F401
    import app.domains.crawl.models  # noqa: F401
    import app.domains.family.models  # noqa: F401
    import app.domains.proxies.models  # noqa: F401
    import app.domains.rates.models  # noqa: F401
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


PRIMARY = "76561198000000001"
FRIEND = "76561198000000002"

# 绑定账号 Cookie（登录态新鲜，session_freshness 不判过期）
COOKIE = f"sessionid=s; steamLoginSecure={PRIMARY}%7C%7Cjwt-token"


async def _seed_account(db, sid=PRIMARY, cookies=COOKIE, wallet=None):
    """绑定账号行：家庭链路按账号表取 Cookie（多账号语义）。"""
    from app.domains.account.models import SteamAccount

    async with db() as session:
        session.add(SteamAccount(
            steam_id=sid, cookies=cookies, wallet_json=wallet,
            is_active=True, bound_at=datetime.utcnow(),
        ))
        await session.commit()


async def _clear_account_cookie(db, sid=PRIMARY):
    """摘除账号行 Cookie（实时聚合必失败的模拟）。"""
    from app.domains.account.models import SteamAccount

    async with db() as session:
        row = await session.get(SteamAccount, sid)
        row.cookies = ""
        await session.commit()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _mock_steam(monkeypatch, *, shared_apps, owned_by_member, group_name="测试家庭"):
    """Mock 三个 Steam 端点：家庭组 / 共享库 / 成员已购。"""
    endpoints = {}

    async def fake_get(url, params, method="GET"):
        if "GetFamilyGroupForUser" in url:
            return _Resp({"response": {
                "family_groupid": 111,
                "family_group": {
                    "name": group_name,
                    "members": [
                        {"steamid": PRIMARY, "role": "1"},
                        {"steamid": FRIEND, "role": "2"},
                    ],
                },
            }})
        if "GetSharedLibraryApps" in url:
            return _Resp({"response": {"apps": shared_apps}})
        if "GetOwnedGames" in url:
            sid = params.get("steamid")
            return _Resp({"response": {"games": owned_by_member.get(sid, [])}})
        if "GetPlayerLinkDetails" in url:
            return _Resp({"response": {"accounts": [
                {"public_data": {"steamid": sid, "persona_name": f"成员{i + 1}", "avatar": ""}}
                for i, sid in enumerate([PRIMARY, FRIEND])
            ]}})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(family_service, "_steam_get", fake_get)


async def _seed_games(db):
    """games 表 + CN 现价（本地元数据补齐验证用）。"""
    async with db() as session:
        session.add(Game(appid=620, name="Portal 2", header_image="https://x/620.jpg",
                         release_date="2011-04-19"))
        session.add(Game(appid=570, name="Dota 2", release_date="2013-07-09"))
        session.add(GameCurrentPrice(appid=620, region_code="CN", price=4200,
                                     cny_fen=4200, discount_percent=0))
        await session.commit()


@pytest.mark.asyncio
async def test_library_merge_and_aggregation(db, monkeypatch):
    """共享库 ∪ 成员已购合并 + presence/owners/playtime 聚合 + 本地元数据补齐。"""
    await _seed_account(db)
    await _seed_games(db)

    # 620 两人共享（presence=2）；570 仅主账号有（不在共享清单但成员已购）
    _mock_steam(
        monkeypatch,
        shared_apps=[
            {"appid": 620, "presence_count": 2},
            # Steam 的 exclude_reason 字段**恒存在**，0 = Included（可共享）。
            # 判「被排除」必须严格小于 0 才算排除，`is not None` 会误伤本款。
            {"appid": 730, "presence_count": 1, "exclude_reason": 0},
            {"appid": 99999, "presence_count": 1, "exclude_reason": 2},  # 本地无行的游戏
        ],
        owned_by_member={
            PRIMARY: [
                {"appid": 620, "playtime_forever": 300, "playtime_2weeks": 60,
                 "rtime_last_played": 1700000100},
                {"appid": 570, "playtime_forever": 120, "playtime_2weeks": 0,
                 "rtime_last_played": 1699000000},
            ],
            FRIEND: [
                {"appid": 620, "playtime_forever": 90, "playtime_2weeks": 30,
                 "rtime_last_played": 1700000200},
            ],
        },
    )

    data = await family_service.fetch_family_library()

    # 成员档案 + 已购计数
    assert data["familyName"] == "测试家庭"
    assert len(data["members"]) == 2
    by_sid = {m["steamid"]: m for m in data["members"]}
    assert by_sid[PRIMARY]["ownedCount"] == 2
    assert by_sid[FRIEND]["ownedCount"] == 1

    games = {g["appid"]: g for g in data["games"]}
    # 合并：620（共享）+ 570（仅已购）+ 730（共享·可共享）+ 99999（仅共享清单）
    assert set(games) == {620, 570, 730, 99999}

    # 620：本地元数据 + CN 价 + 双人共享
    g620 = games[620]
    assert g620["name"] == "Portal 2"
    assert g620["headerImage"] == "https://x/620.jpg"
    assert g620["cnPriceFen"] == 4200
    assert g620["ownerCount"] == 2
    assert sorted(g620["owners"]) == sorted([PRIMARY, FRIEND])
    assert g620["presence"] == 2
    assert g620["inSharedLib"] is True
    # 游玩聚合：300+90=390 分钟，最近游玩取最大时间戳
    assert g620["playtimeMinutes"] == 390
    assert g620["lastPlayed"] == 1700000200

    # 570：不在共享清单但成员已购（inSharedLib=False）
    g570 = games[570]
    assert g570["inSharedLib"] is False
    assert g570["ownerCount"] == 1
    assert g570["cnPriceFen"] is None  # 未播种 CN 价格

    # 99999：本地无行（name=None），被排除标记
    g99999 = games[99999]
    assert g99999["name"] is None
    assert g99999["excluded"] is True

    # 730：exclude_reason=0 = Included → 可共享，不是「已排除」
    g730 = games[730]
    assert g730["inSharedLib"] is True
    assert g730["excluded"] is False

    # 成员游玩序列（按近2周时长排序）
    p_primary = data["memberPlay"][PRIMARY]
    assert p_primary[0]["appid"] == 620  # 2周60分钟 > 570 的 0
    assert p_primary[0]["minutes2w"] == 60


@pytest.mark.asyncio
async def test_library_member_avatar_merge(db, monkeypatch):
    """成员头像合并：GetPlayerLinkDetails 已不返 avatar URL → 从
    family_groups.members_json（同步链路 miniprofile 补齐过）合并，
    快照仍缺的成员走 miniprofile 实时兜底。"""
    await _seed_account(db)
    _mock_steam(monkeypatch, shared_apps=[], owned_by_member={})

    # 模拟同步链路落库的成员档案（_save_group 存 camelCase）
    from app.domains.family.models import FamilyGroup

    async with db() as session:
        session.add(FamilyGroup(
            steamid=PRIMARY,
            family_groupid="111",
            family_name="测试家庭",
            members_json=[
                {"steamid": PRIMARY, "role": "1", "personaName": "主号",
                 "avatarUrl": "https://steamcdn/ava_primary.jpg"},
                {"steamid": FRIEND, "role": "2", "personaName": "", "avatarUrl": ""},
            ],
            member_count=2,
        ))
        await session.commit()

    # FRIEND 快照缺头像 → miniprofile 兜底补齐
    async def fake_preview(sid):
        if sid == FRIEND:
            return {"personaName": "好友昵称", "avatarUrl": "https://steamcdn/ava_friend.jpg"}
        return {"personaName": "", "avatarUrl": ""}
    monkeypatch.setattr(family_service, "_persona_preview", fake_preview)

    data = await family_service.fetch_family_library()
    by_sid = {m["steamid"]: m for m in data["members"]}
    # 主号：实时 persona（mock 里 GetPlayerLinkDetails 的 persona_name）保留，
    # 头像从快照合并
    assert by_sid[PRIMARY]["personaName"] == "成员1"
    assert by_sid[PRIMARY]["avatarUrl"] == "https://steamcdn/ava_primary.jpg"
    # 好友：快照缺头像 → miniprofile 补齐
    assert by_sid[FRIEND]["avatarUrl"] == "https://steamcdn/ava_friend.jpg"


@pytest.mark.asyncio
async def test_library_cache_invalidation(db, monkeypatch):
    """缓存：5 分钟内复用快照；invalidate 后重拉。"""
    await _seed_account(db)
    _mock_steam(monkeypatch, shared_apps=[], owned_by_member={})

    calls = {"n": 0}

    real_fetch = family_service.fetch_family_library

    async def counting_fetch(steam_id=None):
        calls["n"] += 1
        return await real_fetch(steam_id)

    monkeypatch.setattr(family_service, "fetch_family_library", counting_fetch)

    await family_service.cached_family_library()
    await family_service.cached_family_library()
    assert calls["n"] == 1  # 第二次命中缓存

    family_service.invalidate_library_cache()
    await family_service.cached_family_library()
    assert calls["n"] == 2  # 失效后重拉


@pytest.mark.asyncio
async def test_library_requires_cookie(db):
    """无绑定账号 → 尚未绑定引导；账号存在但 Cookie 无登录态 → webapi_token 引导。"""
    with pytest.raises(ValueError, match="尚未绑定"):
        await family_service.fetch_family_library()

    from app.domains.account.models import SteamAccount

    async with db() as session:
        session.add(SteamAccount(
            steam_id=PRIMARY, cookies="sessionid=s",
            is_active=True, bound_at=datetime.utcnow(),
        ))
        await session.commit()
    with pytest.raises(ValueError, match="webapi_token"):
        await family_service.fetch_family_library()


@pytest.mark.asyncio
async def test_shared_library_fields_preserved(db, monkeypatch):
    """共享清单关键字段保留：time_acquired / owner_steamids 原序 / buyer 语义。

    owner_steamids 按入库先后排（[0]=最早、
    at(-1)=最近入库=购买者），rt_time_acquired 是热力图/购买动态的口径源。
    """
    await _seed_account(db)
    _mock_steam(
        monkeypatch,
        shared_apps=[
            {
                "appid": 620,
                "presence_count": 2,
                "rt_time_acquired": 1690000000,
                "owner_steamids": [FRIEND, PRIMARY],  # FRIEND 先入库（购买者=PRIMARY）
                "name": "Portal 2 (steam)",
            },
        ],
        owned_by_member={
            # 注意：成员已购的遍历序是 PRIMARY 在前——共享清单原序必须压过它
            PRIMARY: [{"appid": 620, "playtime_forever": 10}],
            FRIEND: [{"appid": 620, "playtime_forever": 5}],
        },
    )

    data = await family_service.fetch_family_library()
    g = {x["appid"]: x for x in data["games"]}[620]

    assert g["timeAcquired"] == 1690000000
    assert g["owners"] == [FRIEND, PRIMARY]  # Steam 原序（非成员遍历序）
    assert g["buyer"] == PRIMARY             # at(-1) = 最近入库者


@pytest.mark.asyncio
async def test_snapshot_upsert_and_fallback(db, monkeypatch):
    """持久化兜底链：聚合成功落快照 → 实时失败（如 Cookie 失效）时从快照重建。"""
    await _seed_account(db)
    # 组档案先行（真实流程：家庭组同步建组档案，库聚合随后）
    from app.domains.family.models import FamilyGroup

    async with db() as session:
        session.add(FamilyGroup(
            steamid=PRIMARY, family_groupid="111", family_name="测试家庭",
            members_json=[], member_count=2,
        ))
        await session.commit()
    _mock_steam(
        monkeypatch,
        shared_apps=[
            {"appid": 620, "presence_count": 2, "rt_time_acquired": 1690000000,
             "owner_steamids": [FRIEND, PRIMARY], "name": "Portal 2"},
            {"appid": 570, "presence_count": 1, "rt_time_acquired": 1695000000,
             "owner_steamids": [PRIMARY], "name": "Dota 2"},
        ],
        owned_by_member={},
    )

    # 1. 聚合成功 → 快照落库
    data = await family_service.fetch_family_library()
    assert len(data["games"]) == 2
    assert not data.get("fromSnapshot")

    family_service.invalidate_library_cache()

    # 2. Cookie 摘除（实时聚合必失败）→ cached 回快照兜底
    await _clear_account_cookie(db)
    family_service.invalidate_library_cache()
    snap = await family_service.cached_family_library()

    assert snap.get("fromSnapshot") is True
    games = {g["appid"]: g for g in snap["games"]}
    assert set(games) == {620, 570}
    assert games[620]["timeAcquired"] == 1690000000
    assert games[620]["owners"] == [FRIEND, PRIMARY]
    assert games[620]["buyer"] == PRIMARY
    assert games[570]["buyer"] == PRIMARY

    # 3. 完全无快照无 Cookie → 仍诚实抛 ValueError（无兜底可用）
    from app.domains.family.models import FamilyLibrarySnapshot

    async with db() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(FamilyLibrarySnapshot)
        )).scalars().all()
        for r in rows:
            await session.delete(r)
        await session.commit()
    family_service.invalidate_library_cache()

    with pytest.raises(ValueError):
        await family_service.cached_family_library()


@pytest.mark.asyncio
async def test_library_requires_family_group(db, monkeypatch):
    """未加入家庭组 → ValueError 诚实引导。"""
    await _seed_account(db)

    async def fake_get(url, params, method="GET"):
        if "GetFamilyGroupForUser" in url:
            return _Resp({"response": {}})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(family_service, "_steam_get", fake_get)
    with pytest.raises(ValueError, match="未加入家庭组"):
        await family_service.fetch_family_library()


@pytest.mark.asyncio
async def test_family_wishlist_aggregation(db, monkeypatch):
    """家庭愿望单聚合：wantCount 降序 / 本地元数据 / 回退标记。"""
    from app.domains.wishlist.models import WishlistItem
    from datetime import datetime

    # 未同步家庭组 → fallback=True（全部 tracked 账户的愿望单）
    await _seed_games(db)
    now = datetime(2026, 9, 3, 12, 0, 0)
    async with db() as session:
        session.add_all([
            WishlistItem(steamid=PRIMARY, appid=620, added_at=now, active=True, owned=False),
            WishlistItem(steamid=FRIEND, appid=620, added_at=now, active=True, owned=False),
            WishlistItem(steamid=FRIEND, appid=570, added_at=now, active=True, owned=False),
            WishlistItem(steamid=PRIMARY, appid=440, added_at=now, active=True, owned=True),  # 已购不计
        ])
        await session.commit()

    out = await family_service.family_wishlist()
    # 无 Cookie 无绑定 SteamID → get_primary_steamid raise → primary=""（回退全量）
    assert out["fallback"] is True
    by_app = {it["appid"]: it for it in out["items"]}
    assert by_app[620]["wantCount"] == 2
    assert sorted(by_app[620]["members"]) == sorted([PRIMARY, FRIEND])
    assert by_app[620]["name"] == "Portal 2"
    assert by_app[620]["cnPriceFen"] == 4200
    assert 440 not in by_app  # owned=True 不进家庭愿望单
    # wantCount 降序：620(2人) 在 570(1人) 前
    assert out["items"][0]["appid"] == 620


@pytest.mark.asyncio
async def test_play_json_persisted_and_fallback_merged(db, monkeypatch):
    """游玩明细随组落库：聚合成功存 play_json → 快照兜底路径 memberPlay 非空。

    此前快照只存 app 级字段，实时聚合失败时兜底 memberPlay 恒空——
    游玩动态页签只剩「暂无成员游玩数据」空态（游玩动态「经常失败」的
    快照兜底那一半）。
    """
    from app.domains.family.models import FamilyGroup

    await _seed_account(db)
    # 同步链路先建组档案（真实流程 _save_group 先于任何库聚合）
    async with db() as session:
        session.add(FamilyGroup(
            steamid=PRIMARY, family_groupid="111", family_name="测试家庭",
            members_json=[], member_count=1,
        ))
        await session.commit()

    _mock_steam(
        monkeypatch,
        shared_apps=[{"appid": 620, "presence_count": 1}],
        owned_by_member={
            PRIMARY: [
                {"appid": 620, "playtime_forever": 300, "playtime_2weeks": 60,
                 "rtime_last_played": 1700000100},
            ],
        },
    )

    live = await family_service.fetch_family_library()
    assert live["memberPlay"][PRIMARY][0]["minutes2w"] == 60

    # Cookie 摘除 → 实时聚合必败 → cached 回快照兜底
    family_service.invalidate_library_cache()
    await _clear_account_cookie(db)
    snap = await family_service.cached_family_library()
    assert snap.get("fromSnapshot") is True
    # 兜底路径 memberPlay 从 play_json 合并：游玩动态离线也有数据
    assert snap["memberPlay"][PRIMARY][0]["appid"] == 620
    assert snap["memberPlay"][PRIMARY][0]["minutes2w"] == 60


@pytest.mark.asyncio
async def test_wishlist_uncrawled_kick(db, monkeypatch):
    """愿望单未收录（games 表无行）→ 触发后台补爬（读路径自愈）。

    「未收录」= 爬虫从未抓到该游戏，愿望单聚合只做本地 join 补不了数据；
    缺口 appid 应被送进爬取队列，爬完名称/价格自动补齐。
    """
    from datetime import datetime

    from app.domains.wishlist.models import WishlistItem

    family_service._wishlist_crawl_kick_at = None  # 清冷却账本（模块级防串扰）
    await _seed_games(db)
    now = datetime(2026, 9, 3, 12, 0, 0)
    async with db() as session:
        session.add(WishlistItem(
            steamid=PRIMARY, appid=777777, added_at=now, active=True, owned=False,
        ))
        await session.commit()

    kicked: list[list[int]] = []

    async def fake_start_job(**kw):
        kicked.append(list(kw.get("appids") or []))
        return {}

    monkeypatch.setattr("app.domains.crawl.service.start_job", fake_start_job)

    out = await family_service.family_wishlist()
    assert out["items"][0]["appid"] == 777777  # 未收录照样展示（本地聚合语义不变）
    assert kicked == [[777777]]  # 缺口 appid 进了补爬队列

    # 冷却期内不重复触发
    out2 = await family_service.family_wishlist()
    assert out2["items"][0]["appid"] == 777777
    assert kicked == [[777777]]


@pytest.mark.asyncio
async def test_sync_multi_account(db, monkeypatch):
    """多账号家庭组发现：逐账号同步、快照按账号落行、成员入追踪、单账号失败隔离。"""
    SECOND = "76561198000000003"
    THIRD = "76561198000000004"

    async def fake_bound():
        return [(PRIMARY, "c-primary"), (SECOND, "c-second"), (THIRD, "c-third")]

    monkeypatch.setattr(account_service, "list_bound_cookies", fake_bound)
    monkeypatch.setattr(
        family_service, "extract_webapi_token",
        lambda cookies: {"c-primary": "tok-a", "c-second": "tok-b",
                         "c-third": "tok-c",
                         "sessionid=s; steamLoginSecure=second%7C%7Ctok-b": "tok-b"}[cookies],
    )

    async def fake_group(token):
        if token == "tok-a":
            return {"joined": True, "family_groupid": "111", "family_name": "甲组",
                    "members": [{"steamid": PRIMARY, "role": "1"},
                                {"steamid": FRIEND, "role": "2"}]}
        if token == "tok-b":
            return {"joined": False}
        raise RuntimeError("steam 500")

    monkeypatch.setattr(family_service, "_fetch_family_group", fake_group)

    async def fake_details(token, steamids):
        return {sid: {"persona_name": f"成员{sid[-2:]}", "avatar_url": ""}
                for sid in steamids}

    monkeypatch.setattr(family_service, "_fetch_player_details", fake_details)

    async def fake_countries(steamids):
        return {FRIEND: "RU"}

    monkeypatch.setattr(family_service, "_fetch_member_countries", fake_countries)

    r = await family_service.sync_family_group()
    assert r["synced"] == 3 and r["joined"] == 1 and r["failed"] == 1
    assert [x["steamid"] for x in r["results"]] == [PRIMARY, SECOND, THIRD]

    from app.domains.family.models import FamilyGroup
    from app.domains.wishlist.models import TrackedAccount

    async with db() as session:
        row_a = await session.get(FamilyGroup, PRIMARY)
        row_b = await session.get(FamilyGroup, SECOND)
        row_c = await session.get(FamilyGroup, THIRD)
        tracked = (await session.execute(
            __import__("sqlalchemy").select(TrackedAccount)
        )).scalars().all()
    assert row_a.member_count == 2 and row_a.family_name == "甲组"
    assert row_a.members_json[1]["loccountrycode"] == "RU"
    assert row_b.member_count == 0  # 未加入也落行（joined=False 快照）
    assert row_c is None  # 失败账号不落行（无旧快照可保）
    assert "steam 500" in (r["results"][2].get("error") or "")
    assert {t.steamid for t in tracked} >= {PRIMARY, FRIEND}

    # 指定账号单同步：只动该账号（单账号路径按 get_cookies_of 取 Cookie）
    second_cookie = "sessionid=s; steamLoginSecure=second%7C%7Ctok-b"
    async def fake_cookies_of(sid):
        return second_cookie if sid == SECOND else ""
    monkeypatch.setattr(account_service, "get_cookies_of", fake_cookies_of)
    r2 = await family_service.sync_family_group(SECOND)
    assert r2["joined"] == 0 and r2["failed"] == 0 and r2["results"][0]["joined"] is False


@pytest.mark.asyncio
async def test_status_groups_and_region_chain(db, monkeypatch):
    """status 组结构 + 地区判定链：手动 > 钱包 > 资料国家 > 未设置；冲突 cn 残留自愈。"""
    SECOND = "76561198000000003"
    M1, M2, M3 = "76561198000000011", "76561198000000012", "76561198000000013"

    await _seed_account(db, sid=PRIMARY, wallet={"region_code": "in"})
    await _seed_account(db, sid=SECOND, cookies="", wallet=None)
    from app.domains.regions.models import CrawlRegion

    async with db() as session:
        for code in ("cn", "in", "us", "ru"):
            session.add(CrawlRegion(code=code, name=code.upper(), currency=code.upper()))
        await session.commit()
    from app.domains.family.models import FamilyGroup

    async with db() as session:
        session.add(FamilyGroup(
            steamid=PRIMARY, family_groupid="111", family_name="甲组",
            member_count=4,
            members_json=[
                {"steamid": PRIMARY, "role": "1", "personaName": "主号", "avatarUrl": ""},
                {"steamid": M1, "role": "2", "personaName": "m1", "avatarUrl": "",
                 "loccountrycode": "US"},
                {"steamid": M2, "role": "2", "personaName": "m2", "avatarUrl": "",
                 "loccountrycode": "RU"},
                {"steamid": M3, "role": "2", "personaName": "m3", "avatarUrl": ""},
            ],
        ))
        session.add(FamilyGroup(
            steamid=SECOND, family_groupid=None, family_name=None,
            members_json=[], member_count=0, last_error="Cookie 中无法提取 webapi_token",
        ))
        await session.commit()

    await settings_service.set_value("family.member_regions", {
        PRIMARY: "in",   # 非 cn 手动值：始终尊重
        M1: "cn",        # 非绑定成员的 cn：一次性清洗移除，按资料国家呈现
        M2: "cn",
    })

    st = await family_service.get_status()
    assert st["bound"] is True and st["primarySteamid"] == PRIMARY
    assert st["walletRegion"] == "in"
    assert len(st["groups"]) == 2

    g1, g2 = st["groups"]
    assert g1["joined"] is True and g1["familyName"] == "甲组"
    assert g2["joined"] is False and g2["synced"] is True
    assert g2["members"] == []  # 未加入组：空成员而不是他组数据

    region = {m["steamid"]: (m["region"], m["regionSource"]) for m in g1["members"]}
    assert region[PRIMARY] == ("in", "manual")
    assert region[M1] == ("us", "profile")   # cn 残留被资料国家顶替
    assert region[M2] == ("ru", "profile")
    assert region[M3] == (None, None)        # 未设置：不再兜底国区

    assert st["memberRegions"] == {PRIMARY: "in"}  # 非绑定成员的 cn 兜底已清洗
    # 顶层并集 = 各已加入组成员（未加入组不贡献成员）
    assert {m["steamid"] for m in st["members"]} == {PRIMARY, M1, M2, M3}


@pytest.mark.asyncio
async def test_save_member_regions_merge(db):
    """member-regions 合并语义：按键合并、空值清除，不做整表替换。"""
    await family_service.save_member_regions({PRIMARY: "us"})
    merged = await family_service.save_member_regions({FRIEND: "ru"})
    assert merged == {PRIMARY: "us", FRIEND: "ru"}
    merged = await family_service.save_member_regions({PRIMARY: ""})
    assert merged == {FRIEND: "ru"}


@pytest.mark.asyncio
async def test_library_cache_per_account(db, monkeypatch):
    """家庭库缓存按账号分键：两账号各拉各的、互不命中；定向失效只清一方。"""
    SECOND = "76561198000000003"
    payloads = {PRIMARY: {"familyGroupid": "g1"}, SECOND: {"familyGroupid": "g2"}}
    calls: list[str] = []

    async def fake_fetch(steam_id=None):
        calls.append(steam_id)
        return dict(payloads[steam_id])

    monkeypatch.setattr(family_service, "fetch_family_library", fake_fetch)

    a = await family_service.cached_family_library(PRIMARY)
    b = await family_service.cached_family_library(SECOND)
    assert a["familyGroupid"] == "g1" and b["familyGroupid"] == "g2"

    await family_service.cached_family_library(PRIMARY)
    assert calls == [PRIMARY, SECOND]  # 第三次命中 PRIMARY 自己的缓存

    family_service.invalidate_library_cache(PRIMARY)
    await family_service.cached_family_library(PRIMARY)
    await family_service.cached_family_library(SECOND)
    assert calls == [PRIMARY, SECOND, PRIMARY]  # SECOND 缓存未被定向失效波及


@pytest.mark.asyncio
async def test_member_regions_legacy_cleanup(db):
    """一次性清洗：非绑定成员的 cn 兜底条目移除、绑定账号保留、标记后幂等。"""
    await _seed_account(db)  # PRIMARY 为绑定账号
    manual = {PRIMARY: "in", FRIEND: "cn", "76561198000000099": "ru"}
    cleaned = await family_service._cleanup_legacy_member_regions(manual)
    assert cleaned == {PRIMARY: "in", "76561198000000099": "ru"}
    # 标记置位后不再清洗：清洗后用户手动加回的 cn 条目受尊重
    assert await family_service._cleanup_legacy_member_regions({FRIEND: "cn"}) == {FRIEND: "cn"}
