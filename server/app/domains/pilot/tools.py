"""pilot 只读工具：复用 games 域服务取事实，不自建查询、不写库。

产出紧凑事实（几百字节级）供模板摘要与 LLM 解读共用；全量数据仍由
既有接口承载，领航台不重复暴露完整列表。
"""
from __future__ import annotations

import re
import statistics

from app.crawler.utils import get_beijing_time_obj
from app.domains.alerts import service as alerts_service
from app.domains.games import service as games_service
from app.domains.monitoring import service as monitoring_service

_FIND_LIMIT = 5
_PRICE_TAG_RE = re.compile(r"《(.+?)》")
_PRICE_NUM_RE = re.compile(r"(\d+)\s*(?:块|元)")
_THRESH_RE = re.compile(r"(?:低于|以下|跌到|跌至|降至)\s*(\d+(?:\.\d+)?)")
# 写意图对象解析：剥掉意图/条件词后的 token 逐个试搜（整句 LIKE 必然零命中）
_QUERY_CLEAN_RE = re.compile(
    r"把|将|帮我|请|加进关注|加入关注|关注一下|关注|低于|以下|跌到|跌至|降至|史低"
    r"|打折|降价|提醒|告诉我|一声|到价|块|元|了|吗|呢|就|时"
)


def clean_query_tokens(question: str) -> list[str]:
    """问句清洗后的搜索词：先整句清洗，再拆 token（滤掉纯数字与单字）。"""
    cleaned = _QUERY_CLEAN_RE.sub(" ", question or "").strip()
    if not cleaned:
        return []
    tokens = [t for t in cleaned.split() if len(t) >= 2 and not t.isdigit()]
    return [cleaned] + [t for t in tokens if t != cleaned]


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


async def search_games(question: str, query: str | None = None) -> list[dict]:
    """按问句或显式词做库内名称检索（价格意图找对象 / 写意图解析对象用，
    不触发抓取）。"""
    result = await games_service.list_games(
        sort="default", limit=_FIND_LIMIT, q=(query or question or "").strip() or None
    )
    return [_game_item(it) for it in result.get("items", [])]


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
    """按结构化条件挑库内游戏（agent 工具与回退路径共用）。"""
    result = await games_service.list_games(
        sort="default",
        limit=_FIND_LIMIT,
        q=(q or "").strip() or None,
        only_discounted=only_discounted,
        min_rating=min_rating,
        max_price=int(max_price_yuan * 100) if max_price_yuan else None,
    )
    return [_game_item(it) for it in result.get("items", [])]


def tool_specs() -> list[dict]:
    """agent 循环的工具表（OpenAI function 格式）。

    写工具只有加关注 / 设提醒两个单对象可逆动作——白名单即风险门；
    批量 / 删除 / 停用类操作不设工具，模型无从调用。"""
    return [
        {"type": "function", "function": {
            "name": "search_games",
            "description": "在用户游戏库内按名称检索游戏，返回候选（含 appid、现价、折扣、好评率）",
            "parameters": {"type": "object", "properties": {
                "q": {"type": "string", "description": "游戏名称关键词"},
            }, "required": ["q"]},
        }},
        {"type": "function", "function": {
            "name": "get_price_briefing",
            "description": "取某游戏的价格事实：国区现价、历史最低、近一年区间",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "recommend_games",
            "description": "按条件从用户库内挑游戏，可组合：打折中 / 好评率下限 / 价格上限（元）",
            "parameters": {"type": "object", "properties": {
                "only_discounted": {"type": "boolean", "description": "只看打折中的游戏"},
                "min_rating": {"type": "integer", "description": "好评率下限（0-100）"},
                "max_price_yuan": {"type": "number", "description": "现价上限（人民币元）"},
            }},
        }},
        {"type": "function", "function": {
            "name": "add_follow",
            "description": "把某游戏加入用户关注（持续追踪价格）。仅当用户明确要求关注该游戏时调用",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "create_price_alert",
            "description": "为中国区创建价格提醒。仅当用户明确要求提醒时调用；用户给出具体价格用 price 类型，用户说史低提醒用 historic_low 类型",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
                "target_type": {"type": "string", "enum": ["price", "historic_low"]},
                "target_value_yuan": {"type": "number", "description": "price 类必填，人民币元"},
            }, "required": ["appid", "target_type"]},
        }},
    ]


async def execute_tool(name: str, arguments: dict, *, guarded: bool = False) -> dict:
    """工具执行分发。写工具受守卫复核：问句含批量 / 删除 / 停用语义时
    拒绝执行（返回 kind=denied，由模型向用户解释）。"""
    if name == "search_games":
        items = await search_games(str(arguments.get("q") or ""), query=str(arguments.get("q") or ""))
        return {"kind": "games", "items": items}
    if name == "get_price_briefing":
        facts = await price_facts(int(arguments.get("appid") or 0))
        return facts or {"kind": "empty", "note": "no_data"}
    if name == "recommend_games":
        items = await recommend_games_by_filters(
            only_discounted=bool(arguments.get("only_discounted")),
            min_rating=int(arguments.get("min_rating") or 0),
            max_price_yuan=arguments.get("max_price_yuan"),
        )
        return {"kind": "games", "items": items}
    if name == "add_follow":
        if guarded:
            return {"kind": "denied", "note": "guarded"}
        return await monitor_add(int(arguments.get("appid") or 0))
    if name == "create_price_alert":
        if guarded:
            return {"kind": "denied", "note": "guarded"}
        ttype = arguments.get("target_type") or "price"
        yuan = arguments.get("target_value_yuan")
        fen = int(float(yuan) * 100) if yuan is not None else None
        return await alert_add(int(arguments.get("appid") or 0), target_type=ttype, target_value_fen=fen)
    return {"kind": "unknown_tool"}


def extract_title(question: str) -> str | None:
    """问句里《书名号》中的游戏名（写意图解析对象的优先词）。"""
    m = _PRICE_TAG_RE.search(question or "")
    return m.group(1) if m else None


def extract_alert(question: str) -> tuple[str, float | None]:
    """解析提醒条件。返回 (target_type, target_value_fen)：史低类无值；
    价格类按「低于 N 块/元」取 N×100 分；解析不出数值返回 price + None
    （调用方转指引，不臆造阈值）。"""
    q = question or ""
    if "史低" in q:
        return "historic_low", None
    m = _THRESH_RE.search(q)
    if m:
        return "price", int(float(m.group(1)) * 100)
    return "price", None


async def monitor_add(appid: int) -> dict:
    """关注游戏：1:1 映射 monitoring.track（manual 来源 = 用户手动加入）。"""
    state = await monitoring_service.track("game", appid, "manual")
    detail = await games_service.get_game_detail(appid)
    return {
        "action": "monitor_add",
        "appid": appid,
        "name": detail.get("name") if detail else None,
        "state": state,
    }


async def alert_add(appid: int, *, target_type: str, target_value_fen: float | None) -> dict:
    """设价格提醒：1:1 映射 alerts_service.add_alert（中国区，price=分 / historic_low）。"""
    alert = await alerts_service.add_alert(appid, "CN", target_type, target_value_fen)
    detail = await games_service.get_game_detail(appid)
    return {
        "action": "alert_add",
        "appid": appid,
        "name": detail.get("name") if detail else None,
        "targetType": target_type,
        "targetValueFen": target_value_fen,
        "alertId": alert.get("id") if alert else None,
    }
