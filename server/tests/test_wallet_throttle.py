"""钱包轮转降频机制行为测试（不触真实网络）：

1. 活跃感知门禁：前端心跳（mark_seen）在 90s 内 → 快照 2min 内不重抓；
   无心跳（进程刚启动/无人看）→ 30min 内的成功快照也不重抓；
   App 打开瞬间心跳恢复 → 下一轮立即补抓。
2. 失败递增退避：连续失败按级别 2→5→15→30min 递增冷却，成功清零。
3. 429/403 风控信号：直接进 30min 长冷却（不走普通递增梯度）。
4. 熔断：连续 5 次失败进 10min 慢车道，累计 10 次冻结终态（自动轮转
   停止请求）；手动刷新余额（force）或换绑 Cookie 解锁并清零重计。
5. 失败归因二分：冻结原因文案按最后一次失败（网络 / 风控 / 登录过期）
   分流，不一律写成「凭据已失效」；只有全通道都卡在传输层才归网络不通。
6. 在线状态活跃感知门禁：官方批量通道只查超过新鲜度阈值的过期子集
   （NULL 视为过期），子集空则本轮零出网；失败不推进时间戳下轮重试。
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.account import steam_wallet
from app.domains.account.steam_wallet import WalletInfo
from app.domains.settings import service as settings_service

A = "76561198000000001"
B = "76561198000000002"


def cookie(sid: str, token: str = "jwt-token") -> str:
    return f"sessionid=s-{sid[-4:]}; steamLoginSecure={sid}%7C%7C{token}"


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()
@pytest_asyncio.fixture(autouse=True)
async def _isolation(monkeypatch):
    """网络层全隔离 + jitter 置零 + 心跳复位（每用例独立进程态）。"""

    async def _none():
        return None

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(account_service, "_strategy_proxy", _none)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    account_service._last_seen = None


def _wallet_snapshot(balance_fen: int = 10000, minutes_ago: float = 0.0):
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    return {
        "balance": balance_fen / 100.0,
        "balance_display": "¥100.00",
        "checked_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
        "check_ok": True,
        "error": "",
    }


# ── 1. 活跃感知门禁 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_heartbeat_keeps_minute_freshness(db, monkeypatch):
    """App 开着（心跳 <90s）：刚成功的快照（<2min）本轮跳过——分钟级语义不变。"""
    calls = []

    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        calls.append(cookies)
        return WalletInfo(balance=100.0, currency_code="CNY", currency_symbol="¥", currency_id=23, country_code="CN")

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)

    await account_service.save_cookies(cookie(A))
    # 第一轮真实抓一次建立成功快照
    await account_service.sync_wallets_rotational()
    assert len(calls) == 1

    # 心跳：模拟前端 60s 轮询 GET /account
    account_service.mark_seen()
    assert account_service._freshness_threshold_minutes() == 2.0

    # 第二轮：快照 0 分钟新鲜 + 阈值 2min → fresh_skip，不再打 Steam
    result = await account_service.sync_wallets_rotational()
    assert len(calls) == 1
    assert result["ok_count"] == 1  # fresh_skip 计入 ok（数据新鲜即健康）


@pytest.mark.asyncio
async def test_idle_climbs_to_30min_freshness(db):
    """无人看（无心跳）：刚成功 1 分钟的快照也被 30min 阈值拦下——挂机降频。"""
    await account_service.save_cookies(cookie(A))
    # 直接落一个 1 分钟前的成功快照（绕过 fetch）
    await account_service._save_wallet(
        A, snapshot=_wallet_snapshot(minutes_ago=1.0), now=account_service._naive_now()
    )
    # 不调 mark_seen（进程刚启动 = 无人看）
    assert account_service._freshness_threshold_minutes() == 30.0

    result = await account_service.sync_wallets_rotational()
    # 1min 快照 < 30min 阈值 → 本轮全部跳过
    assert result["total"] == 1
    assert result["skipped"] == 0  # fresh_skip 不算 backoff
    assert result["ok_count"] == 1


@pytest.mark.asyncio
async def test_idle_stale_snapshot_still_refreshes(db, monkeypatch):
    """无人看但快照超 30min：照常重抓（降频不是停摆，兜底轮转语义保留）。"""
    calls = []

    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        calls.append(cookies)
        return WalletInfo(balance=100.0, currency_code="CNY", currency_symbol="¥", currency_id=23, country_code="CN")

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)

    await account_service.save_cookies(cookie(A))
    await account_service._save_wallet(
        A, snapshot=_wallet_snapshot(minutes_ago=45.0), now=account_service._naive_now()
    )

    result = await account_service.sync_wallets_rotational()
    assert len(calls) == 1  # 45min > 30min 阈值 → 真抓
    assert result["ok_count"] == 1


@pytest.mark.asyncio
async def test_heartbeat_recovery_triggers_refresh(db, monkeypatch):
    """App 打开瞬间：心跳恢复 → 阈值塌缩到 2min → 下一轮（≤60s）立即补上。"""
    calls = []

    async def fake_fetch(cookies, *, verify=None, proxy_url=None):
        calls.append(cookies)
        return WalletInfo(balance=100.0, currency_code="CNY", currency_symbol="¥", currency_id=23, country_code="CN")

    monkeypatch.setattr(account_service, "fetch_wallet", fake_fetch)

    await account_service.save_cookies(cookie(A))
    # 无人看期间落了个 10 分钟前的快照（idle 下被 30min 阈值保护着）
    await account_service._save_wallet(
        A, snapshot=_wallet_snapshot(minutes_ago=10.0), now=account_service._naive_now()
    )

    # 用户打开 App：心跳恢复
    account_service.mark_seen()
    assert account_service._freshness_threshold_minutes() == 2.0

    # 10min 快照 > 2min 阈值 → 本轮立即真抓（UX 无感补上）
    result = await account_service.sync_wallets_rotational()
    assert len(calls) == 1
    assert result["ok_count"] == 1


# ── 2. 失败递增退避 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_backoff_escalates_by_level(db, monkeypatch):
    """连续失败：冷却按 2→5→15→30min 递增（不再是固定 2min 硬打）。"""
    from app.crawler.utils import get_beijing_time_obj

    async def fail_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletFetchError("无法获取（Cookie 失效或网络不通）")

    monkeypatch.setattr(account_service, "fetch_wallet", fail_fetch)

    await account_service.save_cookies(cookie(A))

    async def _row():
        return await account_service._get_row(A)

    # 第一次失败 → level 1（2min 冷却）
    await account_service._sync_wallet_of(A)
    assert (await _row()).wallet_backoff_level == 1

    # 模拟 3 分钟后（> 2min 冷却）：第二次失败 → level 2（5min 冷却）
    row = await _row()
    row.wallet_checked_at = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=3)
    async with account_service.get_session_factory()() as session:
        session.add(row)
        await session.commit()
    await account_service._sync_wallet_of(A)
    assert (await _row()).wallet_backoff_level == 2

    # 再过 6 分钟（> 5min 冷却）：第三次失败 → level 3（15min 冷却）
    row = await _row()
    row.wallet_checked_at = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=6)
    async with account_service.get_session_factory()() as session:
        session.add(row)
        await session.commit()
    await account_service._sync_wallet_of(A)
    assert (await _row()).wallet_backoff_level == 3

    # 级别 3 冷却内（15min）：轮转应跳过
    result = await account_service.sync_wallets_rotational()
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_backoff_resets_on_success(db, monkeypatch):
    """成功一次：退避级别清零（从 3 级直接回到正常轮转）。"""
    state = {"fail": True}

    async def toggle_fetch(cookies, *, verify=None, proxy_url=None):
        if state["fail"]:
            raise account_service.WalletFetchError("网络抖动")
        return WalletInfo(balance=100.0, currency_code="CNY", currency_symbol="¥", currency_id=23, country_code="CN")

    monkeypatch.setattr(account_service, "fetch_wallet", toggle_fetch)

    await account_service.save_cookies(cookie(A))
    await account_service._sync_wallet_of(A)
    await account_service._sync_wallet_of(A)
    row = await account_service._get_row(A)
    assert row.wallet_backoff_level == 2

    # 网络恢复：成功 → 级别清零
    state["fail"] = False
    result = await account_service._sync_wallet_of(A)
    assert result["ok"] is True
    assert (await account_service._get_row(A)).wallet_backoff_level == 0


# ── 3. 429/403 风控长冷却 ───────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_goes_long_cooldown(db, monkeypatch):
    """429 信号：直接顶格 30min 冷却（不走 2→5→15 递增梯度）。"""
    from app.crawler.utils import get_beijing_time_obj

    from app.domains.account.steam_wallet import WalletRateLimitedError

    async def limited_fetch(cookies, *, verify=None, proxy_url=None):
        raise WalletRateLimitedError(
            "Steam 节点风控（HTTP 429，_fetch_store_account）——请切换 Clash 节点或稍后再试"
        )

    monkeypatch.setattr(account_service, "fetch_wallet", limited_fetch)

    await account_service.save_cookies(cookie(A))
    await account_service._sync_wallet_of(A)
    row = await account_service._get_row(A)
    assert row.wallet_backoff_level == 1  # 级别照常 +1（供下次再失败升级）

    # 模拟 10 分钟后（> 普通 2min 冷却，但 < 30min 风控冷却）：轮转仍跳过
    row.wallet_checked_at = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=10)
    async with account_service.get_session_factory()() as session:
        session.add(row)
        await session.commit()

    result = await account_service.sync_wallets_rotational()
    assert result["skipped"] == 1  # 429 的 30min 长冷却在起作用，不是 2min


@pytest.mark.asyncio
async def test_rate_limit_error_message_identified():
    """风控信号识别：429/403/风控 字样都命中。"""
    assert account_service._is_rate_limited("Steam 节点风控（HTTP 429）")
    assert account_service._is_rate_limited("HTTP 403 拒绝")
    assert account_service._is_rate_limited("节点风控")
    assert not account_service._is_rate_limited("Cookie 失效或网络不通")
    assert not account_service._is_rate_limited("")


# ── 4. 熔断：慢车道 / 冻结终态 / 解锁 ───────────────────────


async def _fail_times(db, monkeypatch, times: int) -> None:
    """连续失败 N 次（直打轮转通道 _sync_wallet_of，跳过门禁计时）。"""

    async def fail_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletFetchError("Cookie 失效或网络不通")

    monkeypatch.setattr(account_service, "fetch_wallet", fail_fetch)
    await account_service.save_cookies(cookie(A))
    for _ in range(times):
        await account_service._sync_wallet_of(A)


def _wind_back(row, minutes: float):
    """把最近检查时间拨回 N 分钟前（模拟冷却已流逝），落库。"""
    from app.crawler.utils import get_beijing_time_obj

    row.wallet_checked_at = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=minutes)
    return row


@pytest.mark.asyncio
async def test_breaker_slow_lane_after_five_failures(db, monkeypatch):
    """连续失败 5 次进慢车道：固定 10min 观察间隔（不是退避梯子的 30min 顶格）。"""
    await _fail_times(db, monkeypatch, account_service._WALLET_BREAKER_SLOW_AFTER)

    row = await account_service._get_row(A)
    assert row.wallet_fail_streak == 5
    assert not row.wallet_frozen  # 还没到冻结线

    async def _put(r):
        async with account_service.get_session_factory()() as session:
            session.add(r)
            await session.commit()

    # 慢车道 10min 未到（6min）：轮转跳过
    await _put(_wind_back(await account_service._get_row(A), 6))
    result = await account_service.sync_wallets_rotational()
    assert result["skipped"] == 1

    # 过 11min：慢车道放行再试（仍失败 → 计数 6）
    await _put(_wind_back(await account_service._get_row(A), 11))
    result = await account_service.sync_wallets_rotational()
    assert result["ok_count"] == 0
    assert (await account_service._get_row(A)).wallet_fail_streak == 6


@pytest.mark.asyncio
async def test_breaker_freezes_after_ten_failures(db, monkeypatch):
    """慢车道再挂到累计 10 次：冻结终态，轮转不再请求（过 1 小时也不打 Steam）。"""
    calls = []

    async def counting_fail(cookies, *, verify=None, proxy_url=None):
        calls.append(cookies)
        raise account_service.WalletFetchError("Cookie 失效或网络不通")

    monkeypatch.setattr(account_service, "fetch_wallet", counting_fail)
    await account_service.save_cookies(cookie(A))
    for _ in range(account_service._WALLET_BREAKER_FREEZE_AFTER):
        await account_service._sync_wallet_of(A)

    row = await account_service._get_row(A)
    assert row.wallet_frozen is True
    assert row.wallet_fail_streak == 10

    async with account_service.get_session_factory()() as session:
        session.add(_wind_back(row, 60))
        await session.commit()

    result = await account_service.sync_wallets_rotational()
    assert len(calls) == account_service._WALLET_BREAKER_FREEZE_AFTER  # 冻结后零请求
    assert result["frozen"] == 1
    assert result["ok_count"] == 0


@pytest.mark.asyncio
async def test_manual_refresh_unfreezes_even_on_failure(db, monkeypatch):
    """冻结后手动刷新余额（force）：解锁 + 计数清零，即使这次仍失败也不回冻结。"""
    await _fail_times(db, monkeypatch, account_service._WALLET_BREAKER_FREEZE_AFTER)
    assert (await account_service._get_row(A)).wallet_frozen is True

    sync = await account_service.sync_wallet(force=True)
    assert sync["ok"] is False  # 手动这次也失败
    row = await account_service._get_row(A)
    assert row.wallet_frozen is False
    assert row.wallet_fail_streak == 0
    assert row.wallet_backoff_level == 0


@pytest.mark.asyncio
async def test_manual_refresh_success_restores_rotation(db, monkeypatch):
    """冻结后手动刷新成功：解锁 + 成功快照落库，轮转恢复正常节奏。"""
    state = {"fail": True}

    async def toggle_fetch(cookies, *, verify=None, proxy_url=None):
        if state["fail"]:
            raise account_service.WalletFetchError("网络抖动")
        return WalletInfo(balance=100.0, currency_code="CNY", currency_symbol="¥", currency_id=23, country_code="CN")

    monkeypatch.setattr(account_service, "fetch_wallet", toggle_fetch)
    await account_service.save_cookies(cookie(A))
    for _ in range(account_service._WALLET_BREAKER_FREEZE_AFTER):
        await account_service._sync_wallet_of(A)
    assert (await account_service._get_row(A)).wallet_frozen is True

    state["fail"] = False
    sync = await account_service.sync_wallet(force=True)
    assert sync["ok"] is True
    row = await account_service._get_row(A)
    assert not row.wallet_frozen
    assert row.wallet_fail_streak == 0

    # 轮转恢复正常：成功快照新鲜 → fresh_skip（不再是冻结/退避）
    result = await account_service.sync_wallets_rotational()
    assert result["ok_count"] == 1
    assert result["frozen"] == 0


@pytest.mark.asyncio
async def test_rebind_unfreezes(db, monkeypatch):
    """重新绑定 Cookie：熔断冻结与失败计数清零（重新绑定即解锁语义）。"""
    await _fail_times(db, monkeypatch, account_service._WALLET_BREAKER_FREEZE_AFTER)
    assert (await account_service._get_row(A)).wallet_frozen is True

    await account_service.save_cookies(cookie(A, token="jwt-new"))
    row = await account_service._get_row(A)
    assert row.wallet_frozen is False
    assert row.wallet_fail_streak == 0
    assert row.wallet_backoff_level == 0


@pytest.mark.asyncio
async def test_breaker_slow_lane_keeps_rate_limit_floor(db, monkeypatch):
    """慢车道内的 429 信号仍按 30min 风控冷却——熔断不削弱风控退避。"""
    from app.crawler.utils import get_beijing_time_obj

    from app.domains.account.steam_wallet import WalletRateLimitedError

    async def limited_fetch(cookies, *, verify=None, proxy_url=None):
        raise WalletRateLimitedError("Steam 节点风控（HTTP 429）")

    monkeypatch.setattr(account_service, "fetch_wallet", limited_fetch)
    await account_service.save_cookies(cookie(A))
    for _ in range(account_service._WALLET_BREAKER_SLOW_AFTER):
        await account_service._sync_wallet_of(A)
    assert (await account_service._get_row(A)).wallet_fail_streak == 5

    # 过 11min（> 慢车道 10min，< 风控 30min）：风控冷却仍拦着
    row = await account_service._get_row(A)
    row.wallet_checked_at = (
        get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=11)
    )
    async with account_service.get_session_factory()() as session:
        session.add(row)
        await session.commit()

    result = await account_service.sync_wallets_rotational()
    assert result["skipped"] == 1
    assert (await account_service._get_row(A)).wallet_fail_streak == 5


# ── 5. 失败归因二分 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_frozen_reason_splits_by_last_failure(db, monkeypatch):
    """冻结原因按最后一次失败分流：网络不通与真·登录过期不能共用一句「凭据已失效」。"""

    async def network_fail(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletFetchError(
            steam_wallet.WALLET_NETWORK_ERROR_HEAD
            + "_fetch_store_account: ConnectError: all attempts failed"
        )

    monkeypatch.setattr(account_service, "fetch_wallet", network_fail)
    await account_service.save_cookies(cookie(A))
    for _ in range(account_service._WALLET_BREAKER_FREEZE_AFTER):
        await account_service._sync_wallet_of(A)
    assert (await account_service._get_row(A)).wallet_frozen is True

    result = await account_service.sync_wallets_rotational()
    assert result["frozen"] == 1
    assert result["reasons"] == {
        "网络或代理通道不通，已停自动重试（手动刷新余额解锁）": 1
    }

    # 换成真·登录过期：必须换分支（出路是重新登录，不是刷新余额）
    await account_service._save_wallet(
        A,
        error=account_service.SESSION_EXPIRED_MESSAGE,
        now=account_service._naive_now(),
    )
    result = await account_service.sync_wallets_rotational()
    assert result["reasons"] == {"登录已过期，等待重新登录": 1}


@pytest.mark.asyncio
async def test_frozen_reason_flags_rate_limit(db, monkeypatch):
    """风控冻结：归因落到风控本身，避免被读成「Cookie 坏了再去重登」。"""

    async def limited_fetch(cookies, *, verify=None, proxy_url=None):
        raise steam_wallet.WalletRateLimitedError(
            "Steam 节点风控（HTTP 429，_fetch_store_account）——请切换 Clash 节点或稍后再试"
        )

    monkeypatch.setattr(account_service, "fetch_wallet", limited_fetch)
    await account_service.save_cookies(cookie(A))
    for _ in range(account_service._WALLET_BREAKER_FREEZE_AFTER):
        await account_service._sync_wallet_of(A)

    result = await account_service.sync_wallets_rotational()
    assert result["reasons"] == {
        "Steam 风控拒绝，已停自动重试（手动刷新余额解锁）": 1
    }


@pytest.mark.asyncio
async def test_fetch_wallet_head_splits_transport_from_http(monkeypatch):
    """归因源头：只有全通道卡在传输层才写「网络不通」；拿到 HTTP 响应不算。"""
    import httpx

    class _Client:
        def __init__(self, fail: str) -> None:
            self.fail = fail

        async def get(self, url, **kwargs):
            if self.fail == "transport":
                raise httpx.ConnectError("All connection attempts failed")
            request = httpx.Request("GET", str(url))
            raise httpx.HTTPStatusError(
                "Server error '500'",
                request=request,
                response=httpx.Response(500, request=request),
            )

    mode = "transport"

    async def _client_factory(proxy_url, verify, timeout, *, fresh=False):
        return _Client(mode)

    monkeypatch.setattr(steam_wallet, "_borrow_client", _client_factory)

    with pytest.raises(steam_wallet.WalletFetchError) as info:
        await steam_wallet.fetch_wallet(cookie(A), verify=True)
    assert str(info.value).startswith(steam_wallet.WALLET_NETWORK_ERROR_HEAD)

    mode = "http"
    with pytest.raises(steam_wallet.WalletFetchError) as info:
        await steam_wallet.fetch_wallet(cookie(A), verify=True)
    assert str(info.value).startswith(steam_wallet.WALLET_AUTH_ERROR_HEAD)


# ── 6. 在线状态活跃感知门禁 ─────────────────────────────────


async def _wallet_fail_path(monkeypatch):
    """钱包通道全程失败（零出网真实路径），让轮转不因钱包成功态干扰断言。"""

    async def fail_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletFetchError("Cookie 失效或网络不通")

    monkeypatch.setattr(account_service, "fetch_wallet", fail_fetch)


async def _set_online_check(steam_id: str, minutes_ago: float | None, **fields) -> None:
    """直改库：拨 online_checked_at 到 N 分钟前（None=清空），附带行字段。"""
    from app.crawler.utils import get_beijing_time_obj

    from app.domains.account.models import SteamAccount

    async with account_service.get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        assert row is not None
        row.online_checked_at = (
            None if minutes_ago is None
            else get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=minutes_ago)
        )
        for key, value in fields.items():
            setattr(row, key, value)
        await session.commit()


async def _get_account(steam_id: str):
    from app.domains.account.models import SteamAccount

    async with account_service.get_session_factory()() as session:
        return await session.get(SteamAccount, steam_id)


@pytest.mark.asyncio
async def test_online_states_active_gate_skips_fresh(db, monkeypatch):
    """活跃心跳下刚采集过的账号零出网；过期后才补查（2min 一发）。"""
    calls: list[list[str]] = []

    async def fake_states(steam_ids, api_key, *, proxy_url=None):
        calls.append(list(steam_ids))
        return {sid: {"state": 1, "in_game": ""} for sid in steam_ids}

    await _wallet_fail_path(monkeypatch)
    monkeypatch.setattr(account_service, "fetch_player_states", fake_states)
    await settings_service.set_value("account.steam_api_key", "TESTKEY")

    await account_service.save_cookies(cookie(A))
    # 首轮：NULL 时间戳视为过期 → 补查一次并落时间戳
    await account_service.refresh_online_states()
    assert len(calls) == 1
    assert (await _get_account(A)).online_checked_at is not None

    # 心跳建立活跃阈值 2min：刚采集的时间戳 < 2min → 子集空，零出网
    account_service.mark_seen()
    assert account_service._freshness_threshold_minutes() == 2.0
    await account_service.sync_wallets_rotational()
    assert len(calls) == 1

    # 拨回 3min 前（> 活跃阈值 2min）：下一轮补查一次，时间戳推进
    await _set_online_check(A, minutes_ago=3.0)
    await account_service.sync_wallets_rotational()
    assert len(calls) == 2
    assert calls[1] == [A]
    assert (await _get_account(A)).online_checked_at is not None


@pytest.mark.asyncio
async def test_online_states_fetches_only_stale_subset(db, monkeypatch):
    """只查过期子集：未查询账号的在线态保持原值，不被响应缺失误写离线。"""
    calls: list[list[str]] = []

    async def fake_states(steam_ids, api_key, *, proxy_url=None):
        calls.append(list(steam_ids))
        return {A: {"state": 1, "in_game": "Dota 2"}}

    await _wallet_fail_path(monkeypatch)
    monkeypatch.setattr(account_service, "fetch_player_states", fake_states)
    await settings_service.set_value("account.steam_api_key", "TESTKEY")

    await account_service.save_cookies(cookie(A))
    await account_service.save_cookies(cookie(B))
    # A 过期 45min（>30min 阈值）；B 新鲜 1min 且带非默认在线值：
    # 若实现误传全量 rows，B 会被响应缺失写离线，与「未触碰」不可区分
    await _set_online_check(A, minutes_ago=45.0)
    await _set_online_check(B, minutes_ago=1.0, is_online=True, in_game="Elden Ring")

    result = await account_service.sync_wallets_rotational()
    assert len(calls) == 1
    assert calls[0] == [A], "必须只把过期子集传给批量通道"

    a = await _get_account(A)
    assert a.is_online is True
    assert a.in_game == "Dota 2"
    assert a.online_checked_at is not None
    b = await _get_account(B)
    assert b.is_online is True, "未查询账号不得被误写离线"
    assert b.in_game == "Elden Ring", "未查询账号的游戏名不得被清"
    assert result["ok_count"] == 0  # 钱包通道全失败不影响在线断言


@pytest.mark.asyncio
async def test_online_states_fetch_failure_keeps_stale(db, monkeypatch):
    """批量通道整体失败：不推进时间戳，下一轮自然重试。"""
    calls: list[list[str]] = []

    async def failing_states(steam_ids, api_key, *, proxy_url=None):
        calls.append(list(steam_ids))
        return {}

    await _wallet_fail_path(monkeypatch)
    monkeypatch.setattr(account_service, "fetch_player_states", failing_states)
    await settings_service.set_value("account.steam_api_key", "TESTKEY")

    await account_service.save_cookies(cookie(A))
    await account_service.sync_wallets_rotational()
    assert len(calls) == 1
    assert (await _get_account(A)).online_checked_at is None, "失败不得推进时间戳"

    await account_service.sync_wallets_rotational()
    assert len(calls) == 2, "失败后下一轮必须重试"


@pytest.mark.asyncio
async def test_online_states_new_account_self_heals_once(db, monkeypatch):
    """新绑定账号（NULL 时间戳）首轮补查一次，成功后进入新鲜期不再连发。"""
    calls: list[list[str]] = []

    async def fake_states(steam_ids, api_key, *, proxy_url=None):
        calls.append(list(steam_ids))
        return {sid: {"state": 1, "in_game": ""} for sid in steam_ids}

    await _wallet_fail_path(monkeypatch)
    monkeypatch.setattr(account_service, "fetch_player_states", fake_states)
    await settings_service.set_value("account.steam_api_key", "TESTKEY")

    await account_service.save_cookies(cookie(A))
    await account_service.sync_wallets_rotational()
    assert len(calls) == 1, "NULL 时间戳视为过期，新账号首轮补查"

    await account_service.sync_wallets_rotational()
    assert len(calls) == 1, "成功后进入新鲜期（30min），不连发"
