"""account 多账号行为测试：绑定 upsert / active 切换 / 主账号语义 / 删除回退 /
激活计数隔离 / 钱包轮转退避 / 旧 KV 迁移（不触真实网络，独立临时库）。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import secretbox
from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.account.steam_wallet import _from_raw_amounts
from app.domains.settings import service as settings_service

A = "76561198000000001"
B = "76561198000000002"
C = "76561198000000003"

# 与 steamLoginSecure 编码格式一致的 Cookie 串构造器
def cookie(sid: str, token: str = "jwt-token") -> str:
    return f"sessionid=s-{sid[-4:]}; steamLoginSecure={sid}%7C%7C{token}"


@pytest.fixture
def db(monkeypatch, tmp_path):
    from app.core.database import get_session_factory


    # 凭据密封的密钥材料（secret.salt）隔离到临时目录，不落开发数据目录
    monkeypatch.setattr(secretbox, "_data_dir", lambda: tmp_path)
    secretbox.clear_key_cache()

    return get_session_factory()
@pytest_asyncio.fixture(autouse=True)
async def _no_proxy(monkeypatch):
    """网络层隔离：代理解析恒直连（fetch 由各用例 monkeypatch）。"""
    async def _none():
        return None

    monkeypatch.setattr(account_service, "_strategy_proxy", _none)


# ── 绑定 upsert / active 语义 ─────────────────────────────

@pytest.mark.asyncio
async def test_bind_two_accounts_second_becomes_active(db):
    r1 = await account_service.save_cookies(cookie(A))
    assert r1["is_new"] is True and r1["steam_id"] == A

    r2 = await account_service.save_cookies(cookie(B))
    assert r2["is_new"] is True and r2["steam_id"] == B

    # 绑定即置 active：当前账号 = B，主账号仍 = A（第一个绑定）
    assert await account_service.get_active_steam_id() == B
    assert await account_service.get_primary_steam_id() == A
    # get_cookies 跟随 active
    assert f"{B}%7C%7C" in await account_service.get_cookies()


@pytest.mark.asyncio
async def test_rebind_same_account_updates_cookie_not_new(db):
    await account_service.save_cookies(cookie(A, "old"))
    r = await account_service.save_cookies(cookie(A, "new"))
    assert r["is_new"] is False
    raw = await account_service.get_cookies()
    assert "new" in raw and "old" not in raw
    assert await account_service.get_primary_steam_id() == A  # 仍是唯一账号


@pytest.mark.asyncio
async def test_set_active_switches_get_cookies(db):
    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await account_service.set_active(A)
    assert await account_service.get_active_steam_id() == A
    assert f"{A}%7C%7C" in await account_service.get_cookies()
    # 主账号不随切换变化（A 先绑定，恒为主账号）
    assert await account_service.get_primary_steam_id() == A


@pytest.mark.asyncio
async def test_set_active_unknown_raises(db):
    with pytest.raises(ValueError):
        await account_service.set_active("76561199999999999")


# ── 删除 / 回退 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_active_falls_back_to_first_remaining(db):
    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await account_service.save_cookies(cookie(C))
    await account_service.set_active(C)
    await account_service.remove_account(C)
    # active 回退到剩余首个
    assert await account_service.get_active_steam_id() in (A, B)
    # 主账号不变
    assert await account_service.get_primary_steam_id() == A


@pytest.mark.asyncio
async def test_remove_primary_promotes_next(db):
    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await account_service.remove_account(A)
    assert await account_service.get_primary_steam_id() == B
    assert await account_service.get_active_steam_id() == B


@pytest.mark.asyncio
async def test_remove_last_account_back_to_unbound(db):
    await account_service.save_cookies(cookie(A))
    await account_service.remove_account(A)
    assert await account_service.get_active_steam_id() == ""
    assert await account_service.get_cookies() == ""
    status = await account_service.get_status()
    assert status["has_cookie"] is False
    assert status["accounts"] == []


@pytest.mark.asyncio
async def test_remove_unknown_raises(db):
    with pytest.raises(ValueError):
        await account_service.remove_account(A)


# ── 删除账号的监控追踪对称拆除 ────────────────────────────


async def _seed_tracking(db, sid: str, appids: list[int]) -> None:
    """直种追踪账与愿望单行（模拟绑定后置任务注册 + 同步链落行的结果）。"""
    from app.domains.wishlist.models import TrackedAccount, WishlistItem

    async with db() as session:
        session.add(TrackedAccount(steamid=sid, kinds_json='{"wishlist": true, "owned": true}'))
        for appid in appids:
            session.add(WishlistItem(steamid=sid, appid=appid, active=True, owned=False))
        await session.commit()


@pytest.mark.asyncio
async def test_remove_account_tears_down_tracking(db):
    """设置页删账号 = 凭据 + 绑定时自动注册的追踪一并拆（与绑定对称）；
    从未绑定、仅手动追踪的账号不受影响。"""
    from sqlalchemy import select

    from app.domains.account.models import SteamAccount
    from app.domains.wishlist.models import TrackedAccount, WishlistItem

    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await _seed_tracking(db, A, [10, 11])
    await _seed_tracking(db, C, [20])  # C 从未绑定凭据

    r = await account_service.remove_account(A)

    assert r["ok"] is True and r["trackingRemoved"] == 2
    async with db() as session:
        tracked = {t.steamid for t in (await session.execute(select(TrackedAccount))).scalars()}
        items = {(w.steamid, w.appid) for w in (await session.execute(select(WishlistItem))).scalars()}
        accounts = {a.steam_id for a in (await session.execute(select(SteamAccount))).scalars()}
    assert tracked == {C}, "被删账号的追踪拆除，未绑定账号的追踪保留"
    assert items == {(C, 20)}, "被删账号的愿望单行随删"
    assert accounts == {B}, "凭据账只少被删的那个"


@pytest.mark.asyncio
async def test_clear_cookies_tears_down_bound_tracking_only(db):
    """解绑全部：有凭据账号的追踪随删；仅手动追踪的账号原样保留。"""
    from sqlalchemy import select

    from app.domains.account.models import SteamAccount
    from app.domains.wishlist.models import TrackedAccount, WishlistItem

    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await _seed_tracking(db, A, [10])
    await _seed_tracking(db, B, [11])
    await _seed_tracking(db, C, [20])  # 从未绑定

    await account_service.clear_cookies()

    async with db() as session:
        tracked = {t.steamid for t in (await session.execute(select(TrackedAccount))).scalars()}
        items = {(w.steamid, w.appid) for w in (await session.execute(select(WishlistItem))).scalars()}
        accounts = (await session.execute(select(SteamAccount))).scalars().all()
    assert accounts == []
    assert tracked == {C} and items == {(C, 20)}, "只拆绑定过的追踪，手动追踪原样"


# ── status / list 载荷 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_list_accounts_payload_shape(db):
    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))

    accounts = await account_service.list_accounts()
    assert len(accounts) == 2
    first, second = accounts
    assert first["steam_id"] == A and first["is_primary"] is True
    assert second["steam_id"] == B and second["is_primary"] is False
    assert second["is_active"] is True  # 刚绑定 = active
    # 好友码换算（SteamID64 - 76561197960265728）
    assert first["friend_code"] == str(int(A) - 76561197960265728)
    # 计数字段存在（未同步 = 0）
    assert first["wishlist_count"] == 0
    assert first["game_count"] == 0
    # 不回传 Cookie 明文
    assert "cookies" not in first


@pytest.mark.asyncio
async def test_get_status_accounts_and_primary(db):
    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await account_service.set_active(A)

    status = await account_service.get_status()
    assert status["primary_steam_id"] == A
    assert status["cookie_steam_id"] == A  # active 的
    assert len(status["accounts"]) == 2
    assert status["has_cookie"] is True


# ── 激活计数隔离（redeem _ACT_LOG 按账号分桶）──────────────

@pytest.mark.asyncio
async def test_activation_usage_isolated_per_account(db):
    from app.domains.redeem import service as redeem_service

    redeem_service._ACT_LOG.clear()
    try:
        for _ in range(3):
            redeem_service._record_activation(A)
        redeem_service._record_activation(B)
        assert redeem_service._account_usage(A) == 3
        assert redeem_service._account_usage(B) == 1
    finally:
        redeem_service._ACT_LOG.clear()


# ── 钱包轮转（每分钟任务；随机延时 mock 掉避免用例拖慢）────

@pytest.mark.asyncio
async def test_rotational_sync_refreshes_all_accounts(db, monkeypatch):
    import asyncio

    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        # 从 Cookie 反解账号身份（steamLoginSecure 前段）
        from app.domains.account.steam_wallet import steam_id_from_cookies

        sid = steam_id_from_cookies(cookies)
        return _from_raw_amounts(10000 if sid == A else 20000, 0, 23, "CN")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    result = await account_service.sync_wallets_rotational()
    assert result["ok"] is True
    assert result["ok_count"] == 2

    accounts = {a["steam_id"]: a for a in await account_service.list_accounts()}
    assert accounts[A]["wallet"]["balance"] == 100.0
    assert accounts[B]["wallet"]["balance"] == 200.0


@pytest.mark.asyncio
async def test_rotational_backoff_skips_recently_failed(db, monkeypatch):
    import asyncio

    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletFetchError("Cookie 已失效")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await account_service.save_cookies(cookie(A))
    # 第一次轮转：失败落库 wallet_error
    await account_service.sync_wallets_rotational()
    # 第二次轮转：2 分钟退避窗口内跳过
    result = await account_service.sync_wallets_rotational()
    assert result["skipped"] == 1
    assert result["ok"] is False


# ── 旧单账号 KV 迁移 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_migrate_legacy_kv_moves_cookie_to_table(db):
    await settings_service.set_value(
        account_service.KEY_COOKIES, f"sessionid=legacy; steamLoginSecure={A}%7C%7Ct"
    )
    await settings_service.set_value(
        account_service.KEY_WALLET, {"balance_display": "¥10.00", "check_ok": True}
    )
    migrated = await account_service.migrate_legacy_kv()
    assert migrated == 1

    # KV 已清（表为唯一真源）
    assert await settings_service.get_value(account_service.KEY_COOKIES, "") == ""
    # 表里有该账号，且置 active
    assert await account_service.get_active_steam_id() == A
    assert f"{A}%7C%7C" in await account_service.get_cookies()
    # 旧钱包快照随迁
    accounts = await account_service.list_accounts()
    assert accounts[0]["wallet"]["balance_display"] == "¥10.00"


@pytest.mark.asyncio
async def test_migrate_legacy_kv_idempotent(db):
    await settings_service.set_value(
        account_service.KEY_COOKIES, f"steamLoginSecure={A}%7C%7Ct"
    )
    assert await account_service.migrate_legacy_kv() == 1
    # 再跑：KV 已清，不重复迁移
    assert await account_service.migrate_legacy_kv() == 0
    assert len(await account_service.list_accounts()) == 1


# ── mismatch 校验保留 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_save_cookies_flags_mismatch_vs_bound_steamid(db):
    await settings_service.set_value("account.steam_id", "76561198000000000")
    r = await account_service.save_cookies(cookie(A))
    assert r["mismatch"] is True


# ── Steam 真实在线状态（miniprofile 条件字段 / 官方批量映射）──

def test_parse_miniprofile_offline_has_no_online_field():
    """离线形态（真机好友码，已脱敏）：只有等级/徽章等，无 online 字段 → 离线。"""
    from app.domains.account.steam_wallet import parse_miniprofile

    offline = {
        "level": 14,
        "level_class": "friendPlayerLevel lvl_10",
        "avatar_url": "https://example.com/a.jpg",
        "persona_name": "someone",
        "favorite_badge": {"name": "badge"},
    }
    p = parse_miniprofile(offline)
    assert p["online"] is False
    assert p["in_game_name"] == ""
    assert p["persona_name"] == "someone"


def test_parse_miniprofile_online_with_ingame():
    from app.domains.account.steam_wallet import parse_miniprofile

    online = {
        "persona_name": "someone",
        "avatar_url": "https://example.com/a.jpg",
        "online": True,
        "in_game": {"name": "Counter-Strike 2", "link": "x"},
    }
    p = parse_miniprofile(online)
    assert p["online"] is True
    assert p["in_game_name"] == "Counter-Strike 2"


def test_parse_miniprofile_nonsteam_game():
    """非 Steam 游戏走 in_nonsteam_game（值为对象，name 可缺）。"""
    from app.domains.account.steam_wallet import parse_miniprofile

    p = parse_miniprofile({"online": True, "in_nonsteam_game": {"game_name": "Elden Ring"}})
    assert p["online"] is True
    assert p["in_game_name"] == "Elden Ring"


@pytest.mark.asyncio
async def test_refresh_online_states_maps_personastate(db, monkeypatch):
    """官方通道：personastate>0=在线，缺失=离线，gameextrainfo=in_game。"""
    from app.domains.account.steam_wallet import _from_raw_amounts  # noqa: F401 保持导入习惯

    async def fake_states(steam_ids, api_key, *, proxy_url=None):
        return {
            A: {"state": 1, "in_game": ""},
            B: {"state": 0, "in_game": ""},
        }  # C 缺失 = 离线

    async def _none():
        return None

    monkeypatch.setattr(account_service, "fetch_player_states", fake_states)
    monkeypatch.setattr(account_service, "_strategy_proxy", _none)
    await settings_service.set_value("account.steam_api_key", "TESTKEY")

    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    await account_service.save_cookies(cookie(C))

    result = await account_service.refresh_online_states()
    assert result["ok"] is True

    by_id = {a["steam_id"]: a for a in await account_service.list_accounts()}
    assert by_id[A]["is_online"] is True and by_id[A]["in_game"] == ""
    assert by_id[B]["is_online"] is False
    assert by_id[C]["is_online"] is False  # 响应缺失 = 离线


@pytest.mark.asyncio
async def test_refresh_online_states_no_key_skips(db):
    """无 API Key：官方通道跳过（miniprofile 兜底负责在线态）。"""
    await account_service.save_cookies(cookie(A))
    result = await account_service.refresh_online_states()
    assert result["ok"] is False
    assert result["status"] == "no_key"
