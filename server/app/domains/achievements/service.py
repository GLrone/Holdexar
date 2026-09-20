"""achievements 域服务：奖杯（成就）与游玩时长的采集与汇总。

数据通道（名册需要 Cookie/Key，明细两条通道按凭证择优）：

**名册（哪些游戏进成就殿堂）**
- IPlayerService/GetOwnedGames/v1（Cookie JWT access_token，或 Key）
  → 本号已购全量（时长/最近游玩/名称），`source=owned`
- IFamilyGroupsService/GetSharedLibraryApps/v1（access_token）
  → 家庭共享池；`exclude_reason == 0`（枚举 0 = Included）且本号不在
    owner_steamids 里 = 本号可玩但未拥有。这类游戏 Steam 侧照样记录本号
    成就，若只按已购建册
    就整批漏掉——`source=shared`
- 免费周末 / 已从库中移除那类：官方清单与未文档化接口都只能按 appid 点名，
  **没有任何枚举通道**（GetOwnedGames 不含、社区游戏列表页需登录态），
  只能手动补录（`source=manual`）

**进度（解锁数/总数）**
- IPlayerService/GetAchievementsProgress/v1（POST，form `appids[N]` 批量 100）
  → 每游戏解锁数/总数；**Key 会 401，只能用 access_token**；接受未拥有的
  appid

**明细（定义 + 解锁态）**
- IPlayerService/GetGameAchievements/v1（Key 或 access_token）
  → 定义（apiname/名称/描述/彩图/灰图/全服占比），替代社区清单页 HTML
- ISteamUserStats/GetPlayerAchievements/v1（**必须 Key**）
  → 解锁态（apiname/achieved/unlocktime）；未拥有的游戏同样返回，且换
    steamid 即可读**其他账号**
- 无 Key 时降级到公开社区页（清单页 + 个人页），靠图标资产名桥接两页——
  保留这条通道是为了「不绑 Key 也能用」，不是主路径

行标识沿用图标资产名（`image_name`）：API 通道直接把 apiname 写进定义行，
解锁态按 apiname 精确对上，不再依赖显示名模糊匹配。
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from app.core.database import get_session_factory
from app.domains.account import service as account_service
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState
from app.domains.family.service import (
    _fetch_family_group,
    _fetch_shared_library,
    _steam_get,
    _strategy_proxy,
    extract_webapi_token,
)
from app.domains.games.models import Game
from app.domains.settings import service as settings_service

logger = logging.getLogger(__name__)

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
PROGRESS_URL = "https://api.steampowered.com/IPlayerService/GetAchievementsProgress/v1/"
GAME_ACHIEVEMENTS_URL = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/"
PLAYER_ACHIEVEMENTS_URL = (
    "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/"
)
GLOBAL_PAGE = "https://steamcommunity.com/stats/{appid}/achievements/"
PLAYER_PAGE = "https://steamcommunity.com/profiles/{steamid}/stats/{appid}/"
# 社区图 CDN：同资产在 shared.fastly 域下直连不可达（需代理），
# cloudflare 域与站点封面同源、直连即可取（字节一致）
ICON_TMPL = "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{image}.jpg"
HEADER_URL_TMPL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"

LANG = "schinese"
SYNC_KEY = "achievements.sync_state"
SYNC_STALE_MINUTES = 30
PROGRESS_BATCH = 100
API_RETRIES = 3  # Web API 网络类失败的同通道重试次数
# 社区页请求最小间隔（秒）：逐游戏两页、全量 380 款约 10 分钟
COMMUNITY_MIN_INTERVAL = 0.7
COMMUNITY_RETRIES = 3
# 清单页变更慢（成就定义数月不变）：超过此龄才重抓
DEFS_TTL_DAYS = 7
# 解锁态增量门禁：计数未变且近此小时数内已刷过明细 → 跳过该游戏抓取
DETAIL_SKIP_HOURS = 24
# 逐款进度兜底（token 失效时的后备通道）的重复点名窗口：这段时间内刷过的行跳过
PROGRESS_FALLBACK_SKIP_MINUTES = 60
# Web API 并发度：逐款明细/逐款进度每请求经代理 2~3 秒，串行全库要按小时计；
# 并发只作用于 api.steampowered.com（社区页通道仍由全局节流串行化）
API_CONCURRENCY = 5

TIMELINE_MONTHS = 24
RAREST_LIMIT = 12
RECENT_LIMIT = 12
NEAR_LIMIT = 12
NEAR_THRESHOLD = 75.0  # 「接近白金」完成度门槛（%）

# 稀有度分档（按全服解锁占比，对齐主机奖杯体系的常/少见/稀有/极稀/传说）
RARITY_BANDS = (("ultra", 1.0), ("very_rare", 5.0), ("rare", 20.0), ("uncommon", 50.0))
GRAY_SUFFIX = "_BW"

# 游戏行来源（achievement_games.source）
SOURCE_OWNED = "owned"    # 本号已购（含玩过的免费游戏）
SOURCE_SHARED = "shared"  # 库外：家庭共享池里本号未拥有、但已达成成就
SOURCE_MANUAL = "manual"  # 手动补录（免费周末 / 已移除那类无法枚举的库外游戏）
EXTERNAL_SOURCES = (SOURCE_SHARED, SOURCE_MANUAL)


class SyncError(Exception):
    """同步通道失败（凭证缺失/页面被拒/网络），区别于「无成就/库空」。"""


# ─── 凭证（仅用于已购与批量进度；成就明细走公开页，无需任何 Key）──

async def _primary_steam_id() -> str:
    steamid = await account_service.get_primary_steam_id()
    if not steamid:
        steamid = (await settings_service.get_value("account.steam_id", "")) or ""
    return steamid


async def resolve_credentials(target: str | None = None) -> tuple[str, list[tuple[str, str]]]:
    """目标账号 SteamID64 + 凭证序列 [(kind, value)]（Key 优先，Cookie JWT 兜底）。

    `target` 为空取主账号；指定其他账号时**复用本地这一份凭证**——任一
    账号的 access_token 都能读任意 steamid 的进度（GetAchievementsProgress
    的 steamid 与凭证身份解耦），Web API Key 本就是全局的。多账号查看因此
    不需要为每个账号维护 Cookie，也不会串号。
    """
    steamid = (target or "").strip() or await _primary_steam_id()
    if not steamid:
        return "", []
    creds: list[tuple[str, str]] = []
    api_key = (await settings_service.get_value("account.steam_api_key", "")) or ""
    if api_key:
        creds.append(("key", api_key))
    try:
        cookies = await account_service.get_primary_cookies()
    except Exception:  # noqa: BLE001 —— Cookie 读取失败降级 Key 单通道
        cookies = ""
    token = extract_webapi_token(cookies) if cookies else None
    if token:
        creds.append(("token", token))
    return steamid, creds


# ─── Steam Web API（已购 / 批量进度）──────────────────────────

async def _post_form(url: str, data: dict) -> httpx.Response:
    """POST 表单（代理优先 + SSL 证书降级，与 family._steam_get 同规则）。"""
    proxy = await _strategy_proxy()
    try:
        async with httpx.AsyncClient(timeout=30, proxy=proxy) as client:
            resp = await client.post(url, data=data)
    except httpx.HTTPError as e:
        if not _is_ssl_error(e):
            raise
        logger.info("Steam API 证书校验失败（经代理场景），降级跳过校验重试")
        async with httpx.AsyncClient(timeout=30, proxy=proxy, verify=False) as client:
            resp = await client.post(url, data=data)
    resp.raise_for_status()
    return resp


def _is_ssl_error(exc: Exception) -> bool:
    clues = ("CERTIFICATE_VERIFY_FAILED", "self-signed", "certificate verify failed",
             "ssl", "SSL", "unable to get local issuer")
    text = f"{type(exc).__name__}: {exc}"
    return any(c in text for c in clues)


async def _api_call(
    url: str, params: dict, creds: list[tuple[str, str]], *, form: dict | None = None
) -> dict:
    """按凭证优先序逐个尝试；401/403 换下一通道，全败抛 SyncError。

    `form` 非空时走 POST 表单（批量进度接口只收 form 参数），否则 GET query。
    网络类失败（超时/连接）同通道重试两次——出网经代理时单次抖动很常见。
    """
    last_exc: Exception | None = None
    for kind, value in creds:
        key_name = "key" if kind == "key" else "access_token"
        for attempt in range(API_RETRIES):
            try:
                if form is not None:
                    resp = await _post_form(url, {**form, key_name: value})
                else:
                    resp = await _steam_get(url, {**params, key_name: value})
                return resp.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt + 1 < API_RETRIES:
                    await asyncio.sleep(2 + attempt * 2)
                    continue
                raise SyncError(f"Steam API 网络失败：{type(e).__name__}: {e}") from e
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    last_exc = e
                    break  # 换下一通道
                raise SyncError(f"Steam API 返回 {e.response.status_code}") from e
    raise SyncError(
        "Steam API 鉴权被拒（401/403）：请在「我」页重新绑定 Steam Cookie 或配置 Web API Key"
    ) from last_exc


async def fetch_owned(steamid: str, creds: list[tuple[str, str]]) -> list[dict]:
    """已购全量（含 F2P）：[{appid, name, playtime_forever, rtime_last_played}]。"""
    data = await _api_call(
        OWNED_URL,
        {"steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1},
        creds,
    )
    return (data.get("response") or {}).get("games") or []


async def fetch_progress(steamid: str, creds: list[tuple[str, str]], appids: list[int]) -> dict[int, dict]:
    """批量成就进度 {appid: {total, unlocked}}（form `appids[N]`，100/批）。"""
    out: dict[int, dict] = {}
    for i in range(0, len(appids), PROGRESS_BATCH):
        chunk = appids[i : i + PROGRESS_BATCH]
        form: dict = {f"appids[{j}]": str(a) for j, a in enumerate(chunk)}
        form["steamid"] = steamid
        data = await _api_call(PROGRESS_URL, {}, creds, form=form)
        items = (data.get("response") or {}).get("achievement_progress") or []
        for it in items:
            try:
                out[int(it["appid"])] = {
                    "total": int(it.get("total", 0)),
                    "unlocked": int(it.get("unlocked", 0)),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return out


async def fetch_progress_keyed(
    steamid: str, appids: list[int], api_key: str, skip: set[int] | None = None
) -> dict[int, dict]:
    """逐款进度兜底（GetPlayerAchievements，Key 通道）。

    批量进度接口只认 access_token；它 401 时用这条把进度补出来：一款一次请求，
    成本随名册线性增长，按 `API_CONCURRENCY`
    路并发摊薄（449 款约 20 分钟 → 4~5 分钟）。只在 token 通道失效时启用，并按
    25 款一档推进同步快照，前端看得见进度而不是卡在 0。

    `skip` = 近一小时内已刷过进度的 appid（由调用方在**本轮 `_apply_owned` 之前**
    取好：那一步会给所有已购行盖新的 `updated_at`，之后再取就全都算「刚刷过」）。
    跳过不影响正确性——`_apply_progress` 不会覆盖未点名的行。
    """
    skipped = skip or set()
    todo = [a for a in appids if a not in skipped]
    if len(todo) != len(appids):
        logger.info(
            "[achievements] 逐款进度：%d/%d 款近 %d 分钟内已刷过，跳过",
            len(appids) - len(todo), len(appids), PROGRESS_FALLBACK_SKIP_MINUTES,
        )
    await _write_snapshot(stage="progress", total=len(todo), done=0)

    out: dict[int, dict] = {}
    done = 0
    sem = asyncio.Semaphore(API_CONCURRENCY)

    async def _one(appid: int) -> None:
        nonlocal done
        async with sem:
            states = await fetch_states_api(steamid, appid, api_key)
        if states:
            out[appid] = {
                "total": len(states),
                "unlocked": sum(1 for s in states.values() if s["achieved"]),
            }
        done += 1
        if done % 25 == 0 or done == len(todo):
            await _write_snapshot(done=done)

    await asyncio.gather(*(_one(a) for a in todo))
    return out


# ─── Web API 明细通道 + 库外名册（官方接口，不做 HTML 解析）────────

def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _api_try(url: str, params: dict, creds: list[tuple[str, str]]) -> dict | None:
    """单个 Web API 方法：任一通道失败即返回 None（由调用方决定是否降级）。

    与 `_api_call` 的分工：名册与批量进度是硬依赖，失败必须显式报错；明细有两条
    通道（API / 公开社区页），API 不通应当安静降级，不能把整轮同步判死。
    """
    for kind, value in creds:
        key_name = "key" if kind == "key" else "access_token"
        try:
            resp = await _steam_get(url, {**params, key_name: value})
            return resp.json()
        except Exception:  # noqa: BLE001 —— 失败原因由最终降级结果体现
            continue
    return None


def _api_key_of(creds: list[tuple[str, str]]) -> str:
    return next((v for k, v in creds if k == "key"), "")


async def _local_token() -> str:
    """本地已绑账号的 access_token（共享池 / 定义清单 / 批量进度用它，免 Key）。"""
    try:
        cookies = await account_service.get_primary_cookies()
    except Exception:  # noqa: BLE001 —— 读不到就当没有
        return ""
    return (extract_webapi_token(cookies) if cookies else "") or ""


async def fetch_defs_api(appid: int, creds: list[tuple[str, str]]) -> list[dict]:
    """成就定义（IPlayerService/GetGameAchievements，Key 或 access_token 均可）。

    返回与 `parse_global_page` 同构的行，外加 `apiname`——解锁态通道只给
    apiname，靠它精确对上定义行，不必再走图标名/显示名的模糊桥接。
    """
    data = await _api_try(GAME_ACHIEVEMENTS_URL, {"appid": appid, "language": LANG}, creds)
    items = ((data or {}).get("response") or {}).get("achievements") or []
    rows: list[dict] = []
    for a in items:
        icon = str(a.get("icon") or "").strip()
        if not icon:
            continue
        gray = str(a.get("icon_gray") or "").strip() or icon
        rows.append({
            "image_name": base_name(icon),
            "apiname": str(a.get("internal_name") or ""),
            "name": str(a.get("localized_name") or "").strip(),
            "description": str(a.get("localized_desc") or "").strip(),
            "icon_url": ICON_TMPL.format(appid=appid, image=base_name(icon)),
            "icon_gray_url": ICON_TMPL.format(appid=appid, image=base_name(gray)),
            "global_percent": _to_float(a.get("player_percent_unlocked")),
        })
    # 展示序对齐社区清单页：全服占比降序（同图资产名再按序编号）
    rows.sort(key=lambda r: -(r["global_percent"] if r["global_percent"] is not None else -1.0))
    for i, r in enumerate(rows):
        r["order"] = i
    _assign_keys(rows, "image_name")
    return rows


async def fetch_states_api(steamid: str, appid: int, api_key: str) -> dict[str, dict] | None:
    """玩家解锁态（ISteamUserStats/GetPlayerAchievements，**必须 Web API Key**）。

    返回 `{apiname: {achieved, unlock_time}}`；不可读（未配 Key / 资料游戏详情
    不公开 / 该游戏无成就系统）返回 None，由调用方降级到公开个人页。
    未拥有的游戏同样返回。
    """
    if not api_key:
        return None
    data = await _api_try(
        PLAYER_ACHIEVEMENTS_URL,
        {"steamid": steamid, "appid": appid, "l": LANG},
        [("key", api_key)],
    )
    rows = ((data or {}).get("playerstats") or {}).get("achievements")
    if not rows:
        return None
    out: dict[str, dict] = {}
    for r in rows:
        apiname = str(r.get("apiname") or "")
        if not apiname:
            continue
        out[apiname] = {
            "achieved": bool(int(r.get("achieved") or 0)),
            "unlock_time": int(r.get("unlocktime") or 0),
        }
    return out or None


def merge_states_api(defs_rows, states: dict[str, dict]) -> tuple[dict[str, dict], int]:
    """apiname 直连：定义行按 `apiname → image_name` 落解锁态。

    定义行是爬虫通道补进来的（无 apiname）时匹配不上——那部分交由调用方
    保持既有解锁态，不在这里猜。
    """
    by_api = {d.apiname: d.image_name for d in defs_rows if d.apiname}
    merged: dict[str, dict] = {}
    unmatched = 0
    for apiname, st in states.items():
        key = by_api.get(apiname)
        if key is None:
            unmatched += 1
            continue
        merged[key] = {**st, "gray_icon": ""}
    return merged, unmatched


async def shared_pool_candidates(steamid: str) -> tuple[list[int], dict[int, str]]:
    """家庭共享池里「本号未拥有」的候选 appid（附名称兜底）。

    口径：`exclude_reason == 0`（Steam 枚举 0 = Included，即真可共享）且本号不在
    `owner_steamids` 里。凭证失效或取不到（未绑 Cookie / 不在家庭组 / 网络失败）
    时回落到本地家庭库快照（家庭页每次聚合成功都会落库），仍取不到才返回空表——
    库外名册是增量能力，不该拖垮已购名册。

    免费周末 / 已从库中移除那类**无法枚举**：官方接口全按 appid 点名，社区
    游戏列表页要登录态，故只能走手动补录。
    """
    rows: list[tuple[int, list[str], str | None]] | None = None
    token = await _local_token()
    if token:
        try:
            group = await _fetch_family_group(token)
            if group.get("joined") and group.get("family_groupid"):
                members = {str(m.get("steamid") or "") for m in (group.get("members") or [])}
                if steamid in members:
                    apps = await _fetch_shared_library(token, str(group["family_groupid"]))
                    rows = [
                        (int(a["appid"]), [str(s) for s in (a.get("owners") or [])], a.get("name"))
                        for a in apps
                        if not a.get("excluded")
                    ]
        except Exception as e:  # noqa: BLE001 —— 实时不可得时走快照
            logger.info("[achievements] 家庭共享池实时获取失败（转本地快照）：%s", e)

    if rows is None:
        rows = await _shared_pool_from_snapshot(steamid)
    if rows is None:
        logger.info("[achievements] 家庭共享池不可得（实时与快照都无），跳过库外名册")
        return [], {}

    out: list[int] = []
    names: dict[int, str] = {}
    for appid, owners, name in rows:
        if steamid in owners:  # 自己也有 = 已购行，不算库外
            continue
        out.append(appid)
        if name:
            names[appid] = str(name)
    return out, names


async def _shared_pool_from_snapshot(
    steamid: str,
) -> list[tuple[int, list[str], str | None]] | None:
    """本地家庭库快照 → 共享池候选（实时通道不可用时的兜底）。

    快照侧只取 `excluded = False` 的行（共享清单口径：已排除的不参与共享），
    并校验目标账号确实在该组成员里。快照可能比实时旧，但「库外行只收有解锁的」
    这道闸让旧快照最多漏几款，不会塞进错的游戏。
    """
    try:
        from app.domains.family.models import FamilyGroup, FamilyLibrarySnapshot

        async with get_session_factory()() as session:
            group = (
                await session.execute(
                    select(FamilyGroup).where(FamilyGroup.steamid == steamid)
                )
            ).scalar_one_or_none()
            if group is None or not group.family_groupid:
                return None
            members = {str(m.get("steamid") or "") for m in (group.members_json or [])}
            if steamid not in members:
                return None
            snaps = (
                (
                    await session.execute(
                        select(FamilyLibrarySnapshot).where(
                            FamilyLibrarySnapshot.family_groupid == group.family_groupid,
                            FamilyLibrarySnapshot.excluded.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [
            (int(s.appid), [str(x) for x in (s.owners_json or [])], s.name) for s in snaps
        ]
    except Exception as e:  # noqa: BLE001 —— 快照不可读时按「无候选」处理
        logger.info("[achievements] 家庭库快照读取失败：%s", e)
        return None


# ─── 公开社区页（免 Key 通道）─────────────────────────────────

_last_community_at = 0.0
# 节流的「读时刻→改时刻」不是原子的：并发 worker 会在彼此 sleep 间同时通过
# 检查。门闩只包住间隔判定与时刻登记，真正 HTTP 在闩外，保持 ≥0.7s 错峰。
_community_gate = asyncio.Lock()


async def _community_get(url: str, params: dict) -> str:
    """公开页抓取：全局节流（最小间隔 + 抖动）+ 429/5xx 退避重试。"""
    global _last_community_at
    proxy = await _strategy_proxy()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125",
        "Referer": "https://steamcommunity.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    last_exc: Exception | None = None
    for attempt in range(COMMUNITY_RETRIES):
        async with _community_gate:
            gap = time.monotonic() - _last_community_at
            wait = COMMUNITY_MIN_INTERVAL - gap
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.2))
            _last_community_at = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=25, proxy=proxy, verify=False, follow_redirects=True, headers=headers
            ) as client:
                resp = await client.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return ""  # 该游戏无成就页
            last_exc = e
            await asyncio.sleep(3 + attempt * 3)
        except httpx.HTTPError as e:
            last_exc = e
            await asyncio.sleep(2 + attempt * 2)
    raise SyncError(f"社区页抓取失败：{type(last_exc).__name__}: {last_exc}") from last_exc


def base_name(image_file: str) -> str:
    """图标资产名 → 行标识（去扩展名与 `_BW` 灰度后缀）。"""
    name = image_file.rsplit(".", 1)[0]
    if name.endswith(GRAY_SUFFIX):
        name = name[: -len(GRAY_SUFFIX)]
    return name


def base_from_icon(url: str | None) -> str:
    """图标 URL → 图标资产名（清单行桥接用）。"""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _clean(text: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _assign_keys(rows: list[dict], base_field: str) -> None:
    """给行分配稳定键：图标名唯一时即图标名，同图复用（部分游戏数十成就共用
    一张图）时追加同图内序号 —— `BONGO#0/#1/...`。序号按各自页序计算，
    两页同图组的相对序一致（清单页占比降序 ≈ 清单序），不一致时由名称兜底。"""
    seen: dict[str, int] = {}
    for r in rows:
        base = r[base_field]
        n = seen.get(base, 0)
        seen[base] = n + 1
        r["key"] = base if n == 0 else f"{base}#{n}"


def parse_global_page(html: str, appid: int) -> list[dict]:
    """清单页 → [{image_name, key, name, description, icon_url, global_percent, order}]。"""
    out: list[dict] = []
    for chunk in html.split('class="achieveRow')[1:]:
        img = re.search(r"community_assets/images/apps/\d+/([^\"]+)\"", chunk)
        name = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        desc = re.search(r"<h5[^>]*>(.*?)</h5>", chunk, re.S)
        pct = re.search(r'class="achievePercent">([\d.]+)%<', chunk)
        if not img:
            continue
        image_name = base_name(img.group(1))
        out.append({
            "image_name": image_name,
            "name": _clean(name.group(1)) if name else "",
            "description": _clean(desc.group(1)) if desc else "",
            "icon_url": ICON_TMPL.format(appid=appid, image=image_name),
            "global_percent": float(pct.group(1)) if pct else None,
            "order": len(out),
        })
    _assign_keys(out, "image_name")
    return out


_ZH_TIME = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(上午|下午)?\s*(\d{1,2}):(\d{2})"
)
_EN_TIME = re.compile(r"(\d{1,2})\s+([A-Za-z]{3}),?\s*(\d{4})\s*@\s*(\d{1,2}):(\d{2})\s*(am|pm)", re.I)
_EN_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
)}


def parse_unlock_time(text: str) -> int:
    """页面时间文本 → epoch 秒（分钟级；页面按访客时区渲染，取本机时区近似）。"""
    m = _ZH_TIME.search(text or "")
    if m:
        y, mo, d, half, hh, mm = m.groups()
        hour = int(hh) % 12
        if half == "下午":
            hour += 12
        try:
            return int(datetime(int(y), int(mo), int(d), hour, int(mm)).timestamp())
        except ValueError:
            return 0
    m = _EN_TIME.search(text or "")
    if m:
        d, mon, y, hh, mm, half = m.groups()
        hour = int(hh) % 12
        if half.lower() == "pm":
            hour += 12
        try:
            return int(datetime(int(y), _EN_MONTHS[mon.lower()[:3]], int(d), hour, int(mm)).timestamp())
        except (ValueError, KeyError):
            return 0
    return 0


def parse_player_page(html: str, appid: int) -> list[dict]:
    """个人成就页 → [{base, name, achieved, unlock_time, gray_icon}]（按清单页序）。"""
    out: list[dict] = []
    for chunk in html.split('class="achieveRow')[1:]:
        img = re.search(r"community_assets/images/apps/\d+/([^\"]+)\"", chunk)
        name = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        time_m = re.search(r'achieveUnlockTime">(.*?)</div>', chunk, re.S)
        if not img and not name:
            continue
        raw_file = img.group(1) if img else ""
        out.append({
            "base": base_name(raw_file) if raw_file else "",
            "name": _clean(name.group(1)) if name else "",
            "achieved": bool(time_m),
            "unlock_time": parse_unlock_time(re.sub(r"\s+", " ", time_m.group(1))) if time_m else 0,
            "gray_icon": ICON_TMPL.format(appid=appid, image=base_name(raw_file))
            if raw_file.endswith(GRAY_SUFFIX + ".jpg")
            else "",
        })
    _assign_keys(out, "base")
    return out


def merge_states(defs: list[dict], rows: list[dict]) -> tuple[dict[str, dict], int]:
    """把解锁态并进清单（返回 {key: row} 与未匹配行数）。

    匹配优先级：复合键（图标名+同图序号）→ 唯一图标名 → 显示名 → 同图剩余行。
    """
    by_key = {d["key"]: d for d in defs}
    base_counts: dict[str, int] = {}
    for d in defs:
        base_counts[d["image_name"]] = base_counts.get(d["image_name"], 0) + 1
    unique_base = {d["image_name"]: d for d in defs if base_counts[d["image_name"]] == 1}
    by_name: dict[str, list[str]] = {}
    for d in defs:
        by_name.setdefault(d["name"], []).append(d["key"])
    by_base_pool: dict[str, list[str]] = {}
    for d in defs:
        by_base_pool.setdefault(d["image_name"], []).append(d["key"])

    used: set[str] = set()
    merged: dict[str, dict] = {}
    unmatched = 0
    for row in rows:
        key: str | None = None
        if row["key"] in by_key and row["key"] not in used:
            key = row["key"]
        if key is None and row["base"] in unique_base:
            cand = unique_base[row["base"]]["key"]
            key = cand if cand not in used else None
        if key is None and row["name"]:
            for cand in by_name.get(row["name"], []):
                if cand not in used:
                    key = cand
                    break
        if key is None and row["base"]:
            for cand in by_base_pool.get(row["base"], []):
                if cand not in used:
                    key = cand
                    break
        if key is None:
            unmatched += 1
            continue
        used.add(key)
        merged[key] = row
    return merged, unmatched


def rarity_tier(percent: float | None) -> str:
    """全服解锁占比 → 稀有度档（ultra/very_rare/rare/uncommon/common）。"""
    if percent is None:
        return "unknown"
    for tier, upper in RARITY_BANDS:
        if percent < upper:
            return tier
    return "common"


# ─── 同步编排（进度快照存 settings KV，跨重启可读）─────────────────

_sync_task: asyncio.Task | None = None


def _empty_snapshot() -> dict:
    return {
        "running": False,
        "ok": None,
        "stage": "",
        "done": 0,
        "total": 0,
        "current": "",
        "error": "",
        "startedAt": "",
        "syncedAt": "",
        # 本轮同步的目标账号：多账号下前端据此判断「这份状态是不是当前所选账号的」
        "steamid": "",
    }


async def _read_snapshot() -> dict:
    snap = await settings_service.get_value(SYNC_KEY, None)
    return {**_empty_snapshot(), **(snap or {})}


# 快照是「读→改→写」三步且各有 await，并发 worker 直写会互相覆盖丢更新；
# 所有快照写入过这一把闩（读侧无谓，get 只在写后发生）。
_snap_lock = asyncio.Lock()


async def _write_snapshot(**fields) -> dict:
    async with _snap_lock:
        snap = await _read_snapshot()
        snap.update(fields)
        await settings_service.set_value(SYNC_KEY, snap)
        return snap


async def _finish_snapshot(ok: bool, error: str = "") -> dict:
    return await _write_snapshot(
        running=False, ok=ok, error=error, stage="", current="",
        syncedAt=datetime.now(timezone.utc).isoformat(),
    )


async def sync_status() -> dict:
    """最近一次同步状态（running / 结果 / 错误 / 实时进度）。"""
    snap = await _read_snapshot()
    return {**snap, "running": bool(snap.get("running"))}


async def start_sync(target: str | None = None) -> dict:
    """发起后台同步（`target` 指定账号，空则主账号）；进行中（且非死锁）拒绝。

    同步任务是进程级单例：多账号共用一个队列，同一时刻只跑一个账号，
    快照里记 `steamid` 说明本轮是谁——避免「切了账号却看到别人的进度」。
    """
    global _sync_task
    if _sync_task and not _sync_task.done():
        raise ValueError("成就同步已在进行中")
    snap = await _read_snapshot()
    if _sync_task and not _sync_task.done():  # await 之后复核，消灭并发窗口
        raise ValueError("成就同步已在进行中")
    if snap.get("running"):
        started = snap.get("startedAt") or ""
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(started)
        except ValueError:
            age = timedelta(0)
        if age <= timedelta(minutes=SYNC_STALE_MINUTES):
            raise ValueError("成就同步已在进行中")
        logger.warning("[achievements] 同步快照 running 卡死超过 %d 分钟，放行重试", SYNC_STALE_MINUTES)
    steamid = (target or "").strip() or await _primary_steam_id()
    await _write_snapshot(running=True, ok=None, error="", stage="library", done=0, total=0,
                          current="", steamid=steamid,
                          startedAt=datetime.now(timezone.utc).isoformat())
    _sync_task = asyncio.create_task(_run_sync(steamid))
    return await sync_status()


async def _run_sync(steamid: str) -> None:
    try:
        await _sync_all(steamid)
    except SyncError as e:
        logger.warning("[achievements] 同步失败：%s", e)
        await _finish_snapshot(ok=False, error=str(e))
    except Exception:  # noqa: BLE001 —— 同步失败只留日志与快照，不影响服务
        logger.exception("[achievements] 同步异常")
        await _finish_snapshot(ok=False, error="同步异常，详见服务日志")
    finally:
        _sync_task = None


async def _sync_all(target: str = "") -> None:
    """一轮全量同步（`target` 为空取主账号）。

    三段：已购名册 → 进度（批量接口，凭证失效时逐款回退）→ 明细（逐游戏）。
    """
    steamid, creds = await resolve_credentials(target)
    if not steamid:
        raise SyncError("未绑定 Steam 账号：请先在「我」页绑定 Steam Cookie")
    if not creds:
        raise SyncError("无可用凭证：请重新绑定 Steam Cookie 或配置 Web API Key")

    # 上一轮计数快照：批量进度 3 秒即可判定哪些游戏有变化，未变的跳过明细抓取。
    # 同时取「近一小时内刷过进度」的 appid —— 必须在 `_apply_owned` 之前取，
    # 那一步会给所有已购行盖新时间戳，之后再取就全都算刚刷过（逐款兜底会空转）。
    fresh_cut = datetime.utcnow() - timedelta(minutes=PROGRESS_FALLBACK_SKIP_MINUTES)
    async with get_session_factory()() as session:
        rows_prev = (
            (
                await session.execute(
                    select(AchievementGame).where(AchievementGame.steamid == steamid)
                )
            )
            .scalars()
            .all()
        )
    prev = {r.appid: (r.total_achievements, r.unlocked, r.state_synced_at) for r in rows_prev}
    recent_progress = {
        r.appid for r in rows_prev if r.updated_at is not None and r.updated_at >= fresh_cut
    }

    owned = await fetch_owned(steamid, creds)
    owned_ids = {int(g["appid"]) for g in owned if str(g.get("appid") or "").isdigit()}
    await _apply_owned(steamid, owned)
    await _write_snapshot(stage="progress", total=len(owned), done=0)

    # 库外名册：家庭共享池里本号未拥有、但本号在上面达成过成就的游戏。
    # 免费周末 / 已从库中移除那类没有任何枚举接口（官方清单全按 appid 点名、
    # 社区游戏列表页要登录态），只能手动补录 —— 见 shared_pool_candidates。
    external_ids, external_names = await shared_pool_candidates(steamid)
    external_ids = [a for a in external_ids if a not in owned_ids]
    if external_ids:
        logger.info("[achievements] 家庭共享池候选 %d 款（本号未拥有）", len(external_ids))

    candidates = sorted(owned_ids) + external_ids
    progress: dict[int, dict] = {}
    if candidates:
        try:
            progress = await fetch_progress(steamid, creds, candidates)
        except SyncError as e:
            # 批量进度接口只认 access_token（Key 401），而 token 是十分钟级的短命
            # JWT：Cookie 一旦失效，没有这条兜底整轮同步都会判死——连已购名册都
            # 进不来。有 Key 就逐款走 GetPlayerAchievements 顶上（慢但通）。
            api_key = _api_key_of(creds)
            if not api_key:
                raise
            logger.warning(
                "[achievements] 批量进度通道不可用（%s），改用单游戏接口逐款回退（%d 款）",
                e, len(candidates),
            )
            progress = await fetch_progress_keyed(
                steamid, candidates, api_key, skip=recent_progress
            )
    await _apply_progress(steamid, progress, owned_ids, external_names)
    await _prune_external(steamid, set(external_ids))

    async with get_session_factory()() as session:
        targets = (
            (
                await session.execute(
                    select(AchievementGame)
                    .where(AchievementGame.steamid == steamid, AchievementGame.total_achievements > 0)
                    .order_by(AchievementGame.playtime_min.desc())
                )
            )
            .scalars()
            .all()
        )
    now = datetime.utcnow()
    pending: list[AchievementGame] = []
    for row in targets:
        p = prev.get(row.appid)
        if (
            p is not None
            and p[0] == row.total_achievements
            and p[1] == row.unlocked
            and p[2] is not None
            and now - p[2] < timedelta(hours=DETAIL_SKIP_HOURS)
        ):
            continue  # 计数未变且近期已刷明细：无新解锁可采
        pending.append(row)
    if len(pending) != len(targets):
        logger.info(
            "[achievements] 增量同步：%d/%d 款计数未变，跳过明细抓取", len(targets) - len(pending), len(targets)
        )

    await _write_snapshot(stage="details", total=len(pending), done=0)

    # 有界并发抓明细：每款 1~2 个请求、经代理单请求 2~3 秒，串行 406 款约一小时；
    # 5 路并发把首輪全量压回一刻钟内。SyncError 只记日志不中断整轮；其余异常等
    # 全部 worker 落定后再抛——避免 _finish_snapshot 与在途写库竞争。
    done = 0
    sem = asyncio.Semaphore(API_CONCURRENCY)
    failures: list[BaseException] = []

    async def _one(row: AchievementGame) -> None:
        nonlocal done
        async with sem:
            try:
                await _sync_game(steamid, row, creds)
            except SyncError as e:
                logger.warning("[achievements] appid=%s 明细失败：%s", row.appid, e)
            except BaseException as e:  # noqa: BLE001 —— 收集后在 gather 外重抛
                failures.append(e)
                return
        done += 1
        await _write_snapshot(done=done, current=row.name or str(row.appid))

    await asyncio.gather(*(_one(r) for r in pending))
    if failures:
        raise failures[0]
    await _finish_snapshot(ok=True)


async def _apply_owned(steamid: str, owned: list[dict]) -> None:
    async with get_session_factory()() as session:
        rows = {
            r.appid: r
            for r in (
                await session.execute(select(AchievementGame).where(AchievementGame.steamid == steamid))
            ).scalars()
        }
        now = datetime.utcnow()
        for g in owned:
            try:
                appid = int(g["appid"])
            except (KeyError, TypeError, ValueError):
                continue
            row = rows.get(appid)
            if row is None:
                row = AchievementGame(steamid=steamid, appid=appid)
                session.add(row)
            row.name = str(g.get("name") or row.name or "")
            row.playtime_min = int(g.get("playtime_forever") or 0)
            row.last_played = int(g.get("rtime_last_played") or 0)
            # 已购优先：同一 appid 既在库里又在共享池时算库内
            row.source = SOURCE_OWNED
            row.updated_at = now
        await session.commit()


async def _apply_progress(
    steamid: str,
    progress: dict[int, dict],
    owned_ids: set[int],
    external_names: dict[int, str] | None = None,
) -> None:
    """批量进度落库；库外 appid 只在**确有解锁**时才建行。

    「未拥有的游戏 + 0 解锁」只是共享池里的库存，不该进成就殿堂（会把它淹掉）；
    而「已购但没成就」是既有口径（`filter=all` 能看到），保持原样。
    """
    names = external_names or {}
    async with get_session_factory()() as session:
        rows = {
            r.appid: r
            for r in (
                await session.execute(select(AchievementGame).where(AchievementGame.steamid == steamid))
            ).scalars()
        }
        now = datetime.utcnow()
        for appid, p in progress.items():
            owned = appid in owned_ids
            row = rows.get(appid)
            if row is None and not owned and int(p.get("unlocked") or 0) <= 0:
                continue
            if row is None:
                row = AchievementGame(steamid=steamid, appid=appid)
                session.add(row)
            row.total_achievements = p["total"]
            row.unlocked = p["unlocked"]
            row.platinum = p["total"] > 0 and p["unlocked"] >= p["total"]
            row.source = SOURCE_OWNED if owned else SOURCE_SHARED
            if not owned and not row.name and names.get(appid):
                row.name = names[appid]
            row.updated_at = now
        await session.commit()


async def _prune_external(steamid: str, candidates: set[int]) -> int:
    """清掉不再成立的库外行，连带其解锁态。

    「不再成立」= 不在本轮共享池候选里（已不在共享/已自己买下），或已被清空到
    0 解锁。**只动非已购行**：已购行一律不删——退款或手动从库中移除的游戏，
    其既有解锁账与明细是这个应用里唯一留存的副本，不做静默清理。
    """
    removed = 0
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(AchievementGame).where(
                        AchievementGame.steamid == steamid,
                        AchievementGame.source != SOURCE_OWNED,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.appid in candidates and row.unlocked > 0:
                continue
            await session.execute(
                delete(AchievementState).where(
                    AchievementState.steamid == steamid, AchievementState.appid == row.appid
                )
            )
            await session.delete(row)
            removed += 1
        if removed:
            await session.commit()
    if removed:
        logger.info("[achievements] 清理失效库外行 %d 款", removed)
    return removed


async def _sync_game(steamid: str, row: AchievementGame, creds: list[tuple[str, str]]) -> None:
    """单游戏明细：定义（Web API 优先、社区清单页兜底）+ 解锁态（有 Key 走 API）。

    两条通道的分工：API 给 apiname，定义↔解锁态精确对上；无 Key 时降级到公开
    社区页，沿用「图标资产名 + 显示名」的模糊桥接（那条通道不给 apiname）。
    """
    appid = row.appid
    api_key = _api_key_of(creds)
    async with get_session_factory()() as session:
        defs_count = (
            await session.execute(
                select(func.count()).select_from(AchievementDef).where(AchievementDef.appid == appid)
            )
        ).scalar() or 0
        defs_stale = (
            row.schema_synced_at is None
            or datetime.utcnow() - row.schema_synced_at > timedelta(days=DEFS_TTL_DAYS)
        )
        need_defs = defs_count != row.total_achievements or defs_stale
        if not need_defs and creds:
            # 老库的定义行没有 apiname（当年只有社区页通道）：补刷一次，之后
            # 解锁态即可走 apiname 精确匹配；有凭证才补，免得无凭证时空转
            no_apiname = (
                await session.execute(
                    select(func.count())
                    .select_from(AchievementDef)
                    .where(
                        AchievementDef.appid == appid,
                        (AchievementDef.apiname.is_(None)) | (AchievementDef.apiname == ""),
                    )
                )
            ).scalar() or 0
            need_defs = no_apiname > 0

    if need_defs:
        defs = await fetch_defs_api(appid, creds)
        if not defs:
            html = await _community_get(GLOBAL_PAGE.format(appid=appid), {"l": LANG})
            defs = parse_global_page(html, appid) if html else []
        if defs:
            await _apply_defs(appid, defs)
            row.total_achievements = len(defs)
            row.schema_synced_at = datetime.utcnow()
            async with get_session_factory()() as session:
                await session.merge(row)
                await session.commit()

    # 解锁态：有 Key 走官方接口（未拥有 / 他人账号同样可读），否则公开个人页
    states = await fetch_states_api(steamid, appid, api_key)
    page_rows: list[dict] = []
    if states is None and not api_key:
        html = await _community_get(PLAYER_PAGE.format(steamid=steamid, appid=appid), {"l": LANG})
        page_rows = parse_player_page(html, appid) if html else []

    async with get_session_factory()() as session:
        defs_rows = (
            (
                await session.execute(
                    select(AchievementDef).where(AchievementDef.appid == appid).order_by(AchievementDef.display_order)
                )
            )
            .scalars()
            .all()
        )
        merged: dict[str, dict] | None = None
        unmatched = 0
        if states is not None:
            merged, unmatched = merge_states_api(defs_rows, states)
        elif page_rows:
            defs = [
                {
                    "key": d.image_name,
                    "image_name": base_from_icon(d.icon_url),
                    "name": d.name or "",
                }
                for d in defs_rows
            ]
            merged, unmatched = merge_states(defs, page_rows)
        if unmatched:
            logger.info("[achievements] appid=%s 有 %d 行未匹配到清单行", appid, unmatched)

        now = datetime.utcnow()
        if merged:
            existing = {
                r.image_name: r
                for r in (
                    await session.execute(
                        select(AchievementState).where(
                            AchievementState.steamid == steamid, AchievementState.appid == appid
                        )
                    )
                ).scalars()
            }
            for image_name, r in merged.items():
                st = existing.get(image_name)
                if st is None:
                    st = AchievementState(steamid=steamid, appid=appid, image_name=image_name)
                    session.add(st)
                st.achieved = bool(r["achieved"])
                st.unlock_time = int(r["unlock_time"] or 0)
                st.synced_at = now
                if r.get("gray_icon"):
                    d = next((x for x in defs_rows if x.image_name == image_name), None)
                    if d is not None and not d.icon_gray_url:
                        d.icon_gray_url = r["gray_icon"]
            unlocked = sum(1 for r in merged.values() if r["achieved"])
            row.unlocked = unlocked
            row.platinum = row.total_achievements > 0 and unlocked >= row.total_achievements
            row.state_synced_at = now
            row.updated_at = now
        else:
            # 两条通道都没读到（无 Key 且页面为空 / 资料游戏详情不公开）：
            # 保留既有解锁态，不拿空结果覆盖已有账
            logger.info("[achievements] appid=%s 明细通道未读到解锁态，保留既有数据", appid)
        await session.merge(row)
        await session.commit()


async def _apply_defs(appid: int, defs: list[dict]) -> None:
    async with get_session_factory()() as session:
        existing = {
            d.image_name: d
            for d in (
                await session.execute(select(AchievementDef).where(AchievementDef.appid == appid))
            ).scalars()
        }
        now = datetime.utcnow()
        for d in defs:
            row = existing.get(d["key"])
            if row is None:
                row = AchievementDef(appid=appid, image_name=d["key"])
                session.add(row)
            row.name = d["name"] or row.name
            row.description = d["description"] or row.description
            row.icon_url = d["icon_url"]
            if d.get("icon_gray_url"):
                row.icon_gray_url = d["icon_gray_url"]
            if d.get("apiname"):
                row.apiname = d["apiname"]
            row.global_percent = d["global_percent"]
            row.display_order = d["order"]
            row.updated_at = now
        await session.commit()


# ─── 读取端（汇总 KPI / 游戏列表 / 单游戏明细）────────────────────

def _header_url(header_image: str | None, appid: int) -> str:
    return header_image or HEADER_URL_TMPL.format(appid=appid)


async def _achieved_rows(session, steamid: str) -> list[dict]:
    """已解锁成就行（含定义与游戏名），汇总分析的数据底座。"""
    result = await session.execute(
        select(
            AchievementState.appid,
            AchievementState.unlock_time,
            AchievementDef.name,
            AchievementDef.image_name,
            AchievementDef.icon_url,
            AchievementDef.global_percent,
            AchievementGame.name,
        )
        .join(
            AchievementDef,
            (AchievementDef.appid == AchievementState.appid)
            & (AchievementDef.image_name == AchievementState.image_name),
        )
        .join(
            AchievementGame,
            (AchievementGame.appid == AchievementState.appid)
            & (AchievementGame.steamid == AchievementState.steamid),
        )
        .where(AchievementState.steamid == steamid, AchievementState.achieved.is_(True))
    )
    return [
        {
            "appid": r[0], "unlockTime": r[1], "name": r[2] or "", "imageName": r[3],
            "icon": r[4], "globalPercent": r[5], "gameName": r[6] or "",
        }
        for r in result.all()
    ]


async def get_summary(target: str | None = None) -> dict:
    """奖杯 KPI 汇总 + 白金陈列 + 稀有成就 + 最近解锁 + 接近白金 + 图表数据。

    `target` 为空取主账号；传其他 steamid 即读该账号（数据按 steamid 隔离）。
    """
    steamid, creds = await resolve_credentials(target)
    snapshot = await _read_snapshot()
    base = {
        "hasCredential": bool(steamid and creds),
        "steamid": steamid,
        "platinum": 0,
        "gamesWithAchievements": 0,
        "playedGames": 0,
        "totalAchievements": 0,
        "unlockedAchievements": 0,
        "completionRate": 0.0,
        "totalPlaytimeMin": 0,
        # 库外（家庭共享等）单独计数：KPI 合计含它们，但要能一眼看出其中多少来自库外
        "externalGames": 0,
        "externalUnlocked": 0,
        "externalPlatinum": 0,
        # 同步状态只对「本轮同步的那个账号」有意义，切号后不该显示别人的时间戳
        "lastSyncedAt": snapshot.get("syncedAt") or None
        if (snapshot.get("steamid") or steamid) == steamid
        else None,
        "platinums": [],
        "rarest": [],
        "recentUnlocks": [],
        "nearCompletion": [],
        "rarityBuckets": [],
        "playtimeTop": [],
        "unlockTimeline": [],
    }
    if not steamid:
        return base

    async with get_session_factory()() as session:
        games = (
            (
                await session.execute(
                    select(AchievementGame, Game.header_image)
                    .outerjoin(Game, Game.appid == AchievementGame.appid)
                    .where(AchievementGame.steamid == steamid)
                )
            )
            .all()
        )
        achieved = await _achieved_rows(session, steamid)

    trophy = [(g, h) for g, h in games if g.total_achievements > 0]
    total_ach = sum(g.total_achievements for g, _ in trophy)
    unlocked = sum(g.unlocked for g, _ in trophy)
    plat_games = [(g, h) for g, h in trophy if g.platinum]
    external = [(g, h) for g, h in trophy if (g.source or SOURCE_OWNED) != SOURCE_OWNED]

    # 白金日期：该游戏最后一条解锁时刻
    platinum_last: dict[int, int] = {}
    for a in achieved:
        platinum_last[a["appid"]] = max(platinum_last.get(a["appid"], 0), a["unlockTime"])

    platinums = [
        {
            "appid": g.appid,
            "name": g.name or "",
            "headerImage": _header_url(h, g.appid),
            "playtimeMin": g.playtime_min,
            "total": g.total_achievements,
            "date": platinum_last.get(g.appid, 0),
            "source": g.source or SOURCE_OWNED,
        }
        for g, h in plat_games
    ]
    platinums.sort(key=lambda x: -x["date"])

    buckets: dict[str, int] = {t: 0 for t, _ in RARITY_BANDS}
    buckets["common"] = 0
    buckets["unknown"] = 0
    for a in achieved:
        buckets[rarity_tier(a["globalPercent"])] += 1

    rarest = [a for a in achieved if a["globalPercent"] is not None]
    rarest.sort(key=lambda x: x["globalPercent"])
    recent = [a for a in achieved if a["unlockTime"] > 0]
    recent.sort(key=lambda x: -x["unlockTime"])

    near = []
    for g, h in trophy:
        if g.platinum or not g.total_achievements:
            continue
        pct = g.unlocked * 100.0 / g.total_achievements
        if pct >= NEAR_THRESHOLD:
            near.append({
                "appid": g.appid,
                "name": g.name or "",
                "headerImage": _header_url(h, g.appid),
                "unlocked": g.unlocked,
                "total": g.total_achievements,
                "percent": round(pct, 1),
                "remaining": g.total_achievements - g.unlocked,
                "playtimeMin": g.playtime_min,
                "source": g.source or SOURCE_OWNED,
            })
    near.sort(key=lambda x: (-x["percent"], x["remaining"]))

    month_counts: dict[str, int] = {}
    for a in achieved:
        if a["unlockTime"] <= 0:
            continue
        d = datetime.fromtimestamp(a["unlockTime"])
        key = f"{d.year:04d}-{d.month:02d}"
        month_counts[key] = month_counts.get(key, 0) + 1
    today = datetime.now()
    timeline = []
    for i in range(TIMELINE_MONTHS - 1, -1, -1):
        m = today.month - i
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        key = f"{y:04d}-{m:02d}"
        timeline.append({"month": key, "count": month_counts.get(key, 0)})

    base.update({
        "platinum": len(plat_games),
        "gamesWithAchievements": len(trophy),
        "playedGames": sum(1 for g, _ in games if g.playtime_min > 0),
        "totalAchievements": total_ach,
        "unlockedAchievements": unlocked,
        "completionRate": round(unlocked * 100.0 / total_ach, 1) if total_ach else 0.0,
        "totalPlaytimeMin": sum(g.playtime_min for g, _ in games),
        "externalGames": len(external),
        "externalUnlocked": sum(g.unlocked for g, _ in external),
        "externalPlatinum": sum(1 for g, _ in external if g.platinum),
        "platinums": platinums[:12],
        "rarest": rarest[:RAREST_LIMIT],
        "recentUnlocks": recent[:RECENT_LIMIT],
        "nearCompletion": near[:NEAR_LIMIT],
        "rarityBuckets": [{"tier": t, "count": buckets[t]} for t, _ in RARITY_BANDS]
        + [{"tier": "common", "count": buckets["common"]}, {"tier": "unknown", "count": buckets["unknown"]}],
        "playtimeTop": [
            {"appid": g.appid, "name": g.name or "", "playtimeMin": g.playtime_min}
            for g, _ in sorted(games, key=lambda x: -x[0].playtime_min)[:10]
        ],
        "unlockTimeline": timeline,
    })
    return base


FILTERS = ("trophy", "platinum", "progress", "external", "all")
SORTS = ("playtime", "progress", "recent", "name")


async def list_games(
    filter_: str = "trophy", sort_: str = "playtime", q: str = "", target: str | None = None
) -> dict:
    """成就视角的游戏列表（含库外；过滤/排序/搜索在行集上做，量级 ≤ 数千）。"""
    steamid, _ = await resolve_credentials(target)
    if not steamid:
        return {"games": [], "count": 0}
    if filter_ not in FILTERS:
        filter_ = "trophy"
    if sort_ not in SORTS:
        sort_ = "playtime"

    async with get_session_factory()() as session:
        pairs = (
            await session.execute(
                select(AchievementGame, Game.header_image)
                .outerjoin(Game, Game.appid == AchievementGame.appid)
                .where(AchievementGame.steamid == steamid)
            )
        ).all()

    needle = (q or "").strip().lower()
    items: list[dict] = []
    for row, header_image in pairs:
        if filter_ == "trophy" and row.total_achievements <= 0:
            continue
        if filter_ == "platinum" and not row.platinum:
            continue
        if filter_ == "progress" and not (0 < row.unlocked < row.total_achievements):
            continue
        if filter_ == "external" and (row.source or SOURCE_OWNED) == SOURCE_OWNED:
            continue
        name = row.name or ""
        if needle and needle not in name.lower():
            continue
        total = row.total_achievements
        items.append({
            "appid": row.appid,
            "name": name,
            "headerImage": _header_url(header_image, row.appid),
            "playtimeMin": row.playtime_min,
            "lastPlayed": row.last_played,
            "total": total,
            "unlocked": row.unlocked,
            "remaining": max(0, total - row.unlocked),
            "percent": round(row.unlocked * 100.0 / total, 1) if total else 0.0,
            "platinum": bool(row.platinum),
            "source": row.source or SOURCE_OWNED,
            "owned": (row.source or SOURCE_OWNED) == SOURCE_OWNED,
        })

    if sort_ == "playtime":
        items.sort(key=lambda x: (-x["playtimeMin"], x["name"].lower()))
    elif sort_ == "progress":
        items.sort(key=lambda x: (-x["percent"], -x["total"], x["name"].lower()))
    elif sort_ == "recent":
        items.sort(key=lambda x: (-x["lastPlayed"], x["name"].lower()))
    else:
        items.sort(key=lambda x: x["name"].lower())
    return {"games": items, "count": len(items)}


async def available_accounts() -> list[dict]:
    """成就页可切换的账号：已绑账号（本地有 Cookie）+ 主账号所在家庭组的成员。

    只回传展示字段（无任何凭证）。读取时统一用本地这份 Key/token 去查目标
    steamid，数据按 (steamid, appid) 落库，天然隔离——不会把 A 的成就并到 B。
    """
    out: dict[str, dict] = {}
    primary = await _primary_steam_id()
    try:
        for a in await account_service.list_accounts():
            sid = str(a.get("steam_id") or "")
            if not sid:
                continue
            out[sid] = {
                "steamid": sid,
                "personaName": a.get("persona_name") or "",
                "avatarUrl": a.get("avatar_url") or "",
                "isPrimary": bool(a.get("is_primary")),
                "isActive": bool(a.get("is_active")),
                "relation": "bound",
            }
    except Exception:  # noqa: BLE001 —— 账号域异常不拖垮成就页
        logger.info("[achievements] 已绑账号列表读取失败", exc_info=True)

    try:
        from app.domains.family.models import FamilyGroup

        async with get_session_factory()() as session:
            stmt = select(FamilyGroup)
            if primary:
                stmt = stmt.where(FamilyGroup.steamid == primary)
            rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            for m in row.members_json or []:
                sid = str(m.get("steamid") or "")
                if not sid or sid in out:
                    continue
                out[sid] = {
                    "steamid": sid,
                    # 快照写入侧是驼峰键（personaName/avatarUrl），下划线键只兜旧库
                    "personaName": m.get("personaName") or m.get("persona_name") or "",
                    "avatarUrl": m.get("avatarUrl") or m.get("avatar_url") or "",
                    "isPrimary": False,
                    "isActive": False,
                    "relation": "family",
                }
    except Exception:  # noqa: BLE001 —— 家庭快照缺失时只列已绑账号
        logger.info("[achievements] 家庭成员列表读取失败", exc_info=True)

    # 名字/头像补齐：tracked_accounts 由账户轮转持续维护（含家庭成员），
    # 成就页不另起网络请求，直接复用这份本地档案。
    try:
        from app.domains.wishlist.models import TrackedAccount

        async with get_session_factory()() as session:
            trows = (await session.execute(select(TrackedAccount))).scalars().all()
        for t in trows:
            entry = out.get(t.steamid)
            if entry is None:
                continue
            entry["personaName"] = entry["personaName"] or t.persona_name or ""
            entry["avatarUrl"] = entry["avatarUrl"] or t.avatar_url or ""
    except Exception:  # noqa: BLE001 —— 档案缺失只影响展示，不阻断账号清单
        logger.info("[achievements] tracked_accounts 档案读取失败", exc_info=True)

    items = list(out.values())
    items.sort(key=lambda x: (
        not x["isPrimary"],
        x["relation"] != "bound",
        (x["personaName"] or x["steamid"]).lower(),
    ))
    return items


async def get_game_detail(appid: int, target: str | None = None) -> dict | None:
    """单游戏成就明细（已解锁在前，未解锁按稀有序）；按 steamid 隔离。"""
    steamid, _ = await resolve_credentials(target)
    if not steamid:
        return None
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AchievementGame).where(
                    AchievementGame.steamid == steamid, AchievementGame.appid == appid
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        header_image = await session.scalar(select(Game.header_image).where(Game.appid == appid))
        defs_rows = (
            (
                await session.execute(
                    select(AchievementDef)
                    .where(AchievementDef.appid == appid)
                    .order_by(AchievementDef.display_order)
                )
            )
            .scalars()
            .all()
        )
        states = {
            r.image_name: r
            for r in (
                await session.execute(
                    select(AchievementState).where(
                        AchievementState.steamid == steamid, AchievementState.appid == appid
                    )
                )
            ).scalars()
        }

    achievements = []
    for d in defs_rows:
        st = states.get(d.image_name)
        achievements.append({
            "imageName": d.image_name,
            "name": d.name or d.image_name,
            "description": d.description or "",
            "icon": d.icon_url,
            "iconGray": d.icon_gray_url,
            "globalPercent": d.global_percent,
            "rarity": rarity_tier(d.global_percent),
            "achieved": bool(st.achieved) if st else False,
            "unlockTime": st.unlock_time if st else 0,
        })
    # 已解锁在前（新→旧）；未解锁按稀有度（越稀有越靠前）
    achievements.sort(key=lambda a: (
        not a["achieved"],
        -a["unlockTime"] if a["achieved"] else 0,
        a["globalPercent"] if a["globalPercent"] is not None else 999,
    ))

    total = row.total_achievements or len(achievements)
    last_unlock = max((a["unlockTime"] for a in achievements if a["achieved"]), default=0)
    return {
        "appid": row.appid,
        "name": row.name or "",
        "headerImage": _header_url(header_image, row.appid),
        "playtimeMin": row.playtime_min,
        "lastPlayed": row.last_played,
        "total": total,
        "unlocked": row.unlocked,
        "percent": round(row.unlocked * 100.0 / total, 1) if total else 0.0,
        "platinum": bool(row.platinum),
        "perfectDate": last_unlock if row.platinum else 0,
        "source": row.source or SOURCE_OWNED,
        "owned": (row.source or SOURCE_OWNED) == SOURCE_OWNED,
        "achievements": achievements,
    }