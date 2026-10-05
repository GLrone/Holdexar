"""pilot 工具层门面：实现已收编进 agent.tools（Agent V2 P3），本模块
保留旧公开符号与问句解析 helper，service / intent / 既有测试零改动。

一处声明（agent/tools/builtin/*/SPECS）+ 一个词条键 = 加一个新工具；
写动作守卫、逐工具预算与时间线映射由 agent/tools 的 policy / projection
统一承载。"""
from __future__ import annotations

import re

from app.domains.account import service as account_service
from app.domains.achievements import service as achievements_service
from app.domains.alerts import service as alerts_service
from app.domains.alerts.notify import get_smtp_config, send_test_mail
from app.domains.bills import service as bills_service
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
from app.domains.agent.runtime import executor as _executor
from app.domains.agent.tools.builtin import _shared
from app.domains.agent.tools.builtin.account import (
    achievements_summary,
    bills_summary,
    family_status,
    list_accounts,
    list_family_library,
    list_owned,
    list_wishlist,
    redeem_quota,
    sync_achievements,
    sync_bills,
    sync_family,
    sync_library,
    sync_wallet,
)
from app.domains.agent.tools.builtin.catalog import (
    _game_item,
    _steam_storesearch,
    ingest_appids,
    recommend_games,
    recommend_games_by_filters,
    search_games,
    search_steam,
    top_games,
)
from app.domains.agent.tools.builtin.navigate import NAV_TARGETS
from app.domains.agent.tools.builtin.offers import (
    calendar_events,
    epic_free,
    hb_monthly,
    price_drops,
    rates_overview,
    steam_free,
)
from app.domains.agent.tools.builtin.prefs import _get_user_preferences, _set_user_preference  # noqa: F401
from app.domains.agent.tools.builtin.prices import (
    _trend_series,
    compare_games,
    diagnose_price,
    price_facts,
    region_prices,
)
from app.domains.agent.tools.builtin.runtime_tools import (
    _TASK_STATUS_ROW,
    _task_row,
    cancel_task,
    list_sessions,
    list_tasks,
    read_session,
    start_task,
)
from app.domains.agent.tools.builtin.system import (
    _parse_ddg,
    notify_test,
    proxy_pool_status,
    recent_job_failures,
    refresh_game_price,
    web_search,
    write_gate_status,
)
from app.domains.agent.tools.builtin.tracking import (
    audit_follows_workflow,
    bundle_follow,
    find_deletables,
    list_alerts,
    list_bundles,
    list_bundle_follows,
    list_follows,
    monitor_add,
    monitor_include,
    alert_add,
    propose_bulk,
    propose_delete,
    retry_removed,
    region_toggle,
    update_price_alert,
    _scan_deletables,
)
from app.domains.agent.tools.projection import (
    BUDGET_DEFAULT_S as _BUDGET_DEFAULT_S,
    BUDGET_SLOW_S as _BUDGET_SLOW_S,
    BUDGET_TASK_S as _BUDGET_TASK_S,
    LABELS as _LABELS,
    QUICK_TOOLS,
    _NET_TOOLS,
    _READ_TOOLS,
    _SYNC_TOOLS,
    _TASK_TOOLS,
    _TOOL_BUDGET_S,
    _WRITE_TOOLS,
    tool_step,
)
from app.domains.agent.tools.registry import REGISTRY

# 触发 builtin 登记（58 个 ToolSpec 进注册表）
from app.domains.agent.tools.builtin import catalog as _catalog  # noqa: F401 — 触发 58 工具注册

_FIND_LIMIT = _shared._FIND_LIMIT
_READ_ROWS_LIMIT = _shared._READ_ROWS_LIMIT
_BULK_MAX_ITEMS = _shared._BULK_MAX_ITEMS
_BULK_ACTIONS = _shared._BULK_ACTIONS
_STEAM_SEARCH_LIMIT = 5
_INGEST_MAX = 20
_COMPARE_MAX = 3
_TREND_MAX_POINTS = 72

_PRICE_TAG_RE = _shared._PRICE_TAG_RE
_PRICE_NUM_RE = _shared._PRICE_NUM_RE
_THRESH_RE = _shared._THRESH_RE
_QUERY_CLEAN_RE = _shared._QUERY_CLEAN_RE

TOOL_META: dict[str, dict] = REGISTRY.meta()


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


def tool_budget_s(name: str) -> float | None:
    """工具执行预算（秒）。"""
    spec = REGISTRY.get(name)
    return spec.timeout_s if spec else _BUDGET_DEFAULT_S


def tool_specs() -> list[dict]:
    """agent 循环的工具表（OpenAI function 格式），来自注册表视图投影。"""
    return REGISTRY.views()


async def execute_tool(name: str, arguments: dict, *, guarded: bool = False,
                       sid: str | None = None) -> dict:
    """工具执行分发：守卫 / 校验 / 超时 / 失败归一由 agent.tools.executor 承载。

    写工具受守卫复核：问句含批量 / 删除 / 停用语义时拒绝执行（返回
    kind=denied，模型改走 propose_bulk 交用户确认）。"""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"kind": "unknown_tool"}
    result = await _executor.execute(spec, arguments, guarded=guarded, sid=sid)
    return result.data
