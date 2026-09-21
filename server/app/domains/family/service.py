"""family 域服务：输入解析 → 家庭组发现 → 成员自动补齐。

接口链（Steam 公开端点）：
- steamLoginSecure 的 JWT 即 webapi_token：`<steamid>||<token>` 后段，
  直接以 `?access_token=` 调 IFamilyGroupsService / IPlayerService —— 免 Web API Key；
- GET IFamilyGroupsService/GetFamilyGroupForUser/v1/?access_token=&include_family_group_response=true
  → {family_groupid, family_group: {name, members: [{steamid, role, ...}]}}
- POST IPlayerService/GetPlayerLinkDetails/v1/?access_token=（body 传 steamids[]）
  → 成员昵称/头像（public_data.persona_name / avatar）
- GET IPlayerService/GetOwnedGames/v1/?access_token=&steamid= → 成员已购库（家庭组内
  资料公开即可读，无需 Key——免 Key 通道）

所有出网走代理策略引擎（proxy_first 默认）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote, unquote

import httpx
from sqlalchemy import select

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from app.domains.settings import service as settings_service

from .models import FamilyGroup

logger = logging.getLogger(__name__)

FAMILY_FOR_USER_URL = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
PLAYER_LINK_URL = "https://api.steampowered.com/IPlayerService/GetPlayerLinkDetails/v1/"
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
SHARED_LIBRARY_URL = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"

STEAM64_BASE = 76561197960265728
MAX_FAMILY = 6


def _norm_avatar(url: str | None) -> str:
    """头像 URL 归一（steam_wallet.normalize_avatar_url 别名，懒加载防环）。"""
    from app.domains.account.steam_wallet import normalize_avatar_url

    return normalize_avatar_url(url)


async def _strategy_proxy() -> str | None:
    try:
        from app.domains.proxies import service as proxies_service

        return await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        return None


def _is_ssl_error(exc: Exception) -> bool:
    clues = ("CERTIFICATE_VERIFY_FAILED", "self-signed", "certificate verify failed",
             "ssl", "SSL", "unable to get local issuer")
    text = f"{type(exc).__name__}: {exc}"
    return any(c in text for c in clues)


async def _steam_get(url: str, params: dict, *, method: str = "GET") -> httpx.Response:
    """Steam API 请求（access_token 通道）：代理优先 + SSL 证书降级重试。

    经 Clash 内核（vless/ws）代理时严格校验必失败（中间链路证书不被系统
    信任链接受），与钱包模块同规则：先严格跑，证书错则 verify=False 重试
    一次——本机回环/自选代理流量，属可接受折中。
    """
    proxy = await _strategy_proxy()
    try:
        async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
            resp = await client.request(method, url, params=params)
    except httpx.HTTPError as e:
        if not _is_ssl_error(e):
            raise
        logger.info("Steam API 证书校验失败（经代理场景），降级跳过校验重试")
        async with httpx.AsyncClient(timeout=20, proxy=proxy, verify=False) as client:
            resp = await client.request(method, url, params=params)
    resp.raise_for_status()
    return resp


# ─── 输入解析（好友码 / SteamID64 / 资料 URL / 自定义 URL）────────

async def resolve_steamid(raw: str) -> dict:
    """把任意输入解析成 {steamid, kind}；好友码即时转换，URL 类走 vanity API。

    好友码 = SteamID64 - 76561197960265728（Steam 好友码列表可复制的整数）。
    附带实时 persona 预览（昵称/头像，miniprofile 免 Key 通道）供输入框确认身份。
    """
    from app.domains.wishlist.service import _resolve_steamid as wishlist_resolve

    text = (raw or "").strip()
    if not text:
        raise ValueError("输入为空")

    if re.fullmatch(r"\d{4,10}", text) and not text.startswith("7656"):
        steamid = str(int(text) + STEAM64_BASE)
        kind = "friend_code"
    else:
        steamid = await wishlist_resolve(text)
        kind = "steamid64" if re.fullmatch(r"7656119\d{10,}", steamid) else "vanity"

    preview = await _persona_preview(steamid)
    return {"steamid": steamid, "kind": kind, **preview}


async def _profile_xml_preview(steamid: str) -> dict:
    """steamcommunity.com/profiles/<sid>/?xml=1 公开资料兜底（匿名可达，无需 Cookie）。

    miniprofile 的匿名会话常返回**空 hash 头像**（URL 形如
    avatars…/.jpg，等于没拿到）——此时按用户主页 XML 接口补一轮：
    资料设为公开的成员返回 avatarFull/avatarMedium 与昵称；隐私非公开
    则该接口也不给头像（Steam 对未登录会话的隐私边界，落占位兜底）。
    拿不到任何字段返回 {}。
    """
    url = f"https://steamcommunity.com/profiles/{steamid}/?xml=1"
    try:
        async with httpx.AsyncClient(
            timeout=10.0, proxy=await _strategy_proxy(),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://steamcommunity.com/"},
        ) as client:
            resp = await client.get(url)
        text = resp.text
    except Exception:  # noqa: BLE001 —— 兜底失败静默
        return {}
    name_m = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", text)
    avatar_m = re.search(r"<avatarFull><!\[CDATA\[(.*?)\]\]></avatarFull>", text) or re.search(
        r"<avatarMedium><!\[CDATA\[(.*?)\]\]></avatarMedium>", text
    )
    if not name_m and not avatar_m:
        return {}
    return {
        "personaName": name_m.group(1).strip() if name_m else "",
        "avatarUrl": _norm_avatar(avatar_m.group(1).strip()) if avatar_m else "",
    }


def _avatar_missing(p: dict) -> bool:
    """miniprofile 结果是否算「没拿到头像」：URL 为空，或空 hash 形态（…/.jpg）。"""
    url = (p.get("avatarUrl") or p.get("avatar_url") or "").strip()
    if not url:
        return True
    # 匿名 miniprofile 的空 hash：avatars…/.jpg / …/_medium.jpg（hash 段为空）
    tail = url.rsplit("/", 1)[-1]
    return tail.startswith(".") or tail.startswith("_")


async def _persona_preview(steamid: str) -> dict:
    """昵称/头像实时预览：miniprofile 免 Key 通道 → 公开资料 XML 兜底。

    miniprofile 匿名会话可能返回空 hash 头像（有 URL 无图），按「缺失」
    处理再走 profiles/?xml=1（用户主页接口）；两段都失败静默返回空。
    """
    from app.domains.account.steam_wallet import fetch_profile

    try:
        p = await fetch_profile(steamid, proxy_url=await _strategy_proxy())
        if p.get("persona_name") and not _avatar_missing(p):
            return {"personaName": p["persona_name"], "avatarUrl": p.get("avatar_url", "")}
    except Exception:  # noqa: BLE001 —— 预览失败不阻断解析
        pass
    fallback = await _profile_xml_preview(steamid)
    if fallback:
        return fallback
    return {"personaName": "", "avatarUrl": ""}


def extract_webapi_token(cookies_raw: str) -> str | None:
    """从 steamLoginSecure（`<steamid>||<jwt>`）提取 webapi_token。

    Cookie 可能以 WebView2/Steam 原生的百分号编码落库（`||` → `%7C%7C`），
    必须先 unquote 再切分，否则整个 `steamid%7C%7Cjwt` 会被当作 token 发
    出去导致 401。
    """
    for part in (cookies_raw or "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip() != "steamLoginSecure":
            continue
        body = unquote(value.strip())
        if "||" in body:
            token = body.rsplit("||", 1)[-1].strip()
            return token or None
        return body or None
    return None


# ─── Steam Web API（access_token 通道，免 Key）─────────────────

async def _fetch_family_group(token: str) -> dict:
    """拉取当前登录账号所在家庭组；未加入家庭组返回 {joined: False}。"""
    resp = await _steam_get(
        FAMILY_FOR_USER_URL,
        {"access_token": token, "include_family_group_response": "true"},
    )
    data = resp.json().get("response", {})
    if not data or not data.get("family_groupid"):
        return {"joined": False}
    group = data.get("family_group") or {}
    members = group.get("members") or []
    return {
        "joined": True,
        "family_groupid": str(data["family_groupid"]),
        "family_name": (group.get("name") or "").strip() or None,
        "members": [
            {
                "steamid": str(m.get("steamid", "")),
                "role": str(m.get("role", "")),
            }
            for m in members if m.get("steamid")
        ],
    }


async def _fetch_player_details(token: str, steamids: list[str]) -> dict[str, dict]:
    """批量拉成员昵称/头像（GetPlayerLinkDetails，public_data 段）。"""
    if not steamids:
        return {}
    params = [("access_token", token)]
    params += [(f"steamids[{i}]", sid) for i, sid in enumerate(steamids)]
    try:
        # GET + query 串传 steamids[]（POST 该端点会 405）
        resp = await _steam_get(PLAYER_LINK_URL, dict(params))
        accounts = resp.json().get("response", {}).get("accounts", []) or []
    except Exception as e:  # noqa: BLE001 —— 昵称失败不阻断家庭组同步
        logger.warning("成员昵称获取失败：%s", e)
        return {}
    out: dict[str, dict] = {}
    for acc in accounts:
        pd = acc.get("public_data") or {}
        sid = str(pd.get("steamid") or "")
        if sid:
            out[sid] = {
                "persona_name": (pd.get("persona_name") or "").strip(),
                "avatar_url": _norm_avatar((pd.get("avatar") or "").strip()),
            }
    return out


async def fetch_member_owned_games(token: str, steamid: str) -> list[dict]:
    """拉某成员已购库（家庭组内公开资料即可读，access_token 通道免 Key）。"""
    try:
        resp = await _steam_get(
            OWNED_GAMES_URL,
            {
                "access_token": token,
                "steamid": steamid,
                "include_appinfo": 0,
                "include_played_free_games": 1,
            },
        )
        games = resp.json().get("response", {}).get("games", []) or []
        return [{"appid": int(g["appid"])} for g in games if g.get("appid")]
    except Exception as e:  # noqa: BLE001 —— 单成员失败不阻断
        logger.warning("成员 %s 已购库获取失败：%s", steamid, e)
        return []


# ─── 绑定 / 同步 ────────────────────────────────────────────

async def get_primary_steamid() -> str:
    """主账号 = 第一个绑定的账号（多账号表首行）；家庭组跟随主账号不随切号变。

    无绑定账号时回退手填 SteamID64（旧部署兼容）。
    """
    from app.domains.account import service as account_service

    primary = await account_service.get_primary_steam_id()
    if primary:
        return primary
    bound = await settings_service.get_value("account.steam_id", "") or ""
    if bound:
        return bound
    raise ValueError("尚未绑定 Steam Cookie，无法发现家庭组（请先在「我」页绑定）")


async def sync_family_group() -> dict:
    """用主账号 Cookie 的 webapi_token 拉家庭组并自动补齐成员。

    - 成员自动写入 tracked_accounts（kinds: wishlist+owned），复用既有同步链路；
    - 快照落 family_groups 表（前端家庭页数据源）；
    - 返回 {joined, familyName, members: [{steamid, role, personaName, avatarUrl}]}。
    """
    # 主账号 Cookie（家庭组跟随主账号，不随当前账号切换）
    from app.domains.account import service as account_service

    cookies = await account_service.get_primary_cookies()
    token = extract_webapi_token(cookies)
    if not token:
        raise ValueError("Cookie 中无法提取 webapi_token，请重新绑定 Steam Cookie")

    primary = await get_primary_steamid()
    group = await _fetch_family_group(token)
    now = datetime.utcnow()

    if not group.get("joined"):
        await _save_group(primary, {
            "joined": False, "fetch_ok": True, "error": None, "members": [],
            "family_name": None, "family_groupid": None,
        }, now)
        return {"joined": False, "steamid": primary,
                "message": "当前账号未加入家庭组（Steam 官方上限 6 人，可在 Steam 客户端创建/加入）"}

    raw_members: list[dict] = group["members"]
    details = await _fetch_player_details(
        token, [m["steamid"] for m in raw_members]
    )
    members: list[dict] = []
    for m in raw_members:
        d = details.get(m["steamid"], {})
        members.append({
            "steamid": m["steamid"],
            "role": m.get("role", ""),
            "personaName": d.get("persona_name", ""),
            "avatarUrl": _norm_avatar(d.get("avatar_url", "")),
        })

    # GetPlayerLinkDetails 的 public_data 已不返 avatar URL（只剩 sha_digest_avatar
    # 摘要），缺头像/昵称的成员走 miniprofile 通道补齐（失败静默，不阻断同步）
    missing = [m for m in members if not m["avatarUrl"] or not m["personaName"]]
    if missing:
        previews = await asyncio.gather(
            *(_persona_preview(m["steamid"]) for m in missing)
        )
        for m, p in zip(missing, previews):
            m["personaName"] = m["personaName"] or p.get("personaName", "")
            m["avatarUrl"] = m["avatarUrl"] or _norm_avatar(p.get("avatarUrl", ""))

    await _sync_members_to_accounts(members, now)
    await _save_group(primary, {
        "joined": True, "fetch_ok": True, "error": None, "members": members,
        "family_name": group.get("family_name"), "family_groupid": group["family_groupid"],
    }, now)

    logger.info("家庭组同步成功：%s %d 人", group.get("family_name"), len(members))
    return {
        "joined": True,
        "steamid": primary,
        "familyGroupid": group["family_groupid"],
        "familyName": group.get("family_name"),
        "members": members,
    }


async def _save_group(primary: str, snap: dict, now: datetime) -> None:
    async with get_session_factory()() as session:
        row = await session.get(FamilyGroup, primary)
        if row is None:
            row = FamilyGroup(steamid=primary)
            session.add(row)
        row.family_groupid = snap.get("family_groupid")
        row.family_name = snap.get("family_name")
        row.members_json = snap.get("members") or []
        row.fetch_ok = snap.get("fetch_ok", True)
        row.last_error = snap.get("error")
        row.member_count = len(snap.get("members") or [])
        row.updated_at = now
        await session.commit()


async def _sync_members_to_accounts(members: list[dict], now: datetime) -> None:
    """家庭成员自动落 tracked_accounts（kinds 全开愿望单+已购）。

    新成员：label 暂代备注名 + persona/头像一并落库（愿望单页账户行展示用）；
    存量成员：只刷新 persona_name/avatar_url（昵称头像会改），label 是用户
    备注名不动。
    """
    from app.domains.wishlist.models import TrackedAccount

    async with get_session_factory()() as session:
        existing = {
            r.steamid: r
            for r in (
                await session.execute(
                    select(TrackedAccount).where(
                        TrackedAccount.steamid.in_([m["steamid"] for m in members])
                    )
                )
            ).scalars()
        }
        added = updated = 0
        for m in members:
            row = existing.get(m["steamid"])
            if row is None:
                session.add(TrackedAccount(
                    steamid=m["steamid"],
                    label=m.get("personaName") or None,
                    kinds_json={"wishlist": True, "owned": True},
                    persona_name=(m.get("personaName") or "").strip() or None,
                    avatar_url=(m.get("avatarUrl") or "").strip() or None,
                    created_at=now,
                ))
                added += 1
                continue
            persona = (m.get("personaName") or "").strip()
            avatar = (m.get("avatarUrl") or "").strip()
            if persona and row.persona_name != persona:
                row.persona_name = persona
                updated += 1
            if avatar and row.avatar_url != avatar:
                row.avatar_url = avatar
                updated += 1
        if added or updated:
            await session.commit()
            logger.info("家庭组成员同步进追踪：新增 %d，档案刷新 %d 处", added, updated)


# app_settings 键：成员地区持久化 {steamid: region_code}（家庭页手动选择的落点）
KEY_MEMBER_REGIONS = "family.member_regions"


# miniprofile 补齐失败的节流窗口：网络断时不让每个 status 请求都空烧外网
_AVATAR_BACKOFF_SECONDS = 60
_avatar_backoff_until: datetime | None = None


async def _backfill_member_avatars(members: list[dict], primary: str) -> list[dict]:
    """成员头像/昵称兜底补齐（读路径自愈，get_status 专用）。

    存量快照的头像缺失有两种成因：GetPlayerLinkDetails 的 public_data 早已
    不返 avatar URL（同步时 miniprofile 补齐又失败过）、或 members_json 里
    还挂着已回收的旧 CDN 域。这里读出即归一，仍缺的成员按 steamid 走
    miniprofile 免 Key 通道（steamcommunity 用户主页同源接口，无需 Cookie）
    并发补齐——成功**写回快照**，下次 status 直接命中不再打外网。

    失败静默 + 节流：一轮全部拉不到（代理断/网络离线）时置 60s 退避，
    期间 status 直接返回现状（前端显示首字符占位），不阻塞页面。
    """
    global _avatar_backoff_until
    for m in members:
        if not isinstance(m, dict):
            continue
        # 存量 members_json 可能是旧 CDN 域（eccdnx/queniuqe）——读出即归一自愈
        m["avatarUrl"] = _norm_avatar(m.get("avatarUrl") or m.get("avatar_url") or "")
        m["personaName"] = m.get("personaName") or m.get("persona_name") or ""
    missing = [
        m for m in members
        if isinstance(m, dict) and m.get("steamid") and (not m.get("avatarUrl") or not m.get("personaName"))
    ]
    now = datetime.utcnow()
    if not missing or now < (_avatar_backoff_until or now):
        return members

    previews = await asyncio.gather(
        *(_persona_preview(str(m["steamid"])) for m in missing)
    )
    got_any = False
    for m, p in zip(missing, previews):
        if p.get("avatarUrl"):
            m["avatarUrl"] = _norm_avatar(p["avatarUrl"])
            got_any = True
        m["personaName"] = m["personaName"] or p.get("personaName", "")

    if not got_any:
        _avatar_backoff_until = now + timedelta(seconds=_AVATAR_BACKOFF_SECONDS)
        return members
    _avatar_backoff_until = None

    # 写回快照（只更 members_json；失败不影响本次返回，下次再自愈）
    try:
        async with get_session_factory()() as session:
            row = await session.get(FamilyGroup, primary)
            if row is not None:
                row.members_json = members
                await session.commit()
                logger.info("[family] status 头像兜底补齐 %d/%d 人并写回快照", len(missing), len(members))
    except Exception:  # noqa: BLE001
        logger.exception("[family] status 头像补齐写回失败（不影响本次返回）")
    return members


async def get_status() -> dict:
    """家庭页数据：主账号 + 家庭组快照（无则 joined=False）。

    附加字段：
    - walletRegion：主账号 Cookie 钱包派生的结算地区（币种反查 crawl_regions），
      前端用于「主账号地区自动切换」；无钱包快照时为 None。
    - memberRegions：各成员手动选择的地区（持久化），前端恢复现场。
    """
    try:
        primary = await get_primary_steamid()
    except ValueError as e:
        return {"bound": False, "message": str(e), "members": []}

    async with get_session_factory()() as session:
        row = await session.get(FamilyGroup, primary)
    wallet = await settings_service.get_value("account.wallet_snapshot", None)
    saved_regions = await settings_service.get_value(KEY_MEMBER_REGIONS, None)
    wallet_region = (
        wallet.get("region_code") if isinstance(wallet, dict) else None
    ) or None
    member_regions = saved_regions if isinstance(saved_regions, dict) else {}

    base = {
        "bound": True,
        "steamid": primary,
        "familyName": row.family_name if row else None,
        "familyGroupid": row.family_groupid if row else None,
        "members": await _backfill_member_avatars(list(row.members_json or []), primary) if row else [],
        "lastError": row.last_error if row else None,
        "updatedAt": row.updated_at.isoformat() if row and row.updated_at else None,
        "walletRegion": wallet_region,
        "memberRegions": member_regions,
    }
    if row is None:
        return {**base, "joined": False,
                "members": [], "message": "尚未同步家庭组（点「同步家庭组」开始）"}
    return {**base, "joined": bool(row.member_count)}


# ─── 家庭共享库（GetSharedLibraryApps + 成员游玩聚合）──────────

async def _fetch_shared_library(token: str, family_groupid: str) -> list[dict]:
    """拉家庭共享库 app 清单（含入库时间与有序拥有者——全部派生分析的字段来源）。

    响应 apps 字段：
    - rt_time_acquired：入库时间戳（秒）——热力图/增长趋势/购买动态/活跃分档的口径
    - owner_steamids：**按入库先后排序**（[0]=最早入库，at(-1)=最近入库=购买者）
    - presence_count：该 app 被多少成员拥有
    - exclude_reason：排除原因枚举，**0 = Included（可共享）**；字段恒存在，
      判「被排除」必须比 0，不能用 `is not None`（那样会把可共享的
      也标成已排除）
    """
    resp = await _steam_get(
        SHARED_LIBRARY_URL,
        {"access_token": token, "family_groupid": family_groupid,
         "include_own": "true", "include_excluded": "true"},
    )
    apps = resp.json().get("response", {}).get("apps", []) or []
    out: list[dict] = []
    for a in apps:
        if not a.get("appid"):
            continue
        out.append({
            "appid": int(a["appid"]),
            "name": (a.get("name") or "").strip() or None,
            "presence": int(a.get("presence_count") or 0),
            "excluded": int(a.get("exclude_reason") or 0) != 0,
            "time_acquired": int(a.get("rt_time_acquired") or 0),
            "owners": [str(sid) for sid in (a.get("owner_steamids") or [])],
        })
    return out


async def fetch_family_library() -> dict:
    """家庭库全量数据（共享库 ∪ 成员已购/游玩聚合 + games 表元数据/CN 价）。

    链路：主账号 Cookie → GetFamilyGroupForUser（组 id+成员）→
    GetSharedLibraryApps（共享清单）→ 每成员 GetOwnedGames（appid+rtime_last_played+
    playtime_forever，含 include_appinfo=1：Steam 直接带 name 作兜底；其他成员
    只回 appid 时 name 为 None）→ games 表 LEFT JOIN 补名称/封面/CN 现价/史低
    （name 双兜底：games 表名 or Steam 返回名）。
    """
    # 主账号 Cookie（家庭库跟随主账号）
    from app.domains.account import service as account_service

    cookies = await account_service.get_primary_cookies()
    token = extract_webapi_token(cookies)
    if not token:
        raise ValueError("Cookie 中无法提取 webapi_token，请重新绑定 Steam Cookie")

    group = await _fetch_family_group(token)
    if not group.get("joined") or not group.get("family_groupid"):
        raise ValueError("当前账号未加入家庭组，无法拉取家庭库")

    shared_apps = await _fetch_shared_library(token, group["family_groupid"])
    shared_map = {a["appid"]: a for a in shared_apps}

    # 成员已购 + 游玩数据（appid → 该游戏在库信息）
    member_rows: list[dict] = group["members"]
    member_ids = [m["steamid"] for m in member_rows if m.get("steamid")]
    details = await _fetch_player_details(token, member_ids)

    owned_by_app: dict[int, list[str]] = {}   # appid → 拥有者 steamid 列表
    steam_name_by_app: dict[int, str] = {}   # appid → Steam 返回的游戏名（兜底源）
    playtime_by_app: dict[int, int] = {}     # appid → 全家总时长（分钟）
    last_play_by_app: dict[int, int] = {}    # appid → 最近游玩时间戳
    member_play: dict[str, list[dict]] = {}   # steamid → [{appid, minutes, last}]
    member_owned_count: dict[str, int] = {}

    # 成员已购/游玩**并发**拉取（此前串行：每成员一趟代理 HTTPS，2-10s/人 ×
    # 全组累计 30s+；任一人超时整个聚合直接失败——游玩动态「经常失败」的主因）。
    # gather + return_exceptions：单成员失败只缺席该成员明细，不再拖垮全家聚合。
    owned_results = await asyncio.gather(
        *(fetch_member_owned_games_full(token, m["steamid"]) for m in member_rows),
        return_exceptions=True,
    )
    for m, result in zip(member_rows, owned_results):
        sid = m["steamid"]
        if isinstance(result, BaseException):
            logger.warning("[family] 成员 %s 已购/游玩拉取失败（该成员明细缺席）：%s", sid, result)
            result = []
        games = result
        member_owned_count[sid] = len(games)
        plist: list[dict] = []
        for g in games:
            appid = g["appid"]
            owned_by_app.setdefault(appid, []).append(sid)
            if g.get("name") and appid not in steam_name_by_app:
                steam_name_by_app[appid] = g["name"]
            playtime_by_app[appid] = playtime_by_app.get(appid, 0) + g.get("playtime_forever", 0)
            lp = g.get("rtime_last_played") or 0
            if lp > last_play_by_app.get(appid, 0):
                last_play_by_app[appid] = lp
            if g.get("playtime_forever") or g.get("playtime_2weeks"):
                plist.append({
                    "appid": appid,
                    "minutes": g.get("playtime_forever", 0),
                    "minutes2w": g.get("playtime_2weeks", 0),
                    "last": lp,
                })
        member_play[sid] = sorted(
            plist, key=lambda x: (-(x.get("minutes2w") or 0), -(x.get("minutes") or 0))
        )

    # Steam 返回的游戏名落 games 表（最小 upsert：新 appid 插入、缺名补名）——
    # 家庭库/愿望单聚合/全站 games join 都靠这张表补名，本地缺行即大量空名
    if steam_name_by_app:
        from app.domains.games.models import Game as GameRow

        async with get_session_factory()() as session:
            existing = {
                r[0]: r[1] for r in (
                    await session.execute(
                        select(GameRow.appid, GameRow.name).where(
                            GameRow.appid.in_(list(steam_name_by_app))
                        )
                    )
                ).all()
            }
            for appid, name in steam_name_by_app.items():
                if appid not in existing:
                    session.add(GameRow(appid=appid, name=name))
                elif not existing[appid]:
                    row = await session.get(GameRow, appid)
                    row.name = name
            await session.commit()

    # 成员档案（昵称/头像）合并 role
    members_out = []
    for m in member_rows:
        sid = m["steamid"]
        d = details.get(sid, {})
        members_out.append({
            "steamid": sid,
            "role": m.get("role", ""),
            "personaName": d.get("persona_name", ""),
            "avatarUrl": _norm_avatar(d.get("avatar_url", "")),
            "ownedCount": member_owned_count.get(sid, 0),
        })

    # GetPlayerLinkDetails 的 public_data 早已不返 avatar URL（见下方同步链路
    # 同款注释），头像从 family_groups.members_json（同步时 miniprofile 补齐过）
    # 合并；仍缺的走 miniprofile 实时补（失败静默）
    try:
        _primary = await get_primary_steamid()
    except ValueError:
        _primary = ""
    _fam_row = None
    if _primary:
        async with get_session_factory()() as session:
            _fam_row = await session.get(FamilyGroup, _primary)
    _saved = {
        str(m.get("steamid")): m
        for m in ((_fam_row.members_json if _fam_row else None) or [])
        if isinstance(m, dict)
    }
    for m in members_out:
        s = _saved.get(m["steamid"], {})
        m["personaName"] = m["personaName"] or s.get("personaName", "") or s.get("persona_name", "")
        # 存量 members_json 可能是旧 CDN 域（eccdnx/queniuqe）——读出即归一自愈
        m["avatarUrl"] = _norm_avatar(
            m["avatarUrl"] or s.get("avatarUrl", "") or s.get("avatar_url", "")
        )
    _missing = [m for m in members_out if not m["avatarUrl"] or not m["personaName"]]
    if _missing:
        _previews = await asyncio.gather(
            *(_persona_preview(m["steamid"]) for m in _missing)
        )
        for m, p in zip(_missing, _previews):
            m["personaName"] = m["personaName"] or p.get("personaName", "")
            m["avatarUrl"] = m["avatarUrl"] or p.get("avatarUrl", "")

    # 全部涉及 appid（共享清单 ∪ 成员已购）
    all_appids = sorted(set(shared_map) | set(owned_by_app))

    # games 表本地补元数据 + CN 现价
    meta = await _local_games_meta(all_appids)

    games_out: list[dict] = []
    for appid in all_appids:
        m = meta.get(appid) or {}
        owners = owned_by_app.get(appid, [])
        shared_info = shared_map.get(appid)
        # 有序拥有者（入库先后序）：共享清单有就用 Steam 原序（购买者=at(-1)、
        # 最早入库=[0]）；清单外的已购游戏无序，回退成员遍历序
        shared_owners = shared_info["owners"] if shared_info else None
        ordered_owners = shared_owners or owners
        games_out.append({
            "appid": appid,
            "name": m.get("name") or steam_name_by_app.get(appid) or (shared_info or {}).get("name"),
            "headerImage": m.get("header_image"),
            "releaseDate": m.get("release_date"),
            "genres": m.get("genres"),
            "cnPriceFen": m.get("cn_price_fen"),
            "originalPriceFen": m.get("original_price_fen"),
            "discount": m.get("discount"),
            "owners": ordered_owners,             # 入库先后序（共享清单口径）
            "ownerCount": len(owners),
            "presence": (shared_info["presence"] if shared_info else len(owners)),
            "excluded": bool(shared_info and shared_info["excluded"]),
            "inSharedLib": appid in shared_map,
            "timeAcquired": (shared_info["time_acquired"] if shared_info else 0),
            "buyer": (ordered_owners[-1] if ordered_owners else None),  # 最近入库者
            "playtimeMinutes": playtime_by_app.get(appid, 0),
            "lastPlayed": last_play_by_app.get(appid, 0),
        })

    # 快照 upsert（持久化兜底：重启/断网时家庭页照常出数据）
    await _upsert_library_snapshot(group["family_groupid"], games_out)

    # 游玩明细随组档案落库（play_json）：库快照表只有 app 级字段，成员游玩
    # 明细此前不落盘——实时聚合一失败、落到快照兜底路径，memberPlay 恒空，
    # 游玩动态就只剩空态（「经常失败」的另一半）。成功即存，兜底也有数据。
    if _primary and member_play:
        try:
            async with get_session_factory()() as session:
                row = await session.get(FamilyGroup, _primary)
                if row is not None:
                    row.play_json = member_play
                    await session.commit()
        except Exception:  # noqa: BLE001 —— 写库失败不影响本次返回
            logger.exception("[family] 游玩明细快照写库失败（不影响本次返回）")

    return {
        "familyGroupid": group["family_groupid"],
        "familyName": group.get("family_name"),
        "members": members_out,
        "games": games_out,
        "memberPlay": member_play,
        "sharedCount": len(shared_apps),
    }


async def fetch_member_owned_games_full(token: str, steamid: str) -> list[dict]:
    """拉某成员已购库（含游玩时长/最近游玩 + 游戏名兜底）。

    include_appinfo=1 让 Steam 直接带 name（主账号会返回；其他成员可能只回
    appid，name 允许为 None，由 games 表本地补齐兜底）。
    """
    resp = await _steam_get(
        OWNED_GAMES_URL,
        {
            "access_token": token,
            "steamid": steamid,
            "include_appinfo": 1,
            "include_played_free_games": 1,
        },
    )
    games = resp.json().get("response", {}).get("games", []) or []
    return [
        {
            "appid": int(g["appid"]),
            "playtime_forever": int(g.get("playtime_forever") or 0),
            "playtime_2weeks": int(g.get("playtime_2weeks") or 0),
            "rtime_last_played": int(g.get("rtime_last_played") or 0),
            "name": (g.get("name") or "").strip() or None,
        }
        for g in games if g.get("appid")
    ]


async def _local_games_meta(appids: list[int]) -> dict[int, dict]:
    """games 表 + game_current_prices CN 行：名称/封面/发行日/类型/CN 价/折扣。"""
    if not appids:
        return {}
    from app.domains.games.models import Game, GameCurrentPrice

    out: dict[int, dict] = {}
    async with get_session_factory()() as session:
        for i in range(0, len(appids), 400):  # SQLite 变量上限分批
            chunk = appids[i:i + 400]
            rows = (
                await session.execute(
                    select(Game, GameCurrentPrice)
                    .outerjoin(
                        GameCurrentPrice,
                        (GameCurrentPrice.appid == Game.appid)
                        & (GameCurrentPrice.region_code == "CN"),
                    )
                    .where(Game.appid.in_(chunk))
                )
            ).all()
            for game, price in rows:
                out[int(game.appid)] = {
                    "name": game.name,
                    "header_image": game.header_image,
                    "release_date": game.release_date,
                    "genres": game.genres,
                    "cn_price_fen": int(price.cny_fen) if price and price.cny_fen is not None else None,
                    # 原价（未折 CNY 分）——价值洞察「原价合计/节省率」口径
                    "original_price_fen": (
                        int(price.original_price) if price and price.original_price is not None else None
                    ),
                    "discount": price.discount_percent if price else 0,
                }
    return out


async def _upsert_library_snapshot(family_groupid: str, games: list[dict]) -> None:
    """家庭库快照落库（持久化兜底，离线可看语义）。

    upsert 语义：新 appid 插入；已有行只在共享清单口径字段上更新
    （owners/timeAcquired/excluded/presence），保留 name（本地兜底名可能更全）。
    """
    from .models import FamilyLibrarySnapshot

    now = datetime.utcnow()
    async with get_session_factory()() as session:
        for g in games:
            snap = await session.get(FamilyLibrarySnapshot, (g["appid"], family_groupid))
            if snap is None:
                session.add(FamilyLibrarySnapshot(
                    appid=g["appid"],
                    family_groupid=family_groupid,
                    name=g.get("name"),
                    owners_json=g.get("owners") or [],
                    time_acquired=g.get("timeAcquired") or 0,
                    presence=g.get("presence") or 0,
                    excluded=bool(g.get("excluded")),
                    updated_at=now,
                ))
            else:
                snap.owners_json = g.get("owners") or []
                snap.time_acquired = g.get("timeAcquired") or 0
                snap.presence = g.get("presence") or 0
                snap.excluded = bool(g.get("excluded"))
                snap.name = g.get("name") or snap.name
                snap.updated_at = now
        await session.commit()
    logger.info("[family] 家庭库快照已落库：%d app（组 %s）", len(games), family_groupid)


async def _library_from_snapshot(family_groupid: str | None = None) -> dict | None:
    """从快照表重建家庭库 payload（实时聚合失败/启动首开时的兜底数据源）。

    返回与 fetch_family_library 同构的 dict（memberPlay 从 family_groups
    的 play_json 快照合并——实时聚合成功时随组落库，兜底路径也有游玩数据）；
    无快照/未指定组且无任何组时返回 None。
    成员档案从 family_groups 快照读（members_json 内含 persona/avatar）。
    """
    from .models import FamilyLibrarySnapshot

    async with get_session_factory()() as session:
        stmt = select(FamilyLibrarySnapshot)
        if family_groupid:
            stmt = stmt.where(FamilyLibrarySnapshot.family_groupid == family_groupid)
        rows = (await session.execute(stmt.order_by(
            FamilyLibrarySnapshot.family_groupid,
            FamilyLibrarySnapshot.time_acquired.desc(),
        ))).scalars().all()
        if not rows:
            return None
        # 未指定组 → 取最新一组（按组内最大 updated_at）
        if not family_groupid:
            groupid = rows[0].family_groupid
            rows = [r for r in rows if r.family_groupid == groupid]
        groupid = rows[0].family_groupid

        # 成员档案（family_groups 快照；_save_group 存 camelCase 字段）。
        # 完全离线可用：主账号读不到（Cookie 摘除/账号表空）时跳过档案，
        # 由快照 owners 全集推成员身份（无名档，前端用 steamid 尾号展示）。
        members_out: list[dict] = []
        fam_row = None
        try:
            primary = await get_primary_steamid()
        except ValueError:
            primary = ""
        if primary:
            fam_row = await session.get(FamilyGroup, primary)
        if fam_row and fam_row.members_json:
            for m in fam_row.members_json:
                sid = str(m.get("steamid", ""))
                if sid:
                    members_out.append({
                        "steamid": sid,
                        "role": str(m.get("role", "")),
                        "personaName": m.get("personaName", "") or m.get("persona_name", ""),
                        "avatarUrl": m.get("avatarUrl", "") or m.get("avatar_url", ""),
                        "ownedCount": sum(
                            1 for r in rows if r.owners_json and sid in r.owners_json
                        ),
                    })
        if not members_out:
            owner_ids: list[str] = []
            for r in rows:
                for sid in (r.owners_json or []):
                    if sid and sid not in owner_ids:
                        owner_ids.append(sid)
            members_out = [{
                "steamid": sid, "role": "", "personaName": "", "avatarUrl": "",
                "ownedCount": sum(1 for r in rows if r.owners_json and sid in r.owners_json),
            } for sid in owner_ids]
        member_ids = {m["steamid"] for m in members_out}

        all_appids = sorted({int(r.appid) for r in rows})
        meta = await _local_games_meta(all_appids)

        games_out = []
        for r in rows:
            m = meta.get(int(r.appid)) or {}
            owners = list(r.owners_json or [])
            games_out.append({
                "appid": int(r.appid),
                "name": m.get("name") or r.name,
                "headerImage": m.get("header_image"),
                "releaseDate": m.get("release_date"),
                "genres": m.get("genres"),
                "cnPriceFen": m.get("cn_price_fen"),
                "originalPriceFen": m.get("original_price_fen"),
                "discount": m.get("discount") or 0,
                "owners": owners,
                "ownerCount": len(owners),
                "presence": r.presence or len(owners),
                "excluded": bool(r.excluded),
                "inSharedLib": True,
                "timeAcquired": int(r.time_acquired or 0),
                "buyer": owners[-1] if owners else None,
                "playtimeMinutes": 0,
                "lastPlayed": 0,
            })

    # 游玩明细（组档案的 play_json 快照）：实时聚合成功时随组落库，
    # 兜底路径同样有游玩数据——此前恒空 dict，游玩动态只能空态
    play_saved = (
        fam_row.play_json
        if fam_row is not None and isinstance(fam_row.play_json, dict)
        else {}
    )

    return {
        "familyGroupid": groupid,
        "familyName": fam_row.family_name if fam_row else None,
        "members": members_out,
        "games": games_out,
        "memberPlay": play_saved,
        "sharedCount": len(games_out),
        "fromSnapshot": True,  # 前端可标注「快照数据（离线/拉取失败兜底）」
    }


def _start_library_refresh() -> None:
    """后台拉新（幂等：已在刷则跳过）。成功替换缓存；失败保留旧条目并把
    时间戳前移——否则过期条目会**每个请求**都触发一次注定失败的 HTTPS
    尝试（Cookie 失效时是常态），白白占用代理配额。"""
    global _LIBRARY_REFRESHING, _LIBRARY_REFRESH_TASK

    if _LIBRARY_REFRESHING:
        return
    _LIBRARY_REFRESHING = True

    async def _bg() -> None:
        global _LIBRARY_CACHE, _LIBRARY_REFRESHING
        try:
            _LIBRARY_CACHE = (datetime.utcnow(), await fetch_family_library())
        except Exception as e:  # noqa: BLE001 —— 后台刷新失败保留旧条目
            logger.info("[family] 后台刷新失败（保留现缓存）：%s", e)
            if _LIBRARY_CACHE:
                _LIBRARY_CACHE = (datetime.utcnow(), _LIBRARY_CACHE[1])
        finally:
            _LIBRARY_REFRESHING = False

    _LIBRARY_REFRESH_TASK = asyncio.create_task(_bg())


async def cached_family_library() -> dict:
    """家庭库快照（进程内 TTL 缓存，stale-while-revalidate）——前端 tabs 共用。

    取数顺序（修复「首开等 HTTPS」——实时聚合要逐成员调 GetOwnedGames，
    代理 HTTPS 秒级起步，首开不能干等）：
    1. 内存缓存 <TTL：直接回；
    2. 过期但有旧条目：**先回旧条目** + 后台拉新；
    3. 冷缓存（启动后首开）：**快照表优先**——立即回快照（fromSnapshot=true
       供前端标注）+ 后台拉新，拉新成功后下一轮请求即为实时数据；
    4. 无任何快照（全新安装首开）：现拉（诚实加载态），失败如实报错。

    ⚠️ 兜底/快照条目同样入缓存（TTL 更短，见 _SNAPSHOT_TTL_SECONDS）。只让成功
    路径写缓存的话，Cookie 失效期间每个请求都要先付一次完整的失败
    HTTPS 往返才回退快照（2.1~3.3s/次）——这也是「板块切换 1-2 秒」的主因。
    """
    global _LIBRARY_CACHE
    now = datetime.utcnow()
    if _LIBRARY_CACHE:
        # 快照命中的条目用更短的 TTL：快照是陈旧数据，而失败往往是瞬时的
        ttl = (
            _SNAPSHOT_TTL_SECONDS
            if _LIBRARY_CACHE[1].get("fromSnapshot")
            else _LIVE_TTL_SECONDS
        )
        age = (now - _LIBRARY_CACHE[0]).total_seconds()
        if age < ttl:
            return _LIBRARY_CACHE[1]
        _start_library_refresh()
        return _LIBRARY_CACHE[1]
    snap = await _library_from_snapshot()
    if snap is not None:
        _LIBRARY_CACHE = (now, snap)
        _start_library_refresh()
        return snap
    try:
        data = await fetch_family_library()
        _LIBRARY_CACHE = (now, data)
        return data
    except Exception as e:  # noqa: BLE001 —— 无快照可兜底，如实上抛
        logger.warning("[family] 实时聚合失败且无快照可兜底：%s", e)
        raise


def invalidate_library_cache() -> None:
    """清空家庭库缓存（强制刷新入口用）。"""
    global _LIBRARY_CACHE
    _LIBRARY_CACHE = None


# 实时聚合成活时的缓存时长
_LIVE_TTL_SECONDS = 300
# 快照兜底命中的缓存时长。刻意远短于实时数据：快照本身是陈旧的，而失败往往是
# 瞬时的（Cookie 刚被限流、代理刚断），60 秒足够吸收「切 tab / 返回页面」这类
# 密集重复请求，又不会让 Cookie 修好后继续吃陈旧数据太久。
_SNAPSHOT_TTL_SECONDS = 60

_LIBRARY_CACHE: tuple[datetime, dict] | None = None
_LIBRARY_REFRESHING: bool = False
_LIBRARY_REFRESH_TASK: asyncio.Task | None = None  # 防 create_task 被 GC 提前取消


# 未收录补爬节流：同一批缺口不因页面反复刷新而重复起任务
_WISHLIST_CRAWL_KICK_COOLDOWN_SECONDS = 1800
_wishlist_crawl_kick_at: datetime | None = None


async def _kick_uncrawled_games(appids: list[int]) -> None:
    """愿望单里从未爬过的游戏（games 表无行）触发后台补爬（读路径自愈）。

    「未收录」的成因：games/game_current_prices 无行 = 爬虫从未抓到该游戏——
    愿望单聚合只做本地 join 不补数据，用户看到的就是名称空（AppID 兜底）+
    价格「未收录」。这里把缺口 appid 送进爬取队列（kind="family_wishlist"），
    爬完名称/封面/CN 价自动补齐。

    任务互斥（占用抛错即放弃，下轮同步与全池刷新仍是兜底）；30 分钟冷却
    防页面刷新重复触发。
    """
    global _wishlist_crawl_kick_at
    if not appids:
        return
    now = datetime.utcnow()
    if _wishlist_crawl_kick_at and (
        now - _wishlist_crawl_kick_at
    ).total_seconds() < _WISHLIST_CRAWL_KICK_COOLDOWN_SECONDS:
        return
    _wishlist_crawl_kick_at = now
    try:
        from app.domains.crawl import service as crawl_service

        await crawl_service.start_job(
            scope="appids", appids=appids, kind="family_wishlist"
        )
        logger.info("[family] 愿望单 %d 款未收录，已触发补爬", len(appids))
    except Exception as e:  # noqa: BLE001 —— 任务占用/网络失败都静默
        logger.info("[family] 愿望单未收录补爬未触发（%d 款）：%s", len(appids), e)


async def family_wishlist() -> dict:
    """家庭成员愿望单聚合（wishlist_items × games 表本地 join，无需 Cookie）。

    数据源：family_groups 快照成员的 active 且未购（owned=False）愿望单行，
    按 appid 聚合想要人数；games 表补名称/封面/类型/发行日/CN 现价/折扣。
    未同步家庭组时回退全部 tracked_accounts（含主账户，行为诚实标注）。
    """
    from app.domains.wishlist.models import WishlistItem

    try:
        primary = await get_primary_steamid()
    except ValueError:
        primary = ""
    members: list[str] = []
    family_name: str | None = None
    if primary:
        async with get_session_factory()() as session:
            row = await session.get(FamilyGroup, primary)
        if row and row.members_json:
            members = [str(m.get("steamid")) for m in row.members_json if m.get("steamid")]
            family_name = row.family_name
    fallback = not members

    async with get_session_factory()() as session:
        stmt = (
            select(WishlistItem.steamid, WishlistItem.appid, WishlistItem.added_at)
            .where(WishlistItem.active.is_(True), WishlistItem.owned.is_(False))
        )
        if members:
            stmt = stmt.where(WishlistItem.steamid.in_(members))
        rows = (await session.execute(stmt)).all()

    # appid → (想要者, 各自添加时间)
    by_app: dict[int, dict] = {}
    for steamid, appid, added_at in rows:
        slot = by_app.setdefault(int(appid), {"members": [], "addedAt": None})
        slot["members"].append(str(steamid))
        if added_at and (slot["addedAt"] is None or added_at < added_at):
            slot["addedAt"] = added_at

    appids = sorted(by_app)
    meta = await _local_games_meta(appids)
    # games 表无行 = 爬虫从未抓到（名称/价格全空 → 前端「未收录」）：
    # 读路径自愈，把缺口送进爬取队列（带冷却，静默失败）
    await _kick_uncrawled_games([a for a in appids if a not in meta])

    items = []
    for appid in appids:
        m = meta.get(appid) or {}
        slot = by_app[appid]
        items.append({
            "appid": appid,
            "name": m.get("name"),
            "headerImage": m.get("header_image"),
            "genres": m.get("genres"),
            "releaseDate": m.get("release_date"),
            "cnPriceFen": m.get("cn_price_fen"),
            "discount": m.get("discount") or 0,
            "wantCount": len(slot["members"]),
            "members": slot["members"],
            "addedAt": slot["addedAt"].isoformat() if slot["addedAt"] else None,
        })
    items.sort(key=lambda x: -x["wantCount"])

    return {
        "fallback": fallback,  # True = 未同步家庭组，展示的是全部追踪账户
        "familyName": family_name,
        "memberIds": members,
        "items": items,
    }



async def save_member_regions(regions: dict[str, str]) -> dict:
    """保存成员地区选择（家庭页手动切换的持久化落点）。

    regions 形如 {"76561198...": "in"}；值为空串的键剔除（清除该成员覆盖）。
    """
    cleaned = {str(k): str(v) for k, v in (regions or {}).items() if str(v or "").strip()}
    await settings_service.set_value(KEY_MEMBER_REGIONS, cleaned)
    return cleaned


async def get_member_regions() -> dict:
    saved = await settings_service.get_value(KEY_MEMBER_REGIONS, None)
    return saved if isinstance(saved, dict) else {}
