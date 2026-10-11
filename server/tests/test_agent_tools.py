"""agent.tools 契约单测：注册表 / 投影 / 策略 / 执行器（不触网）。"""
import asyncio

import pytest

from app.domains.agent.runtime import executor
from app.domains.agent.tools import policy, projection
from app.domains.agent.tools.base import ToolSpec, derive_status
from app.domains.agent.tools.registry import REGISTRY
from app.domains.pilot import tools as pilot_tools


def _spec(name):
    spec = REGISTRY.get(name)
    assert spec is not None, name
    return spec


def test_registry_holds_all_tools():
    assert len(REGISTRY.names()) == 65
    assert len(set(REGISTRY.names())) == 65
    assert set(REGISTRY.names()) == set(projection.LABELS)


def test_views_shape_and_order():
    views = REGISTRY.views()
    assert [v["function"]["name"] for v in views] == REGISTRY.names()
    assert views[0]["function"]["name"] == "search_games"
    for v in views:
        assert v["type"] == "function"
        assert set(v["function"]) == {"name", "description", "parameters"}
        assert isinstance(v["function"]["parameters"], dict)


def test_facade_meta_matches_registry():
    assert pilot_tools.TOOL_META == REGISTRY.meta()
    assert pilot_tools.TOOL_META["search_games"] == {"label": "search"}


def test_group_sets_match_projection():
    """注册表分组与投影集合（守卫 / 预算 / 时间线依据）一一对应。"""
    assert REGISTRY.names_of_group("write") == frozenset(projection._WRITE_TOOLS)
    all_names = set(REGISTRY.names())
    legacy_non_write = all_names - set(projection._WRITE_TOOLS) - {"navigate"}
    assert {n for n in legacy_non_write if _spec(n).group != "navigate"} == legacy_non_write - {"navigate"} - {
        n for n in legacy_non_write if _spec(n).group == "navigate"}
    assert REGISTRY.get("navigate").group == "navigate"


def test_timeouts_match_legacy_budget():
    for name in REGISTRY.names():
        assert _spec(name).timeout_s == projection.budget_for(name), name
    assert pilot_tools.tool_budget_s("add_follow") == projection.BUDGET_SLOW_S
    assert pilot_tools.tool_budget_s("start_task") == projection.BUDGET_TASK_S
    assert pilot_tools.tool_budget_s("search_games") == projection.BUDGET_DEFAULT_S
    assert pilot_tools.tool_budget_s("diagnose_price") == 60.0
    assert pilot_tools.tool_budget_s("nope") == projection.BUDGET_DEFAULT_S


def test_guard_words_and_deny_shape():
    assert policy.GUARD_WORDS == ("清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有")
    assert policy.is_guarded("清空所有提醒规则") is True
    assert policy.is_guarded("看看我的关注") is False
    assert pilot_intent_guarded_parity()


def pilot_intent_guarded_parity():
    from app.domains.pilot import intent as pilot_intent

    for w in policy.GUARD_WORDS:
        if not pilot_intent.is_guarded(f"问题{w}问题"):
            return False
    return True


def test_executor_denies_write_on_guard():
    result = asyncio.run(executor.execute(_spec("add_follow"), {"appid": 1}, guarded=True))
    assert result.status == "denied"
    assert result.data == {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}
    assert pilot_tools.execute_tool.__doc__  # 门面在位


def test_executor_allows_read_even_guarded():
    async def _fast(args, sid=None):
        return {"kind": "rows", "rows": [{"k": "a"}]}

    spec = ToolSpec(name="t_read", group="read", risk="low", description="d",
                    parameters={"type": "object", "properties": {}},
                    handler=_fast, step_label="t", timeout_s=1.0)
    result = asyncio.run(executor.execute(spec, {}, guarded=True))
    assert result.status == "ok"


def test_executor_timeout_shape():
    async def _slow(args, sid=None):
        await asyncio.sleep(5)

    spec = ToolSpec(name="t_slow", group="read", risk="low", description="d",
                    parameters={"type": "object", "properties": {}},
                    handler=_slow, step_label="t", timeout_s=0.05)
    result = asyncio.run(executor.execute(spec, {}))
    assert result.status == "timeout"
    assert result.data == {"kind": "timeout", "tool": "t_slow", "budget_s": 0}


def test_executor_exception_normalized():
    async def _boom(args, sid=None):
        raise ValueError("bad input")

    spec = ToolSpec(name="t_boom", group="read", risk="low", description="d",
                    parameters={"type": "object", "properties": {}},
                    handler=_boom, step_label="t", timeout_s=1.0)
    result = asyncio.run(executor.execute(spec, {}))
    assert result.status == "error"
    assert result.data == {"kind": "failed", "tool": "t_boom", "note": "ValueError"}


def test_facade_unknown_tool():
    out = asyncio.run(pilot_tools.execute_tool("no_such_tool", {}))
    assert out == {"kind": "unknown_tool"}


def test_derive_status_branches():
    assert derive_status({"kind": "denied"}) == "denied"
    assert derive_status({"kind": "empty"}) == "empty"
    assert derive_status({"kind": "failed"}) == "error"
    assert derive_status({"kind": "timeout"}) == "timeout"
    assert derive_status({"kind": "rows"}) == "ok"
    assert derive_status({}) == "ok"


def test_tool_step_projection():
    assert pilot_tools.tool_step("add_follow", {"kind": "denied"}) == {"label": "denied", "status": "denied", "data": {}}
    assert pilot_tools.tool_step("web_search", {"kind": "timeout"})["status"] == "timeout"
    assert pilot_tools.tool_step("web_search", {"kind": "failed"})["status"] == "failed"
    step = pilot_tools.tool_step("search_games", {"kind": "games", "items": [1, 2]})
    assert step == {"label": "search", "status": "ok", "data": {"count": 2}}
    step = pilot_tools.tool_step("navigate", {"kind": "navigate", "target": "alerts", "path": "/alerts"})
    assert step == {"label": "navigate", "status": "ok", "data": {"target": "alerts", "path": "/alerts", "section": ""}}
    step = pilot_tools.tool_step("list_alerts", {"kind": "rows", "rows": []})
    assert step["status"] == "empty" and step["data"] == {"count": 0}


def test_tool_budget_step_and_specs_facade():
    """门面三件套与注册表同源：specs / meta / budget / step。

    默认切面只给核心工具（模型侧省固定开销，见 pilot/tools.py::tool_specs）；
    full=True 才是全量表，等价性对比走它。emit_card 只注入产卡工具。"""
    specs = pilot_tools.tool_specs(full=True)
    assert specs == REGISTRY.views(card_tools=pilot_tools.CARD_TOOLS)
    assert [s["function"]["name"] for s in specs][0] == "search_games"
    assert REGISTRY.get("cancel_task").parameters["properties"].get("run_id") is not None
    # 默认切面 = 全量表的核心子集，且恒含发现入口与附件读取
    core = {s["function"]["name"] for s in pilot_tools.tool_specs()}
    assert core < {s["function"]["name"] for s in specs}
    assert {"list_tools", "read_attachment"} <= core


def test_card_tools_are_the_only_ones_with_emit_card():
    """emit_card 只给产卡工具：其余工具带上它，是每次请求都白付的固定开销。"""
    by_name = {s["function"]["name"]: s["function"] for s in pilot_tools.tool_specs(full=True)}
    with_emit = {n for n, f in by_name.items() if "emit_card" in (f["parameters"].get("properties") or {})}
    assert with_emit == set(pilot_tools.CARD_TOOLS)
    assert "navigate" in with_emit and "read_logs" not in with_emit


def test_core_and_expandable_partition_the_catalog():
    """核心切面 + 可解锁 + list_tools 正好是全部工具，不重不漏。"""
    all_names = set(REGISTRY.names())
    core = set(pilot_tools.CORE_TOOLS)
    extra = set(pilot_tools.expandable_tools())
    assert core | extra == all_names
    assert not (core & extra)
    assert pilot_tools.LIST_TOOLS in core


def test_sid_passes_through_to_handler():
    seen = {}

    async def _need_sid(args, sid=None):
        seen["sid"] = sid
        return {"kind": "empty"}

    spec = ToolSpec(name="t_sid", group="read", risk="low", description="d",
                    parameters={"type": "object", "properties": {}},
                    handler=_need_sid, step_label="t", timeout_s=1.0)
    asyncio.run(executor.execute(spec, {}, sid="s-1"))
    assert seen["sid"] == "s-1"


@pytest.mark.parametrize("name,label", [
    ("get_user_preferences", "getPreferences"),
    ("set_user_preference", "setPreference"),
    ("list_family_library", "famLibrary"),
    ("find_deletables", "deletables"),
    ("propose_delete", "proposeDelete"),
])
def test_step_labels_without_i18n_keys_pinned(name, label):
    """5 个尚未有 pilot.step 词条键的 label 在此钉住（补键后本用例即护栏）。"""
    assert _spec(name).step_label == label


class TestPerceptionTools:
    """感知面工具：日志读取 / 诊断证据面 / 导航板块与游戏详情。"""

    def test_navigate_section_whitelist(self):
        from app.domains.agent.tools.builtin.navigate import _navigate

        out = asyncio.run(_navigate({"target": "gamelib", "section": "insights"}))
        assert out == {"kind": "navigate", "target": "gamelib", "path": "/gamelib",
                       "section": "insights"}
        # 白名单外板块拒绝（原样不带 section）
        out2 = asyncio.run(_navigate({"target": "gamelib", "section": "nope"}))
        assert "section" not in out2
        # 不支持板块的页面忽略 section
        out3 = asyncio.run(_navigate({"target": "alerts", "section": "ledger"}))
        assert "section" not in out3

    def test_navigate_game_detail(self):
        from app.domains.agent.tools.builtin.navigate import _navigate

        out = asyncio.run(_navigate({"target": "game", "appid": 2358720}))
        assert out == {"kind": "navigate", "target": "game", "path": "/game/2358720",
                       "appid": 2358720}
        # 无 appid / 非法 appid 拒绝
        assert asyncio.run(_navigate({"target": "game"}))["path"] == ""
        assert asyncio.run(_navigate({"target": "game", "appid": "x"}))["path"] == ""

    def test_navigate_tool_step_carries_section(self):
        step = pilot_tools.tool_step("navigate", {"kind": "navigate", "target": "bills",
                                                  "path": "/bills", "section": "cdk"})
        assert step["data"]["section"] == "cdk"

    def test_read_logs_filters_and_counts(self):
        from app.domains.agent.tools.builtin.system import read_logs

        out = asyncio.run(read_logs(tail=50))
        assert out["kind"] == "rows" and out["titleKey"] == "logs"
        assert out["total"] >= 0 and isinstance(out["rows"], list)
        # 过滤形态：关键词 + 级别（环内无命中也必须结构合法）
        out2 = asyncio.run(read_logs(keyword="__no_such_marker__", level="error", tail=10))
        assert out2["rows"] == [] and out2["total"] == 0

    def test_system_diagnostics_shape(self):
        from app.domains.agent.tools.builtin.system import system_diagnostics

        out = asyncio.run(system_diagnostics())
        assert out["kind"] == "rows" and out["titleKey"] == "diag"
        assert isinstance(out["rows"], list) and out["rows"]

    def test_sync_subscriptions_no_subs(self, monkeypatch):
        from app.domains.agent.tools.builtin import system

        async def _empty(kind=None):
            return []

        monkeypatch.setattr(system.proxies_service, "list_subscriptions", _empty)
        out = asyncio.run(system.sync_subscriptions())
        assert out["rows"] == [{"k": "", "vKey": "subsNone", "tone": "warn"}]

    def test_sync_subscriptions_reports_without_url(self, monkeypatch):
        from app.domains.agent.tools.builtin import system

        async def _subs(kind=None):
            return [{"id": 7, "kind": "clash", "label": "机场A", "deprecated": False},
                    {"id": 8, "kind": "clash", "label": "旧", "deprecated": True}]

        async def _refresh(sub_id):
            assert sub_id == 7
            return {"restarted": False}

        monkeypatch.setattr(system.proxies_service, "list_subscriptions", _subs)
        monkeypatch.setattr(system.proxies_service, "refresh_clash_subscription", _refresh)
        out = asyncio.run(system.sync_subscriptions())
        assert out["total"] == 1
        assert out["rows"][0]["k"] == "机场A" and out["rows"][0]["tone"] == "ok"
        # 红线：回执任何位置不得出现订阅 URL
        import json as _json
        assert "http" not in _json.dumps(out, ensure_ascii=False)

    def test_new_tools_registered_with_budgets(self):
        for name in ("read_logs", "system_diagnostics", "sync_subscriptions"):
            spec = REGISTRY.get(name)
            assert spec is not None
            assert spec.timeout_s == projection.budget_for(name)
        assert "sync_subscriptions" in projection._SYNC_TOOLS
