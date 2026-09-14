"""wishlist 域单元测试：已购双通道拉取 + owned 标记保全语义（mock，不触网）。

覆盖：
- JWT 优先（主账号 Cookie 有 token 即走免 Key 通道）
- JWT 失败回退 WebAPI Key
- 两个通道都不可用 → OwnedFetchError；sync_account 中断覆写、保留标记
- HTTP 200 + 空 games = 合法空库（回落愿望单条目语义）
- 已购拉取失败时：开着 owned 同步 → 已购行保留；关闭 owned 同步 → 照常回落
- 已购同步按账户开关（kinds.owned，任务页·账户设置）
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.wishlist import service as wishlist_service
from app.domains.wishlist.models import TrackedAccount, WishlistItem


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wishlist_service, "get_session_factory", lambda: factory)

    # account 域 Cookie 读取不走测试库（其模块级 factory 引用未被打桩，
    # 会直读生产库）；所有用例显式 mock get_primary_cookies。
    import app.domains.account.service as account_service

    async def _no_primary():
        return ""

    monkeypatch.setattr(account_service, "get_primary_cookies", _no_primary)

    # sync_account 会触发 regions 读取：mock 掉避免依赖未建表
    import app.domains.regions.service as regions_service

    async def fake_owned_regions():
        return None

    monkeypatch.setattr(regions_service, "owned_regions", fake_owned_regions)

    # persona 拉取（miniprofile）不打桩会真触网；测试里一律空档案
    async def fake_persona(steamid):
        return {"persona_name": "", "avatar_url": "", "online": False, "in_game_name": ""}

    monkeypatch.setattr(wishlist_service, "_fetch_persona", fake_persona)
    # 家庭组快照回填默认关闭（多数用例无 family_groups 表；快照回填专项用例显式开）
    async def no_family():
        return {}

    monkeypatch.setattr(wishlist_service, "_family_personas", no_family)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.wishlist.models  # noqa: F401
    # games 主档：list_items 的 name/headerImage LEFT JOIN 源
    import app.domains.games.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


PRIMARY = "76561198000000001"
FRIEND = "76561198000000002"

GAMES = [{"appid": 620, "name": "Portal 2"}, {"appid": 570, "name": "Dota 2"}]


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _jwt_cookies(sid=PRIMARY, token="jwt-token"):
    return f"sessionid=s; steamLoginSecure={sid}%7C%7C{token}"


def _mock_account_cookies(monkeypatch, cookies: str | None):
    async def fake_get_primary():
        return cookies or ""

    import app.domains.account.service as account_service

    monkeypatch.setattr(account_service, "get_primary_cookies", fake_get_primary)


def _mock_settings(monkeypatch, key_value: str | None):
    async def fake_get(key, default=None):
        if key == "account.steam_api_key":
            return key_value or ""
        return default

    from app.domains.settings import service as settings_service

    monkeypatch.setattr(settings_service, "get_value", fake_get)


def _mock_fetch_owned(monkeypatch, *, raise_error=None, games=GAMES, source="jwt"):
    """直接桩掉 fetch_owned_games 编排层——测试 sync_account 对结果的消化，
    不让任何真实 DB/网络进入测试（account 域 factory 引用无法打桩）。"""
    async def fake_fetch(steamid):
        if raise_error:
            raise raise_error
        return games, source

    monkeypatch.setattr(wishlist_service, "fetch_owned_games", fake_fetch)


def _mock_jwt_channel(monkeypatch, *, raise_error=None, games=GAMES):
    async def fake_via_jwt(steamid, token):
        if raise_error:
            raise raise_error
        return games

    monkeypatch.setattr(wishlist_service, "fetch_owned_games_via_jwt", fake_via_jwt)


def _mock_key_channel(monkeypatch, *, raise_error=None, games=GAMES):
    async def fake_via_key(steamid, api_key):
        if raise_error:
            raise raise_error
        return games

    monkeypatch.setattr(wishlist_service, "fetch_owned_games_via_key", fake_via_key)


def _mock_wishlist(monkeypatch, items):
    async def fake_fetch_wishlist(steamid):
        return items

    monkeypatch.setattr(wishlist_service, "fetch_wishlist", fake_fetch_wishlist)


async def _seed_account(db, steamid=PRIMARY, kinds=None):
    async with db() as session:
        session.add(TrackedAccount(
            steamid=steamid, label=None,
            kinds_json=kinds or {"wishlist": True, "owned": True},
            item_count=0,
        ))
        await session.commit()


async def _seed_item(db, appid, *, steamid=PRIMARY, owned=False, active=True):
    async with db() as session:
        session.add(WishlistItem(
            steamid=steamid, appid=appid, active=active, owned=owned,
        ))
        await session.commit()


async def _get_items(db, steamid=PRIMARY):
    from sqlalchemy import select

    async with db() as session:
        rows = (
            await session.execute(
                select(WishlistItem).where(WishlistItem.steamid == steamid)
            )
        ).scalars().all()
    return {int(r.appid): r for r in rows}


# ── 通道选择 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jwt_first(db, monkeypatch):
    """主账号 Cookie 有 token：走 JWT，不碰 Key。"""
    _mock_account_cookies(monkeypatch, _jwt_cookies())
    _mock_settings(monkeypatch, None)
    _mock_jwt_channel(monkeypatch, games=GAMES)

    async def fail_key(steamid, api_key):
        raise AssertionError("不应回退 Key：JWT 通道应直接成功")

    _mock_key_channel(monkeypatch)
    monkeypatch.setattr(
        wishlist_service, "fetch_owned_games_via_key", fail_key
    )

    games, source = await wishlist_service.fetch_owned_games(FRIEND)
    assert source == "jwt"
    assert {g["appid"] for g in games} == {620, 570}


@pytest.mark.asyncio
async def test_jwt_fail_fallback_key(db, monkeypatch):
    """JWT 被拒（401）→ 回退 WebAPI Key 成功。"""
    from app.domains.wishlist.service import OwnedFetchError

    _mock_account_cookies(monkeypatch, _jwt_cookies())
    _mock_settings(monkeypatch, "key-123")
    _mock_jwt_channel(
        monkeypatch, raise_error=OwnedFetchError("JWT 通道被拒（401）")
    )
    _mock_key_channel(monkeypatch, games=[{"appid": 620, "name": "Portal 2"}])

    games, source = await wishlist_service.fetch_owned_games(FRIEND)
    assert source == "key"
    assert [g["appid"] for g in games] == [620]


@pytest.mark.asyncio
async def test_no_cookie_no_key_raises(db, monkeypatch):
    """未绑 Cookie 且未配 Key → OwnedFetchError（不再静默返回 []）。"""
    from app.domains.wishlist.service import OwnedFetchError

    _mock_account_cookies(monkeypatch, None)
    _mock_settings(monkeypatch, None)
    _mock_jwt_channel(monkeypatch, games=GAMES)  # 不应被调用

    with pytest.raises(OwnedFetchError, match="未绑定"):
        await wishlist_service.fetch_owned_games(FRIEND)


@pytest.mark.asyncio
async def test_empty_games_is_valid(db, monkeypatch):
    """无 Cookie 有 Key：Key 通道 HTTP 200 + 空 games = 合法空库。"""
    _mock_account_cookies(monkeypatch, None)
    _mock_settings(monkeypatch, "key-123")
    _mock_jwt_channel(monkeypatch)
    _mock_key_channel(monkeypatch, games=[])

    games, source = await wishlist_service.fetch_owned_games(FRIEND)
    assert source == "key"
    assert games == []


# ── list_accounts 家庭组快照档案回填 ────────────────────────


@pytest.mark.asyncio
async def test_list_accounts_backfills_persona_from_family_snapshot(db, monkeypatch):
    """存量账户无档案时：昵称/头像从家庭组快照合并返回 + 落库自愈（零网络）。"""
    monkeypatch.setattr(
        wishlist_service,
        "_family_personas",
        _fake_family_snapshot(PRIMARY, FRIEND),
    )
    async with db() as session:
        session.add(TrackedAccount(steamid=PRIMARY, label=None, kinds_json={"wishlist": True, "owned": True}))
        session.add(TrackedAccount(
            steamid=FRIEND,
            label=None,
            kinds_json={"wishlist": True, "owned": True},
            persona_name="已有昵称",  # 账户行已有档案：不被快照覆盖
        ))
        session.add(WishlistItem(steamid=PRIMARY, appid=620, owned=True, active=True))
        session.add(WishlistItem(steamid=PRIMARY, appid=570, owned=True, active=True))
        session.add(WishlistItem(steamid=PRIMARY, appid=400, owned=False, active=True))
        await session.commit()

    accounts = {a["steamid"]: a for a in await wishlist_service.list_accounts()}

    # 快照补齐：昵称/头像来自快照；ownedCount 只数 owned 行
    assert accounts[PRIMARY]["personaName"] == "主号快照昵称"
    assert accounts[PRIMARY]["avatarUrl"] == "https://avatars.fastly.steamstatic.com/p1_full.jpg"
    assert accounts[PRIMARY]["friendCode"] == str(int(PRIMARY) - 76561197960265728)
    assert accounts[PRIMARY]["ownedCount"] == 2
    # 账户行已有档案优先，不被快照覆盖
    assert accounts[FRIEND]["personaName"] == "已有昵称"

    # 落库自愈：下次查询直接从账户行读到（快照桩此时返回空）
    monkeypatch.setattr(wishlist_service, "_family_personas", _fake_family_snapshot())
    again = {a["steamid"]: a for a in await wishlist_service.list_accounts()}
    assert again[PRIMARY]["personaName"] == "主号快照昵称"
    assert again[PRIMARY]["avatarUrl"] == "https://avatars.fastly.steamstatic.com/p1_full.jpg"


def _fake_family_snapshot(*steamids):
    """快照桩：只含指定成员（空参 = 无快照）。"""
    members = [
        {
            "steamid": sid,
            "personaName": "主号快照昵称" if sid == PRIMARY else "成员快照昵称",
            "avatarUrl": "https://avatars.fastly.steamstatic.com/p1_full.jpg",
        }
        for sid in steamids
    ]

    async def _get():
        return {m["steamid"]: (m["personaName"], m["avatarUrl"]) for m in members}

    return _get


# ── sync_account 语义 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_channel_fail_keeps_owned_marks(db, monkeypatch):
    """开着 owned 同步但通道失败：owned 标记保留、不并入新条目、已购行不停用。"""
    from app.domains.wishlist.service import OwnedFetchError

    await _seed_account(db, kinds={"wishlist": True, "owned": True})
    await _seed_item(db, 620, owned=True)   # 已购专属（不在愿望单）
    await _seed_item(db, 570, owned=False)  # 愿望单条目
    _mock_wishlist(monkeypatch, [{"appid": 570, "added_at": None}])  # 620 不在愿望单
    _mock_fetch_owned(
        monkeypatch, raise_error=OwnedFetchError("两个通道均失败")
    )

    result = await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    assert result["ownedError"] is not None
    assert result["ownedCount"] == 0
    items = await _get_items(db)
    assert items[620].owned is True   # 标记保留
    assert items[620].active is True  # 已购行保守保留（不误判为愿望单移除）
    assert items[570].active is True


@pytest.mark.asyncio
async def test_sync_owned_disabled_falls_back(db, monkeypatch):
    """关闭某账户已购同步（kinds.owned=False，任务页·账户设置）：已购专属
    条目出池（active=False）；owned 标记按历史语义保留（覆写只在开着时
    执行），通道失败不提供保护。"""
    await _seed_account(db, kinds={"wishlist": True, "owned": False})
    await _seed_item(db, 620, owned=True)
    _mock_account_cookies(monkeypatch, None)
    _mock_settings(monkeypatch, None)
    _mock_wishlist(monkeypatch, [{"appid": 570, "added_at": None}])

    result = await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    items = await _get_items(db)
    # 620 不在愿望单：kinds.owned=False → owned_failed=False → 照常出池
    assert items[620].active is False
    # 历史语义：覆写仅在 owned_synced=True 时执行，标记保留
    assert items[620].owned is True
    # 通道失败信息仍然回报（kinds 关闭时无实际影响）
    assert result["ownedError"] is not None


@pytest.mark.asyncio
async def test_sync_owned_new_split_accounting(db, monkeypatch):
    """成功同步：新增项按来源拆分记账（newOwnedAppids / newAppids）。"""
    await _seed_account(db, kinds={"wishlist": True, "owned": True})
    _mock_wishlist(monkeypatch, [{"appid": 570, "added_at": None}])
    _mock_fetch_owned(monkeypatch, games=[{"appid": 620, "name": "Portal 2"}], source="jwt")

    result = await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    assert result["ownedSource"] == "jwt"
    assert result["newOwnedAppids"] == [620]
    assert result["newAppids"] == [570, 620]
    items = await _get_items(db)
    assert items[620].owned is True
    assert items[570].owned is False


# ── list_items（监控条目名称/封面 JOIN）─────────────────


@pytest.mark.asyncio
async def test_list_items_joins_game_name_and_cover(db):
    """已爬条目：name/headerImage 从 games 主档带出（LEFT JOIN）。"""
    from app.domains.games.models import Game

    await _seed_account(db)
    await _seed_item(db, 620, active=True)
    await _seed_item(db, 570, active=False)  # 停用条目不出列
    await _seed_item(db, 99801, active=True)  # 未爬：games 无行
    async with db() as session:
        session.add(Game(appid=620, name="传送门 2", name_en="Portal 2", header_image="https://x/620/header.jpg"))
        await session.commit()

    rows = await wishlist_service.list_items(None)
    by_appid = {r["appid"]: r for r in rows}

    assert by_appid[620]["name"] == "传送门 2"
    assert by_appid[620]["nameEn"] == "Portal 2"
    assert by_appid[620]["headerImage"] == "https://x/620/header.jpg"
    # 新绑未爬：games 无行 → null（前端回落显示 appid）
    assert by_appid[99801]["name"] is None
    assert by_appid[99801]["nameEn"] is None
    assert by_appid[99801]["headerImage"] is None
    # active=False 不出列
    assert 570 not in by_appid


@pytest.mark.asyncio
async def test_list_items_filter_by_steamid(db):
    """按账户过滤 + 原有字段（appid/addedAt）不受 JOIN 影响；
    条目按 appid 聚合，账户落在 steamids 数组里。"""
    await _seed_account(db, steamid=PRIMARY)
    await _seed_account(db, steamid=FRIEND)
    await _seed_item(db, 620, steamid=PRIMARY)
    await _seed_item(db, 570, steamid=FRIEND)

    mine = await wishlist_service.list_items(PRIMARY)
    assert [r["appid"] for r in mine] == [620]
    assert mine[0]["steamids"] == [PRIMARY]
    assert mine[0]["addedAt"] is None


@pytest.mark.asyncio
async def test_sync_manual_item_immune_to_deactivation(db, monkeypatch):
    """手动导入条目（manual，任务页批量导入）：不在 Steam 真实愿望单也不
    是已购时，同步反向核对不洗掉——15min 高频同步下手动监控的存续
    不取决于 Steam 愿望单。普通条目照常停用（对照语义不被稀释）。"""
    await _seed_account(db)
    await _seed_item(db, 620, active=True)  # 手动导入
    await _seed_item(db, 570, active=True)  # 普通条目（对照）
    async with db() as session:
        manual_row = await session.get(WishlistItem, (PRIMARY, 620))
        manual_row.manual = True
        await session.commit()

    _mock_account_cookies(monkeypatch, None)
    _mock_settings(monkeypatch, None)
    _mock_fetch_owned(monkeypatch, games=[], source="jwt")
    # Steam 真实愿望单里只有 99801：620/570 都不在（非空单，停用分支生效）
    _mock_wishlist(monkeypatch, [{"appid": 99801, "added_at": None}])

    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    items = await _get_items(db)
    assert items[620].active is True, "manual 条目应免疫停用"
    assert items[620].manual is True
    assert items[570].active is False, "普通条目照常停用"
