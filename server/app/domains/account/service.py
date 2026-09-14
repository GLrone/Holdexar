"""account 域服务：多账号 Cookie 存取 / 钱包同步 / 账号地区派生。

账号体系：
- **主账号** = bound_at 最早的账号（第一个绑定的）。愿望单主池 / 家庭组 /
  账单同步跟随主账号，不随切换变化。
- **当前账号**（active）= 全局唯一 is_active=True 的操作账号。钱包展示 /
  CDK 激活 / 免费领取跟随 active，各模块下拉只是切换入口之一。
- 激活计数天然按 steamid 分桶（redeem _ACT_LOG），切号即重计。

旧单账号迁移：启动时把 app_settings 的 account.steam_cookies / 钱包快照 /
资料一次性迁入 steam_accounts 表（首个账号即主账号 + active），迁移后清 KV。

地区派生规则（不变）：**结算币种是唯一真源**。钱包返回的币种 ISO 代号在
crawl_regions 表里反查区服 code（USD 多区时优先 us），这样钱包地区
与全站区服/价格体系天然一致。钱包 country_code 仅在币种查不到对应
区时兜底展示。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from app.domains.account.models import SteamAccount
from app.domains.regions.models import CrawlRegion
from app.domains.settings import service as settings_service
from .steam_wallet import (
    UnknownCurrencyError,
    WalletFetchError,
    WalletInfo,
    WalletRateLimitedError,
    fetch_player_states,
    fetch_profile,
    fetch_wallet,
    filter_login_cookies,
    steam_id_from_cookies,
)

logger = logging.getLogger(__name__)

# app_settings 旧单账号键（迁移源 + 兼容回退，迁移后不再写入）
KEY_COOKIES = "account.steam_cookies"
KEY_WALLET = "account.wallet_snapshot"
KEY_PROFILE = "account.profile"

# 主账号手填 SteamID64（愿望单主池等主账号语义的历史键）。
# 多账号下该键仅作 profile 兼容回退，主账号身份已由表首行承载。
KEY_STEAM_ID = "account.steam_id"

# 结算币种反查区服时，多区共用币种固定取的"基准区"
_CURRENCY_PRIMARY_REGION = {"USD": "us"}

# 每分钟轮转时每账号抓取前的随机延时区间（秒）：多账号错峰，避免同刻齐发
_ROTATION_JITTER_SECONDS = (1.0, 8.0)

# 手动同步的钱包快照缓存有效期（小时）：超期后前端触发刷新
WALLET_STALE_HOURS = 6

# 递增退避表（分钟）：失败级别 0→1→2→3→4+ 依次映射，封顶 30min。
# 单纯 Cookie 失效/网络抖动按此梯度冷却；429/403 风控信号另行直接
# 进 30min 长冷却（_RATE_LIMIT_COOLDOWN_MINUTES）——出口 IP 被盯上时
# 短间隔重试只会加深风控印象（redeem 域节点限流实测同款语义）。
_WALLET_BACKOFF_MINUTES = (2.0, 5.0, 15.0, 30.0)
_RATE_LIMIT_COOLDOWN_MINUTES = 30.0

# 钱包熔断（独立于退避梯子的连续失败计数器）：梯子只管单次失败的短冷却，
# 连续失败到 _WALLET_BREAKER_SLOW_AFTER 次说明是账号级故障（Cookie 失效/
# 被风控），进 10min 慢车道按固定间隔观察；慢车道内再失败到累计
# _WALLET_BREAKER_FREEZE_AFTER 次即冻结终态——自动轮转不再请求该账号，
# 只等手动刷新余额（sync_wallet force=True）或换绑 Cookie 解锁。
_WALLET_BREAKER_SLOW_AFTER = 5
_WALLET_BREAKER_SLOW_MINUTES = 10.0
_WALLET_BREAKER_FREEZE_AFTER = 10

# 活跃感知降频（无人看时自动拉长间隔，心跳恢复即塌缩回高频）。
# 前端每 60s 轮询 GET /account 天然是"有人在看"心跳——后端记录最近
# 心跳时间，轮转按快照新鲜度分级：
# - 有人看（心跳 <90s）→ 快照 2min 内不重抓（分钟级刷新语义不变）
# - 无人看 → 阈值自动爬升到 30min（挂机下载/夜间场景砍 ~90% 请求）
# - 打开 App 瞬间心跳恢复，阈值塌缩，下一轮（≤60s）即补抓，UX 无感
_ACTIVE_PING_SECONDS = 90.0
_ACTIVE_FRESH_MINUTES = 2.0
_IDLE_FRESH_MINUTES = 30.0

# 进程内心跳时间戳（前端轮询 GET /account 时更新；datetime 不易持久化，
# 重启即"无人看"起步，首轮轮转自然补抓一次，无正确性问题）
_last_seen: datetime | None = None


def _naive_now() -> datetime:
    return get_beijing_time_obj().replace(tzinfo=None)


def mark_seen() -> None:
    """前端活跃心跳（GET /account 轮询时调用）：更新进程内最近活跃时间。"""
    global _last_seen
    _last_seen = _naive_now()


def _freshness_threshold_minutes() -> float:
    """当前快照新鲜度阈值（分钟）：有人看 = 分钟级，无人看 = 30min。

    App 开着的瞬间心跳即恢复，阈值塌缩，下一轮轮转（≤60s）即补抓——
    无人值守时请求量自动砍 ~90%，打开时秒级跟上，UX 无感。
    """
    now = _naive_now()
    if _last_seen is not None and (now - _last_seen).total_seconds() < _ACTIVE_PING_SECONDS:
        return _ACTIVE_FRESH_MINUTES
    return _IDLE_FRESH_MINUTES


# ── 账号行级读取 ────────────────────────────────────────────

async def _all_accounts() -> list[SteamAccount]:
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(SteamAccount).order_by(SteamAccount.bound_at, SteamAccount.steam_id)
                )
            )
            .scalars()
            .all()
        )
        # detach 前留档（expire_on_commit=False 时属性仍可读）
        return list(rows)


async def _get_row(steam_id: str) -> SteamAccount | None:
    async with get_session_factory()() as session:
        return await session.get(SteamAccount, steam_id)


async def get_primary_account() -> SteamAccount | None:
    """主账号行（bound_at 最早）；无绑定时 None。"""
    rows = await _all_accounts()
    return rows[0] if rows else None


async def get_primary_steam_id() -> str:
    """主账号 SteamID64；无绑定返回空串。"""
    row = await get_primary_account()
    return row.steam_id if row else ""


async def get_active_account() -> SteamAccount | None:
    """当前账号行；无 active（数据异常）时回退主账号。"""
    async with get_session_factory()() as session:
        row = (
            (
                await session.execute(
                    select(SteamAccount).where(SteamAccount.is_active.is_(True))
                )
            )
            .scalars()
            .first()
        )
    if row is not None:
        return row
    return await get_primary_account()


async def get_active_steam_id() -> str:
    row = await get_active_account()
    return row.steam_id if row else ""


async def get_cookies() -> str:
    """当前账号（active）的登录 Cookie；未绑定返回空串。

    所有消费方（bills/redeem/family/…）经此取 Cookie——切换账号即整体跟随。
    """
    row = await get_active_account()
    if row is None:
        # 兼容回退：未迁移的旧部署（表空但 KV 有值）
        return (await settings_service.get_value(KEY_COOKIES, "")) or ""
    return row.cookies or ""


async def get_primary_cookies() -> str:
    """主账号 Cookie（账单/家庭组等主账号语义消费方专用）。"""
    row = await get_primary_account()
    if row is None:
        return (await settings_service.get_value(KEY_COOKIES, "")) or ""
    return row.cookies or ""


# ── 绑定 / 切换 / 删除 ──────────────────────────────────────

async def save_cookies(cookies_raw: str) -> dict:
    """upsert 账号（SteamID 识别身份）+ 置 active；返回结果与一致性校验信息。

    - 新 SteamID → 新增账号行（后续绑定即第二账号）；
    - 已有 SteamID → 更新该账号 Cookie（换绑/续期语义）；
    - 均置 is_active=True（其余账号取消 active）。
    - mismatch 校验保留：cookie SteamID 与手填 account.steam_id 主身份不一致时告警。
    """
    raw = (cookies_raw or "").strip()
    if raw and "steamLoginSecure" not in raw:
        raise ValueError("Cookie 中缺少 steamLoginSecure，请复制登录后的完整 Cookie")

    filtered = filter_login_cookies(raw)
    cookie_sid = steam_id_from_cookies(filtered)
    if not cookie_sid:
        raise ValueError("无法从 Cookie 解析 SteamID64，请确认复制的是登录后的完整 Cookie")

    bound_sid = (await settings_service.get_value(KEY_STEAM_ID, "")) or ""
    mismatch = bool(
        bound_sid and cookie_sid and bound_sid.strip() != cookie_sid
    )

    now = _naive_now()
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, cookie_sid)
        is_new = row is None
        if row is None:
            row = SteamAccount(steam_id=cookie_sid, bound_at=now)
            session.add(row)
        row.cookies = filtered
        row.is_active = True
        # active 移交：其余账号取消
        others = (
            (
                await session.execute(
                    select(SteamAccount).where(
                        SteamAccount.steam_id != cookie_sid,
                        SteamAccount.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for o in others:
            o.is_active = False
        await session.commit()

    # 换绑/重绑 = 用户显式介入：熔断冻结与失败计数一并清零（新账号本来就干净）
    await _reset_wallet_breaker(cookie_sid)

    return {
        "ok": True,
        "steam_id": cookie_sid,
        "is_new": is_new,
        "bound_steam_id": bound_sid,
        "mismatch": mismatch,
        "message": (
            "Cookie 中的 SteamID 与已保存的 SteamID64 不一致，请核对账号"
            if mismatch
            else ""
        ),
    }


async def set_active(steam_id: str) -> dict:
    """切换当前账号（全局唯一 active）。账号不存在时 ValueError。"""
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        if row is None:
            raise ValueError(f"账号不存在: {steam_id}")
        others = (
            (
                await session.execute(
                    select(SteamAccount).where(
                        SteamAccount.steam_id != steam_id,
                        SteamAccount.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for o in others:
            o.is_active = False
        row.is_active = True
        await session.commit()
    return {"ok": True, "steam_id": steam_id}


async def remove_account(steam_id: str) -> dict:
    """删除指定账号。删除 active 时回退剩余首个（即新主账号）。"""
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        if row is None:
            raise ValueError(f"账号不存在: {steam_id}")
        was_active = row.is_active
        await session.delete(row)
        await session.flush()
        rest = (
            (
                await session.execute(
                    select(SteamAccount).order_by(SteamAccount.bound_at, SteamAccount.steam_id)
                )
            )
            .scalars()
            .all()
        )
        if rest:
            if was_active:
                rest[0].is_active = True  # 回退剩余首个
            elif not any(r.is_active for r in rest):
                rest[0].is_active = True  # 数据兜底：无 active 也归位
        await session.commit()
        remaining = len(rest)
    return {"ok": True, "removed": steam_id, "remaining": remaining}


# ── 钱包 / 资料 同步 ────────────────────────────────────────

async def _derive_region(currency_code: str, region_name: str = "") -> str:
    """结算币种 → crawl_regions 反查区服 code（小写）。

    判定顺序：
    1. 多区共用币种（USD/EUR/…）且有地区名（history 页「货币转换至
       INR（印度、29）」的括号地区名）→ 按 crawl_regions.name 精确匹配；
    2. 共用币种无地区名 → 固定基准区（USD→us…）；
    3. 唯一币种区（KZT→kz、INR→in…）直接反查。
    region_name 与币种不一致时以币种为准（name 可能来自过期的转换记录）。
    查不到返回空串（前端退化为只展示币种）。
    """
    code = (currency_code or "").strip().upper()
    name = (region_name or "").strip()
    if not code:
        return ""
    async with get_session_factory()() as session:
        matches = (
            await session.execute(
                select(CrawlRegion.code, CrawlRegion.currency).where(
                    CrawlRegion.currency == code
                )
            )
        ).all()
    if len(matches) > 1:
        # 多区共用：地区名匹配（CC_LIST 命名带后缀如「南亚（USD）」，
        # history 页是纯名「南亚」，故按前缀匹配），否则基准区
        if name:
            async with get_session_factory()() as session:
                by_name = (
                    await session.execute(
                        select(CrawlRegion.code).where(
                            CrawlRegion.name == name
                        )
                    )
                ).first()
                if by_name is None:
                    by_name = (
                        await session.execute(
                            select(CrawlRegion.code).where(
                                CrawlRegion.name.startswith(name)
                            )
                        )
                    ).first()
            if by_name and by_name[0] in {m[0] for m in matches}:
                return by_name[0]
        return _CURRENCY_PRIMARY_REGION.get(code, matches[0][0])
    if matches:
        return matches[0][0]
    return ""


async def sync_wallet(*, force: bool = False) -> dict:
    """抓取**当前账号**钱包并落其行内快照。force=False 且快照新鲜时返回缓存。

    force=True 是用户显式的「手动刷新余额」：熔断冻结状态在此解锁
    （计数清零后重试，成败都不回冻结态——再挂了由轮转从零重新升级）。
    """
    row = await get_active_account()
    if row is None or not row.cookies or "steamLoginSecure" not in row.cookies:
        return {"ok": False, "status": "no_cookie", "error": "尚未绑定 Steam Cookie"}

    if force and (row.wallet_frozen or int(row.wallet_fail_streak or 0) > 0):
        await _reset_wallet_breaker(row.steam_id)
    snapshot = row.wallet_json if isinstance(row.wallet_json, dict) else None
    now = _naive_now()
    if (
        not force
        and isinstance(snapshot, dict)
        and snapshot.get("checked_at")
    ):
        age_h = (now - datetime.fromisoformat(snapshot["checked_at"])).total_seconds() / 3600
        if age_h < WALLET_STALE_HOURS:
            return {"ok": True, "cached": True, "wallet": snapshot}

    try:
        info: WalletInfo = await fetch_wallet(row.cookies, proxy_url=await _strategy_proxy())
    except UnknownCurrencyError as exc:
        await _save_wallet(row.steam_id, error=str(exc), now=now)
        await _sync_profile_row(row.steam_id, row.cookies, now)  # 钱包失败不连坐资料
        return {"ok": False, "error": str(exc)}
    except WalletFetchError as exc:
        await _save_wallet(row.steam_id, error=str(exc), now=now)
        await _sync_profile_row(row.steam_id, row.cookies, now)
        return {"ok": False, "error": str(exc)}

    # country_code 在 store account 通道承载 history 页地区名（中文），交判定层翻译
    region_code = await _derive_region(info.currency_code, info.country_code)

    snapshot = {
        "balance": info.balance,
        "balance_display": info.balance_display,
        "currency_code": info.currency_code,
        "currency_symbol": info.currency_symbol,
        "currency_id": info.currency_id,
        "region_code": region_code,
        "country_code": info.country_code,
        "steam_id": info.steam_id,  # store account 通道的页面归属账户
        "checked_at": now.isoformat(),
        "check_ok": True,
        "error": "",
    }
    await _save_wallet(row.steam_id, snapshot=snapshot, now=now)

    # 顺带补/换账户资料（头像/昵称）：无资料时拉取，静默失败
    await _sync_profile_row(row.steam_id, row.cookies, now)

    return {"ok": True, "cached": False, "wallet": snapshot}


async def _save_wallet(
    steam_id: str,
    *,
    snapshot: dict | None = None,
    error: str = "",
    now: datetime | None = None,
    backoff_level: int | None = None,
    fail_streak: int | None = None,
    frozen: bool | None = None,
) -> None:
    """落账号行的钱包快照/错误（失败时保留旧余额字段，只翻 check_ok/error）。

    backoff_level：显式传值时覆盖写（成功清零 / 失败递增由调用方算好）；
    None = 不动（保留现有级别）。fail_streak / frozen 同语义（熔断计数与
    冻结终态），同样只由调用方算好传入。
    """
    now = now or _naive_now()
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        if row is None:
            return
        if snapshot is not None:
            row.wallet_json = snapshot
            row.wallet_error = ""
            if backoff_level is not None:
                row.wallet_backoff_level = backoff_level
            if fail_streak is not None:
                row.wallet_fail_streak = fail_streak
            if frozen is not None:
                row.wallet_frozen = frozen
        else:
            old = row.wallet_json if isinstance(row.wallet_json, dict) else {}
            row.wallet_json = {**old, "check_ok": False, "error": error,
                               "checked_at": now.isoformat()}
            row.wallet_error = error[:300]
            if backoff_level is not None:
                row.wallet_backoff_level = backoff_level
            if fail_streak is not None:
                row.wallet_fail_streak = fail_streak
            if frozen is not None:
                row.wallet_frozen = frozen
        row.wallet_checked_at = now
        await session.commit()


async def _reset_wallet_breaker(steam_id: str) -> None:
    """熔断解锁（手动刷新余额 / 换绑 Cookie）：冻结、连续失败计数、
    退避级别一并清零——用户显式介入后从零重计，不带着旧账进新一轮。"""
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        if row is None:
            return
        row.wallet_frozen = False
        row.wallet_fail_streak = 0
        row.wallet_backoff_level = 0
        await session.commit()


def _is_rate_limited(error: str) -> bool:
    """钱包错误串里的风控信号（429/403 节点拒绝）。"""
    text = (error or "").lower()
    return "429" in text or "403" in text or "风控" in text


async def _sync_profile_row(steam_id: str, cookies: str, now: datetime) -> None:
    """刷账号资料 + 在线态兜底（miniprofile 公开通道，不依赖页面登录态）。

    每轮转都刷（在线态是实时语义）；昵称只在缺失时补写。头像 URL 为
    **归一化覆盖写**（fetch_profile 已归一）：抓到非空且与存量不同才写——
    CDN 域名轮换（akamaized 已死/queniuqe 存量）时旧域 URL 在此自愈，
    也不会因网络抖动把头像清空。在线字段为官方 GetPlayerSummaries 不可用
    时的兜底（粗糙：只区分离线/在线/游戏名）。
    """
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, steam_id)
        if row is None:
            return
        fetched = await fetch_profile(steam_id, proxy_url=await _strategy_proxy())
        if fetched.get("persona_name") and not row.persona_name:
            row.persona_name = fetched["persona_name"]
        new_avatar = fetched.get("avatar_url", "")
        if new_avatar and new_avatar != row.avatar_url:
            row.avatar_url = new_avatar
        row.is_online = bool(fetched.get("online"))
        row.in_game = (fetched.get("in_game_name") or "")[:200]
        await session.commit()


async def refresh_online_states(rows: list[SteamAccount] | None = None) -> dict:
    """官方在线状态批量刷新（ISteamUser/GetPlayerSummaries/v2，一次查全部账号）。

    有 API Key 时为**权威通道**：personastate 1-6 = 在线（2 忙碌/3 离开/4 打盹
    对 dot 均显示在线绿，具体语义放 tooltip），gameextrainfo = 正在玩的游戏名。
    无 Key / 调用失败时保留 miniprofile 兜底结果（_sync_profile_row 已写）。
    """
    rows = rows if rows is not None else await _all_accounts()
    if not rows:
        return {"ok": False, "status": "no_account"}
    api_key = (await settings_service.get_value("account.steam_api_key", "")) or ""
    if not api_key:
        return {"ok": False, "status": "no_key"}

    states = await fetch_player_states(
        [r.steam_id for r in rows], api_key, proxy_url=await _strategy_proxy()
    )
    if not states:
        return {"ok": False, "status": "fetch_failed"}
    async with get_session_factory()() as session:
        for r in rows:
            s = states.get(r.steam_id)
            if s is None:  # 响应缺失 = 离线（私密档案同款语义）
                row = await session.get(SteamAccount, r.steam_id)
                if row is not None:
                    row.is_online = False
                    row.in_game = ""
                continue
            row = await session.get(SteamAccount, r.steam_id)
            if row is not None:
                row.is_online = s["state"] > 0
                row.in_game = s["in_game"][:200]
        await session.commit()
    return {"ok": True, "count": len(states)}


async def sync_wallets_rotational() -> dict:
    """每分钟轮转：按需刷新全部绑定账号的钱包（活跃感知降频 + 递增退避 + 熔断）。

    四层门禁（空闲自动拉长间隔 / 失败冷却 / 限制访问回落）：

    0. **熔断冻结**：连续失败累计 10 次（5 次进 10min 慢车道后再挂 5 次）
       的账号停止一切自动请求，等手动刷新余额或换绑解锁——Cookie 失效/
       被风控的账号不再陪跑。
    1. **快照新鲜度（活跃感知）**：快照距上次成功 < 阈值则本轮跳过。
       阈值随前端心跳分级——App 开着（60s 轮询即心跳）= 2min，
       无人看自动爬升到 30min。打开 App 瞬间阈值塌缩，≤60s 内补上。
    2. **失败递增退避**：上次失败的账号按级别冷却 2→5→15→30min
       （成功清零）。Cookie 失效/网络故障不每分钟硬打 Steam。
    3. **429/403 长冷却**：风控信号直接进 30min 冷却——出口 IP 被
       盯上时短间隔重试只会加深风控印象。熔断慢车道（10min）不覆盖
       本冷却：风控信号在场时仍按 30min。

    账号间随机延时错峰不变；多账号语义不变。
    """
    import asyncio
    import random

    now = _naive_now()
    rows = await _all_accounts()
    if not rows:
        return {"ok": False, "status": "no_cookie"}

    fresh_minutes = _freshness_threshold_minutes()

    async def _one(row: SteamAccount) -> dict:
        # 门禁 0：熔断冻结（连续失败终态）——自动轮转不再请求该账号，
        # 解锁只走手动刷新余额（sync_wallet force）或换绑 Cookie
        if row.wallet_frozen:
            return {"ok": False, "status": "frozen", "steam_id": row.steam_id}
        # 门禁 1：快照新鲜（距上次成功 < 阈值）→ 本轮跳过，不吃配额
        snapshot = row.wallet_json if isinstance(row.wallet_json, dict) else None
        if (
            snapshot is not None
            and snapshot.get("check_ok")
            and snapshot.get("checked_at")
        ):
            try:
                age_min = (now - datetime.fromisoformat(snapshot["checked_at"])).total_seconds() / 60
            except ValueError:
                age_min = None
            if age_min is not None and age_min < fresh_minutes:
                return {"ok": True, "status": "fresh_skip", "steam_id": row.steam_id}
        # 门禁 2+3：失败退避（级别查表递增；429/403 信号直接顶格；
        # 熔断慢车道——连续失败达标后固定 10min 观察间隔，风控信号在场除外）
        if row.wallet_error and row.wallet_checked_at is not None:
            elapsed_min = (now - row.wallet_checked_at).total_seconds() / 60
            if _is_rate_limited(row.wallet_error):
                cooldown = _RATE_LIMIT_COOLDOWN_MINUTES
            elif int(row.wallet_fail_streak or 0) >= _WALLET_BREAKER_SLOW_AFTER:
                cooldown = _WALLET_BREAKER_SLOW_MINUTES
            else:
                level = min(int(row.wallet_backoff_level or 0), len(_WALLET_BACKOFF_MINUTES) - 1)
                cooldown = _WALLET_BACKOFF_MINUTES[level]
            if elapsed_min < cooldown:
                return {"ok": False, "status": "backoff", "steam_id": row.steam_id}
        await asyncio.sleep(random.uniform(*_ROTATION_JITTER_SECONDS))
        return await _sync_wallet_of(row.steam_id)

    results = await asyncio.gather(*(_one(r) for r in rows), return_exceptions=True)
    ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    skipped = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "backoff")
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[钱包轮转] 同步异常：%s", r)

    # 官方在线状态批量刷新（有 API Key 时权威覆盖 miniprofile 兜底；一次调用查全部）
    state_result = await refresh_online_states(rows)
    if not state_result.get("ok"):
        logger.debug("[钱包轮转] 官方在线状态未刷新（%s）", state_result.get("status"))

    # 失败原因汇总。httpx 的逐请求日志已被静音（它把请求 URL 连同 API Key
    # 一起打进了日志），于是「HTTP 403」这类唯一的状态码线索也一并消失——
    # 调用方若只说「无成功账号」，鉴权失效会被误读成「退避中」，排障白跑一趟。
    # 这里按「原因 × 条数」带出去；日志侧另有脱敏过滤器兜底。
    reasons: dict[str, int] = {}
    for r in results:
        if not isinstance(r, dict) or r.get("ok"):
            continue
        label = str(r.get("error") or r.get("status") or "未知")
        reasons[label[:120]] = reasons.get(label[:120], 0) + 1

    frozen_count = sum(
        1 for r in results if isinstance(r, dict) and r.get("status") == "frozen"
    )

    return {
        "ok": ok_count > 0,
        "ok_count": ok_count,
        "skipped": skipped,
        "frozen": frozen_count,
        "total": len(rows),
        "reasons": reasons,
    }


async def _sync_wallet_of(steam_id: str) -> dict:
    """指定账号的钱包同步（轮转通道；逻辑与 sync_wallet 同源，行内快照）。

    失败时退避级别 +1（上限 4）且熔断计数 +1，成功双双清零——级别落行内，
    轮转门禁按表（_WALLET_BACKOFF_MINUTES）换算冷却分钟；计数达
    _WALLET_BREAKER_FREEZE_AFTER 置冻结终态，此后轮转门禁 0 直接跳过，
    等手动刷新余额或换绑解锁。
    """
    row = await _get_row(steam_id)
    if row is None or not row.cookies or "steamLoginSecure" not in row.cookies:
        return {"ok": False, "status": "no_cookie", "error": "尚未绑定 Steam Cookie"}

    now = _naive_now()
    next_level = min(int(row.wallet_backoff_level or 0) + 1, len(_WALLET_BACKOFF_MINUTES))
    next_streak = int(row.wallet_fail_streak or 0) + 1
    will_freeze = next_streak >= _WALLET_BREAKER_FREEZE_AFTER
    try:
        info: WalletInfo = await fetch_wallet(row.cookies, proxy_url=await _strategy_proxy())
    except UnknownCurrencyError as exc:
        await _save_wallet(steam_id, error=str(exc), now=now, backoff_level=next_level,
                           fail_streak=next_streak, frozen=will_freeze)
        if will_freeze and not row.wallet_frozen:
            logger.warning(
                "[钱包轮转] 账号 %s 连续 %d 次钱包同步失败，熔断冻结自动刷新"
                "（手动刷新余额或换绑 Cookie 解锁）", steam_id, next_streak,
            )
        return {"ok": False, "error": str(exc)}
    except WalletFetchError as exc:
        await _save_wallet(steam_id, error=str(exc), now=now, backoff_level=next_level,
                           fail_streak=next_streak, frozen=will_freeze)
        if will_freeze and not row.wallet_frozen:
            logger.warning(
                "[钱包轮转] 账号 %s 连续 %d 次钱包同步失败，熔断冻结自动刷新"
                "（手动刷新余额或换绑 Cookie 解锁）", steam_id, next_streak,
            )
        return {"ok": False, "error": str(exc)}

    region_code = await _derive_region(info.currency_code, info.country_code)
    snapshot = {
        "balance": info.balance,
        "balance_display": info.balance_display,
        "currency_code": info.currency_code,
        "currency_symbol": info.currency_symbol,
        "currency_id": info.currency_id,
        "region_code": region_code,
        "country_code": info.country_code,
        "steam_id": info.steam_id,
        "checked_at": now.isoformat(),
        "check_ok": True,
        "error": "",
    }
    await _save_wallet(steam_id, snapshot=snapshot, now=now, backoff_level=0,
                       fail_streak=0, frozen=False)
    await _sync_profile_row(steam_id, row.cookies, now)
    return {"ok": True, "wallet": snapshot}


async def clear_cookies() -> None:
    """解绑全部（清账号表 + 旧 KV；手填 SteamID64 / API Key 不动）。"""
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(select(SteamAccount))
            )
            .scalars()
            .all()
        )
        for r in rows:
            await session.delete(r)
        await session.commit()
    await settings_service.set_value(KEY_COOKIES, "")


# ── 状态汇总（永不回传 Cookie 明文）─────────────────────────

def _friend_code(steam_id: str) -> str:
    """SteamID64 → 好友码（Steam 好友码列表可复制的整数）。"""
    try:
        return str(int(steam_id) - 76561197960265728)
    except (TypeError, ValueError):
        return ""


async def _wishlist_counts(steam_ids: list[str]) -> dict[str, dict]:
    """按 steamid 统计 wishlist_items：愿望单条数（owned=False）/ 游戏数（owned=True）。

    只统计 active 条目（任务池语义）；未注册的账号返回 0/0。
    """
    if not steam_ids:
        return {}
    from app.domains.wishlist.models import WishlistItem

    out = {sid: {"wishlist_count": 0, "game_count": 0} for sid in steam_ids}
    try:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(
                        WishlistItem.steamid,
                        WishlistItem.owned,
                        func.count(WishlistItem.appid),
                    ).where(
                        WishlistItem.steamid.in_(steam_ids),
                        WishlistItem.active.is_(True),
                    )
                    .group_by(WishlistItem.steamid, WishlistItem.owned)
                )
            ).all()
        for sid, owned, cnt in rows:
            if owned:
                out[sid]["game_count"] = cnt
            else:
                out[sid]["wishlist_count"] = cnt
    except Exception:  # noqa: BLE001
        logger.debug("愿望单计数查询失败（表缺失等），按 0 处理", exc_info=True)
    return out


def _account_usage(steam_id: str) -> int:
    """该账号 30 分钟窗口内已用激活次数（redeem 进程内计数）。"""
    try:
        from app.domains.redeem import service as redeem_service

        return redeem_service._account_usage(steam_id)
    except Exception:  # noqa: BLE001
        return 0


async def _account_payload(row: SteamAccount, primary_id: str, counts: dict) -> dict:
    """账号行的对外展示载荷（不含 Cookie 明文）。"""
    wallet = row.wallet_json if isinstance(row.wallet_json, dict) else None
    c = counts or {"wishlist_count": 0, "game_count": 0}
    return {
        "steam_id": row.steam_id,
        "friend_code": _friend_code(row.steam_id),
        "persona_name": row.persona_name,
        "avatar_url": row.avatar_url,
        "is_active": bool(row.is_active),
        "is_primary": row.steam_id == primary_id,
        "bound_at": row.bound_at.isoformat() if row.bound_at else None,
        "wallet": wallet,
        "wallet_error": row.wallet_error or "",
        "wallet_frozen": bool(row.wallet_frozen),
        "wishlist_count": c.get("wishlist_count", 0),
        "game_count": c.get("game_count", 0),
        "redeem_used": _account_usage(row.steam_id),
        "is_online": bool(row.is_online),
        "in_game": row.in_game or "",
    }


async def list_accounts() -> list[dict]:
    """账号列表（绑定顺序；第一个即主账号）。"""
    rows = await _all_accounts()
    if not rows:
        return []
    primary_id = rows[0].steam_id
    counts = await _wishlist_counts([r.steam_id for r in rows])
    return [await _account_payload(r, primary_id, counts.get(r.steam_id, {})) for r in rows]


async def get_status() -> dict:
    """当前账号（active）状态汇总 + 全账号列表（永不回传 Cookie 明文）。

    顶层字段保持旧单账号语义（active 的钱包/资料），前端顶栏胶囊零改动适配；
    accounts 为多账号列表，primary_steam_id 标识主账号。
    """
    row = await get_active_account()
    accounts = await list_accounts()
    if row is None:
        return {
            "has_cookie": False,
            "cookie_steam_id": "",
            "bound_steam_id": (await settings_service.get_value(KEY_STEAM_ID, "")) or "",
            "mismatch": False,
            "profile": None,
            "wallet": None,
            "sync_error": "",
            "message": "",
            "accounts": [],
            "primary_steam_id": "",
        }
    bound_sid = (await settings_service.get_value(KEY_STEAM_ID, "")) or ""
    profile = {"persona_name": row.persona_name, "avatar_url": row.avatar_url,
               "steam_id": row.steam_id} if (row.persona_name or row.avatar_url) else None
    return {
        "has_cookie": bool(row.cookies and "steamLoginSecure" in row.cookies),
        "cookie_steam_id": row.steam_id,
        "bound_steam_id": bound_sid,
        "mismatch": bool(bound_sid and bound_sid != row.steam_id),
        "profile": profile,
        "wallet": row.wallet_json if isinstance(row.wallet_json, dict) else None,
        "sync_error": row.wallet_error or "",
        "message": "",
        "accounts": accounts,
        "primary_steam_id": accounts[0]["steam_id"] if accounts else "",
        # 当前账号 Steam 真实在线状态（顶栏头像 dot 数据源）
        "is_online": bool(row.is_online),
        "in_game": row.in_game or "",
    }


# ── 绑定后置任务（账号注册 + 资料拉取 + 愿望单注册）──────────

async def after_bind(steam_id: str) -> None:
    """绑定成功后：确保 wishlist tracked_accounts 注册该账号（愿望单/游戏计数来源）。

    - tracked_accounts 无该账号则注册（kinds 默认 wishlist+owned 双开）；
    - 注册即可计数（counts 查 wishlist_items，未同步时为 0，由同步链补齐）。
    """
    try:
        from app.domains.wishlist import service as wishlist_service
        from app.domains.wishlist.models import TrackedAccount

        async with get_session_factory()() as session:
            existing = await session.get(TrackedAccount, steam_id)
        if existing is None:
            await wishlist_service.add_account(steam_id, kinds={"wishlist": True, "owned": True})
            logger.info("[account] 账号 %s 已注册愿望单追踪（愿望单+已购双开）", steam_id)
    except Exception:  # noqa: BLE001
        logger.warning("[account] 账号 %s 愿望单注册失败（不阻断绑定）", steam_id, exc_info=True)


async def _strategy_proxy() -> str | None:
    """按代理策略引擎解析本次出网代理（代理策略走代理，直连策略 None）。

    steamcommunity / api.steampowered.com 直连常被墙，默认策略应为 proxy_first。
    策略引擎不可用（表缺失等）时回退直连。
    """
    try:
        from app.domains.proxies import service as proxies_service

        return await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        return None


# ── 旧单账号 KV → 多账号表 迁移（启动幂等）──────────────────

async def migrate_legacy_kv() -> int:
    """旧 account.steam_cookies（+快照/资料）迁入 steam_accounts；已迁移则跳过。

    返回迁移的账号数（0 = 无需迁移）。迁移后清 Cookie KV，钱包/资料快照 KV
    一并清空（数据已入表）。
    """
    raw = (await settings_service.get_value(KEY_COOKIES, "")) or ""
    if not raw or "steamLoginSecure" not in raw:
        return 0
    cookie_sid = steam_id_from_cookies(raw)
    if not cookie_sid:
        return 0

    async with get_session_factory()() as session:
        existing = await session.get(SteamAccount, cookie_sid)
        if existing is not None:
            # 表里已有该账号（多账号时代再回到旧部署不会发生，防御式跳过）
            await settings_service.set_value(KEY_COOKIES, "")
            return 0
        row = SteamAccount(
            steam_id=cookie_sid,
            cookies=raw,
            persona_name="",
            avatar_url="",
            wallet_json=await settings_service.get_value(KEY_WALLET, None),
            is_active=True,
            bound_at=_naive_now(),
            wallet_checked_at=None,
            wallet_error="",
        )
        # 旧资料快照（account.profile）迁入行内
        profile = await settings_service.get_value(KEY_PROFILE, None)
        if isinstance(profile, dict):
            row.persona_name = profile.get("persona_name", "") or ""
            row.avatar_url = profile.get("avatar_url", "") or ""
        session.add(row)
        await session.commit()

    # KV 清空（表为唯一真源；手填 account.steam_id / API Key 不动）
    await settings_service.set_value(KEY_COOKIES, "")
    await settings_service.set_value(KEY_WALLET, None)
    await settings_service.set_value(KEY_PROFILE, None)
    logger.info("[account] 旧单账号 Cookie 已迁移至多账号表：%s", cookie_sid)
    return 1
