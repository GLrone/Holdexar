"""目录检索类工具：本地检索 / Steam 全网检索 / 入库 / 推荐 / 热销榜。"""
from __future__ import annotations

import asyncio
import re

import aiohttp

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.crawler.epic_free import STEAM_HTTP_HEADERS, STORESEARCH_URL
from app.domains.games import service as games_service
from app.domains.proxies import service as proxies_service

_FIND_LIMIT = _shared._FIND_LIMIT
_STEAM_SEARCH_LIMIT = 5
_INGEST_MAX = 20

_PRICE_TAG_RE = _shared._PRICE_TAG_RE
_PRICE_NUM_RE = _shared._PRICE_NUM_RE


def _game_item(it: dict) -> dict:
    return {
        "appid": it.get("appid"),
        "name": it.get("name"),
        "cnyFen": it.get("basePriceFen"),
        "discount": it.get("discount"),
        "positiveRate": it.get("positiveRate"),
        "reviewCount": it.get("reviewCount"),
        "chineseSupport": it.get("chineseSupport") or it.get("chinese_support"),
    }


async def search_games(question: str, query: str | None = None) -> list[dict]:
    """按问句或显式词做目录名检索（含未爬价/锁区游戏；价格意图找对象 /
    写意图解析对象用，不触发抓取）。"""
    return await games_service.search_catalog((query or question or "").strip())


async def recommend_games(question: str) -> list[dict]:
    """推荐意图（确定性回退路径）：问句里解析属性条件（打折 / 好评 / 价格
    上限 / 《书名号》名称），组合成 list_games 筛选——自然语言整句不能当
    名称去搜。"""
    q = (question or "").strip()
    m = _PRICE_TAG_RE.search(q)
    only_discounted = any(w in q for w in ("打折", "折扣", "特惠", "特卖", "降价"))
    min_rating = 80 if any(w in q for w in ("好评", "口碑", "评价高")) else 0
    max_price_yuan = None
    nm = _PRICE_NUM_RE.search(q)
    if nm and any(w in q for w in ("以内", "以下", "不超过", "块内", "预算")):
        max_price_yuan = float(nm.group(1))
    return await recommend_games_by_filters(
        q=m.group(1) if m else None,
        only_discounted=only_discounted,
        min_rating=min_rating,
        max_price_yuan=max_price_yuan,
    )


async def recommend_games_by_filters(
    *,
    q: str | None = None,
    only_discounted: bool = False,
    min_rating: int = 0,
    max_price_yuan: float | None = None,
) -> list[dict]:
    """按结构化条件挑库内游戏（agent 工具与回退路径共用）。

    排序用库内智能排序（scoring 已算好的 smartScore）——推荐就是推荐，
    不回退到目录默认序。"""
    result = await games_service.list_games(
        sort="smart",
        limit=_FIND_LIMIT,
        q=(q or "").strip() or None,
        only_discounted=only_discounted,
        min_rating=min_rating,
        max_price=int(max_price_yuan * 100) if max_price_yuan else None,
    )
    return [_game_item(it) for it in result.get("items", [])]


async def _steam_storesearch(term: str, proxy: str | None) -> dict | None:
    """storesearch 匿名轻端点单次检索（走代理出口）；失败返回 None（不抛）。

    cc=US&l=english：该端点只匹配英文索引（中文词零结果，epic 匹配同经验），
    且国区商店视图会剔除锁区游戏——US 视图才能搜到国区锁区的游戏。"""
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2, ttl_dns_cache=60),
            timeout=aiohttp.ClientTimeout(total=12),
        ) as session:
            async with session.get(
                STORESEARCH_URL,
                params={"term": term, "cc": "US", "l": "english"},
                headers=STEAM_HTTP_HEADERS,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def search_steam(term: str) -> dict:
    """Steam 商店全网检索（不限本地目录）：拿 appid/名称，供入库首爬。"""
    term = " ".join((term or "").split())[:80]
    if not term:
        return {"kind": "empty", "note": "bad_term"}
    try:
        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001 — 出口解析失败按无代理直试
        proxy = None
    data = await _steam_storesearch(term, proxy)
    if not isinstance(data, dict):
        return {"kind": "empty", "note": "search_failed"}
    items: list[dict] = []
    for it in (data.get("items") or [])[:_STEAM_SEARCH_LIMIT]:
        try:
            appid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        # 候选价只有币种恰为 CNY 才进 cnyFen（US 视图为美元，换算不在此处做）
        price = it.get("price") or {}
        final, initial = price.get("final"), price.get("initial")
        cny_fen = int(final) if price.get("currency") == "CNY" and isinstance(final, int) and final > 0 else None
        discount = 0
        if isinstance(final, int) and isinstance(initial, int) and initial > final > 0:
            discount = round((1 - final / initial) * 100)
        items.append({
            "appid": appid,
            "name": str(it.get("name") or f"AppID {appid}"),
            "cnyFen": cny_fen,
            "discount": discount,
        })
    return {"kind": "games", "items": items}


async def ingest_appids(appids: list) -> dict:
    """指定 AppID 入库：1:1 任务页「添加游戏」链——目录导入 + 新导入首爬
    （kind=import 同一执行入口）。只进目录与价格，不建立关注。"""
    from app.domains.crawl import service as crawl_service

    clean: list[int] = []
    for raw in appids or []:
        try:
            appid = int(raw)
        except (TypeError, ValueError):
            continue
        if appid > 0 and appid not in clean:
            clean.append(appid)
    if not clean:
        return {"kind": "empty", "note": "no_target"}
    if len(clean) > _INGEST_MAX:
        return {"kind": "empty", "note": "too_many", "data": {"max": _INGEST_MAX}}
    result = await crawl_service.import_appids(clean)
    per = {
        int(r["appid"]): str(r.get("status") or "")
        for r in result.get("results", []) if r.get("appid")
    }
    fresh = [a for a in clean if per.get(a) == "ok"]
    started = False
    if fresh:
        try:
            await crawl_service.start_job(scope="appids", appids=fresh, kind="import")
            started = True
        except RuntimeError:
            started = False  # 已有任务在跑：目录已导入，首爬让位现役任务
    names = await games_service.names_for(clean)
    rows = []
    for a in clean:
        st = per.get(a)
        if st == "ok":
            vkey = "ingestQueued" if started else "ingestImported"
        elif st == "own":
            vkey = "ingestOwned"
        else:
            vkey = "ingestFail"
        rows.append({
            "k": names.get(a) or f"AppID {a}",
            "vKey": vkey,
            "v": f"#{a}",
            "tone": "ok" if st in ("ok", "own") else "bad",
        })
    return _shared.rows_result("ingest", rows, total=len(rows))


async def top_games() -> dict:
    """Steam 热销榜 TOP100 的库内前段（榜单缓存，零外网）——顺序即榜序。"""
    from app.domains.games import boards as games_boards

    appids = await games_boards.get_board("topsellers")
    if not appids:
        return {"kind": "empty", "note": "no_data"}
    ids = [int(a) for a in appids[:_shared._READ_ROWS_LIMIT]]
    briefs = await games_service.briefs_for(ids)
    items = [_shared.brief_item(i, briefs.get(i)) for i in ids]
    return _shared.games_card("top", items, total=len(appids))


async def _search_games(args: dict, sid: str | None = None) -> dict:
    items = await search_games(str(args.get("q") or ""), query=str(args.get("q") or ""))
    return {"kind": "games", "items": items}


async def _search_steam(args: dict, sid: str | None = None) -> dict:
    return await search_steam(str(args.get("term") or args.get("q") or ""))


async def _ingest_appids(args: dict, sid: str | None = None) -> dict:
    return await ingest_appids(args.get("appids") or [])


async def _recommend_games(args: dict, sid: str | None = None) -> dict:
    items = await recommend_games_by_filters(
        only_discounted=bool(args.get("only_discounted")),
        min_rating=int(args.get("min_rating") or 0),
        max_price_yuan=args.get("max_price_yuan"),
    )
    return {"kind": "games", "items": items}


async def _top_games(args: dict, sid: str | None = None) -> dict:
    return await top_games()


SPECS = [
    ToolSpec(
        name="search_games", group="read", risk="low",
        description="在本地游戏目录按名称检索游戏（中英文均可），返回候选（含 appid、现价、折扣、好评率）。"
                    "注意：目录未收录、或收录了但还没爬过价格的游戏检索不到——那时改用 search_steam 去 Steam 商店搜",
        parameters={"type": "object", "properties": {
            "q": {"type": "string", "description": "游戏名称关键词"},
        }, "required": ["q"]},
        handler=_search_games, step_label="search",
    ),
    ToolSpec(
        name="search_steam", group="read", risk="medium",
        description="在 Steam 商店全网搜索游戏（不限于本地目录，国区锁区游戏也搜得到），"
                    "返回候选的 appid 与名称。注意：该检索只匹配英文名——中文词搜不到时"
                    "改用英文名重试（如 卡赞→Khazan）。本地目录检索不到某款游戏、或用户明确要求"
                    "『去 Steam 搜』时调用；拿到 appid 后可用 ingest_appids 让它入库，之后即可查价格",
        parameters={"type": "object", "properties": {
            "term": {"type": "string", "description": "搜索词（优先用游戏英文名）"},
        }, "required": ["term"]},
        handler=_search_steam, step_label="steamSearch",
    ),
    ToolSpec(
        name="ingest_appids", group="read", risk="medium",
        description="把指定 AppID 的游戏导入本地目录并立即首爬价格（与任务页『添加游戏』是同一动作），"
                    "导入后即可查价格、史低、设提醒。用户要求『让某游戏入库/抓一下某游戏/收录某游戏』时调用；"
                    "appid 来自 search_steam 结果或用户提供。一次最多 20 款；"
                    "导入不等于关注——要持续追踪降价需另调 add_follow",
        parameters={"type": "object", "properties": {
            "appids": {"type": "array", "items": {"type": "integer"},
                       "description": "要导入的游戏 AppID 列表（一次最多 20 个）"},
        }, "required": ["appids"]},
        handler=_ingest_appids, step_label="ingest",
    ),
    ToolSpec(
        name="recommend_games", group="read", risk="low",
        description="按条件从用户库内挑游戏，可组合：打折中 / 好评率下限 / 价格上限（元）",
        parameters={"type": "object", "properties": {
            "only_discounted": {"type": "boolean", "description": "只看打折中的游戏"},
            "min_rating": {"type": "integer", "description": "好评率下限（0-100）"},
            "max_price_yuan": {"type": "number", "description": "现价上限（人民币元）"},
        }},
        handler=_recommend_games, step_label="recommend",
    ),
    ToolSpec(
        name="top_games", group="read", risk="low",
        description="列出 Steam 热销榜前列。用户问『现在什么游戏火/热销榜』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_top_games, step_label="topGames",
    ),
]
