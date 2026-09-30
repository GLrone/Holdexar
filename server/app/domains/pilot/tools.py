"""pilot 只读工具：复用 games 域服务取事实，不自建查询、不写库。

产出紧凑事实（几百字节级）供模板摘要与 LLM 解读共用；全量数据仍由
既有接口承载，领航台不重复暴露完整列表。
"""
from __future__ import annotations

import re
import statistics

from app.crawler.utils import get_beijing_time_obj
from app.domains.games import service as games_service

_FIND_LIMIT = 5
_PRICE_TAG_RE = re.compile(r"《(.+?)》")
_PRICE_NUM_RE = re.compile(r"(\d+)\s*(?:块|元)")


def _game_item(it: dict) -> dict:
    return {
        "appid": it.get("appid"),
        "name": it.get("name"),
        "cnyFen": it.get("basePriceFen"),
        "discount": it.get("discount"),
        "positiveRate": it.get("positiveRate"),
        "reviewCount": it.get("reviewCount"),
    }


async def price_facts(appid: int) -> dict | None:
    """单游戏价格事实：国区现价 + 史低 + 近一年区间。无数据时返回 None。"""
    detail = await games_service.get_game_detail(appid)
    if detail is None:
        return None

    today = get_beijing_time_obj().strftime("%Y-%m-%d")
    ctx = await games_service.get_price_context(appid, today, "cn")
    history = await games_service.get_game_history(appid, region="cn", days=365)
    year_fens = sorted(
        p["cnyFen"] for p in history.get("points", []) if p.get("cnyFen", 0) > 0
    )

    cn_col = (detail.get("priceMatrix") or {}).get("CN")
    cn_fen = cn_col[1] if cn_col else None
    cn_discount = cn_col[3] if cn_col else 0
    at = ctx.get("at") or {}
    if cn_fen is None and at.get("cnyFen"):
        cn_fen = at["cnyFen"]
        cn_discount = at.get("discount") or 0

    lowest = ctx.get("lowest") or None
    year = None
    if year_fens:
        year = {
            "minFen": year_fens[0],
            "maxFen": year_fens[-1],
            "medianFen": int(statistics.median(year_fens)),
            "count": len(year_fens),
        }

    return {
        "kind": "price",
        "appid": appid,
        "name": detail.get("name"),
        "positiveRate": detail.get("positiveRate"),
        "reviewCount": detail.get("reviewCount"),
        "cn": {"cnyFen": cn_fen, "discount": cn_discount},
        "lowest": {"cnyFen": lowest["cnyFen"], "snapshotAt": lowest["snapshotAt"]} if lowest else None,
        "year": year,
    }


async def search_games(question: str) -> list[dict]:
    """按问句原文做库内名称检索（价格意图找对象用，不触发抓取）。"""
    result = await games_service.list_games(
        sort="default", limit=_FIND_LIMIT, q=(question or "").strip() or None
    )
    return [_game_item(it) for it in result.get("items", [])]


async def recommend_games(question: str) -> list[dict]:
    """推荐意图：问句里解析属性条件（打折 / 好评 / 价格上限 / 《书名号》名称），
    组合成 list_games 筛选——自然语言整句不能当名称去搜。"""
    q = (question or "").strip()
    m = _PRICE_TAG_RE.search(q)
    only_discounted = any(w in q for w in ("打折", "折扣", "特惠", "特卖", "降价"))
    min_rating = 80 if any(w in q for w in ("好评", "口碑", "评价高")) else 0
    max_price = None
    nm = _PRICE_NUM_RE.search(q)
    if nm and any(w in q for w in ("以内", "以下", "不超过", "块内", "预算")):
        max_price = int(nm.group(1)) * 100
    result = await games_service.list_games(
        sort="default",
        limit=_FIND_LIMIT,
        q=m.group(1) if m else None,
        only_discounted=only_discounted,
        min_rating=min_rating,
        max_price=max_price,
    )
    return [_game_item(it) for it in result.get("items", [])]
