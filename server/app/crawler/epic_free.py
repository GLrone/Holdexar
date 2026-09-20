"""Epic 喜加一发现源：官方促销端点 → 白送判定 → Steam appid 匹配。

数据链（替代停更的外部手工管道）：
- 端点 GET freeGamesPromotions（无凭据公开接口；域名带 -ipv4 前缀，
  旧域名已 404）：返回当前 + 即将上线的促销元素（约两周窗口）
- 白送判定：promotions 两层列表里 discountSetting.discountPercentage == 0
  （幽灵行者 2 那种 20 = 普通打折，必须排除）；upcomingPromotionalOffers
  同构 = 下周预告
- Epic 侧无 Steam appid → storesearch 按英文标题匹配（locale=en-US 拉
  英文标题；精确名首选必先命中，demo/原声带排其后）
- 落库形态对齐 games.epic_date 现行语义「开始日期」单日期（非区间），
  存 UTC 日期原文（换班时刻 15/17:00 UTC，北京当日 23/次日 01）
- 移动端每周白送与 PC 同属促销端点（每周四换班）：当期白送元素谁在
  android/ios sandbox offers 有 0 元 Claim 条目即本周移动白送
  （resolve_mobile_freebie），无命中降级 CMS 移动页 breaker 立绘兜底

节流口径（对齐 boards.py 独立轻量会话）：单批拉一次促销端点 +
每个未匹配标题一次 storesearch；间隔 500ms；不接爬虫主链路 429 熔断。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

# 现役端点（旧域名 store-site-backend-static.ak 已 404）
FREE_GAMES_URL = (
    "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
)
# 移动端每周白送兜底信号：官方 CMS 移动页的 Free Giveaway breaker
# （URL 固定、内容每周四换）。移动端与 PC 同属「每周四白送」计划，移动
# 白送游戏同样出现在促销端点元素里，主判别走 sandbox Claim 探测，breaker
# 只在探测失败时兜底出卡。
MOBILE_CMS_URL = (
    "https://store-content-ipv4.ak.epicgames.com/api/en-US/content/static/mobile"
)
EPIC_MOBILE_PAGE_URL = "https://store.epicgames.com/en-US/mobile"
# 移动端判别数据源：egs-platform-service 公开 sandbox offers 接口（无需
# 认证；UA 用 App 风格）。促销端点的当期白送元素谁在 android/ios 名下有
# 0 元 Claim 条目，谁就是本周移动端白送——官方数据直出真名/截止日，
# 结账直链当场拼装。
EGS_PLATFORM_SERVICE = "https://egs-platform-service.store.epicgames.com"
EGS_APP_UA = "EpicGamesStore/50102 Android/14"
STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
# 英文标题用于匹配（中文标题对 storesearch 无效；en-US 拉双语对照）
EN_PARAMS = {"locale": "en-US", "country": "US"}
CN_PARAMS = {"locale": "zh-CN", "country": "CN"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
_REQUEST_INTERVAL = 0.5  # storesearch 批间隔 500ms（对齐 boards 批间隔）


@dataclass(frozen=True)
class EpicFreeGame:
    """一条喜加一记录（appid 由 storesearch 匹配，可能为 None=未上 Steam）。

    展示扩展字段（image/url/price_original/free_end）供仪表盘卡片与外链
    使用；标记链不用它们，缺省为空即可。
    """

    title: str           # 英文标题（匹配键）
    title_cn: str        # Epic 中文标题（可能为空，仅日志对照）
    appid: int | None    # Steam appid；None = 匹配失败/未上架
    free_start: str      # 白送开始日（UTC 日期原文，对齐 epic_date 单日期语义）
    offer_type: str      # BASE_GAME / BUNDLE / ADD_ON
    upcoming: bool       # True = 下周预告（尚未开始白送）
    element_id: str = "" # Epic 目录元素 id（中英响应按此对键回填 title_cn）
    namespace: str = ""  # Epic 目录命名空间 id（移动端 sandbox offers 探测键）
    free_end: str = ""   # 白送结束日（UTC 日期原文，取白送 offer 里最晚的）
    image: str = ""      # 横版封面（OfferImageWide 优先，回退店头图/缩略图）
    url: str = ""        # Epic 商店页（productSlug → pageSlug → urlSlug 三级取）
    price_original: str = ""  # 原价文案（中文区响应优先，如 "¥88.00"；划线展示用）


def _iter_offers(promos: dict | None) -> list[dict]:
    """promotions 两层列表扁平化（[{promotionalOffers: [...]}, ...] → 内层列表）。"""
    if not promos:
        return []
    out: list[dict] = []
    for group in promos.get("promotionalOffers") or []:
        out.extend(group.get("promotionalOffers") or [])
    for group in promos.get("upcomingPromotionalOffers") or []:
        out.extend(group.get("promotionalOffers") or [])
    return out


def _is_free_offer(offer: dict) -> bool:
    """白送判定：百分比折扣为 0（打折 20% 的促销不是喜加一）。"""
    disc = (offer.get("discountSetting") or {}).get("discountPercentage")
    return disc == 0


def _offer_start_date(offer: dict) -> str | None:
    """白送开始日 → 'YYYY-M-D'（UTC；对齐存量 epic_date 无前导零格式）。"""
    return _iso_to_day(offer.get("startDate"))


def _iso_to_day(raw: str | None) -> str | None:
    """ISO 时间戳 → 'YYYY-M-D'（UTC）；缺/坏值返回 None。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{dt.astimezone(timezone.utc).year}-{dt.month}-{dt.day}"


def _cover_image(element: dict) -> str:
    """keyImages 里挑横版封面：OfferImageWide > 店头宽图 > 缩略图 > 首张。"""
    by_type: dict[str, str] = {}
    first = ""
    for img in element.get("keyImages") or []:
        url = img.get("url") or ""
        if not url:
            continue
        by_type[img.get("type") or ""] = url
        if not first:
            first = url
    for t in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail"):
        if by_type.get(t):
            return by_type[t]
    return first


def _store_url(element: dict) -> str:
    """Epic 商店页 URL：productSlug（剥 /home 后缀）→ 目录页 slug → urlSlug。

    三级回退对齐社区抓取器的取法；全缺时退免费游戏总览页（不产死链）。
    """
    slug = (element.get("productSlug") or "").split("/")[0]
    if not slug:
        mappings = (element.get("catalogNs") or {}).get("mappings") or []
        slug = (mappings[0].get("pageSlug") if mappings else "") or ""
    if not slug:
        slug = element.get("urlSlug") or ""
    if not slug:
        return "https://store.epicgames.com/en-US/free-games"
    return f"https://store.epicgames.com/en-US/p/{slug}"


def _original_price(element: dict) -> str:
    """原价展示文案（fmtPrice；中文区响应是 ¥ 形态，回填时覆盖）。"""
    try:
        return str(
            (((element.get("price") or {}).get("totalPrice") or {}).get("fmtPrice") or {})
            .get("originalPrice")
            or ""
        )
    except AttributeError:  # noqa: PERF203 — 嵌套取值遇 None 中途断链
        return ""


def parse_free_games(payload: dict) -> list[EpicFreeGame]:
    """freeGamesPromotions 响应 → 白送元素列表（appid 全 None，待匹配）。

    一个元素可能挂多条促销（过去一次 + 下周预告）：当前窗口内任一条
    白送即入选；起期取**最早**的白送 offer（历史重复赠送时本次不重复
    补——上游导入侧同 appid 已标记则跳过）。
    """
    elements = (
        ((payload.get("data") or {}).get("Catalog") or {})
        .get("searchStore", {})
        .get("elements")
    ) or []
    out: list[EpicFreeGame] = []
    for e in elements:
        offers = _iter_offers(e.get("promotions"))
        free_offers = [o for o in offers if _is_free_offer(o)]
        if not free_offers:
            continue
        starts = [d for o in free_offers if (d := _offer_start_date(o))]
        if not starts:
            continue
        ends = [d for o in free_offers if (d := _iso_to_day(o.get("endDate")))]
        # upcoming 判定：全部白送 offer 均未到 startDate（预告元素）；
        # 只要有一条已开始即视为「当期在送」
        now = datetime.now(timezone.utc)
        upcoming = all(
            datetime.fromisoformat(o["startDate"].replace("Z", "+00:00")) > now
            for o in free_offers
        )
        out.append(
            EpicFreeGame(
                title=e.get("title") or "",
                title_cn="",  # 由调用方在拉中文响应后回填
                appid=None,
                free_start=min(starts),
                offer_type=e.get("offerType") or "",
                upcoming=upcoming,
                element_id=str(e.get("id") or ""),
                namespace=str(e.get("namespace") or ""),
                free_end=max(ends) if ends else "",
                image=_cover_image(e),
                url=_store_url(e),
                price_original=_original_price(e),
            )
        )
    return out


def _fill_title_cn(en_list: list[EpicFreeGame], cn_payload: dict) -> None:
    """中文响应按元素 id 回填 title_cn 与原价文案。

    两个 locale 的元素集合可能不同（区域目录差异，12 对 11）且
    白送列表是过滤后子集——按下标对齐必然错位，必须按元素 id 对键。
    中文侧无此 id 时保持空串/英文价（title_cn 仅对照、原价回落英文区，
    缺失无害）。
    """
    elements = (
        ((cn_payload.get("data") or {}).get("Catalog") or {})
        .get("searchStore", {})
        .get("elements")
    ) or []
    cn_map = {
        str(e.get("id") or ""): (e.get("title") or "", _original_price(e))
        for e in elements
    }
    for g in en_list:
        hit = cn_map.get(g.element_id)
        if hit is None:
            continue
        object.__setattr__(g, "title_cn", hit[0])
        if hit[1]:
            object.__setattr__(g, "price_original", hit[1])


async def match_steam_appid(session: aiohttp.ClientSession, title: str,
                            proxy: str | None = None) -> int | None:
    """storesearch 匹配 Steam appid；失败/无结果返回 None（不抛）。

    匹配策略：首个结果的精确名优先——demo/原声带/DLC 排在精确
    匹配之后，直接取 items[0]。参数对齐站内现行 cc=US&l=english。
    """
    try:
        async with session.get(
            STORESEARCH_URL,
            params={"term": title, "cc": "US", "l": "english"},
            headers=_HEADERS,
            proxy=proxy,
        ) as resp:
            if resp.status != 200:
                logger.warning("[epic] storesearch '%s' HTTP %d", title, resp.status)
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.warning("[epic] storesearch '%s' 网络错误", title)
        return None
    items = data.get("items") or []
    if not items:
        return None
    first = items[0]
    aid = first.get("id")
    return int(aid) if isinstance(aid, (int, str)) and str(aid).isdigit() else None


async def _fetch_parsed(
    proxy: str | None, session: aiohttp.ClientSession
) -> list[EpicFreeGame] | None:
    """拉英文+中文促销响应并解析（中文回填失败不阻塞）；网络失败返回 None。

    两个入口共用：fetch_free_games（标记链，再做 storesearch 匹配）与
    fetch_free_offers（仪表盘卡片，纯展示轻量链）。
    """
    # ① 英文促销响应（匹配键源）
    try:
        async with session.get(FREE_GAMES_URL, params=EN_PARAMS,
                               headers=_HEADERS, proxy=proxy) as resp:
            if resp.status != 200:
                logger.warning("[epic] 促销端点 HTTP %d（本轮放弃）", resp.status)
                return None
            en_payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("[epic] 促销端点网络错误：%s", e)
        return None

    games = parse_free_games(en_payload)
    if not games:
        return []

    # ② 中文响应回填对照标题与原价文案（失败不影响主链）
    try:
        async with session.get(FREE_GAMES_URL, params=CN_PARAMS,
                               headers=_HEADERS, proxy=proxy) as resp:
            if resp.status == 200:
                _fill_title_cn(games, await resp.json(content_type=None))
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.info("[epic] 中文对照标题拉取失败（忽略）")
    return games


async def fetch_free_offers(proxy: str | None = None,
                            session: aiohttp.ClientSession | None = None) -> list[EpicFreeGame]:
    """仪表盘卡片轻量链：白送元素 + 展示字段，不做 storesearch 匹配。

    独立轻量会话语义同 fetch_free_games；空列表 = 无白送或拉取失败，
    调用方（服务层）据此决定缓存与 ok 标记。
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        games = await _fetch_parsed(proxy, session)
        if games:
            logger.info("[epic-offers] 白送元素 %d 个（展示链，未匹配）", len(games))
        return games or []
    finally:
        if owns_session:
            await session.close()


def parse_mobile_breaker(payload: dict) -> dict | None:
    """CMS 移动页 → 当期移动白送 breaker {image, url}；找不到返回 None。

    只认 title == "Free Giveaway" 的 breaker：图 = 当期游戏立绘（名称在图里），
    链接固定指官方移动页（领取动作只在 App 内存在）。
    """
    sections = ((payload.get("layout") or {}).get("section")) or []
    for sec in sections:
        for breaker in ((sec.get("breakers") or {}).get("breakerList")) or []:
            if (breaker.get("title") or "") != "Free Giveaway":
                continue
            image = breaker.get("backgroundImage") or ""
            if image:
                return {"image": image, "url": EPIC_MOBILE_PAGE_URL}
    return None


async def fetch_mobile_breaker(proxy: str | None = None,
                               session: aiohttp.ClientSession | None = None) -> dict | None:
    """拉 CMS 移动页取当期移动白送 breaker；网络失败/结构变更返回 None（不抛）。

    展示链附属信号：失败只降级（卡片少一张移动端），不影响 PC 白送列表。
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        try:
            async with session.get(MOBILE_CMS_URL, headers=_HEADERS, proxy=proxy) as resp:
                if resp.status != 200:
                    logger.info("[epic-mobile] CMS 移动页 HTTP %d（本轮跳过）", resp.status)
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.info("[epic-mobile] CMS 移动页网络错误（忽略）：%s", e)
            return None
        return parse_mobile_breaker(payload)
    finally:
        if owns_session:
            await session.close()


def _sandbox_offers_sync(namespace: str, platform: str) -> list[dict]:
    """namespace 下移动端全部 offer（公开接口无需认证；UA 用 APP 风格）。"""
    qs = urllib.parse.urlencode({
        "count": "10", "country": "US", "locale": "en-US",
        "platform": platform, "start": "0", "categories": "games",
    })
    url = (f"{EGS_PLATFORM_SERVICE}/api/v2/public/sandboxes/{namespace}"
           f"/offers?{qs}")
    req = urllib.request.Request(url, headers={
        "User-Agent": EGS_APP_UA, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status != 200:
                logger.info("[epic-mobile] sandbox offers %s HTTP %d", platform, resp.status)
                return []
            return (json.loads(resp.read().decode("utf-8", "replace"))
                    .get("data") or [])
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.info("[epic-mobile] sandbox offers %s 失败（忽略）：%s", platform, e)
        return []


def _parse_claim_offers(rows: list[dict], platform: str) -> list[dict]:
    """data[].content.purchase[] → 免费领取条目（Claim 且 0 元，双判据）。"""
    out = []
    for row in rows or []:
        content = row.get("content") or {}
        for promo in content.get("purchase") or []:
            payload = promo.get("purchasePayload") or {}
            price = (promo.get("price") or {}).get("decimalPrice")
            if promo.get("purchaseType") != "Claim" or price not in (0, "0", 0.0):
                continue
            if not payload.get("offerId") or not payload.get("sandboxId"):
                continue
            end = ((promo.get("discount") or {}).get("discountEndDate") or "")[:10]
            out.append({
                "offerId": payload["offerId"],
                "sandboxId": payload["sandboxId"],
                "end": end or None,
                "platform": platform,
            })
    return out


async def resolve_mobile_freebie(games: list[EpicFreeGame]) -> dict | None:
    """当期白送元素 → 移动端白送 {title,image,url,end,worth}；无命中返回 None。

    判别式：PC 与移动同属「每周四白送」计划，移动白送游戏就在促销端点的
    元素里——当期（非预告）元素逐个探测 sandbox offers，谁在 android/ios
    名下有 0 元 Claim 条目，谁就是本周移动端白送（官方数据直出真名/
    截止日/封面，不依赖第三方源）。多元素同时命中取截止日最晚者（排除
    上一期的尾巴）。全无 Claim 返回 None（调用方降级 breaker）。
    """
    best: tuple[str, EpicFreeGame, list[dict]] | None = None
    for g in games:
        if g.upcoming or not g.namespace:
            continue
        rows_android = await asyncio.to_thread(
            _sandbox_offers_sync, g.namespace, "android")
        rows_ios = await asyncio.to_thread(
            _sandbox_offers_sync, g.namespace, "ios")
        claims = (_parse_claim_offers(rows_android, "android")
                  + _parse_claim_offers(rows_ios, "ios"))
        if not claims:
            continue
        end = max((c["end"] for c in claims if c.get("end")), default="")
        if best is not None and end <= best[0]:
            continue
        best = (end, g, claims)
    if best is None:
        return None
    end, g, claims = best
    # 结账直链（需浏览器 Epic 登录态）；双端 Claim 条目合并成单链
    parts = dict.fromkeys(
        f"1-{c['sandboxId']}-{c['offerId']}" for c in claims)
    query = "&".join(f"offers={p}" for p in parts)
    return {
        "title": g.title_cn or g.title,
        "image": g.image,
        "url": f"https://store.epicgames.com/purchase?{query}",
        "end": end or None,
        "worth": g.price_original or None,
    }


async def fetch_free_games(proxy: str | None = None,
                           session: aiohttp.ClientSession | None = None) -> list[EpicFreeGame]:
    """拉取 + 匹配完整链：白送元素 → storesearch 逐个补 appid。

    独立轻量会话（不接主链 429 熔断，对齐 boards 语义）；调用方
    传入 session（测试注入 mock）或自动开新会话。
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        games = await _fetch_parsed(proxy, session)
        if not games:
            logger.info("[epic] 本轮无白送元素")
            return []

        # ③ storesearch 逐个匹配（间隔 500ms 防限流）
        matched = 0
        for g in games:
            object.__setattr__(g, "appid", await match_steam_appid(session, g.title, proxy))
            if g.appid is not None:
                matched += 1
            await asyncio.sleep(_REQUEST_INTERVAL)
        logger.info("[epic] 白送元素 %d 个，Steam 匹配 %d 个",
                    len(games), matched)
        return games
    finally:
        if owns_session:
            await session.close()
