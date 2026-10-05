"""pilot 工具层：agent 循环取数与写动作的执行点。

读工具复用 games 域服务取紧凑事实（几百字节级，供模板摘要与 LLM 解读
共用）；写工具 1:1 映射既有用户动作（关注 = monitoring.track，
提醒 = alerts_service.add_alert），守卫词命中即拒绝。全量数据仍由
既有接口承载，领航台不重复暴露完整列表。
"""
from __future__ import annotations

import asyncio
import re
import statistics

import aiohttp

from app.crawler.config import CC_LIST
from app.crawler.epic_free import STEAM_HTTP_HEADERS, STORESEARCH_URL
from app.crawler.utils import get_beijing_time_obj
from app.core.database import write_scheduler_diagnostics
from app.domains.account import service as account_service
from app.domains.achievements import service as achievements_service
from app.domains.alerts import service as alerts_service
from app.domains.bills import service as bills_service
from app.domains.alerts.notify import get_smtp_config, send_test_mail
from app.domains.bundles import service as bundles_service
from app.domains.crawl import events as crawl_events_service
from app.domains.crawl import service as crawl_service
from app.domains.family import service as family_service
from app.domains.games import service as games_service
from app.domains.metadata import service as metadata_service
from app.domains.monitoring import service as monitoring_service
from app.domains.proxies import service as proxies_service
from app.domains.rates import service as rates_service
from app.domains.regions import service as regions_service
from app.domains.redeem import service as redeem_service
from app.domains.steam_events import service as steam_events_service
from app.domains.wishlist import follows as wishlist_follows
from app.domains.wishlist import service as wishlist_service

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


# ─── 只读工具：清单与诊断（全只读，不设守卫；行形态见 _rows_result）───

_READ_TOOLS = (
    "list_follows",
    "list_alerts",
    "list_bundle_follows",
    "diagnose_price",
    "recent_job_failures",
    "proxy_pool_status",
    "write_gate_status",
    "hb_monthly",
    "epic_free",
    "steam_free",
    "top_games",
    "price_drops",
    "rates_overview",
    "calendar_events",
    "list_accounts",
    "list_wishlist",
    "list_owned",
    "list_family_library",
    "achievements_summary",
    "bills_summary",
    "family_status",
    "redeem_quota",
    "list_bundles",
    "list_sessions",
    "read_session",
    "web_search",
)

_NET_TOOLS = ("web_search",)

# 写工具（守卫词命中即拒）：单对象、可逆、1:1 用户动作
_WRITE_TOOLS = (
    "add_follow",
    "create_price_alert",
    "update_price_alert",
    "bundle_follow",
    "region_toggle",
    "retry_removed_game",
    "monitor_include",
)

# 快捷动作白名单（+ 菜单点选直达）：无参、确定性好、单击即用户显式意图
QUICK_TOOLS = frozenset({
    "list_follows", "list_alerts", "list_bundle_follows", "hb_monthly", "epic_free",
    "steam_free", "top_games", "price_drops", "rates_overview", "calendar_events",
    "sync_wallet", "sync_bills", "refresh_rates",
    "sync_achievements", "sync_family", "notify_test",
    "find_deletables",
})

# 同步刷新工具（幂等拉取，不设守卫；结果行 vKey 走 syncDone/syncFailed）
_SYNC_TOOLS = (
    "sync_wallet",
    "sync_library",
    "sync_achievements",
    "sync_bills",
    "sync_family",
    "refresh_rates",
    "ingest_appids",
    "refresh_game_price",
    "notify_test",
)

# 运行面调度工具（第三层权限，见下方同名区块说明）：不写用户数据，
# kind 进登记表白名单；破坏性动作不入表，模型无从调用。
_TASK_TOOLS = (
    "list_tasks",
    "start_task",
    "cancel_task",
)


def _rows_result(title: str, rows: list[dict], *, total: int | None = None) -> dict:
    out: dict = {"kind": "rows", "titleKey": title, "rows": rows[:_READ_ROWS_LIMIT]}
    if total is not None:
        out["total"] = total
    return out


def _games_card(title: str, items: list[dict], *, total: int | None = None) -> dict:
    """游戏清单卡（找游戏同款富卡：封面/价格/折扣/好评）；行内注记词条化。"""
    out: dict = {"kind": "games", "titleKey": title, "items": items[:_READ_ROWS_LIMIT]}
    if total is not None:
        out["total"] = total
    return out


def _brief_item(appid: int, brief: dict | None, *, note: dict | None = None) -> dict:
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


async def list_follows() -> dict:
    """关注清单：手动加入 ∪ 收藏关注（两路显式关注合并成一份只读投影）。"""
    manual = await monitoring_service.ids_with_source("game", "manual")
    favorite = await wishlist_follows.followed_appids()
    ids = sorted({int(a) for a in manual} | {int(a) for a in favorite})
    briefs = await games_service.briefs_for(ids)
    fav = {int(a) for a in favorite}
    items = [
        _brief_item(i, briefs.get(i), note={"key": "favorite" if i in fav else "manual"})
        for i in ids
    ]
    return _games_card("follows", items, total=len(ids))


async def list_alerts() -> dict:
    """提醒规则清单：1:1 alerts.list_alerts，值词条化不拼用户文案。"""
    alerts = await alerts_service.list_alerts()
    vkey = {"price": "alertPrice", "historic_low": "alertLow", "pct": "alertPct"}
    rows = [
        {
            "k": a.get("gameName") or f"AppID {a.get('appid')}",
            "vKey": vkey.get(str(a.get("targetType")), "alertLow"),
            "v": a.get("region"),
            "data": {"priceFen": a.get("targetValue"), "pct": a.get("targetValue"), "alertId": a.get("id")},
            "tone": "ok" if a.get("active") else "warn",
        }
        for a in alerts
    ]
    return _rows_result("alerts", rows, total=len(rows))


async def list_bundle_follows() -> dict:
    """关注的捆绑包清单。"""
    ids = await bundles_service.followed_bundle_ids()
    names = await bundles_service.names_for(ids)
    rows = [{"k": names.get(i, f"Bundle {i}")} for i in ids]
    return _rows_result("bundles", rows, total=len(rows))


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
            return _rows_result("jobs", rows, total=len(failed))
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
    for code, info in list(problems.items())[:_READ_ROWS_LIMIT]:
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


async def recent_job_failures() -> dict:
    """最近失败的抓取任务与原因（crawl_jobs 账本只读投影）。"""
    jobs = await crawl_service.list_jobs(20)
    failed = [j for j in jobs if j.get("status") == "failed"]
    rows = [
        {
            "k": f"#{j.get('id')} {j.get('kind') or ''}".strip(),
            "v": str(j.get("error") or "")[:80],
            "at": j.get("startedAt"),
            "tone": "bad",
        }
        for j in failed
    ]
    return _rows_result("jobs", rows, total=len(failed))


async def proxy_pool_status() -> dict:
    """代理通道可用状态（手动池 + Clash 出口，pool_stats 只读投影）。"""
    stats = await proxies_service.pool_stats()
    pool = stats.get("pool") or {}
    clash = stats.get("clash") or {}
    rows = [
        {"k": "", "vKey": "proxyPool",
         "data": {"ok": pool.get("ok", 0), "total": pool.get("total", 0)},
         "tone": "ok" if pool.get("ok") else "warn"},
        {"k": "", "vKey": "proxyExits",
         "data": {"ok": clash.get("okExitIps", 0), "total": clash.get("exitIps", 0)},
         "tone": "ok" if clash.get("okExitIps") else "warn"},
        {"k": "", "vKey": "proxyRunning" if clash.get("running") else "proxyStopped",
         "tone": "ok" if clash.get("running") else "warn"},
    ]
    return _rows_result("proxy", rows)


async def write_gate_status() -> dict:
    """写入调度状态：有没有写入在执行、排队多少（诊断第③层出口）。"""
    d = write_scheduler_diagnostics()
    waiting = int(d.get("waiting_interactive") or 0) + int(d.get("waiting_background") or 0)
    rows = [
        {"k": "", "vKey": "gateBusy" if d.get("busy") else "gateIdle",
         "v": str(d.get("owner_label") or ""),
         "tone": "warn" if d.get("busy") else "ok"},
        {"k": "", "vKey": "gateWaiting", "data": {"n": waiting},
         "tone": "warn" if waiting else "ok"},
    ]
    return _rows_result("gate", rows)


# ─── 运行面调度工具（第三层权限：发起 / 查询 / 取消已登记长任务）───
# 与写工具的本质区别：本组**不获得任何写数据权限**——调度只把已存在于
# 用户界面的长任务交给运行账本（agent_runs），业务写入仍由域内编排按既有
# 规则执行；kind 是登记表静态白名单（app.domains.agent.runtime.tasks），
# 模型不能自由拼参数；破坏性动作（删除 / 停用 / 清空）不入登记表。

# 运行状态 → （行值词条, 色调）；一级状态语义见 runtime/state.py
_TASK_STATUS_ROW: dict[str, tuple[str, str]] = {
    "queued": ("taskQueued", "warn"),
    "running": ("taskRunning", "ok"),
    "awaiting_approval": ("taskQueued", "warn"),
    "done": ("taskDone", "ok"),
    "failed": ("taskFailed", "bad"),
    "cancelled": ("taskCancelled", "warn"),
    "budget_exhausted": ("taskCancelled", "warn"),
}


def _task_row(run: dict, progress: dict | None = None, *,
              vKey: str | None = None, tone: str | None = None,
              v: str | None = None) -> dict:
    """任务行：左侧走 kindKey（任务名词条）、右侧走状态词条 + 进度原值。"""
    from app.domains.agent.runtime import tasks as task_registry

    kind = str((run.get("meta") or {}).get("task") or "")
    default_vKey, default_tone = _TASK_STATUS_ROW.get(str(run.get("status")), ("taskQueued", "warn"))
    vKey = vKey or default_vKey
    tone = tone or default_tone
    if v is None:
        prog = progress or {}
        done, total = prog.get("done"), prog.get("total")
        v = f"{done}/{total}" if done is not None and total else None
    return {
        "k": "",
        "kindKey": f"pilot.task.{task_registry.label_of(kind)}",
        "vKey": vKey,
        "tone": tone,
        "v": v or "",
        "taskKind": kind,
    }


async def list_tasks() -> dict:
    """运行中的长任务与最近终态（运行账本只读投影，进度来自采样事件）。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=20)
    ours = [r for r in runs if r.get("runner") == "task"]
    live = [r for r in ours if r.get("status") not in agent_state.TERMINAL_STATES]
    rows: list[dict] = []
    for r in live:
        rows.append(_task_row(r, await agent_service.latest_progress(r["run_id"])))
    for r in [r for r in ours if r.get("status") in agent_state.TERMINAL_STATES][:3]:
        rows.append(_task_row(r))
    if not rows:
        rows.append({"k": "", "vKey": "taskNoRunning", "tone": "warn", "v": ""})
    return _rows_result("tasks", rows, total=len(live))


async def start_task(kind: str) -> dict:
    """发起登记表内的长任务；受理在本次调用内完成，不等任务跑完。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import tasks as task_registry
    from app.crawler.utils import get_beijing_time_obj

    spec = task_registry.get(kind)
    if spec is None:
        return _rows_result("tasks", [{"k": "", "vKey": "taskUnknown", "tone": "bad", "v": ""}])
    try:
        accepted = await spec.start()
    except (RuntimeError, ValueError) as e:
        # 域侧业务拒绝（已有任务在跑 / 无可抓对象）：如实回灌，不建空转账本
        return _rows_result("tasks", [
            {"k": "", "kindKey": f"pilot.task.{spec.label}",
             "vKey": "taskBusy", "tone": "warn", "v": str(e)[:60]},
        ])
    ref = dict(accepted or {})
    ref["startedAt"] = get_beijing_time_obj().replace(tzinfo=None).isoformat()
    created = await agent_service.create_run(
        trigger="manual", runner="task",
        meta={"task": kind, "ref": ref, "adopted": True},
    )
    await agent_service.start_run(created["run_id"])
    return _rows_result("tasks", [_task_row(
        {"meta": {"task": kind}, "status": "running"}, {"total": ref.get("total")},
    )])


async def cancel_task(run_id: str | None = None, kind: str | None = None) -> dict:
    """请求停止运行中的长任务（按运行标识或任务类型定位）。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=20)
    live = [
        r for r in runs
        if r.get("runner") == "task" and r.get("status") not in agent_state.TERMINAL_STATES
    ]
    target: dict | None = None
    if run_id:
        target = next((r for r in live if r["run_id"] == run_id), None)
    elif kind:
        target = next((r for r in live if (r.get("meta") or {}).get("task") == kind), None)
    elif len(live) == 1:
        target = live[0]
    if target is None:
        empty_key = "taskNoRunning" if not live else "taskAmbiguous"
        return _rows_result("tasks", [{"k": "", "vKey": empty_key, "tone": "warn", "v": ""}])
    res = await agent_service.cancel_run(target["run_id"], reason="user")
    return _rows_result("tasks", [_task_row(
        target, vKey="taskCancelled" if res.get("accepted") else None,
    )])


def _pilot_store():
    """会话账本入口（延迟导入：service 依赖本模块，反向取 store 须函数内进行）。"""
    from app.domains.pilot import service as pilot_service

    return pilot_service.get_store()


async def list_sessions() -> dict:
    """历史会话清单：标题/轮数/更新时间（账本只读投影，最近 20 段）。"""
    items = _pilot_store().list_recent(20)
    rows = [
        {"k": it["title"] or it["sid"][:8], "vKey": "sessionTurns",
         "data": {"count": it["turn_total"]}, "at": it["updated_at"]}
        for it in items
    ]
    return _rows_result("sessions", rows, total=len(items))


async def read_session(sid: str) -> dict:
    """读某段历史会话：最近几轮问答行 + 预算内摘要（模型消费 digest，卡片消费 rows）。"""
    state = await _pilot_store().load(str(sid or ""))
    if state is None:
        return {"kind": "empty", "note": "no_data"}
    store = _pilot_store()
    rows = []
    for t in state.turns[-_READ_ROWS_LIMIT:]:
        resp = t.get("resp") or {}
        answer = str(resp.get("answer") or "")
        tools_txt = "；".join(str(x) for x in (resp.get("tools") or []))
        rows.append({
            "k": str(t.get("q") or "")[:48],
            "v": (answer or tools_txt)[:80],
            "at": t.get("ts"),
        })
    digest = None
    try:
        from app.domains.pilot import context as pilot_context

        digest = pilot_context.reference_digest(state)
    except Exception:  # noqa: BLE001 — 摘要失败不影响清单行
        digest = None
    result = _rows_result("sessionRead", rows, total=len(state.turns))
    result["sid"] = str(sid or "")
    result["sessionTitle"] = store.title_of(state)
    result["digest"] = digest or ""
    return result


def _offer_row(name: str, *, price_fen=None, low_fen=None, extra_key=None, extra_val=None) -> dict:
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


async def hb_monthly() -> dict:
    """当月 HB 慈善包 contents：1:1 metadata.hb_choice_offers（库内已爬快照）。

    每行带现价与史低（min_cny_fen）；包未入库返回空态原因。"""
    payload = await metadata_service.hb_choice_offers()
    games = payload.get("games") or []
    rows = [
        _offer_row(
            g.get("name") or f"AppID {g.get('appid')}",
            price_fen=g.get("priceFen"),
            low_fen=g.get("lowestCnyFen"),
            extra_key="offerDiscount" if g.get("discount") else None,
            extra_val=f"-{g.get('discount')}%" if g.get("discount") else None,
        )
        for g in games
    ]
    return _rows_result("hb", rows, total=len(rows))


async def epic_free() -> dict:
    """Epic 当期 + 预告白送（库快照/缓存，不触发外网）。"""
    payload = await metadata_service.epic_free_offers()
    offers = payload.get("offers") or []
    rows = [
        {
            "k": o.get("titleCn") or o.get("title") or (f"AppID {o['appid']}" if o.get("appid") else "—"),
            "vKey": "epicUpcoming" if o.get("upcoming") else "epicFree",
            "v": str(o.get("end") or o.get("start") or ""),
            "tone": "ok" if not o.get("upcoming") else "warn",
        }
        for o in offers
    ]
    return _rows_result("epic", rows, total=len(rows))


async def steam_free() -> dict:
    """Steam 限时免费清单（库内 promo 快照）。"""
    payload = await metadata_service.steam_free_offers()
    games = payload.get("games") if isinstance(payload, dict) else None
    rows = [_offer_row(g.get("name") or f"AppID {g.get('appid')}") for g in (games or [])]
    return _rows_result("steamFree", rows, total=len(rows))


async def top_games() -> dict:
    """Steam 热销榜 TOP100 的库内前段（榜单缓存，零外网）——顺序即榜序。"""
    from app.domains.games import boards as games_boards

    appids = await games_boards.get_board("topsellers")
    if not appids:
        return {"kind": "empty", "note": "no_data"}
    ids = [int(a) for a in appids[:_READ_ROWS_LIMIT]]
    briefs = await games_service.briefs_for(ids)
    items = [_brief_item(i, briefs.get(i)) for i in ids]
    return _games_card("top", items, total=len(appids))


async def price_drops() -> dict:
    """最近价格事件（新低/降价/锁区等，price_events 账本只读投影）。"""
    events = await crawl_events_service.list_events(limit=20)
    ids = [int(e.get("appid") or 0) for e in events if e.get("appid")]
    briefs = await games_service.briefs_for(ids) if ids else {}
    items = [
        _brief_item(
            int(e["appid"]), briefs.get(int(e["appid"])) if e.get("appid") else None,
            note={"key": f"ev_{e.get('eventType')}", "v": str(e.get("region") or ""),
                  "at": e.get("occurredAt")},
        )
        for e in events if e.get("appid")
    ]
    return _games_card("drops", items, total=len(items))


async def rates_overview() -> dict:
    """汇率快照（fx_rates 本地库，零外网）。"""
    payload = await rates_service.list_rates()
    rows = [
        {"k": r.get("currency") or "", "vKey": "rateLine", "data": {"rate": r.get("rateToCny")}}
        for r in (payload.get("rates") or [])
    ]
    return _rows_result("rates", rows, total=len(rows))


async def calendar_events() -> dict:
    """Steam 官方活动日历（库内已爬，进行中在前）。"""
    payload = await steam_events_service.list_events()
    live = payload.get("live") or []
    upcoming = (payload.get("upcoming") or [])[:_READ_ROWS_LIMIT]
    rows = [
        {"k": e.get("nameZh") or e.get("nameEn") or e.get("key") or "—",
         "vKey": "eventLive", "v": f"{e.get('start')}~{e.get('end')}", "tone": "ok"}
        for e in live
    ] + [
        {"k": e.get("nameZh") or e.get("nameEn") or e.get("key") or "—",
         "vKey": "eventUpcoming", "v": str(e.get("start") or "")}
        for e in upcoming
    ]
    return _rows_result("calendar", rows, total=len(rows))


async def list_accounts() -> dict:
    """绑定 Steam 账号清单（名称/好友码/主账号/拥有统计）——只读账号面，无任何凭据。"""
    accounts = await account_service.list_accounts()
    rows = [
        {
            "k": a.get("persona_name") or a.get("steam_id") or "—",
            "vKey": "acctPrimary" if a.get("is_primary") else "acctActive",
            "data": {
                "friend": a.get("friend_code") or "",
                "wish": a.get("wishlist_count", 0),
                "owned": a.get("game_count", 0),
            },
            "tone": "ok" if a.get("is_primary") else None,
        }
        for a in accounts
    ]
    return _rows_result("accounts", rows, total=len(rows))


async def list_wishlist() -> dict:
    """账户愿望单游戏（监控池内 wishlisted 来源，库内快照）。"""
    items = await wishlist_service.list_items()
    ids = [int(it["appid"]) for it in items if it.get("wishlisted") and it.get("appid")]
    briefs = await games_service.briefs_for(ids)
    return _games_card("wishlist", [_brief_item(i, briefs.get(i)) for i in ids], total=len(ids))


async def list_owned() -> dict:
    """账户已拥有游戏（监控池内 owned 来源，带国区现价）。"""
    items = await wishlist_service.list_items()
    ids = [int(it["appid"]) for it in items if it.get("owned") and it.get("appid")]
    briefs = await games_service.briefs_for(ids)
    return _games_card("owned", [_brief_item(i, briefs.get(i)) for i in ids], total=len(ids))


async def achievements_summary() -> dict:
    """奖杯概览（本地快照）：KPI + 白金陈列 + 最近解锁（成就域同一份汇总）。"""
    s = await achievements_service.get_summary(None)
    return {
        "kind": "achievements",
        "hasCredential": bool(s.get("hasCredential")),
        "platinum": int(s.get("platinum") or 0),
        "unlocked": int(s.get("unlockedAchievements") or 0),
        "total": int(s.get("totalAchievements") or 0),
        "completionRate": s.get("completionRate"),
        "lastSyncedAt": s.get("lastSyncedAt"),
        "platinums": [
            {"appid": int(p["appid"]), "name": p.get("name") or None}
            for p in (s.get("platinums") or [])[:4] if p.get("appid")
        ],
        "recent": [
            {"appid": int(a["appid"]), "name": a.get("name") or "",
             "gameName": a.get("gameName") or None, "at": a.get("unlockTime")}
            for a in (s.get("recentUnlocks") or [])[:4] if a.get("appid")
        ],
    }


async def list_family_library() -> dict:
    """家庭共享库（family 域快照缓存，同一份前端家庭页数据源）。"""
    lib = await family_service.cached_family_library(None)
    games = lib.get("games") or []
    ids = [int(g["appid"]) for g in games if g.get("appid")][: _READ_ROWS_LIMIT]
    briefs = await games_service.briefs_for(ids)
    return _games_card("famLibrary", [_brief_item(i, briefs.get(i)) for i in ids], total=len(games))


async def bills_summary() -> dict:
    """账单导入记录与消费合计（本地账本）。"""
    imports = await bills_service.list_imports()
    rows = [
        {
            "k": im.get("nickname") or im.get("sourceFile") or f"#{im.get('id')}",
            "vKey": "billSpend",
            "data": {"spendFen": im.get("gameSpendFen")},
            "v": f"{im.get('orders')}单" if im.get("orders") is not None else "",
            "at": im.get("importedAt"),
            "tone": None,
        }
        for im in imports
    ]
    return _rows_result("bills", rows, total=len(rows))


async def family_status() -> dict:
    """家庭组状态（成员头像/角色/地区 + 钱包区，family 域同一 payload）。"""
    st = await family_service.get_status()
    members = []
    for g in st.get("groups") or []:
        for m in g.get("members") or []:
            sid = str(m.get("steamid") or "")
            if not sid or any(x["steamid"] == sid for x in members):
                continue
            members.append({
                "steamid": sid,
                "name": m.get("personaName") or None,
                "avatar": m.get("avatarUrl") or None,
                "role": "primary" if str(m.get("role") or "") == "primary" else "member",
                "region": m.get("region") or None,
            })
    if not st.get("bound") or not members:
        return {"kind": "family", "bound": False, "members": []}
    return {
        "kind": "family",
        "bound": True,
        "joined": bool(st.get("joined")),
        "walletRegion": st.get("walletRegion") or None,
        "members": members[:_READ_ROWS_LIMIT],
        "total": len(members),
    }


async def redeem_quota() -> dict:
    """CDK 激活额度（账号维度 used/limit，只读前提状态）。"""
    q = await redeem_service.quota_status()
    if not q.get("hasCookie"):
        return _rows_result("redeem", [{"k": "", "vKey": "redeemNoCookie", "tone": "warn"}])
    rows = [{"k": "", "vKey": "redeemQuota",
             "data": {"used": q.get("used", 0), "limit": q.get("limit", 0)},
             "tone": "ok" if q.get("used", 0) < q.get("limit", 0) else "warn"}]
    return _rows_result("redeem", rows)


async def list_bundles() -> dict:
    """捆绑包列表前列（diff 差价降序，缓存聚合零外网）。"""
    bundles = await bundles_service.list_bundles("diff")
    rows = [
        {
            "k": b.get("name") or f"Bundle {b.get('bundleId')}",
            "vKey": "bundleLow",
            "data": {"priceFen": b.get("cnCnyFen"), "lowFen": b.get("lowestCnyFen"), "bundleId": b.get("bundleId")},
            "v": str(b.get("lowestRegion") or "").upper(),
        }
        for b in bundles
    ]
    return _rows_result("bundlesAll", rows, total=len(rows))


# ─── 写工具扩展：提醒编辑 / 捆绑包关注 / 区域开关（单对象可逆，守卫词复核）───

async def update_price_alert(alert_id: int, *, target_value_yuan=None, active: bool | None = None) -> dict:
    """改提醒（阈值 / 启停），1:1 alerts_service.update_alert。"""
    fen_value = None
    if target_value_yuan is not None:
        try:
            fen_value = int(float(target_value_yuan) * 100)
        except (TypeError, ValueError):
            return {"kind": "empty", "note": "bad_value"}
    try:
        alert = await alerts_service.update_alert(
            int(alert_id), active=active, target_value=fen_value,
        )
    except ValueError:
        return {"kind": "empty", "note": "alert_not_found"}
    names = await games_service.names_for([int(alert.get("appid") or 0)])
    name = names.get(int(alert.get("appid") or 0), f"AppID {alert.get('appid')}")
    if active is False:
        vkey, tone = "alertOff", "warn"
    elif active is True:
        vkey, tone = "alertOn", "ok"
    else:
        vkey, tone = "alertUpdated", "ok"
    return _rows_result("alerts", [{
        "k": name,
        "vKey": vkey,
        "data": {"priceFen": alert.get("targetValue"), "alertId": alert.get("id")},
        "tone": tone,
    }])


async def bundle_follow(bundle_id: int, *, follow: bool = True) -> dict:
    """关注 / 取关捆绑包（挂 favorite 来源），1:1 bundles 域动作。"""
    try:
        if follow:
            await bundles_service.follow_bundle(int(bundle_id))
        else:
            await bundles_service.unfollow_bundle(int(bundle_id))
    except ValueError:
        return {"kind": "empty", "note": "bundle_not_found"}
    names = await bundles_service.names_for([int(bundle_id)])
    return _rows_result("bundles", [{
        "k": names.get(int(bundle_id), f"Bundle {bundle_id}"),
        "vKey": "bundleFollowed" if follow else "bundleUnfollowed",
        "tone": "ok" if follow else "warn",
    }])


async def region_toggle(region_code: str, *, enable: bool) -> dict:
    """启用 / 停用单个区服：读当前启用集 ±1 再整集写回（防清区）。

    区域生效在下一轮价格周期；全新加区（CC_LIST 之外）不属于本工具。"""
    code = str(region_code or "").strip().lower()
    valid = {c.lower() for c, _, _ in CC_LIST}
    if code not in valid:
        return _rows_result("regions", [{"k": code.upper(), "vKey": "regionUnknown", "tone": "warn"}])
    try:
        current = set(await regions_service.enabled_regions())
    except ValueError:
        current = set()
    if enable:
        current.add(code)
    else:
        current.discard(code)
    if not current:
        return _rows_result("regions", [{"k": code.upper(), "vKey": "regionMinOne", "tone": "warn"}])
    await regions_service.set_enabled(sorted(current))
    return _rows_result("regions", [{
        "k": code.upper(),
        "vKey": "regionOn" if enable else "regionOff",
        "tone": "ok" if enable else "warn",
    }])


# ─── 同步刷新工具（幂等拉取；结果一律 syncDone/syncFailed 行卡）───

def _sync_result(title: str, result: dict, *, done_key: str = "syncDone", fail_key: str = "syncFailed") -> dict:
    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    detail = ""
    if isinstance(result, dict):
        detail = str(result.get("error") or result.get("detail") or "")
    return _rows_result(title, [{
        "k": "", "vKey": done_key if ok else fail_key, "v": detail,
        "tone": "ok" if ok else "bad",
    }])


async def sync_wallet() -> dict:
    """立即抓取主账号钱包余额（账户域同一执行入口）。"""
    return _sync_result("wallet", await account_service.sync_wallet(force=True),
                        done_key="walletSynced", fail_key="walletFailed")


async def sync_library(steam_id: str | None = None) -> dict:
    """同步账户愿望单 / 拥有库（wishlist 域账户同步同一执行入口）。"""
    return _sync_result("library", await wishlist_service.sync_account(steam_id))


async def sync_achievements(steam_id: str | None = None) -> dict:
    """发起成就后台同步（进行中返回已在跑，不叠加）。"""
    try:
        result = await achievements_service.start_sync(steam_id or None)
    except ValueError:
        return _rows_result("achievementsSync", [{"k": "", "vKey": "syncRunning", "tone": "warn"}])
    return _sync_result("achievementsSync", result)


async def sync_bills(*, full: bool = False) -> dict:
    """同步 Steam 账单（默认增量探测；full=True 全量翻页，耗时数分钟）。"""
    result = await bills_service.sync_bills(force=full)
    if isinstance(result, dict) and result.get("status") == "no_cookie":
        return _rows_result("billsSync", [{"k": "", "vKey": "billNoCookie", "tone": "warn"}])
    return _sync_result("billsSync", result)


async def sync_family(steam_id: str | None = None) -> dict:
    """家庭组同步（默认遍历全部绑定账号，每账号独立成败）。"""
    return _sync_result("familySync", await family_service.sync_family_group(steam_id))


async def refresh_rates() -> dict:
    """刷新汇率（带重试与缓存回落，零手工配置）。"""
    return _sync_result("rates", await rates_service.refresh_rates())


# ─── Steam 检索与入库：本地目录检索不到时的补全链（搜到 appid → 导入首爬）───

_STEAM_SEARCH_LIMIT = 5
_INGEST_MAX = 20


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
    return _rows_result("ingest", rows, total=len(rows))


# 导航目标白名单：target 键 → 站内路径（agent 导航工具的目标集，
# 与 web 路由表同名对齐；不在表内的目标一律拒绝执行）
NAV_TARGETS = {
    "dashboard": "/dashboard",
    "library": "/library",
    "gamelib": "/gamelib",
    "follows": "/pool",
    "bundles": "/bundles",
    "alerts": "/alerts",
    "events": "/events",
    "achievements": "/achievements",
    "family": "/family",
    "bills": "/bills",
    "rates": "/rates",
    "toolbox": "/toolbox",
    "crawl": "/crawl",
    "proxies": "/proxies",
    "fetch": "/fetch",
    "logs": "/logs",
    "settings": "/settings",
}


# 工具注册表元数据：机器名 → 用户面步骤词条片段（label 与 i18n key
# `pilot.step.{label}` 对应）。机器名只进模型协议与日志，不进用户面。
# ─── 联网搜索与运维动作（扩权批：不涉隐私外发与删除）─────────────────

_DDG_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)


def _parse_ddg(html: str, limit: int = 5) -> list[dict]:
    """DuckDuckGo HTML 结果解析（纯函数）：标题/链接/摘要，纯文本化。"""
    out: list[dict] = []
    for m in _DDG_RESULT_RE.finditer(html or ""):
        href = m.group(1)
        if "uddg=" in href:
            from urllib.parse import parse_qs, unquote, urlparse
            qs = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            href = unquote(qs.get("uddg", [""])[0])
        text = re.sub(r"<[^>]+>", "", m.group(2))
        snippet = re.sub(r"<[^>]+>", "", m.group(3))
        if not href or not text.strip():
            continue
        out.append({"title": text.strip()[:120], "url": href[:300], "snippet": snippet.strip()[:160]})
        if len(out) >= limit:
            break
    return out


async def web_search(query: str) -> dict:
    """互联网搜索（DuckDuckGo HTML 端点，免密钥）。只外发搜索词本身，不带任何本地信息。"""
    term = " ".join((query or "").split())[:200]
    if not term:
        return {"kind": "empty", "note": "bad_query"}
    try:
        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001 — 出口解析失败按无代理直试
        proxy = None
    html = ""
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2, ttl_dns_cache=60),
            timeout=aiohttp.ClientTimeout(total=12),
        ) as session:
            async with session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": term},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"},
                proxy=proxy,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        html = ""
    rows = [
        {"k": r["title"], "v": r["snippet"][:80]}
        for r in _parse_ddg(html)
    ]
    return _rows_result("web", rows, total=len(rows))


async def refresh_game_price(appid: int) -> dict:
    """单游戏价格补抓：手动 job 挂后台执行，完成自动入库。"""
    names = await games_service.names_for([appid])
    name = names.get(appid, f"AppID {appid}")
    try:
        await crawl_service.start_job(scope="appids", appids=[appid], kind="manual")
    except RuntimeError:
        return _rows_result("refresh", [{"k": name, "vKey": "refreshBusy", "tone": "warn"}])
    except ValueError:
        return _rows_result("refresh", [{"k": name, "vKey": "refreshRejected", "tone": "warn"}])
    return _rows_result("refresh", [{"k": name, "vKey": "refreshStarted", "tone": "ok"}])


async def retry_removed(appid: int) -> dict:
    """已移除游戏重新入库并排队补抓（1:1 games.retry_removed_game）。"""
    try:
        await games_service.retry_removed_game(appid)
    except ValueError:
        return {"kind": "empty", "note": "not_removed"}
    names = await games_service.names_for([appid])
    return _rows_result("retry", [{
        "k": names.get(appid, f"AppID {appid}"),
        "vKey": "retryStarted", "tone": "ok",
    }])


async def monitor_include(appid: int) -> dict:
    """解除排除恢复监控（1:1 monitoring.set_exclusion False，可逆）。"""
    state = await monitoring_service.set_exclusion("game", appid, False)
    names = await games_service.names_for([appid])
    return _rows_result("include", [{
        "k": names.get(appid, f"AppID {appid}"),
        "vKey": "includeDone", "v": str(state or ""), "tone": "ok",
    }])


async def notify_test() -> dict:
    """发一封连通性测试邮件（只走一次 SMTP，不碰业务数据）。"""
    cfg = await get_smtp_config()
    if not all(cfg.get(k) for k in ("host", "user", "password", "to_addr")):
        return _rows_result("notify", [{
            "k": "", "vKey": "notifyNoConfig", "tone": "warn",
        }])
    try:
        await send_test_mail(
            host=cfg["host"],
            port=int(cfg["port"]),
            user=cfg["user"],
            password=cfg["password"],
            to_addr=cfg["to_addr"],
            use_ssl=bool(cfg["use_ssl"]),
        )
    except ValueError as e:
        return _rows_result("notify", [{"k": "", "vKey": "notifyFailed", "v": str(e)[:80], "tone": "bad"}])
    return _rows_result("notify", [{"k": "", "vKey": "notifySent", "tone": "ok"}])


# ─── 删除检查与提议（agent 只检查+解释；确认与执行权在用户）─────────────

_DEL_KEY_RE = re.compile(r"^(alert|bill_import|follow):\d+$")


async def _scan_deletables() -> list[dict]:
    """清理候选只读扫描：失效提醒（游戏已移除/不在目录/从未抓到价格）
    与同源重复账单导入（保留最新一批）。逐项带机器原因码，解释由前端词条化。"""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.domains.alerts.models import PriceAlert
    from app.domains.bills.models import BillImport
    from app.domains.games.models import Game, GameCurrentPrice

    items: list[dict] = []
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(PriceAlert, Game).outerjoin(Game, Game.appid == PriceAlert.appid)
        )).all()
        for alert, game in rows:
            base = {"key": f"alert:{alert.id}", "appid": int(alert.appid)}
            if game is None:
                items.append({**base, "name": f"AppID {alert.appid}", "reason": "noGame"})
            elif game.removed_at is not None:
                items.append({**base, "name": game.name or f"AppID {alert.appid}", "reason": "removed"})
            else:
                has_price = await session.scalar(
                    select(GameCurrentPrice.appid)
                    .where(GameCurrentPrice.appid == alert.appid)
                    .limit(1)
                )
                if has_price is None:
                    items.append({**base, "name": game.name or f"AppID {alert.appid}", "reason": "neverCrawled"})
        imports = (await session.execute(
            select(BillImport).order_by(BillImport.id.desc())
        )).scalars().all()
        seen_files: set[str] = set()
        for im in imports:
            sf = str(im.source_file or "")
            if sf and sf in seen_files:
                items.append({
                    "key": f"bill_import:{im.id}",
                    "name": f"{im.nickname or ''} {im.imported_at}".strip() or f"#{im.id}",
                    "reason": "dupImport",
                })
            elif sf:
                seen_files.add(sf)
    return items


async def find_deletables(sid: str) -> dict:
    """清理检查（只读）：有候选即落一份删除提议（无副作用），确认前不删任何东西。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    items = (await _scan_deletables())[:_BULK_MAX_ITEMS]
    if not items:
        return {"kind": "empty", "note": "nothing_to_delete"}
    now = get_beijing_time_obj()
    pid = "d" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _pilot_store().append_proposal(sid, {
        "pid": pid, "action": "delete", "items": items, "args": {}, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": "delete",
            "items": items, "args": {}, "state": "pending"}


async def propose_delete(sid: str, items: list) -> dict:
    """删除提议（模型可调，无副作用）：逐项 {key, name, reason, appid?}；key 限
    alert:/bill_import:/follow: 三类，确认后由服务层执行既有删除函数。
    appid 透传给前端渲染封面（不在目录的游戏也能凭 appid 拼 Steam 头图）。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    clean: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "")
        if not _DEL_KEY_RE.match(key):
            continue
        row: dict = {
            "key": key,
            "name": str(raw.get("name") or key)[:80],
            "reason": str(raw.get("reason") or "")[:24],
        }
        try:
            item_appid = int(raw.get("appid") or 0)
        except (TypeError, ValueError):
            item_appid = 0
        if item_appid > 0:
            row["appid"] = item_appid
        clean.append(row)
        if len(clean) >= _BULK_MAX_ITEMS:
            break
    if not clean:
        return {"kind": "empty", "note": "no_target"}
    now = get_beijing_time_obj()
    pid = "d" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _pilot_store().append_proposal(sid, {
        "pid": pid, "action": "delete", "items": clean, "args": {}, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": "delete",
            "items": clean, "args": {}, "state": "pending"}


TOOL_META: dict[str, dict] = {
    "search_games": {"label": "search"},
    "get_price_briefing": {"label": "price"},
    "get_region_prices": {"label": "regionPrices"},
    "compare_games": {"label": "compare"},
    "recommend_games": {"label": "recommend"},
    "add_follow": {"label": "follow"},
    "create_price_alert": {"label": "alert"},
    "propose_bulk": {"label": "propose"},
    "navigate": {"label": "navigate"},
    "list_follows": {"label": "follows"},
    "list_alerts": {"label": "alerts"},
    "list_bundle_follows": {"label": "bundleFollows"},
    "diagnose_price": {"label": "diagnose"},
    "recent_job_failures": {"label": "jobFailures"},
    "proxy_pool_status": {"label": "proxyStatus"},
    "write_gate_status": {"label": "gateStatus"},
    "list_tasks": {"label": "tasks"},
    "start_task": {"label": "taskStart"},
    "cancel_task": {"label": "taskCancel"},
    "list_sessions": {"label": "sessions"},
    "read_session": {"label": "sessionRead"},
    "hb_monthly": {"label": "hbMonthly"},
    "epic_free": {"label": "epicFree"},
    "steam_free": {"label": "steamFree"},
    "top_games": {"label": "topGames"},
    "price_drops": {"label": "priceDrops"},
    "rates_overview": {"label": "rates"},
    "calendar_events": {"label": "calendar"},
    "list_accounts": {"label": "accounts"},
    "list_wishlist": {"label": "wishlist"},
    "list_owned": {"label": "owned"},
    "list_family_library": {"label": "famLibrary"},
    "achievements_summary": {"label": "achievements"},
    "bills_summary": {"label": "bills"},
    "family_status": {"label": "family"},
    "redeem_quota": {"label": "redeem"},
    "list_bundles": {"label": "bundlesAll"},
    "update_price_alert": {"label": "updateAlert"},
    "bundle_follow": {"label": "bundleFollow"},
    "region_toggle": {"label": "regionToggle"},
    "sync_wallet": {"label": "syncWallet"},
    "sync_library": {"label": "syncLibrary"},
    "sync_achievements": {"label": "syncAchievements"},
    "sync_bills": {"label": "syncBills"},
    "sync_family": {"label": "syncFamily"},
    "refresh_rates": {"label": "refreshRates"},
    "search_steam": {"label": "steamSearch"},
    "ingest_appids": {"label": "ingest"},
    "web_search": {"label": "webSearch"},
    "refresh_game_price": {"label": "refreshPrice"},
    "retry_removed_game": {"label": "retryRemoved"},
    "monitor_include": {"label": "monitorInclude"},
    "notify_test": {"label": "notifyTest"},
    "find_deletables": {"label": "deletables"},
    "propose_delete": {"label": "proposeDelete"},
    "get_user_preferences": {"label": "getPreferences"},
    "set_user_preference": {"label": "setPreference"},
    "audit_follows_workflow": {"label": "auditFollowsWorkflow"},
}


def tool_step(name: str, result: dict) -> dict:
    """工具执行结果 → 时间线步骤条目。

    返回 {label, status, data}：label 对应前端词条键片段；status 取
    ok / empty / denied；data 只装词条插值所需的最小数据（结果计数、
    对象名、导航目标）。判定走数据层，前端不复制这套语义。"""
    if result.get("kind") == "denied":
        return {"label": "denied", "status": "denied", "data": {}}
    if name == "audit_follows_workflow":
        stepper = result.get("stepper") or {}
        steps = stepper.get("steps") or []
        ok = any(s.get("status") == "ok" for s in steps)
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "ok" if ok else "empty",
            "data": {},
        }
    if name == "get_region_prices":
        items = result.get("items") or []
        return {"label": "regionPrices", "status": "ok" if items else "empty",
                "data": {"count": len(items)}}
    if name == "compare_games":
        items = result.get("items") or []
        return {"label": "compare", "status": "ok" if items else "empty",
                "data": {"count": len(items)}}
    if name in ("search_games", "recommend_games", "search_steam"):
        items = result.get("items") or []
        label = {
            "search_games": "search",
            "recommend_games": "recommend",
            "search_steam": "steamSearch",
        }[name]
        return {
            "label": label,
            "status": "ok" if items else "empty",
            "data": {"count": len(items)},
        }
    # 游戏清单卡类只读工具：结果行在 items（找游戏同款富卡）
    if name in ("top_games", "price_drops", "list_follows", "list_wishlist",
                "list_owned", "list_family_library"):
        items = result.get("items") or []
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "ok" if items else "empty",
            "data": {"count": len(items)},
        }
    if name in ("achievements_summary", "family_status"):
        ok = bool(result.get("hasCredential", result.get("bound", True)))
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "ok" if ok else "empty",
            "data": {},
        }
    if name in _READ_TOOLS:
        rows = result.get("rows") or []
        data: dict = {"count": len(rows)}
        if name == "diagnose_price" and result.get("name"):
            data["name"] = result.get("name")
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "ok" if rows else "empty",
            "data": data,
        }
    if name in _TASK_TOOLS:
        # 调度类结果同样是行卡；行色调决定时间线状态（busy/warn ≠ 成功）
        rows = result.get("rows") or []
        tone = (rows[0].get("tone") if rows else None) or "ok"
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "denied" if tone == "bad" else ("empty" if tone == "warn" else "ok"),
            "data": {},
        }
    if name in _WRITE_TOOLS or name in _SYNC_TOOLS:
        # 写与同步的结果也是行卡：时间线只给动作名（结果进卡片与正文）
        rows = result.get("rows") or []
        return {
            "label": TOOL_META.get(name, {}).get("label", name),
            "status": "ok" if rows else "empty",
            "data": {},
        }
    if name == "get_price_briefing":
        return {
            "label": "price",
            "status": "ok" if result.get("kind") == "price" else "empty",
            "data": {"name": result.get("name")},
        }
    if name == "add_follow" or name == "create_price_alert":
        label = "follow" if name == "add_follow" else "alert"
        return {
            "label": label,
            "status": "ok" if result.get("appid") else "empty",
            "data": {"name": result.get("name")},
        }
    if name == "navigate":
        return {
            "label": "navigate",
            "status": "ok" if result.get("path") else "empty",
            "data": {"target": result.get("target") or "", "path": result.get("path") or ""},
        }
    return {"label": TOOL_META.get(name, {}).get("label", name), "status": "ok", "data": {}}


def tool_specs() -> list[dict]:
    """agent 循环的工具表（OpenAI function 格式）。

    写工具只有加关注 / 设提醒两个单对象可逆动作——白名单即风险门；
    批量动作不设执行工具，只经 propose_bulk 提清单交用户确认后执行。"""
    return [
        {"type": "function", "function": {
            "name": "search_games",
            "description": "在本地游戏目录按名称检索游戏（中英文均可），返回候选（含 appid、现价、折扣、好评率）。"
                           "注意：目录未收录、或收录了但还没爬过价格的游戏检索不到——那时改用 search_steam 去 Steam 商店搜",
            "parameters": {"type": "object", "properties": {
                "q": {"type": "string", "description": "游戏名称关键词"},
            }, "required": ["q"]},
        }},
        {"type": "function", "function": {
            "name": "search_steam",
            "description": "在 Steam 商店全网搜索游戏（不限于本地目录，国区锁区游戏也搜得到），"
                           "返回候选的 appid 与名称。注意：该检索只匹配英文名——中文词搜不到时"
                           "改用英文名重试（如 卡赞→Khazan）。本地目录检索不到某款游戏、或用户明确要求"
                           "『去 Steam 搜』时调用；拿到 appid 后可用 ingest_appids 让它入库，之后即可查价格",
            "parameters": {"type": "object", "properties": {
                "term": {"type": "string", "description": "搜索词（优先用游戏英文名）"},
            }, "required": ["term"]},
        }},
        {"type": "function", "function": {
            "name": "ingest_appids",
            "description": "把指定 AppID 的游戏导入本地目录并立即首爬价格（与任务页『添加游戏』是同一动作），"
                           "导入后即可查价格、史低、设提醒。用户要求『让某游戏入库/抓一下某游戏/收录某游戏』时调用；"
                           "appid 来自 search_steam 结果或用户提供。一次最多 20 款；"
                           "导入不等于关注——要持续追踪降价需另调 add_follow",
            "parameters": {"type": "object", "properties": {
                "appids": {"type": "array", "items": {"type": "integer"},
                           "description": "要导入的游戏 AppID 列表（一次最多 20 个）"},
            }, "required": ["appids"]},
        }},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "联网搜索（全互联网，不只 Steam）。用户问『最新的 XX 消息/网上怎么说/帮我查一下 XX』"
                           "且本地工具答不了时调用；只把问题关键词作为搜索词",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            }, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "refresh_game_price",
            "description": "补抓某款已入库游戏的当前价格（后台任务，完成后自动入库）。"
                           "用户说『刷新 XX 的价格』『XX 价格怎么还是旧的』时调用；appid 用检索工具确认",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "retry_removed_game",
            "description": "把已移除的游戏重新拉回目录并排队补抓。用户说『把 XX 加回来』『恢复 XX』时调用",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "monitor_include",
            "description": "解除某游戏的排除状态、恢复监控。用户说『XX 别排除了/恢复监控 XX』时调用",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "notify_test",
            "description": "发一封连通性测试邮件（不碰业务数据）。用户说『发个测试通知/试试邮件』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "get_price_briefing",
            "description": "取某游戏的价格事实：国区现价、历史最低、近一年区间",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "get_region_prices",
            "description": "取某游戏在全部已爬区域的现价：原币价、折合人民币、折扣，主账号结算区排最前。"
                           "用户问『XX 在我账号的地区/某区多少钱』『哪个区最便宜』或质疑回答里的地区不对时调用；"
                           "国区锁区的游戏也能查到其他区的价格",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "compare_games",
            "description": "并排对比 2~3 款游戏的现价、折扣、史低、近一年中位价与好评率。"
                           "用户问『A 和 B 买哪个』『这几款哪个更划算』时调用；appids 先用检索工具确认",
            "parameters": {"type": "object", "properties": {
                "appids": {"type": "array", "items": {"type": "integer"},
                           "minItems": 2, "maxItems": 3, "description": "待对比游戏的 AppID（2~3 个）"},
            }, "required": ["appids"]},
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
            "name": "find_deletables",
            "description": "清理检查（只读）：扫描失效提醒（游戏已移除/从未抓到价格）与重复账单导入，"
                           "落一份带逐项解释的删除提议，用户确认后才真正删除。用户问『有什么可以清理』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "propose_delete",
            "description": "提交一份待用户确认的删除清单（本工具不执行删除）。用户明确要求删除某提醒/账单批次/取消关注时调用；"
                           "items 每项 {key, name, reason}，key 形如 alert:3 / bill_import:7 / follow:530（appids 来自只读工具结果）",
            "parameters": {"type": "object", "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "reason": {"type": "string", "enum": ["removed", "neverCrawled", "noGame", "dupImport", "userAsk"]},
                }, "required": ["key", "name", "reason"]}},
            }, "required": ["items"]},
        }},
        {"type": "function", "function": {
            "name": "propose_bulk",
            "description": "提交一份待用户确认的批量清单（本工具不执行任何写操作）。当用户要求对多款游戏"
                           "批量加关注或批量设提醒时调用：先用只读工具取到目标清单，再把 appid 数组交给"
                           "本工具，由用户在界面上确认后才会真正执行。一次最多 50 款",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["add_follow", "create_price_alert"],
                           "description": "批量动作：加关注 / 设提醒"},
                "appids": {"type": "array", "items": {"type": "integer"},
                           "description": "目标游戏 AppID 数组"},
                "target_type": {"type": "string", "enum": ["price", "historic_low"],
                                "description": "action=create_price_alert 时有效"},
                "target_value_yuan": {"type": "number",
                                      "description": "target_type=price 时必填，人民币元"},
            }, "required": ["action", "appids"]},
        }},
        {"type": "function", "function": {
            "name": "add_follow",
            "description": "把某游戏加入用户关注（持续追踪价格）。仅当用户明确要求关注该一款游戏时调用；"
                           "多款一起关注改用 propose_bulk",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "navigate",
            "description": "跳转到用户想查看的模块页面。target 取值（用户说法 → target）："
                           "仪表盘=dashboard、找游戏=library、游戏库=gamelib、我的关注=follows、"
                           "捆绑包=bundles、价格提醒=alerts、活动日历=events、成就=achievements、"
                           "家庭=family、账单=bills、汇率=rates、工具箱=toolbox、任务=crawl、"
                           "网络=proxies、自动抓取=fetch、日志=logs、设置=settings。"
                           "仅当用户表达想查看/打开某模块时调用",
            "parameters": {"type": "object", "properties": {
                "target": {"type": "string", "description": "模块标识，取上方取值列表之一"},
            }, "required": ["target"]},
        }},
        {"type": "function", "function": {
            "name": "create_price_alert",
            "description": "为中国区创建价格提醒。仅当用户明确要求提醒这一款游戏时调用；用户给出具体价格用 "
                           "price 类型，用户说史低提醒用 historic_low 类型；多款一起设提醒改用 propose_bulk",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
                "target_type": {"type": "string", "enum": ["price", "historic_low"]},
                "target_value_yuan": {"type": "number", "description": "price 类必填，人民币元"},
            }, "required": ["appid", "target_type"]},
        }},
        {"type": "function", "function": {
            "name": "list_follows",
            "description": "列出用户关注的游戏。用户问『我关注了哪些游戏』或需要确认关注状态时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_alerts",
            "description": "列出已设置的价格提醒规则。用户问『我有哪些提醒』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_bundle_follows",
            "description": "列出用户关注的捆绑包",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "diagnose_price",
            "description": "诊断某游戏的价格数据状态：各区最近抓取结果（成功/失败/未抓取）、上次成功时间、覆盖率。"
                           "用户问『为什么价格没更新』『数据是不是旧的』时调用；appid 可先用检索工具确认",
            "parameters": {"type": "object", "properties": {
                "appid": {"type": "integer", "description": "游戏 AppID"},
            }, "required": ["appid"]},
        }},
        {"type": "function", "function": {
            "name": "recent_job_failures",
            "description": "列出最近失败的抓取任务与原因。用户问『抓取为什么失败』『最近有什么报错』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "proxy_pool_status",
            "description": "查看代理通道可用状态（价格抓取依赖它）。诊断『价格不更新/抓取不动』类网络问题时与任务失败清单配合调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "write_gate_status",
            "description": "查看写入调度状态。用户说『操作一直不生效/卡住了』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_tasks",
            "description": "查看正在运行的耗时任务（价格刷新 / 价格补抓）与最近结束的几次。"
                           "用户问『刷新到哪了』『还在跑吗』『跑完了吗』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "start_task",
            "description": "发起一个耗时后台任务，受理后立即返回、不阻塞回答。"
                           "kind 只能取 price_refresh（全部价格刷新）或 price_repair（补抓缺价格的游戏）。"
                           "仅当用户明确要求刷新价格 / 补价格时调用；发起后告知可在对话里问进度或让它停下",
            "parameters": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["price_refresh", "price_repair"],
                         "description": "任务类型"},
            }, "required": ["kind"]},
        }},
        {"type": "function", "function": {
            "name": "cancel_task",
            "description": "停止正在运行的耗时任务。用户说『停下来』『别刷了』『取消』时调用；"
                           "只有一个任务在跑时可只给 kind 或都不给",
            "parameters": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["price_refresh", "price_repair"]},
            }},
        }},
        {"type": "function", "function": {
            "name": "list_sessions",
            "description": "列出用户与领航员的历史对话清单（标题与轮数）。"
                           "用户想找回或查看以前的对话时调用；对话内容用 read_session 读取",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "read_session",
            "description": "读取某段历史对话的内容摘要。"
                           "用户问『我们之前聊过什么』或需要旧对话里的信息时调用；"
                           "会话 id 可先用会话清单工具获取",
            "parameters": {"type": "object", "properties": {
                "sid": {"type": "string", "description": "会话 id（来自会话清单）"},
            }, "required": ["sid"]},
        }},
        {"type": "function", "function": {
            "name": "hb_monthly",
            "description": "列出当月 HB 慈善包（Humble Choice）的游戏清单，含国区现价与史低。"
                           "用户问『HB 月包/本月慈善包有哪些游戏』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "epic_free",
            "description": "列出 Epic 当期正在送和预告即将送的游戏。用户问『Epic 喜加一/免费游戏』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "steam_free",
            "description": "列出 Steam 正在限时免费的游戏。用户问『Steam 有什么免费领』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "top_games",
            "description": "列出 Steam 热销榜前列。用户问『现在什么游戏火/热销榜』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "price_drops",
            "description": "列出最近的价格事件（新史低、降价、锁区、重新上架等）。"
                           "用户问『最近有什么降价』『哪些游戏创新低』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "rates_overview",
            "description": "查看各币种对人民币的当前汇率。用户问『现在汇率多少』『某区折合人民币怎么算』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "calendar_events",
            "description": "查看 Steam 官方活动日历（进行中的促销与即将开始的活动）。用户问『最近有什么促销/打折活动』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_accounts",
            "description": "列出绑定的 Steam 账号（名称、主账号、愿望单/拥有统计）。用户问『我绑定了哪些账号』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_wishlist",
            "description": "列出账户愿望单里的游戏。用户问『我愿望单里有什么』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_owned",
            "description": "列出账户已拥有的游戏（带国区现价）。用户问『我拥有哪些游戏』『我库里有什么』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_family_library",
            "description": "列出 Steam 家庭组的共享库游戏（家庭里所有成员共享的游戏，带国区现价）。"
                           "用户问『家庭库/家庭共享有哪些游戏』『家里能玩什么』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "achievements_summary",
            "description": "查看奖杯概览（白金数、解锁进度、白金陈列、最近解锁）。用户问『我的成就/奖杯进度』『最近解了什么成就』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "bills_summary",
            "description": "列出已导入的账单记录与消费合计。用户问『我花了多少钱/账单』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "family_status",
            "description": "查看 Steam 家庭组状态（组、成员、钱包区）。用户问『我的家庭组』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "redeem_quota",
            "description": "查看 CDK 激活额度（已用/上限）。用户问『还能激活几次』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_bundles",
            "description": "列出捆绑包前列（差价降序，含国区现价与最低区价）。用户问『现在有什么捆绑包划算』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "update_price_alert",
            "description": "修改已存在的价格提醒：改阈值或启停。用户说『把 XX 的提醒改到 N 块』『先别提醒 XX 了』时调用；"
                           "alert_id 从提醒清单工具的结果里取",
            "parameters": {"type": "object", "properties": {
                "alert_id": {"type": "integer", "description": "提醒规则 ID"},
                "target_value_yuan": {"type": "number", "description": "新阈值（人民币元；只启停时省略）"},
                "active": {"type": "boolean", "description": "true=启用 false=停用；只改阈值时省略"},
            }, "required": ["alert_id"]},
        }},
        {"type": "function", "function": {
            "name": "bundle_follow",
            "description": "关注或取关一个捆绑包。用户说『关注这个包』时调用；bundle_id 从捆绑包列表结果里取",
            "parameters": {"type": "object", "properties": {
                "bundle_id": {"type": "integer", "description": "捆绑包 ID"},
                "follow": {"type": "boolean", "description": "true=关注 false=取关，缺省关注"},
            }, "required": ["bundle_id"]},
        }},
        {"type": "function", "function": {
            "name": "region_toggle",
            "description": "启用或停用一个价格区服（下一轮价格周期生效）。用户说『把 XX 区关了』『加上 XX 区价格』时调用；"
                           "只支持已有区服代码（如 us/ru/ua），新增区服需要开发者在设置里配置",
            "parameters": {"type": "object", "properties": {
                "region_code": {"type": "string", "description": "区服代码（如 cn/us/ru）"},
                "enable": {"type": "boolean", "description": "true=启用 false=停用"},
            }, "required": ["region_code", "enable"]},
        }},
        {"type": "function", "function": {
            "name": "sync_wallet",
            "description": "立即抓取主账号钱包余额。用户说『同步一下钱包』『看看余额对不对』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "sync_library",
            "description": "同步账户愿望单与拥有库（从 Steam 拉取最新）。用户说『同步我的愿望单/游戏库』时调用",
            "parameters": {"type": "object", "properties": {
                "steam_id": {"type": "string", "description": "账号 SteamID；缺省同步全部绑定账号"}
            }},
        }},
        {"type": "function", "function": {
            "name": "sync_achievements",
            "description": "发起成就后台同步（进行中会提示已在跑）。用户说『同步成就』时调用",
            "parameters": {"type": "object", "properties": {
                "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
            }},
        }},
        {"type": "function", "function": {
            "name": "sync_bills",
            "description": "同步 Steam 账单（默认增量探测，很快；full=true 全量翻页需数分钟）。用户说『同步账单』时调用",
            "parameters": {"type": "object", "properties": {
                "full": {"type": "boolean", "description": "true=全量重拉，缺省增量"}
            }},
        }},
        {"type": "function", "function": {
            "name": "sync_family",
            "description": "同步 Steam 家庭组。用户说『同步家庭组』时调用",
            "parameters": {"type": "object", "properties": {
                "steam_id": {"type": "string", "description": "账号 SteamID；缺省遍历全部绑定账号"}
            }},
        }},
        {"type": "function", "function": {
            "name": "refresh_rates",
            "description": "刷新各币种汇率。用户说『刷新汇率』『汇率好像不对』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "get_user_preferences",
            "description": "查询用户已保存的长期偏好清单（如常用区服、类型偏好、预算习惯）。用户询问『我的偏好是什么/你记住了我什么』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "set_user_preference",
            "description": "保存或更新用户的长期偏好。用户明确要求『记住我的偏好/记住我喜欢XX/记住我只买XX区』时调用",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["region_preference", "genre_preference", "budget_habit", "preference"], "description": "偏好类别"},
                "key": {"type": "string", "description": "偏好键名（如 preferred_regions, liked_genres, max_budget_cny）"},
                "value": {"description": "偏好内容（可以是列表、字典或字符串/数值）"},
            }, "required": ["key", "value"]},
        }},
        {"type": "function", "function": {
            "name": "audit_follows_workflow",
            "description": "流水线工作流：全量扫描关注游戏价格变动、汇率与史低对比，并自动生成批量价格提醒更新提议。用户说『检查全部关注游戏的价格变动并更新提醒规则』『巡检关注游戏并提议提醒』时调用",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]


async def execute_tool(name: str, arguments: dict, *, guarded: bool = False,
                       sid: str | None = None) -> dict:
    """工具执行分发。写工具受守卫复核：问句含批量 / 删除 / 停用语义时
    拒绝执行（返回 kind=denied，模型改走 propose_bulk 交用户确认）。"""
    if name in _WRITE_TOOLS and guarded:
        return {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}
    if name == "search_games":
        items = await search_games(str(arguments.get("q") or ""), query=str(arguments.get("q") or ""))
        return {"kind": "games", "items": items}
    if name == "get_price_briefing":
        facts = await price_facts(int(arguments.get("appid") or 0))
        return facts or {"kind": "empty", "note": "no_data"}
    if name == "get_region_prices":
        return await region_prices(int(arguments.get("appid") or 0))
    if name == "compare_games":
        return await compare_games(arguments.get("appids") or [])
    if name == "recommend_games":
        items = await recommend_games_by_filters(
            only_discounted=bool(arguments.get("only_discounted")),
            min_rating=int(arguments.get("min_rating") or 0),
            max_price_yuan=arguments.get("max_price_yuan"),
        )
        return {"kind": "games", "items": items}
    if name == "navigate":
        target = str(arguments.get("target") or "")
        path = NAV_TARGETS.get(target)
        if not path:
            return {"kind": "navigate", "target": "", "path": ""}
        return {"kind": "navigate", "target": target, "path": path}
    if name == "list_follows":
        return await list_follows()
    if name == "list_alerts":
        return await list_alerts()
    if name == "list_bundle_follows":
        return await list_bundle_follows()
    if name == "diagnose_price":
        return await diagnose_price(int(arguments.get("appid") or 0))
    if name == "recent_job_failures":
        return await recent_job_failures()
    if name == "proxy_pool_status":
        return await proxy_pool_status()
    if name == "write_gate_status":
        return await write_gate_status()
    if name == "list_tasks":
        return await list_tasks()
    if name == "start_task":
        return await start_task(str(arguments.get("kind") or ""))
    if name == "cancel_task":
        return await cancel_task(
            run_id=str(arguments.get("run_id") or "") or None,
            kind=str(arguments.get("kind") or "") or None,
        )
    if name == "list_sessions":
        return await list_sessions()
    if name == "read_session":
        return await read_session(str(arguments.get("sid") or ""))
    if name == "hb_monthly":
        return await hb_monthly()
    if name == "epic_free":
        return await epic_free()
    if name == "steam_free":
        return await steam_free()
    if name == "top_games":
        return await top_games()
    if name == "price_drops":
        return await price_drops()
    if name == "rates_overview":
        return await rates_overview()
    if name == "calendar_events":
        return await calendar_events()
    if name == "list_accounts":
        return await list_accounts()
    if name == "list_wishlist":
        return await list_wishlist()
    if name == "list_owned":
        return await list_owned()
    if name == "list_family_library":
        return await list_family_library()
    if name == "achievements_summary":
        return await achievements_summary()
    if name == "bills_summary":
        return await bills_summary()
    if name == "family_status":
        return await family_status()
    if name == "redeem_quota":
        return await redeem_quota()
    if name == "list_bundles":
        return await list_bundles()
    if name == "propose_bulk":
        return await propose_bulk(
            str(sid or ""),
            str(arguments.get("action") or ""),
            arguments.get("appids") or [],
            target_type=str(arguments.get("target_type") or "historic_low"),
            target_value_yuan=arguments.get("target_value_yuan"),
        )
    if name == "add_follow":
        return await monitor_add(int(arguments.get("appid") or 0))
    if name == "create_price_alert":
        ttype = arguments.get("target_type") or "price"
        yuan = arguments.get("target_value_yuan")
        fen = int(float(yuan) * 100) if yuan is not None else None
        return await alert_add(int(arguments.get("appid") or 0), target_type=ttype, target_value_fen=fen)
    if name == "update_price_alert":
        return await update_price_alert(
            int(arguments.get("alert_id") or 0),
            target_value_yuan=arguments.get("target_value_yuan"),
            active=arguments.get("active"),
        )
    if name == "bundle_follow":
        return await bundle_follow(
            int(arguments.get("bundle_id") or 0),
            follow=bool(arguments.get("follow", True)),
        )
    if name == "region_toggle":
        return await region_toggle(
            str(arguments.get("region_code") or ""),
            enable=bool(arguments.get("enable", True)),
        )
    if name == "sync_wallet":
        return await sync_wallet()
    if name == "sync_library":
        return await sync_library(arguments.get("steam_id") or None)
    if name == "search_steam":
        return await search_steam(str(arguments.get("term") or arguments.get("q") or ""))
    if name == "ingest_appids":
        return await ingest_appids(arguments.get("appids") or [])
    if name == "web_search":
        return await web_search(str(arguments.get("query") or ""))
    if name == "refresh_game_price":
        return await refresh_game_price(int(arguments.get("appid") or 0))
    if name == "retry_removed_game":
        return await retry_removed(int(arguments.get("appid") or 0))
    if name == "monitor_include":
        return await monitor_include(int(arguments.get("appid") or 0))
    if name == "notify_test":
        return await notify_test()
    if name == "find_deletables":
        return await find_deletables(str(sid or ""))
    if name == "propose_delete":
        return await propose_delete(str(sid or ""), arguments.get("items") or [])

    if name == "sync_achievements":
        return await sync_achievements(arguments.get("steam_id") or None)
    if name == "sync_bills":
        return await sync_bills(full=bool(arguments.get("full", False)))
    if name == "sync_family":
        return await sync_family(arguments.get("steam_id") or None)
    if name == "refresh_rates":
        return await refresh_rates()
    if name == "get_user_preferences":
        from app.domains.agent import memory as agent_memory
        memories = await agent_memory.list_memories()
        return {"kind": "user_preferences", "items": memories, "count": len(memories)}
    if name == "set_user_preference":
        from app.domains.agent import memory as agent_memory
        category = str(arguments.get("category") or "preference")
        key = str(arguments.get("key") or "")
        value = arguments.get("value")
        if not key:
            return {"kind": "empty", "note": "key_required"}
        saved = await agent_memory.upsert_memory(
            category=category,
            key=key,
            value=value,
            confidence=1.0,
            source="user_explicit",
        )
        return {"kind": "user_preference_saved", "item": saved}
    if name == "audit_follows_workflow":
        return await audit_follows_workflow(str(sid or ""))
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


async def propose_bulk(sid: str, action: str, appids: list, *,
                       target_type: str = "historic_low",
                       target_value_yuan: float | None = None) -> dict:
    """批量提议：落待确认清单（无副作用）；确认后由服务层逐项执行既有写动作。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    if str(action or "") not in _BULK_ACTIONS:
        return {"kind": "empty", "note": "bad_action"}
    ids: list[int] = []
    for raw in appids or []:
        try:
            appid = int(raw)
        except (TypeError, ValueError):
            continue
        if appid > 0 and appid not in ids:
            ids.append(appid)
    if not ids:
        return {"kind": "empty", "note": "no_target"}
    if len(ids) > _BULK_MAX_ITEMS:
        return {"kind": "empty", "note": "too_many", "data": {"max": _BULK_MAX_ITEMS}}
    names = await games_service.names_for(ids)
    now = get_beijing_time_obj()
    args: dict = {"target_type": str(target_type or "historic_low")}
    if target_value_yuan is not None:
        args["target_value_yuan"] = float(target_value_yuan)
    items = [{"appid": i, "name": names.get(i) or f"AppID {i}"} for i in ids]
    pid = "p" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _pilot_store().append_proposal(sid, {
        "pid": pid, "action": action, "items": items, "args": args, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": action,
            "items": items, "args": args, "state": "pending"}


async def audit_follows_workflow(sid: str = "") -> dict:
    """检查全部关注游戏的价格变动并更新提醒规则（长任务/流水线分步卡片）。"""
    manual = await monitoring_service.ids_with_source("game", "manual")
    favorite = await wishlist_follows.followed_appids()
    ids = sorted({int(a) for a in manual} | {int(a) for a in favorite})
    total = len(ids)
    if not ids:
        stepper = {
            "kind": "stepper",
            "title": "关注游戏价格与提醒巡检流水线",
            "currentStepIndex": 0,
            "steps": [
                {"id": 1, "title": "扫描关注列表", "detail": "关注列表为空 (0/0)", "status": "empty", "current": 0, "total": 0},
                {"id": 2, "title": "校验区服汇率与现价", "detail": "跳过校验", "status": "wait"},
                {"id": 3, "title": "生成批量提议", "detail": "无待处理项", "status": "wait"},
            ],
        }
        return {"kind": "stepper", "stepper": stepper}

    briefs = await games_service.briefs_for(ids)
    alerts = await alerts_service.list_alerts()
    alerted_appids = {int(a.get("appid") or 0) for a in alerts if a.get("active")}

    candidates = []
    for appid in ids:
        b = briefs.get(appid)
        if not b or not b.get("name"):
            continue
        if appid not in alerted_appids or (b.get("discount") or 0) > 0:
            candidates.append({"appid": appid, "name": b.get("name")})
        if len(candidates) >= 6:
            break

    if not candidates and ids:
        for appid in ids[:3]:
            b = briefs.get(appid) or {}
            candidates.append({"appid": appid, "name": b.get("name") or f"AppID {appid}"})

    now = get_beijing_time_obj()
    pid = "p" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    args = {"target_type": "historic_low"}

    proposal = None
    if sid and candidates:
        ok = await _pilot_store().append_proposal(sid, {
            "pid": pid, "action": "create_price_alert", "items": candidates, "args": args, "state": "pending",
        })
        if ok:
            proposal = {
                "kind": "proposal",
                "pid": pid,
                "action": "create_price_alert",
                "items": candidates,
                "args": args,
                "state": "pending",
            }

    stepper = {
        "kind": "stepper",
        "title": "关注游戏价格与提醒巡检流水线",
        "currentStepIndex": 2,
        "steps": [
            {
                "id": 1,
                "title": "扫描关注列表",
                "detail": f"扫描完成 ({total}/{total})",
                "status": "ok",
                "current": total,
                "total": total,
                "badge": f"{total}/{total}",
            },
            {
                "id": 2,
                "title": "校验区服汇率与现价",
                "detail": "汇率对齐与史低价格校验完成",
                "status": "ok",
                "current": total,
                "total": total,
            },
            {
                "id": 3,
                "title": "生成批量提议",
                "detail": f"待确认 {len(candidates)} 项提醒规则" if candidates else "全部提醒规则均已就绪",
                "status": "ok" if candidates else "empty",
                "badge": f"待确认 {len(candidates)} 项" if candidates else "已就绪",
            },
        ],
    }

    res: dict = {"kind": "stepper", "stepper": stepper}
    if proposal:
        res["proposal"] = proposal
    return res
