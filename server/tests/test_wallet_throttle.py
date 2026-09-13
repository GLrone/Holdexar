"""钱包轮转降频机制行为测试（不触真实网络）：

1. 活跃感知门禁：前端心跳（mark_seen）在 90s 内 → 快照 2min 内不重抓；
   无心跳（进程刚启动/无人看）→ 30min 内的成功快照也不重抓；
   App 打开瞬间心跳恢复 → 下一轮立即补抓。
2. 失败递增退避：连续失败按级别 2→5→15→30min 递增冷却，成功清零。
3. 429/403 风控信号：直接进 30min 长冷却（不走普通递增梯度）。
4. 熔断：连续 5 次失败进 10min 慢车道，累计 10 次冻结终态（自动轮转
   停止请求）；手动刷新余额（force）或换绑 Cookie 解锁并清零重计。
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
from app.domains.account.steam_wallet import _from_raw_amounts
from app.domains.settings import service as settings_service

A = "76561198000000001"
B = "76561198000000002"


def cookie(sid: str, token: str = "jwt-token") -> str:
    return f"sessionid=s-{sid[-4:]}; steamLoginSecure={sid}%7C%7C{token}"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.account.models  # noqa: F401
    import app.domains.crawl.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
        return _from_raw_amounts(10000, 0, 23, "CN")

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
        return _from_raw_amounts(10000, 0, 23, "CN")

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
        return _from_raw_amounts(10000, 0, 23, "CN")

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
        return _from_raw_amounts(10000, 0, 23, "CN")

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

    async def limited_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletRateLimitedError(
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
        return _from_raw_amounts(10000, 0, 23, "CN")

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

    async def limited_fetch(cookies, *, verify=None, proxy_url=None):
        raise account_service.WalletRateLimitedError("Steam 节点风控（HTTP 429）")

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
