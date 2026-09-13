"""wishlist 域服务：账户绑定 / 愿望单同步 / 差异入库。

数据源（对齐 07_user_misc/fetch_user_games.py）：
- 愿望单 IWishlistService/GetWishlist/v1 —— 免 Key，公开资料可读
- 已购游戏 IPlayerService/GetOwnedGames/v1 —— 双通道：
  JWT 优先（主账号 Cookie 的 steamLoginSecure 解 webapi_token，
  与 family 域同款免 Key 通道）→ WebAPI Key 兜底（设置页 account.steam_api_key）
同步后新增条目自动触发爬取任务（domains.crawl.start_job）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from .models import TrackedAccount, WishlistItem

logger = logging.getLogger(__name__)

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
RESOLVE_VANITY_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"

# Steam Friend Code → SteamID64 转换常量
# 公式：SteamID64 = int(friend_code) + 76561197960265728
# （Steam 社区公开换算公式）
STEAM64_BASE = 76561197960265728
STEAMID64_REGEX = r"7656119\d{10}"


async def _strategy_proxy() -> str | None:
    """按代理策略引擎解析出网代理；直连策略/引擎不可用时 None（直连）。

    api.steampowered.com 在直连网络下常被墙，同步必须能跟随 clash 策略。
    """
    try:
        from app.domains.proxies import service as proxies_service

        return await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        return None


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def friend_code(steamid: str) -> str | None:
    """SteamID64 → Steam 好友码（前端账户行展示口径，不落库）。"""
    try:
        return str(int(steamid) - STEAM64_BASE)
    except ValueError:
        return None


async def _fetch_persona(steamid: str) -> dict:
    """昵称/头像实时拉取（miniprofile 免 Key 通道）。

    失败静默返回空字段——persona 只是展示增强，不阻断绑定/同步。
    """
    from app.domains.account.steam_wallet import fetch_profile

    try:
        return await fetch_profile(steamid, proxy_url=await _strategy_proxy())
    except Exception:  # noqa: BLE001
        return {"persona_name": "", "avatar_url": "", "online": False, "in_game_name": ""}


def _apply_persona(account: TrackedAccount, persona: dict) -> None:
    """persona 字段落行（非空才覆写，离线失败保留旧值）。"""
    if persona.get("persona_name"):
        account.persona_name = persona["persona_name"]
    if persona.get("avatar_url"):
        account.avatar_url = persona["avatar_url"]


# persona 后台补拉：防重复的进行中账本 + 任务引用（防 create_task 被 GC）
_PERSONA_BACKFILLING: set[str] = set()
_PERSONA_BACKFILL_TASK: asyncio.Task | None = None


async def _backfill_personas(steamids: list[str]) -> None:
    """缺昵称/头像的账户逐个补拉（miniprofile 免 Key）。

    list_accounts 发现缺口时后台触发，前端延迟重取即可看到名字渐次补全；
    任何失败静默——persona 只是展示增强。
    """
    for sid in steamids:
        if sid in _PERSONA_BACKFILLING:
            continue
        _PERSONA_BACKFILLING.add(sid)
        try:
            persona = await _fetch_persona(sid)
            if persona.get("persona_name") or persona.get("avatar_url"):
                async with get_session_factory()() as session:
                    account = await session.get(TrackedAccount, sid)
                    if account is not None:
                        _apply_persona(account, persona)
                        await session.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _PERSONA_BACKFILLING.discard(sid)


async def _resolve_steamid(raw: str) -> str:
    """支持 SteamID64、好友码、个人资料 URL、自定义 URL。

    好友码转换公式：SteamID64 = friend_code + 76561197960265728
    好友码判定：纯数字 4-10 位且不以 7656 开头。
    """
    import re

    raw = raw.strip()

    # 1. 直接是 SteamID64
    if re.match(f"^{STEAMID64_REGEX}$", raw):
        return raw

    # 2. 个人资料 URL（含 SteamID64）
    profile_match = re.search(r"steamcommunity\.com/profiles/(\d+)", raw)
    if profile_match:
        return profile_match.group(1)

    # 3. 好友码（纯数字 4-10 位，不以 7656 开头）
    if re.match(r"^\d{4,10}$", raw) and not raw.startswith("7656"):
        steamid = int(raw) + STEAM64_BASE
        return str(steamid)

    # 4. 自定义 URL（完整 URL 或纯 vanity name）
    from app.domains.settings.service import get_value

    api_key = (await get_value("account.steam_api_key", "")) or ""
    vanity_match = re.search(r"steamcommunity\.com/id/([^/]+)", raw)
    vanity = vanity_match.group(1) if vanity_match else raw

    # 如果是纯数字但不符合好友码格式，报错
    if vanity.isdigit() and not re.match(r"^\d{4,10}$", vanity):
        raise ValueError(f"无法识别的 Steam 标识符: {raw}")

    # vanity name 需要调用 Steam API（需要 API Key）
    if not re.match(r"^[a-zA-Z0-9_-]+$", vanity):
        raise ValueError(f"无法识别的 Steam 标识符: {raw}")

    if not api_key:
        raise ValueError(
            "非好友码/SteamID64 输入需要先在「我」页配置 Steam Web API Key 才能解析自定义 URL"
        )
    async with httpx.AsyncClient(timeout=15, proxy=await _strategy_proxy()) as client:
        resp = await client.get(
            RESOLVE_VANITY_URL, params={"key": api_key, "vanityurl": vanity}
        )
    data = resp.json().get("response", {})
    if data.get("success") != 1 or not data.get("steamid"):
        raise ValueError(f"无法解析自定义 URL: {vanity}")
    return str(data["steamid"])


async def fetch_wishlist(steamid: str) -> list[dict]:
    """返回 [{appid, added_at}]；私有/空返回 []。"""
    async with httpx.AsyncClient(timeout=15, proxy=await _strategy_proxy()) as client:
        resp = await client.get(WISHLIST_URL, params={"steamid": steamid})
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("items", []) or []
    result = []
    for it in items:
        appid = it.get("appid")
        if appid:
            result.append({"appid": int(appid), "added_at": it.get("added_at")})
    return result


class OwnedFetchError(Exception):
    """已购拉取通道失败（鉴权/网络）。区别于『库空/私有』：通道失败必须中断
    owned 覆写，否则会把已有 owned=True 标记静默清空。"""


def _parse_owned_payload(data: dict) -> list[dict]:
    games = data.get("response", {}).get("games", []) or []
    return [{"appid": int(g["appid"]), "name": g.get("name", "")} for g in games]


async def fetch_owned_games_via_jwt(steamid: str, token: str) -> list[dict]:
    """已购拉取（免 Key 通道）：主账号 Cookie 的 webapi_token 直调。

    与 family 域 fetch_member_owned_games 同款用法（access_token query 参数）。
    401/403 抛 OwnedFetchError（token 失效/无权读该账号）。
    """
    from app.domains.family.service import _steam_get

    try:
        resp = await _steam_get(
            OWNED_URL,
            params={
                "access_token": token,
                "steamid": steamid,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
        )
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            raise OwnedFetchError(f"JWT 通道被拒（{status}）：token 失效或无权读取该账号")
        raise OwnedFetchError(f"Steam API 返回 {status}")
    except httpx.HTTPError as e:
        raise OwnedFetchError(f"JWT 通道网络失败: {e}") from e
    return _parse_owned_payload(resp.json())


async def fetch_owned_games_via_key(steamid: str, api_key: str) -> list[dict]:
    """已购拉取（WebAPI Key 通道，历史路径）。401/403/网络错抛 OwnedFetchError。"""
    try:
        async with httpx.AsyncClient(timeout=20, proxy=await _strategy_proxy()) as client:
            resp = await client.get(
                OWNED_URL,
                params={
                    "key": api_key,
                    "steamid": steamid,
                    "include_appinfo": 1,
                    "include_played_free_games": 1,
                },
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise OwnedFetchError(f"WebAPI Key 通道被拒（{e.response.status_code}）") from e
    except httpx.HTTPError as e:
        raise OwnedFetchError(f"WebAPI Key 通道网络失败: {e}") from e
    return _parse_owned_payload(resp.json())


async def fetch_owned_games(steamid: str) -> tuple[list[dict], str | None]:
    """已购游戏双通道拉取。返回 (games, source)；source ∈ {"jwt", "key"}。

    优先级：主账号 Cookie JWT → 设置页 WebAPI Key。
    两个通道都不可用（未绑 Cookie 且未配 Key）抛 OwnedFetchError。
    HTTP 200 + 空 games 是合法结果（库空/私有/无已购），不是错误。
    """
    from app.domains.account import service as account_service
    from app.domains.family.service import extract_webapi_token

    try:
        cookies = await account_service.get_primary_cookies()
        token = extract_webapi_token(cookies) if cookies else None
    except Exception as e:  # noqa: BLE001 —— Cookie 读取失败降级走 Key，不阻断同步
        logger.warning("主账号 Cookie 读取失败，已购拉取直接走 Key 通道: %s", e)
        token = None
    if token:
        try:
            games = await fetch_owned_games_via_jwt(steamid, token)
            return games, "jwt"
        except OwnedFetchError as e:
            logger.warning("已购拉取 JWT 通道失败（%s），回退 WebAPI Key", e)

    from app.domains.settings.service import get_value

    api_key = (await get_value("account.steam_api_key", "")) or ""
    if not api_key:
        raise OwnedFetchError("已购拉取不可用：未绑定 Steam Cookie，且未配置 Web API Key")
    games = await fetch_owned_games_via_key(steamid, api_key)
    return games, "key"


async def _family_personas() -> dict[str, tuple[str, str]]:
    """家庭组快照里的成员档案 {steamid: (昵称, 头像)}——离线可用、零请求。

    存量账户（家庭组同步早期写入，只落了 label 没落档案）的档案回填首选源：
    快照里现成就有，不必去 Steam 拉。读不到返回空 dict。
    """
    from app.domains.family.models import FamilyGroup

    try:
        from app.domains.family.service import get_primary_steamid

        primary = await get_primary_steamid()
    except Exception:  # noqa: BLE001 —— 主账号不可知时无快照可读
        return {}
    if not primary:
        return {}
    try:
        async with get_session_factory()() as session:
            row = await session.get(FamilyGroup, primary)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, tuple[str, str]] = {}
    for m in ((row.members_json if row else None) or []):
        if not isinstance(m, dict) or not m.get("steamid"):
            continue
        name = (m.get("personaName") or m.get("persona_name") or "").strip()
        avatar = (m.get("avatarUrl") or m.get("avatar_url") or "").strip()
        # 快照可能是旧 CDN 域（eccdnx 等）：读出即归一自愈（与前端 HlAvatar 同规则）
        if avatar:
            from app.domains.account.steam_wallet import normalize_avatar_url

            avatar = normalize_avatar_url(avatar)
        if name or avatar:
            out[str(m["steamid"])] = (name, avatar)
    return out


async def list_accounts() -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(TrackedAccount).order_by(TrackedAccount.created_at.desc())
            )
        ).scalars().all()
        # 各账户已购条目数（账户设置弹窗展示「拥有的游戏数量」口径）
        owned_counts: dict[str, int] = dict(
            (
                await session.execute(
                    select(WishlistItem.steamid, func.count())
                    .where(WishlistItem.owned.is_(True), WishlistItem.active.is_(True))
                    .group_by(WishlistItem.steamid)
                )
            ).all()
        )
    # 档案回填三级链：账户行已有 → 家庭组快照补（离线零请求）→
    # miniprofile 后台拉（快照也没有时，如纯手动绑定的外账户）。
    # 快照补到的顺手落库——下次打开页面不再重复拼装。
    fam = await _family_personas()
    global _PERSONA_BACKFILL_TASK
    missing = [
        a.steamid
        for a in rows
        if not ((a.persona_name or "").strip() or a.steamid in fam)
    ]
    if missing:
        _PERSONA_BACKFILL_TASK = asyncio.create_task(_backfill_personas(missing))
    pending = [
        (a, fam[a.steamid])
        for a in rows
        if not (a.persona_name or "").strip() and a.steamid in fam
    ]
    if pending:
        from app.domains.account.steam_wallet import normalize_avatar_url

        async with get_session_factory()() as session:
            for account, (name, avatar) in pending:
                row = await session.get(TrackedAccount, account.steamid)
                if row is None:
                    continue
                if name:
                    row.persona_name = name
                if avatar:
                    row.avatar_url = normalize_avatar_url(avatar)
            await session.commit()
    from app.domains.account.steam_wallet import normalize_avatar_url

    return [
        {
            "steamid": a.steamid,
            "label": a.label,
            "personaName": (a.persona_name or "").strip() or fam.get(a.steamid, ("", ""))[0] or None,
            # 读出即归一（历史 CDN 域自愈；前端 HlAvatar 亦有同款兜底）
            "avatarUrl": normalize_avatar_url(
                (a.avatar_url or "").strip() or fam.get(a.steamid, ("", ""))[1] or ""
            ) or None,
            "friendCode": friend_code(a.steamid),
            "kinds": a.kinds_json or {"wishlist": True, "owned": False},
            "lastSyncAt": a.last_sync_at.isoformat() if a.last_sync_at else None,
            "itemCount": a.item_count,
            "ownedCount": owned_counts.get(a.steamid, 0),
        }
        for a in rows
    ]


async def add_account(steamid_or_vanity: str, label: str = "", kinds: dict | None = None) -> dict:
    steamid = await _resolve_steamid(steamid_or_vanity)
    now = _naive(get_beijing_time_obj())
    # 绑定即抓昵称/头像（愿望单页账户行展示；失败不阻断绑定）
    persona = await _fetch_persona(steamid)
    async with get_session_factory()() as session:
        existing = await session.get(TrackedAccount, steamid)
        if existing:
            raise ValueError(f"账户已存在: {steamid}")
        account = TrackedAccount(
            steamid=steamid,
            label=label or None,
            kinds_json=kinds or {"wishlist": True, "owned": True},
            item_count=0,
            created_at=now,
        )
        _apply_persona(account, persona)
        session.add(account)
        await session.commit()
    logger.info("已绑定账户 %s", steamid)
    return {"steamid": steamid, "friendCode": friend_code(steamid)}


async def remove_account(steamid: str) -> bool:
    async with get_session_factory()() as session:
        account = await session.get(TrackedAccount, steamid)
        if account is None:
            return False
        await session.delete(account)
        items = (
            await session.execute(
                select(WishlistItem).where(WishlistItem.steamid == steamid)
            )
        ).scalars().all()
        for it in items:
            await session.delete(it)
        await session.commit()
    return True


async def sync_account(steamid: str, auto_crawl: bool = True) -> dict:
    """同步愿望单（+已购，双通道拉取）。返回 {added, total, active}。

    已购拉取可按账户关闭（kinds.owned，任务页·已购游戏抓取·账户设置）：
    关闭后不再覆写 owned 标记，已有已购条目回落为愿望单条目。
    新增条目在 auto_crawl=True 且无任务运行时自动触发爬取。
    """
    async with get_session_factory()() as session:
        account = await session.get(TrackedAccount, steamid)
        if account is None:
            raise ValueError(f"账户不存在: {steamid}")

        wishlist = await fetch_wishlist(steamid)

        # 已购拉取：通道失败 ≠ 库空。失败时中断 owned 覆写（保留既有标记），
        # 否则会把全部 owned=True 静默清空；HTTP 200 + 空 games 才是"库空/私有"。
        owned: list[dict] = []
        owned_error: str | None = None
        owned_source: str | None = None
        try:
            owned, owned_source = await fetch_owned_games(steamid)
        except OwnedFetchError as e:
            owned_error = str(e)
            logger.warning("已购拉取失败（%s），本次保留既有 owned 标记", e)

        existing = {
            int(r.appid): r
            for r in (
                await session.execute(
                    select(WishlistItem).where(WishlistItem.steamid == steamid)
                )
            ).scalars()
        }

        now = _naive(get_beijing_time_obj())
        new_appids: list[int] = []
        new_owned_appids: list[int] = []

        for it in wishlist:
            appid = it["appid"]
            added = it.get("added_at")
            added_dt = (
                datetime.fromtimestamp(int(added)) if added else now
            )
            row = existing.get(appid)
            if row is None:
                session.add(
                    WishlistItem(
                        steamid=steamid, appid=appid, added_at=_naive(added_dt), active=True
                    )
                )
                new_appids.append(appid)
            elif not row.active:
                row.active = True

        # 已购并入任务池（active 标记，kinds 按账户开关——任务页「已购游戏抓取
        # ·账户设置」可关掉库太大的账户，只盯愿望单）
        kinds = dict(account.kinds_json or {"wishlist": True, "owned": True})
        owned_failed = bool(kinds.get("owned")) and owned_error is not None
        owned_synced = bool(kinds.get("owned")) and owned_error is None
        owned_ids = {g["appid"] for g in owned} if owned_synced else set()
        if owned_synced and owned:
            for g in owned:
                row = existing.get(g["appid"])
                if row is None:
                    session.add(
                        WishlistItem(
                            steamid=steamid,
                            appid=g["appid"],
                            added_at=now,
                            active=True,
                            owned=True,
                        )
                    )
                    new_owned_appids.append(g["appid"])
                    new_appids.append(g["appid"])
                elif not row.active:
                    row.active = True

        # 已购标记覆写：以本次 GetOwnedGames 结果为准（库转私有时回落为愿望单条目）。
        # 通道失败时 owned_synced 已置 False，跳过覆写——标记保留到下次成功同步。
        if owned_synced:
            for appid, row in existing.items():
                row.owned = appid in owned_ids

        # 愿望单里已移除的且非已购 → active=0（已购条目常驻任务池，不随愿望单移除停用）。
        # 例外：通道失败时已购状态未知，已购行保守保留，等下次成功同步再判定；
        # 手动关注条目（manual，游戏卡星标）免疫——存续不取决于 Steam 真实愿望单
        # （账户同步 15min 高频后，无免疫的手动条目 15 分钟内即被反向核对洗掉）。
        wished_ids = {it["appid"] for it in wishlist}
        if wishlist:
            for appid, row in existing.items():
                if appid in wished_ids or appid in owned_ids or not row.active:
                    continue
                if getattr(row, "manual", False):
                    continue
                if owned_failed and row.owned:
                    continue
                row.active = False

        total_active = (
            await session.execute(
                select(WishlistItem.appid).where(
                    WishlistItem.steamid == steamid, WishlistItem.active.is_(True)
                )
            )
        ).all()

        # 昵称/头像随手刷新（Steam 昵称会改；拉取失败静默保留旧值）
        _apply_persona(account, await _fetch_persona(steamid))
        account.last_sync_at = now
        account.item_count = len(total_active)
        await session.commit()

    result = {
        "steamid": steamid,
        "wishlistCount": len(wishlist),
        "ownedCount": len(owned),
        "ownedSource": owned_source if owned_error is None else None,
        "ownedError": owned_error,
        "added": len(new_appids),
        "addedOwned": len(new_owned_appids),
        "active": len(total_active),
        "newAppids": new_appids,
        "newOwnedAppids": new_owned_appids,
    }
    logger.info(
        "同步 %s：愿望单 %d / 已购 %d（%s）/ 新增 %d（已购新增 %d）",
        steamid, len(wishlist), len(owned),
        f"通道 {owned_source}" if owned_error is None else f"失败：{owned_error}",
        len(new_appids), len(new_owned_appids),
    )

    if auto_crawl and new_appids:
        import asyncio

        from app.domains.crawl import service as crawl_service

        # 首次入库场景禁用打折预检（无历史数据可判断"刷新跳过"）。
        # 自定义已购区域时：愿望单新增走全局区、已购新增走自定义区；
        # 跟随模式（未配置）保持单 job 合并抓取——不为此多拆一次预检。
        try:
            from app.domains.regions.service import owned_regions

            owned_cfg = await owned_regions()
        except Exception:  # noqa: BLE001 —— 配置读不到按跟随处理
            owned_cfg = None

        owned_new_set = set(new_owned_appids)
        wish_only_new = [a for a in new_appids if a not in owned_new_set]
        need_split = owned_cfg is not None and bool(wish_only_new) and bool(new_owned_appids)

        if need_split:
            # 双来源 + 自定义区：后台链式（愿望单 job → 已购 job 串行，
            # 单任务模型不能并行；HTTP 语境不等待爬完）
            if crawl_service.active_job_id() is not None:
                result["crawlTriggered"] = False
                logger.info("已有爬取任务运行，新增 %d 项未自动爬取", len(new_appids))
            else:
                asyncio.create_task(
                    crawl_service.run_sequential(
                        [
                            {
                                "scope": "appids",
                                "appids": wish_only_new,
                                "kind": "wishlist_sync",
                            },
                            {
                                "scope": "appids",
                                "appids": new_owned_appids,
                                "regions": owned_cfg,
                                "kind": "wishlist_sync_owned",
                            },
                        ],
                        from_scheduler=True,  # 自动路径过代理闸门：无可用代理不自动爬
                    )
                )
                result["crawlTriggered"] = True
        else:
            try:
                crawl_service.active_job_id()  # 探测占用
                if owned_cfg is not None and new_owned_appids and not wish_only_new:
                    # 全部新增都来自已购且已配自定义区：单 job 直接带自定义区
                    await crawl_service.start_job(
                        scope="appids",
                        appids=new_appids,
                        regions=owned_cfg,
                        kind="wishlist_sync_owned",
                        from_scheduler=True,  # 自动路径过代理闸门
                    )
                else:
                    await crawl_service.start_job(
                        scope="appids",
                        appids=new_appids,
                        kind="wishlist_sync",
                        from_scheduler=True,  # 自动路径过代理闸门
                    )
                result["crawlTriggered"] = True
            except ValueError as e:
                # 代理闸门（无可用代理不自动爬）：静默跳过，下轮同步再探
                result["crawlTriggered"] = False
                logger.info("无可用代理，新增 %d 项未自动爬取（%s）", len(new_appids), e)
            except RuntimeError:
                result["crawlTriggered"] = False  # 已有任务运行，稍后手动触发
                logger.info("已有爬取任务运行，新增 %d 项未自动爬取", len(new_appids))

    return result


async def list_items(steamid: str | None = None) -> list[dict]:
    """监控条目列表（名称/封面从 games 主档 LEFT JOIN 带出）。

    新绑未爬的条目 games 无行（name/headerImage 为 None）——前端回落显示 appid。
    """
    from app.domains.games.models import Game

    wi = WishlistItem
    query = (
        select(wi, Game.name, Game.name_en, Game.header_image)
        .where(wi.active.is_(True))
        .outerjoin(Game, Game.appid == wi.appid)
    )
    if steamid:
        query = query.where(wi.steamid == steamid)
    query = query.order_by(wi.appid)
    async with get_session_factory()() as session:
        rows = (await session.execute(query)).all()
    return [
        {
            "steamid": item.steamid,
            "appid": int(item.appid),
            "addedAt": item.added_at.isoformat() if item.added_at else None,
            "name": name,
            "nameEn": name_en,
            "headerImage": header_image,
        }
        for item, name, name_en, header_image in rows
    ]


async def list_appids() -> dict:
    """监控池 appid 轻量全集（不带名称/封面）。

    dashboard 只需要 appid 集合（展厅入选判定）与条目总数；全量条目接口
    每行都带名称与封面 URL，监控池上万条时传输/解析成本全在浪费。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(WishlistItem.appid).where(WishlistItem.active.is_(True))
            )
        ).all()
    return {"appids": [r[0] for r in rows], "total": len(rows)}


async def owned_library() -> dict:
    """游戏库页数据源：全部追踪账户的已购游戏矩阵。

    一次请求带回三块：
    - accounts：追踪账户档案 + 已购统计（款数 / CN 价值合计 / 免费款数），
      主账号排首，其余按已购数降序；
    - games：按 appid 去重的游戏主档（名称/封面/类型/发行日/CN 价），
      owners 带每个拥有者与其入库时间（wishlist_items.added_at——本系统
      首次看到该账户拥有此游戏的时间，非 Steam 购买时间）；
    - generatedAt：组装时刻。

    价值口径与家庭库一致：game_current_prices 的 CN 行 cny_fen（未爬到的
    游戏价格为 null，不计入价值合计，前端诚实展示「暂无价格」）。
    已购同步关闭的账户（kinds.owned=false）仍会列出，ownedCount 如实为
    库中现存标记数——前端据此提示「未开启已购同步」。
    """
    from app.domains.account import service as account_service
    from app.domains.account.steam_wallet import normalize_avatar_url
    from app.domains.games.models import Game, GameCurrentPrice

    primary = (await account_service.get_primary_steam_id()) or ""
    async with get_session_factory()() as session:
        accounts = (
            await session.execute(select(TrackedAccount).order_by(TrackedAccount.created_at))
        ).scalars().all()
        rows = (
            await session.execute(
                select(
                    WishlistItem,
                    Game.name,
                    Game.name_en,
                    Game.header_image,
                    Game.genres,
                    Game.release_date,
                    GameCurrentPrice.cny_fen,
                    GameCurrentPrice.original_price,
                    GameCurrentPrice.discount_percent,
                )
                .where(WishlistItem.active.is_(True), WishlistItem.owned.is_(True))
                .outerjoin(Game, Game.appid == WishlistItem.appid)
                .outerjoin(
                    GameCurrentPrice,
                    (GameCurrentPrice.appid == WishlistItem.appid)
                    & (GameCurrentPrice.region_code == "CN"),
                )
                .order_by(WishlistItem.appid)
            )
        ).all()

    games: dict[int, dict] = {}
    stats: dict[str, dict] = {
        a.steamid: {"count": 0, "valueFen": 0, "free": 0} for a in accounts
    }
    for item, name, name_en, header_image, genres, release_date, cny_fen, original_price, discount in rows:
        g = games.get(int(item.appid))
        if g is None:
            g = games[int(item.appid)] = {
                "appid": int(item.appid),
                "name": name,
                "nameEn": name_en,
                "headerImage": header_image,
                "genres": genres,
                "releaseDate": release_date,
                "cnPriceFen": int(cny_fen) if cny_fen is not None else None,
                "originalPriceFen": (
                    int(original_price) if original_price is not None else None
                ),
                "discount": int(discount or 0),
                "owners": [],
            }
        g["owners"].append(
            {
                "steamid": item.steamid,
                "addedAt": item.added_at.isoformat() if item.added_at else None,
            }
        )
        st = stats.get(item.steamid)
        if st is None:  # tracked 行已删的残留条目：照常带出，不计入账户统计
            continue
        st["count"] += 1
        if cny_fen is not None:
            st["valueFen"] += int(cny_fen)
            if int(cny_fen) == 0:
                st["free"] += 1

    acc_list = [
        {
            "steamid": a.steamid,
            "label": a.label,
            "personaName": (a.persona_name or "").strip() or None,
            "avatarUrl": normalize_avatar_url(a.avatar_url) if a.avatar_url else None,
            "friendCode": friend_code(a.steamid),
            "isPrimary": bool(primary and a.steamid == primary),
            "kinds": a.kinds_json or {"wishlist": True, "owned": False},
            "lastSyncAt": a.last_sync_at.isoformat() if a.last_sync_at else None,
            "ownedCount": stats[a.steamid]["count"],
            "valueFen": stats[a.steamid]["valueFen"],
            "freeCount": stats[a.steamid]["free"],
        }
        for a in accounts
    ]
    acc_list.sort(key=lambda x: (not x["isPrimary"], -x["ownedCount"]))
    for g in games.values():
        g["owners"].sort(key=lambda o: (o["addedAt"] is None, o["addedAt"] or ""))

    return {
        "accounts": acc_list,
        "games": list(games.values()),
        "generatedAt": _naive(get_beijing_time_obj()).isoformat(),
    }


async def update_kinds(steamid: str, kinds_patch: dict) -> dict:
    """增量更新账户同步类型（kinds）——任务页「账户设置」的已购开关落点。

    下次同步生效：关闭后该账户不再拉取/覆写已购库（已有条目回落愿望单条目）。
    """
    async with get_session_factory()() as session:
        account = await session.get(TrackedAccount, steamid)
        if account is None:
            raise ValueError(f"账户不存在: {steamid}")
        kinds = dict(account.kinds_json or {"wishlist": True, "owned": True})
        kinds.update(kinds_patch)
        account.kinds_json = kinds
        await session.commit()
    logger.info("更新账户 %s 同步类型: %s", steamid, kinds)
    return {"steamid": steamid, "kinds": kinds}


async def ownership(appids: list[int]) -> dict:
    """批量查询游戏归属（游戏卡左上角状态徽章数据源）。

    返回 {appid: {"type": owned|family|wishlist, "owners": [显示名],
    "ownerAvatars": [头像URL]}}——ownerAvatars 与 owners 按下标对齐，
    无头像的账户为空串（前端落首字符占位）；无归属的 appid 不在结果里。
    - owned：主账户拥有；未配置主账户时任一账户拥有
    - family：非主账户的追踪账户拥有（原版语义：家庭共享来源）
    - wishlist：仅存在于愿望单
    """
    from app.domains.settings.service import get_value
    from app.domains.account import service as account_service
    from app.domains.account.steam_wallet import normalize_avatar_url

    if not appids:
        return {"ownerships": {}}
    # 主账号跟随账户表（与家庭页 get_primary_steamid 同源），旧设置项只作回退——
    # 桌面登录绑定的账号只进账户表；只读设置项时「我」永不命中，owned/family
    # 归属分类随之塌掉（实测：全家人的已购全标成 owned）
    primary = (await account_service.get_primary_steam_id()) or (
        (await get_value("account.steam_id", "")) or ""
    )
    async with get_session_factory()() as session:
        accounts = {
            a.steamid: a
            for a in (await session.execute(select(TrackedAccount))).scalars().all()
        }
        rows = (
            await session.execute(
                select(WishlistItem).where(
                    WishlistItem.appid.in_(appids), WishlistItem.active.is_(True)
                )
            )
        )
        rows = rows.scalars().all()

    def display(a: TrackedAccount) -> tuple[str, str]:
        # 存量 avatar_url 可能是旧 CDN 域——读出即归一自愈（与家庭页同口径）
        avatar = normalize_avatar_url(a.avatar_url) if a.avatar_url else ""
        if primary and a.steamid == primary:
            return "我", avatar
        # 名称三级兜底：备注名 → Steam 昵称 → steamid。缺 persona_name 时
        # 无备注账户会把 ID 数串直接甩给用户（实测复现过）
        return a.label or a.persona_name or a.steamid, avatar

    owned_by: dict[int, list[tuple[str, str]]] = {}
    family_by: dict[int, list[tuple[str, str]]] = {}
    wished_by: dict[int, list[tuple[str, str]]] = {}
    for row in rows:
        acct = accounts.get(row.steamid)
        if acct is None:
            continue
        who = display(acct)
        if row.owned:
            if primary and acct.steamid != primary:
                family_by.setdefault(row.appid, []).append(who)
            else:
                owned_by.setdefault(row.appid, []).append(who)
        else:
            wished_by.setdefault(row.appid, []).append(who)

    def out(pairs: list[tuple[str, str]]) -> dict:
        return {
            "owners": [n for n, _ in pairs],
            "ownerAvatars": [u for _, u in pairs],
        }

    result: dict[str, dict] = {}
    for appid in appids:
        if owned_by.get(appid):
            result[str(appid)] = {"type": "owned", **out(owned_by[appid])}
        elif family_by.get(appid):
            result[str(appid)] = {"type": "family", **out(family_by[appid])}
        elif wished_by.get(appid):
            result[str(appid)] = {"type": "wishlist", **out(wished_by[appid])}
    return {"ownerships": result}
