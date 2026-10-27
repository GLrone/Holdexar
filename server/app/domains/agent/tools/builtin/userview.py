"""用户面只读工具：账号状态 / 通知 / 成就 / 账单 / 提醒历史 / 更新状态。"""
from __future__ import annotations

from app.core.config import get_settings
from app.domains.account import service as account_service
from app.domains.achievements import service as achievements_service
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.alerts import service as alerts_service
from app.domains.bills import service as bills_service


def _mask_addr(addr: str) -> str:
    """收件邮箱脱敏：保留首字符与域名。"""
    if "@" not in addr:
        return ""
    head, _, domain = addr.partition("@")
    return f"{head[:1]}***@{domain}" if head else f"***@{domain}"


def _clamp(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


async def account_status(args: dict, sid: str | None = None) -> dict:
    """Steam 账号状态 + 钱包余额 + 登录态新鲜度（账号域同一 payload，无任何凭据）。"""
    st = await account_service.get_status()
    if not st.get("has_cookie"):
        return {
            "kind": "empty", "hasCookie": False,
            "bound": bool(st.get("bound_steam_id")), "mismatch": bool(st.get("mismatch")),
        }
    profile = st.get("profile") or {}
    wallet = st.get("wallet") or {}
    rows = [
        {"k": profile.get("persona_name") or st.get("cookie_steam_id") or "—",
         "vKey": "acctStatus",
         "data": {"online": bool(st.get("is_online")), "inGame": st.get("in_game") or ""},
         "tone": "ok" if st.get("is_online") else None},
        {"k": "", "vKey": "walletBalance", "v": wallet.get("balance_display") or "",
         "data": {"okAt": wallet.get("ok_at"), "checkOk": bool(wallet.get("check_ok"))},
         "tone": "ok" if wallet.get("check_ok") else "warn"},
        {"k": "", "vKey": "sessionFresh",
         "data": {"expired": bool(st.get("session_expired")),
                  "expiresAt": st.get("session_expires_at"),
                  "hasRefresh": bool(st.get("session_has_refresh"))},
         "tone": None if st.get("session_expired") else "ok"},
    ]
    out = _shared.rows_result("accountStatus", rows, total=len(rows))
    out.update({
        "steamId": st.get("cookie_steam_id") or "",
        "personaName": profile.get("persona_name"),
        "mismatch": bool(st.get("mismatch")),
        "walletBalance": wallet.get("balance_display"),
        "sessionExpired": bool(st.get("session_expired")),
        "accountsCount": len(st.get("accounts") or []),
    })
    return out


async def notifications_status(args: dict, sid: str | None = None) -> dict:
    """通知偏好 + 投递指标 + 最近一次投递（通知设置页与统计同源合并视图）。"""
    from app.domains.notifications.router import get_prefs, stats

    prefs = await get_prefs()
    stat = await stats()
    smtp = prefs.get("smtp") or {}
    disabled = [c.get("key") for c in (prefs.get("categories") or []) if not c.get("enabled")]
    rows = [
        {"k": "", "vKey": "notifyEnabled", "tone": "ok" if prefs.get("enabled") else "warn",
         "data": {"quiet": bool(prefs.get("quietEnabled")),
                  "quietStart": prefs.get("quietStart"), "quietEnd": prefs.get("quietEnd")}},
        {"k": "", "vKey": "notifyStats",
         "data": {"delivered": stat.get("delivered", 0), "failed": stat.get("failed", 0),
                  "pending": stat.get("pending", 0), "retryable": stat.get("retryable", 0)},
         "tone": "bad" if stat.get("failed") else "ok"},
        {"k": "", "vKey": "notifyChannel",
         "data": {"configured": bool(smtp.get("configured")),
                  "account": smtp.get("userMasked") or ""},
         "tone": "ok" if smtp.get("configured") else "warn"},
    ]
    last = prefs.get("lastDelivery")
    if last:
        rows.append({"k": "", "vKey": "notifyLast", "v": last.get("status") or "",
                     "at": last.get("at"),
                     "data": {"attempts": last.get("attempts"),
                              "reason": last.get("reason") or ""},
                     "tone": "ok" if last.get("status") == "delivered" else "bad"})
    out = _shared.rows_result("notifyStatus", rows, total=len(rows))
    out.update({
        "enabled": bool(prefs.get("enabled")),
        "disabledCategories": disabled,
        "smtpConfigured": bool(smtp.get("configured")),
        "delivered": stat.get("delivered", 0),
        "failed": stat.get("failed", 0),
    })
    return out


async def notification_history(args: dict, sid: str | None = None) -> dict:
    """通知投递账本最近记录（候选表只读，收件邮箱脱敏）。"""
    from app.domains.notifications.router import list_candidates

    limit = _clamp(args.get("limit"), 20, 1, 500)
    items = (await list_candidates(limit=limit)).get("items") or []
    if not items:
        return {"kind": "empty", "items": []}
    rows = [
        {"k": f"{it.get('eventType') or ''}·{it.get('region') or ''}".strip("·"),
         "vKey": "notifyCandidate", "v": _mask_addr(it.get("recipient") or ""),
         "at": it.get("deliveredAt"),
         "data": {"appid": it.get("appid"), "status": it.get("status"),
                  "attempts": it.get("attempts")},
         "tone": "ok" if it.get("status") == "delivered"
         else ("bad" if it.get("status") == "failed" else None)}
        for it in items
    ]
    return _shared.rows_result("notifyHistory", rows, total=len(items))


async def achievements_near_completion(args: dict, sid: str | None = None) -> dict:
    """快白金游戏清单（成就域进行中视角按完成度降序，取前 N）。"""
    limit = _clamp(args.get("limit"), 20, 1, 50)
    data = await achievements_service.list_games(
        filter_="progress", sort_="progress", target=args.get("steam_id") or None)
    games = (data.get("games") or [])[:limit]
    if not games:
        return {"kind": "empty", "games": []}
    rows = [
        {"k": g.get("name") or str(g.get("appid")), "vKey": "achNear",
         "data": {"percent": g.get("percent"), "unlocked": g.get("unlocked"),
                  "total": g.get("total"), "remaining": g.get("remaining"),
                  "platinum": bool(g.get("platinum"))}}
        for g in games
    ]
    return _shared.rows_result("achNearDone", rows, total=len(games))


async def achievements_game_detail(args: dict, sid: str | None = None) -> dict:
    """单游戏成就明细（进度概览 + 成就列表，成就域同一数据源）。"""
    appid = int(args["appid"])
    detail = await achievements_service.get_game_detail(appid, target=args.get("steam_id") or None)
    if detail is None:
        return {"kind": "empty", "appid": appid}
    achievements = [
        {"name": a.get("name"), "achieved": bool(a.get("achieved")),
         "rarity": a.get("rarity"), "globalPercent": a.get("globalPercent"),
         "unlockTime": a.get("unlockTime")}
        for a in (detail.get("achievements") or [])[: _shared._READ_ROWS_LIMIT]
    ]
    rows = [
        {"k": detail.get("name") or str(appid), "vKey": "achProgress",
         "data": {"percent": detail.get("percent"), "unlocked": detail.get("unlocked"),
                  "total": detail.get("total"), "platinum": bool(detail.get("platinum")),
                  "playtimeMin": detail.get("playtimeMin")},
         "tone": "ok"},
        *[{"k": a["name"] or "", "vKey": "achItem",
           "data": {"achieved": a["achieved"], "rarity": a["rarity"],
                    "unlockTime": a["unlockTime"]},
           "tone": "ok" if a["achieved"] else None}
          for a in achievements],
    ]
    out = _shared.rows_result("achGame", rows, total=len(rows))
    out.update({
        "appid": appid, "name": detail.get("name"),
        "percent": detail.get("percent"), "unlocked": detail.get("unlocked"),
        "total": detail.get("total"), "achievements": achievements,
    })
    return out


async def bills_spend_query(args: dict, sid: str | None = None) -> dict:
    """消费查询：账单导入总览或游戏交易明细（账单域同一查询）。"""
    import_id = int(args["import_id"])
    mode = args.get("mode") or "overview"
    if mode == "game_txs":
        data = await bills_service.game_txs(
            import_id,
            year=args.get("year") or None, month=args.get("month") or None,
            tx_type=args.get("tx_type") or None, search=args.get("search") or None,
            limit=_clamp(args.get("limit"), 50, 1, 200),
        )
        txs = data.get("rows") or []
        if not txs:
            return {"kind": "empty", "importId": import_id, "mode": mode}
        rows = [
            {"k": ((t.get("items") or [""])[0] if t.get("items") else "") or t.get("txType") or "",
             "vKey": "billTx", "at": t.get("date"),
             "data": {"cnyFen": t.get("cnyFen"), "txType": t.get("txType"),
                      "isRefund": bool(t.get("isRefund"))},
             "tone": None}
            for t in txs
        ]
        return _shared.rows_result("billsQuery", rows, total=data.get("total") or len(txs))

    try:
        data = await bills_service.overview(import_id)
    except KeyError:
        return {"kind": "empty", "importId": import_id, "mode": mode}
    summary = data.get("summary") or {}
    rows = [
        {"k": data.get("nickname") or data.get("sourceFile") or f"#{import_id}",
         "vKey": "billSpend", "at": data.get("importedAt"),
         "data": {"netFen": summary.get("netFen"), "spendFen": summary.get("spendFen"),
                  "refundFen": summary.get("refundFen"), "orders": summary.get("orders")}},
        {"k": "", "vKey": "billTopup",
         "data": {"netFen": summary.get("topupNetFen"),
                  "spendFen": summary.get("topupSpendFen")}},
    ]
    out = _shared.rows_result("billsQuery", rows, total=len(rows))
    out.update({
        "importId": import_id,
        "summary": {
            "netFen": summary.get("netFen"), "spendFen": summary.get("spendFen"),
            "refundFen": summary.get("refundFen"), "orders": summary.get("orders"),
            "txCount": summary.get("txCount"),
            "accountValueFen": summary.get("accountValueFen"),
            "topupNetFen": summary.get("topupNetFen"),
        },
        "years": [
            {"year": y.get("year"), "netFen": y.get("netFen"), "count": y.get("count")}
            for y in (data.get("years") or [])[:6]
        ],
        "counts": data.get("counts") or {},
    })
    return out


async def alert_history(args: dict, sid: str | None = None) -> dict:
    """价格提醒触发历史（提醒域事件账本只读）。"""
    limit = _clamp(args.get("limit"), 20, 1, 200)
    events = await alerts_service.list_events(limit=limit)
    if not events:
        return {"kind": "empty", "events": []}
    rows = [
        {"k": e.get("gameName") or str(e.get("appid")), "vKey": "alertHit",
         "at": e.get("triggeredAt"),
         "data": {"region": e.get("region"), "priceCny": e.get("priceCny"),
                  "priceText": e.get("priceText"), "notified": bool(e.get("notified"))},
         "tone": "ok"}
        for e in events
    ]
    return _shared.rows_result("alertHistory", rows, total=len(events))


async def update_status(args: dict, sid: str | None = None) -> dict:
    """应用更新状态（当前版本 / 下载进度 / 暂存包；只读，绝不触发联网检查或下载）。"""
    from app.core import updater

    current = get_settings().version
    progress = updater.download_progress()
    pending = updater.pending_status()
    rows = [{"k": "", "vKey": "updateCurrent", "v": current, "tone": "ok"}]
    if progress.get("running") or progress.get("percent") is not None:
        rows.append({"k": "", "vKey": "updateDownloading",
                     "data": {"phase": progress.get("phase"),
                              "percent": progress.get("percent"),
                              "received": progress.get("received"),
                              "total": progress.get("total"),
                              "error": (progress.get("error") or "")[:120] or None},
                     "tone": "bad" if progress.get("error") else None})
    if pending.get("pending"):
        rows.append({"k": "", "vKey": "updatePending", "v": pending.get("tag") or "",
                     "data": {"swapFailed": bool(pending.get("swapFailed"))},
                     "tone": "warn"})
    out = _shared.rows_result("updateStatus", rows, total=len(rows))
    out.update({
        "currentVersion": current,
        "checkedAt": None,
        "pending": bool(pending.get("pending")),
        "pendingTag": pending.get("tag"),
        "swapFailed": bool(pending.get("swapFailed")),
        "justUpdated": bool(pending.get("justUpdated")),
        "updateReport": pending.get("justUpdated"),
        "installing": bool(pending.get("installing")),
        "download": {
            "running": bool(progress.get("running")), "phase": progress.get("phase"),
            "percent": progress.get("percent"), "received": progress.get("received"),
            "total": progress.get("total"),
            "error": (progress.get("error") or "")[:120] or None,
        },
    })
    return out


SPECS = [
    ToolSpec(
        name="account_status", group="read", risk="low",
        description="查看 Steam 账号状态（昵称、在线状态、钱包余额、登录是否过期）。"
                    "用户问『我的账号状态』『余额还有多少』『登录过期了吗』时调用",
        parameters={"type": "object", "properties": {}},
        handler=account_status, step_label="accountStatus",
    ),
    ToolSpec(
        name="notifications_status", group="read", risk="low",
        description="查看通知开关与投递情况（总开关、静默时段、送达/失败计数、最近一封结果）。"
                    "用户问『通知开了吗』『邮件发成功没有』时调用",
        parameters={"type": "object", "properties": {}},
        handler=notifications_status, step_label="notifyStatus",
    ),
    ToolSpec(
        name="notification_history", group="read", risk="low",
        description="查看最近的通知投递记录（发了什么、状态如何、重试几次）。"
                    "用户问『最近发过哪些通知』『哪封没发出去』时调用",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，缺省 20"}
        }},
        handler=notification_history, step_label="notifyHistory",
    ),
    ToolSpec(
        name="achievements_near_completion", group="read", risk="low",
        description="列出快白金的游戏（成就进行中、按完成度从高到低）。"
                    "用户问『哪些游戏快白金了』『马上能白金的游戏』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"},
            "limit": {"type": "integer", "description": "返回条数，缺省 20"}
        }},
        handler=achievements_near_completion, step_label="achNearDone",
    ),
    ToolSpec(
        name="achievements_game_detail", group="read", risk="low",
        description="查看单个游戏的成就明细（总进度、白金状态、逐条成就与稀有度）。"
                    "用户问『这个游戏成就差哪些』『白金了吗』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 appid"},
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
        }, "required": ["appid"]},
        handler=achievements_game_detail, step_label="achGame",
    ),
    ToolSpec(
        name="bills_spend_query", group="read", risk="low",
        description="查询 Steam 消费账单（某次导入的总览合计，或逐笔游戏交易明细）。"
                    "用户问『这次导入花了多少』『某年买了什么』时调用",
        parameters={"type": "object", "properties": {
            "import_id": {"type": "integer", "description": "账单导入记录 id（bills 清单里可见）"},
            "mode": {"type": "string", "enum": ["overview", "game_txs"],
                     "description": "overview=总览合计（缺省）；game_txs=逐笔游戏交易"},
            "year": {"type": "string", "description": "game_txs 按年过滤，如 2025"},
            "month": {"type": "string", "description": "game_txs 按月过滤，如 2025-06"},
            "tx_type": {"type": "string", "description": "game_txs 按交易类型过滤"},
            "search": {"type": "string", "description": "game_txs 按游戏名搜索"},
            "limit": {"type": "integer", "description": "game_txs 返回条数，缺省 50"}
        }, "required": ["import_id"]},
        handler=bills_spend_query, step_label="billsQuery",
    ),
    ToolSpec(
        name="alert_history", group="read", risk="low",
        description="查看价格提醒的触发历史（哪个游戏、哪个区、什么价、有没有发通知）。"
                    "用户问『提醒最近触发过吗』『降价的记录』时调用",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，缺省 20"}
        }},
        handler=alert_history, step_label="alertHistory",
    ),
    ToolSpec(
        name="update_status", group="read", risk="low",
        description="查看应用更新状态（当前版本、下载进度、是否有待重启安装的更新包）；只读，不会触发检查或下载。"
                    "用户问『现在什么版本』『更新下到哪了』时调用",
        parameters={"type": "object", "properties": {}},
        handler=update_status, step_label="updateStatus",
    ),
]
