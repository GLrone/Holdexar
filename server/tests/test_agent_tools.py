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
    assert len(REGISTRY.names()) == 60
    assert len(set(REGISTRY.names())) == 60
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
    assert step == {"label": "navigate", "status": "ok", "data": {"target": "alerts", "path": "/alerts"}}
    step = pilot_tools.tool_step("list_alerts", {"kind": "rows", "rows": []})
    assert step["status"] == "empty" and step["data"] == {"count": 0}


def test_tool_budget_step_and_specs_facade():
    """门面三件套与注册表同源：specs / meta / budget / step。"""
    specs = pilot_tools.tool_specs()
    assert specs == REGISTRY.views()
    assert [s["function"]["name"] for s in specs][0] == "search_games"
    assert REGISTRY.get("cancel_task").parameters["properties"].get("run_id") is not None


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
