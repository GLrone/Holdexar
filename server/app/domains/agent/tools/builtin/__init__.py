"""builtin 工具集：58 个 ToolSpec 按旧 tool_specs() 顺序登记进注册表。

每工具预算（timeout_s）统一取 projection.budget_for（旧 tool_budget_s 分类），
等价性由 test_agent_tools.py 全量对比钉住。"""
from __future__ import annotations

from dataclasses import replace

from app.domains.agent.tools.builtin import (
    account,
    catalog,
    navigate,
    offers,
    prefs,
    prices,
    runtime_tools,
    system,
    tracking,
)
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.projection import budget_for
from app.domains.agent.tools.registry import REGISTRY

_SPEC_BY_NAME: dict[str, ToolSpec] = {}
for _mod in (catalog, prices, tracking, offers, account, system, runtime_tools, navigate, prefs):
    for _spec in _mod.SPECS:
        if _spec.name in _SPEC_BY_NAME:
            raise ValueError(f"重复注册工具: {_spec.name}")
        _SPEC_BY_NAME[_spec.name] = _spec

# 模型可见顺序 = 旧 tool_specs() 书写顺序（行为等价的一部分）
_ORDER = (
    "search_games",
    "search_steam",
    "ingest_appids",
    "web_search",
    "refresh_game_price",
    "retry_removed_game",
    "monitor_include",
    "notify_test",
    "get_price_briefing",
    "get_region_prices",
    "compare_games",
    "recommend_games",
    "find_deletables",
    "propose_delete",
    "propose_bulk",
    "add_follow",
    "navigate",
    "create_price_alert",
    "list_follows",
    "list_alerts",
    "list_bundle_follows",
    "diagnose_price",
    "recent_job_failures",
    "proxy_pool_status",
    "write_gate_status",
    "list_tasks",
    "start_task",
    "cancel_task",
    "list_sessions",
    "read_session",
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
    "update_price_alert",
    "bundle_follow",
    "region_toggle",
    "sync_wallet",
    "sync_library",
    "sync_achievements",
    "sync_bills",
    "sync_family",
    "refresh_rates",
    "get_user_preferences",
    "set_user_preference",
    "audit_follows_workflow",
)

if set(_ORDER) != set(_SPEC_BY_NAME) or len(_ORDER) != len(_SPEC_BY_NAME):
    _missing = set(_SPEC_BY_NAME) - set(_ORDER)
    _extra = set(_ORDER) - set(_SPEC_BY_NAME)
    raise RuntimeError(f"builtin 工具集与登记顺序不一致 missing={_missing} extra={_extra}")

for _name in _ORDER:
    REGISTRY.register(replace(_SPEC_BY_NAME[_name], timeout_s=budget_for(_name)))
