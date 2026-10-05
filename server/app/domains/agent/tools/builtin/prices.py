"""价格类工具：单游戏价格事实 / 全区现价 / 并排对比 / 价格诊断。"""
from __future__ import annotations

import asyncio
import statistics

from app.domains.account import service as account_service
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.agent.tools.projection import budget_for
from app.domains.crawl import service as crawl_service
from app.domains.games import service as games_service


async def price_facts(appid: int) -> dict | None:
    """单游戏价格事实：国区现价 + 史低 + 近一年区间。无数据时返回 None。"""
    detail = await games_service.get_game_detail(appid)
    if detail is None:
        return None

    from app.crawler.utils import get_beijing_time_obj

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

    # 国区不可买（锁区/未爬）时取最低可购区：直接消费 get_game_detail 的
    # lowest 事实（全区最低已由同一处判定），折扣从价格矩阵补齐
    alt = None
    if cn_fen is None:
        low_fen = detail.get("lowestCnyFen")
        low_code = str(detail.get("lowestRegionCode") or "").upper()
        if isinstance(low_fen, int) and low_fen > 0 and low_code:
            col = (detail.get("priceMatrix") or {}).get(low_code)
            alt = {
                "region": low_code,
                "cnyFen": low_fen,
                "discount": int(col[3] or 0) if col and len(col) > 3 else 0,
            }

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
        "alt": alt,
        "lowest": {"cnyFen": lowest["cnyFen"], "snapshotAt": lowest["snapshotAt"]} if lowest else None,
        "year": year,
        "trend": _trend_series(history.get("points") or []),
    }


_TREND_MAX_POINTS = 72


def _trend_series(points: list[dict]) -> list[list]:
    """近一年国区价格走势：同日取末值、只保留价格发生变化的日点 [YYYY-MM-DD, 分]
    （阶梯图无损），超上限时等距抽样并保住最低点与末点。"""
    by_day: dict[str, int] = {}
    for p in points:
        fen = p.get("cnyFen") or 0
        ts = p.get("timestamp")
        if fen > 0 and ts:
            by_day[str(ts)[:10]] = int(fen)
    seq: list[list] = []
    for day in sorted(by_day):
        if not seq or seq[-1][1] != by_day[day]:
            seq.append([day, by_day[day]])
    if len(seq) <= _TREND_MAX_POINTS:
        return seq
    stride = len(seq) / _TREND_MAX_POINTS
    keep = {int(i * stride) for i in range(_TREND_MAX_POINTS)}
    keep.add(len(seq) - 1)
    keep.add(min(range(len(seq)), key=lambda i: seq[i][1]))
    return [seq[i] for i in sorted(keep)]


async def region_prices(appid: int) -> dict:
    """全区域现价清单：原币价/折合人民币/折扣，主账号结算区置顶。

    get_game_detail 的 priceMatrix 是唯一事实源（CN 锁区等无价区不进清单）；
    账号结算区由钱包快照派生（account.wallet_regions），小写对齐。"""
    detail = await games_service.get_game_detail(appid)
    if detail is None:
        return {"kind": "empty", "note": "no_game"}
    accounts = await account_service.list_accounts()
    primary_sid = accounts[0].get("steam_id") if accounts else ""
    wallet = await account_service.wallet_regions()
    account_region = str(wallet.get(primary_sid, "") or "").lower()
    items: list[dict] = []
    for code, col in sorted((detail.get("priceMatrix") or {}).items()):
        if not isinstance(col, list) or len(col) < 2:
            continue
        cny = col[1]
        if not isinstance(cny, int) or cny <= 0:
            continue
        items.append({
            "region": str(code).upper(),
            "display": col[0],
            "cnyFen": cny,
            "discount": int(col[3] or 0) if len(col) > 3 else 0,
        })
    items.sort(key=lambda r: (r["region"].lower() != account_region, r["region"], r["cnyFen"]))
    return {
        "kind": "region_prices",
        "appid": appid,
        "name": detail.get("name"),
        "accountRegion": account_region,
        "items": items,
        "count": len(items),
    }


_COMPARE_MAX = 3


async def compare_games(appids: list) -> dict:
    """并排对比：每款复用 price_facts 的同一份价格事实（国区不可买时取最低可购区）。"""
    ids: list[int] = []
    for raw in appids if isinstance(appids, list) else []:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in ids:
            ids.append(aid)
    if len(ids) < 2:
        return {"kind": "empty", "note": "need_two"}
    facts = await asyncio.gather(*(price_facts(a) for a in ids[:_COMPARE_MAX]))
    items: list[dict] = []
    for f in facts:
        if not f:
            continue
        cn = f.get("cn") or {}
        alt = f.get("alt") or None
        use_alt = cn.get("cnyFen") is None and alt is not None
        src = alt if use_alt else cn
        year = f.get("year") or {}
        low = f.get("lowest") or {}
        items.append({
            "appid": f["appid"],
            "name": f.get("name"),
            "positiveRate": f.get("positiveRate"),
            "reviewCount": f.get("reviewCount"),
            "cnyFen": src.get("cnyFen"),
            "discount": src.get("discount") or 0,
            "region": alt["region"] if use_alt else None,
            "lowestFen": low.get("cnyFen"),
            "medianFen": year.get("medianFen"),
        })
    if len(items) < 2:
        return {"kind": "empty", "note": "no_data"}
    return {"kind": "compare", "items": items}


async def diagnose_price(appid: int) -> dict:
    """价格异常归因：现价 + 覆盖概况 + 问题区明细（结果复用游戏卡同一覆盖口径）。"""
    if appid <= 0:
        failures = await crawl_service.list_jobs(10)
        failed = [j for j in failures if j.get("status") == "failed"]
        if failed:
            rows = [
                {"k": f"#{j.get('id')} {j.get('kind') or ''}".strip(),
                 "v": str(j.get("error") or "")[:80],
                 "at": j.get("startedAt"),
                 "tone": "bad"}
                for j in failed[:5]
            ]
            return _shared.rows_result("jobs", rows, total=len(failed))
        return {"kind": "empty", "note": "need_appid"}
    diag = await games_service.price_diagnosis(appid)
    if diag is None:
        return {"kind": "empty", "note": "no_data"}
    cov = diag.get("coverage") or {}
    problems = cov.get("regions") or {}
    name = diag.get("name") or f"AppID {appid}"
    rows: list[dict] = []
    if cov:
        rows.append({
            "k": name,
            "vKey": "coverage",
            "data": {"ok": cov.get("success", 0), "total": cov.get("expectedUnits", 0)},
            "tone": "ok" if not problems else "warn",
        })
    for code, info in list(problems.items())[:_shared._READ_ROWS_LIMIT]:
        rows.append({
            "k": code,
            "vKey": str(info.get("outcome") or "failed"),
            "v": str(info.get("answer") or ""),
            "at": info.get("lastSuccessAt"),
            "tone": "bad" if info.get("outcome") == "failed" else "warn",
        })
    if not rows:
        rows.append({"k": name, "vKey": "noCoverage", "tone": "warn"})
    return {
        "kind": "rows",
        "titleKey": "diagnosis",
        "rows": rows,
        "appid": appid,
        "name": name,
        "cn": diag.get("cn"),
        "lowestPriceFen": diag.get("lowestPriceFen"),
    }


async def _get_price_briefing(args: dict, sid: str | None = None) -> dict:
    facts = await price_facts(int(args.get("appid") or 0))
    return facts or {"kind": "empty", "note": "no_data"}


async def _get_region_prices(args: dict, sid: str | None = None) -> dict:
    return await region_prices(int(args.get("appid") or 0))


async def _compare_games(args: dict, sid: str | None = None) -> dict:
    return await compare_games(args.get("appids") or [])


async def _diagnose_price(args: dict, sid: str | None = None) -> dict:
    return await diagnose_price(int(args.get("appid") or 0))


SPECS = [
    ToolSpec(
        name="get_price_briefing", group="read", risk="low",
        description="取某游戏的价格事实：国区现价、历史最低、近一年区间",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_get_price_briefing, step_label="price",
    ),
    ToolSpec(
        name="get_region_prices", group="read", risk="low",
        description="取某游戏在全部已爬区域的现价：原币价、折合人民币、折扣，主账号结算区排最前。"
                    "用户问『XX 在我账号的地区/某区多少钱』『哪个区最便宜』或质疑回答里的地区不对时调用；"
                    "国区锁区的游戏也能查到其他区的价格",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_get_region_prices, step_label="regionPrices",
    ),
    ToolSpec(
        name="compare_games", group="read", risk="low",
        description="并排对比 2~3 款游戏的现价、折扣、史低、近一年中位价与好评率。"
                    "用户问『A 和 B 买哪个』『这几款哪个更划算』时调用；appids 先用检索工具确认",
        parameters={"type": "object", "properties": {
            "appids": {"type": "array", "items": {"type": "integer"},
                       "minItems": 2, "maxItems": 3, "description": "待对比游戏的 AppID（2~3 个）"},
        }, "required": ["appids"]},
        handler=_compare_games, step_label="compare",
    ),
    ToolSpec(
        name="diagnose_price", group="read", risk="low",
        description="诊断某游戏的价格数据状态：各区最近抓取结果（成功/失败/未抓取）、上次成功时间、覆盖率。"
                    "用户问『为什么价格没更新』『数据是不是旧的』时调用；appid 可先用检索工具确认",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_diagnose_price, step_label="diagnose",
    ),
]
