"""builtin 公共结果形状与常量（自 pilot/tools.py 逐字收编）。"""
from __future__ import annotations

import re

_FIND_LIMIT = 5
_READ_ROWS_LIMIT = 8
# 批量提议上限：单次可提交确认的款数（防一次吞掉整个库）
_BULK_MAX_ITEMS = 50
# 可批量化的动作：与写工具白名单同源，不新增动作种类
_BULK_ACTIONS = ("add_follow", "create_price_alert")

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


def pilot_store():
    """会话账本入口（延迟导入：pilot service 依赖工具层，反向取须函数内进行）。"""
    from app.domains.pilot import service as pilot_service

    return pilot_service.get_store()


def rows_result(title: str, rows: list[dict], *, total: int | None = None) -> dict:
    out: dict = {"kind": "rows", "titleKey": title, "rows": rows[:_READ_ROWS_LIMIT]}
    if total is not None:
        out["total"] = total
    return out


def games_card(title: str, items: list[dict], *, total: int | None = None) -> dict:
    """游戏清单卡（找游戏同款富卡：封面/价格/折扣/好评）；行内注记词条化。"""
    out: dict = {"kind": "games", "titleKey": title, "items": items[:_READ_ROWS_LIMIT]}
    if total is not None:
        out["total"] = total
    return out


def brief_item(appid: int, brief: dict | None, *, note: dict | None = None) -> dict:
    """briefs_for 行 → 清单卡 item（缺行降级为纯 appid，不丢对象）。"""
    b = brief or {}
    return {
        "appid": appid,
        "name": b.get("name"),
        "cnyFen": b.get("cnyFen"),
        "discount": b.get("discount") or 0,
        "positiveRate": b.get("positiveRate"),
        "reviewCount": b.get("reviewCount"),
        "chineseSupport": b.get("chineseSupport") or b.get("chinese_support"),
        **({"note": note} if note else {}),
    }


def offer_row(name: str, *, price_fen=None, low_fen=None, extra_key=None, extra_val=None) -> dict:
    data: dict = {}
    has_price = isinstance(price_fen, int) and price_fen > 0
    has_low = isinstance(low_fen, int) and low_fen > 0
    if has_price:
        data["priceFen"] = price_fen
    if has_low:
        data["lowFen"] = low_fen
    default_key = "offerLow" if has_price and has_low else "offerPrice"
    row: dict = {"k": name, "vKey": extra_key or default_key, "data": data}
    if extra_key and extra_val is not None:
        row["v"] = str(extra_val)
    return row


def sync_result(title: str, result: dict, *, done_key: str = "syncDone", fail_key: str = "syncFailed") -> dict:
    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    detail = ""
    if isinstance(result, dict):
        detail = str(result.get("error") or result.get("detail") or "")
    return rows_result(title, [{
        "k": "", "vKey": done_key if ok else fail_key, "v": detail,
        "tone": "ok" if ok else "bad",
    }])
