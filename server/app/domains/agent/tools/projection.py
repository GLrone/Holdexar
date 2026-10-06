"""工具投影：分组集合、逐工具执行预算与时间线步骤映射。

自 pilot/tools.py 逐字收编（Agent V2 P3）；分组集合与注册表 group
的一致性由 test_agent_tools.py 钉住。"""
from __future__ import annotations

# ─── 只读工具：清单与诊断（全只读，不设守卫；行形态见 _rows_result）───

_READ_TOOLS = (
    "list_follows",
    "list_alerts",
    "list_bundle_follows",
    "diagnose_price",
    "recent_job_failures",
    "proxy_pool_status",
    "proxy_exit_detail",
    "proxy_latency_test",
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

# 逐工具执行预算（秒）：只读与网络类防「永不返回」；写与同步类的等待多为共享
# 写闸排队，预算只作兜底——短预算会把正常排队误判成失败。分类默认值 + 单工具覆盖。
BUDGET_DEFAULT_S = 20.0
BUDGET_SLOW_S = 240.0
BUDGET_TASK_S = 60.0
_TOOL_BUDGET_S: dict[str, float] = {
    # 网络往返类（工具内另有 12s 请求超时，预算给足往返与解析）
    "search_steam": 40.0, "web_search": 40.0, "hb_monthly": 40.0, "epic_free": 40.0,
    "steam_free": 40.0, "calendar_events": 40.0, "rates_overview": 40.0,
    # 重扫 / 聚合类（跨表聚合与全量扫描显著更慢）
    "diagnose_price": 60.0, "bills_summary": 60.0, "achievements_summary": 60.0,
    "list_family_library": 60.0, "list_owned": 60.0, "top_games": 60.0,
    "price_drops": 60.0, "list_bundles": 60.0, "read_session": 60.0,
    "find_deletables": 90.0, "audit_follows_workflow": 90.0,
    # 全量出口检测（逐节点探测，分钟级；工具内轮询窗 170s 略小于此预算）
    "proxy_latency_test": 180.0,
}


def budget_for(name: str) -> float:
    """工具执行预算（秒）。"""
    if name in _WRITE_TOOLS or name in _SYNC_TOOLS or name in ("propose_bulk", "propose_delete"):
        return BUDGET_SLOW_S
    if name in _TASK_TOOLS:
        return BUDGET_TASK_S
    return _TOOL_BUDGET_S.get(name, BUDGET_DEFAULT_S)


# 旧名兼容（test 断言引用）
_BUDGET_DEFAULT_S = BUDGET_DEFAULT_S
_BUDGET_SLOW_S = BUDGET_SLOW_S
_BUDGET_TASK_S = BUDGET_TASK_S


def tool_step(name: str, result: dict) -> dict:
    """工具执行结果 → 时间线步骤条目。

    返回 {label, status, data}：label 对应前端词条键片段；status 取
    ok / empty / denied / timeout / failed；data 只装词条插值所需的最小数据
    （结果计数、对象名、导航目标）。判定走数据层，前端不复制这套语义。"""
    if result.get("kind") == "denied":
        return {"label": "denied", "status": "denied", "data": {}}
    if result.get("kind") in ("timeout", "failed"):
        return {
            "label": LABELS.get(name, name),
            "status": result["kind"],
            "data": {},
        }
    if name == "audit_follows_workflow":
        stepper = result.get("stepper") or {}
        steps = stepper.get("steps") or []
        ok = any(s.get("status") == "ok" for s in steps)
        return {
            "label": LABELS.get(name, name),
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
            "label": LABELS.get(name, name),
            "status": "ok" if items else "empty",
            "data": {"count": len(items)},
        }
    if name in ("achievements_summary", "family_status"):
        ok = bool(result.get("hasCredential", result.get("bound", True)))
        return {
            "label": LABELS.get(name, name),
            "status": "ok" if ok else "empty",
            "data": {},
        }
    if name in _READ_TOOLS:
        rows = result.get("rows") or []
        data: dict = {"count": len(rows)}
        if name == "diagnose_price" and result.get("name"):
            data["name"] = result.get("name")
        return {
            "label": LABELS.get(name, name),
            "status": "ok" if rows else "empty",
            "data": data,
        }
    if name in _TASK_TOOLS:
        # 调度类结果同样是行卡；行色调决定时间线状态（busy/warn ≠ 成功）
        rows = result.get("rows") or []
        tone = (rows[0].get("tone") if rows else None) or "ok"
        return {
            "label": LABELS.get(name, name),
            "status": "denied" if tone == "bad" else ("empty" if tone == "warn" else "ok"),
            "data": {},
        }
    if name in _WRITE_TOOLS or name in _SYNC_TOOLS:
        # 写与同步的结果也是行卡：时间线只给动作名（结果进卡片与正文）
        rows = result.get("rows") or []
        return {
            "label": LABELS.get(name, name),
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
    return {"label": LABELS.get(name, name), "status": "ok", "data": {}}


# 机器名 → 用户面步骤词条片段（label 与 i18n key `pilot.step.{label}` 对应）。
# 机器名只进模型协议与日志，不进用户面。来源：旧 TOOL_META 全量 58 条。
LABELS: dict[str, str] = {
    "search_games": "search",
    "get_price_briefing": "price",
    "get_region_prices": "regionPrices",
    "compare_games": "compare",
    "recommend_games": "recommend",
    "add_follow": "follow",
    "create_price_alert": "alert",
    "propose_bulk": "propose",
    "navigate": "navigate",
    "list_follows": "follows",
    "list_alerts": "alerts",
    "list_bundle_follows": "bundleFollows",
    "diagnose_price": "diagnose",
    "recent_job_failures": "jobFailures",
    "proxy_pool_status": "proxyStatus",
    "proxy_exit_detail": "proxyExits",
    "proxy_latency_test": "proxyLatencyTest",
    "write_gate_status": "gateStatus",
    "list_tasks": "tasks",
    "start_task": "taskStart",
    "cancel_task": "taskCancel",
    "list_sessions": "sessions",
    "read_session": "sessionRead",
    "hb_monthly": "hbMonthly",
    "epic_free": "epicFree",
    "steam_free": "steamFree",
    "top_games": "topGames",
    "price_drops": "priceDrops",
    "rates_overview": "rates",
    "calendar_events": "calendar",
    "list_accounts": "accounts",
    "list_wishlist": "wishlist",
    "list_owned": "owned",
    "list_family_library": "famLibrary",
    "achievements_summary": "achievements",
    "bills_summary": "bills",
    "family_status": "family",
    "redeem_quota": "redeem",
    "list_bundles": "bundlesAll",
    "update_price_alert": "updateAlert",
    "bundle_follow": "bundleFollow",
    "region_toggle": "regionToggle",
    "sync_wallet": "syncWallet",
    "sync_library": "syncLibrary",
    "sync_achievements": "syncAchievements",
    "sync_bills": "syncBills",
    "sync_family": "syncFamily",
    "refresh_rates": "refreshRates",
    "search_steam": "steamSearch",
    "ingest_appids": "ingest",
    "web_search": "webSearch",
    "refresh_game_price": "refreshPrice",
    "retry_removed_game": "retryRemoved",
    "monitor_include": "monitorInclude",
    "notify_test": "notifyTest",
    "find_deletables": "deletables",
    "propose_delete": "proposeDelete",
    "get_user_preferences": "getPreferences",
    "set_user_preference": "setPreference",
    "audit_follows_workflow": "auditFollowsWorkflow",
}
