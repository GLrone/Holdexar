"""pilot 意图路由、agent 工具循环与降级路径单测（LLM 以替身注入，不触网、不依赖真实库）。"""
import asyncio
import json

import pytest

from app.domains.pilot import config as pilot_config
from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import service
from app.domains.pilot import llm as pilot_llm
from app.domains.pilot.llm import PilotLlmError

_CFG = {
    "protocol": "openai",
    "enabled": True,
    "base_url": "http://127.0.0.1:9/v1",
    "model": "test-model",
    "api_key": "sk-test",
    "monthly_cap": 1000,
}
_FACTS = {
    "kind": "price",
    "appid": 1,
    "name": "测试游戏",
    "positiveRate": 0.9,
    "reviewCount": 100,
    "cn": {"cnyFen": 5000, "discount": 50},
    "lowest": {"cnyFen": 4000, "snapshotAt": "2026-01-01T00:00:00"},
    "year": {"minFen": 4000, "maxFen": 12000, "medianFen": 8000, "count": 50},
}


def _async_return(value):
    async def _fn(*a, **kw):
        return value
    return _fn


def _patch_ready(
    monkeypatch,
    *,
    cfg=None,
    usage=0,
    stream=None,
    fail=False,
    usage_store=None,
):
    """agent 路径替身：chat_stream 产出脚本化事件序列；fail 时抛错。"""
    monkeypatch.setattr(pilot_config, "load_config", _async_return(cfg or _CFG))
    usage_dict = usage if isinstance(usage, dict) else {"inp": 0, "out": 0, "calls": 0, "total": usage}
    monkeypatch.setattr(pilot_config, "usage_month", _async_return(usage_dict))
    store = usage_store if usage_store is not None else {}

    async def _add(inp, out):
        store["tokens"] = store.get("tokens", 0) + inp + out

    monkeypatch.setattr(pilot_config, "add_usage", _add)
    if fail:
        async def _boom(**kw):
            raise PilotLlmError("boom")
            yield  # pragma: no cover —— 使本函数成为异步生成器（循环内首帧即抛）
        monkeypatch.setattr(service.pilot_llm, "chat_stream", _boom)
    elif stream is not None:
        it = iter(stream)

        async def _scripted(**kw):
            for item in it:
                yield item

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _scripted)


def _tool_stub(executed, result):
    """execute_tool 替身：记录 (name, appid, guarded) 并返回固定回执。"""

    async def _fn(name, arguments, guarded=False):
        executed.append((name, arguments.get("appid"), guarded))
        return {**result, "appid": arguments.get("appid")}

    return _fn


@pytest.fixture(autouse=True)
def _clear_sessions():
    service._sessions.clear()
    yield
    service._sessions.clear()


class TestIntentRoute:
    def test_write_intents(self):
        assert pilot_intent.route("把双人成行加进关注") == pilot_intent.ADD_MONITOR
        assert pilot_intent.route("关注一下 DAVE THE DIVER") == pilot_intent.ADD_MONITOR
        assert pilot_intent.route("星露谷物语低于 48 块提醒我") == pilot_intent.CREATE_ALERT
        assert pilot_intent.route("打五折的话提醒我一声") == pilot_intent.CREATE_ALERT

    def test_guard_blocks_write(self):
        assert pilot_intent.route("清空所有提醒规则") != pilot_intent.CREATE_ALERT
        assert pilot_intent.route("批量移除监控对象") != pilot_intent.ADD_MONITOR
        assert pilot_intent.is_guarded("清空所有提醒规则") is True

    def test_how_to(self):
        assert pilot_intent.route("怎么设置价格提醒") == pilot_intent.HOW_TO
        assert pilot_intent.route("在哪加关注") == pilot_intent.HOW_TO

    def test_price_and_find(self):
        assert pilot_intent.route("赛博朋克2077 现在史低多少") == pilot_intent.PRICE_ANALYSIS
        assert pilot_intent.route("推荐两个适合双人玩的游戏") == pilot_intent.FIND_GAMES
        assert pilot_intent.route("今天天气怎么样") == pilot_intent.CHAT


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_tool_loop_executes_and_answers(self, monkeypatch):
        executed = []
        usage = {}
        _patch_ready(
            monkeypatch,
            usage_store=usage,
            stream=[
                ("tool_calls", [{"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030}}]),
                ("answer", "结论：值得。"),
                ("usage", (100, 20)),
            ],
        )
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, _FACTS),
        )
        events = [e async for e in service.ask_stream("这游戏值得入手吗", session_id="s1")]
        kinds = [e["type"] for e in events]
        assert "tool_start" in kinds and "tool" in kinds
        started = [e for e in events if e["type"] == "tool_start"][0]
        assert started["name"] == "get_price_briefing" and started["label"] == "price"
        tool_ev = [e for e in events if e["type"] == "tool"][0]
        assert tool_ev["label"] == "price" and tool_ev["status"] == "ok"
        assert tool_ev["data"] == {"name": "测试游戏"}
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert executed == [("get_price_briefing", 292030, False)]
        assert done["tools"] == ["get_price_briefing: 测试游戏"]
        # 时间线随 done 全量下发（前端历史轮按它还原过程）
        assert done["steps"] == [{"label": "price", "status": "ok", "data": {"name": "测试游戏"}}]
        assert usage["tokens"] == 120
        # 工具结果沉淀为结构化卡片（前端组件渲染数据层）
        assert done["cards"] == [{"kind": "price", **{**_FACTS, "appid": 292030}}]

    @pytest.mark.asyncio
    async def test_write_guard_denies_execution(self, monkeypatch):
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [{"id": "t1", "name": "add_follow", "arguments": {"appid": 2}}]),
                ("answer", "已拒绝。"),
            ],
        )
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, {"kind": "denied", "note": "guarded"}),
        )
        events = [e async for e in service.ask_stream("批量把所有游戏加进关注")]
        tool_events = [e for e in events if e["type"] == "tool"]
        assert tool_events and tool_events[0]["status"] == "denied"
        assert tool_events[0]["label"] == "denied"
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert done["steps"] == [{"label": "denied", "status": "denied", "data": {}}]

    @pytest.mark.asyncio
    async def test_steps_accumulate_across_rounds(self, monkeypatch):
        _patch_ready(monkeypatch)
        scripts = [
            [("tool_calls", [{"id": "t1", "name": "search_games", "arguments": {"q": "x"}}])],
            [("tool_calls", [{"id": "t2", "name": "get_price_briefing", "arguments": {"appid": 7}}])],
            [("answer", "完成")],
        ]
        rounds = {"n": 0}

        async def _rounds(**kw):
            for item in scripts[rounds["n"]]:
                yield item
            rounds["n"] += 1

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _rounds)

        async def _multi(name, arguments, guarded=False):
            if name == "search_games":
                return {"kind": "games", "items": [{"appid": i} for i in range(3)]}
            return {**_FACTS, "appid": arguments.get("appid")}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _multi)
        events = [e async for e in service.ask_stream("找一款试试", session_id="s-steps")]
        pairs = [(e["type"], e.get("label")) for e in events if e["type"] in ("tool_start", "tool")]
        assert pairs == [("tool_start", "search"), ("tool", "search"),
                         ("tool_start", "price"), ("tool", "price")]
        done = events[-1]
        assert done["steps"] == [
            {"label": "search", "status": "ok", "data": {"count": 3}},
            {"label": "price", "status": "ok", "data": {"name": "测试游戏"}},
        ]

    @pytest.mark.asyncio
    async def test_session_history_reaches_model(self, monkeypatch):
        seen_messages = []
        _patch_ready(
            monkeypatch,
            stream=[("answer", "好")],
        )

        async def _scripted(**kw):
            seen_messages.append([dict(m) for m in kw["messages"]])
            for item in [("answer", "好"),]:
                yield item

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _scripted)
        await service.ask("巫师 3 值得入手吗", session_id="s-h")
        await service.ask("把它加进关注", session_id="s-h")
        second = seen_messages[1]
        roles = [m["role"] for m in second]
        assert roles.count("user") == 2
        assert any(m.get("role") == "assistant" and m.get("content") == "好" for m in second)


class TestFallback:
    @pytest.mark.asyncio
    async def test_llm_off_price_facts(self, monkeypatch):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(service.pilot_tools, "price_facts", _async_return(dict(_FACTS)))
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["facts"]["kind"] == "price"

    @pytest.mark.asyncio
    async def test_llm_off_chat_reason(self, monkeypatch):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        resp = await service.ask("今天天气怎么样")
        assert resp["source"] == "none"
        assert resp["reason"] == "llm_off"

    @pytest.mark.asyncio
    async def test_cap_degrades_to_facts(self, monkeypatch):
        _patch_ready(monkeypatch, usage={"inp": 1000, "out": 0, "calls": 1, "total": 1000})
        monkeypatch.setattr(service.pilot_tools, "price_facts", _async_return(dict(_FACTS)))
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["reason"] == "cap_reached"

    @pytest.mark.asyncio
    async def test_llm_failure_degrades_to_facts(self, monkeypatch):
        _patch_ready(monkeypatch, fail=True)
        monkeypatch.setattr(service.pilot_tools, "price_facts", _async_return(dict(_FACTS)))
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["reason"] == "llm_failed"

    @pytest.mark.asyncio
    async def test_guide(self, monkeypatch):
        _patch_ready(monkeypatch)
        resp = await service.ask("怎么设置价格提醒")
        assert resp["source"] == "guide"

    @pytest.mark.asyncio
    async def test_fallback_ambiguous_write(self, monkeypatch):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(
            service.pilot_tools,
            "search_games",
            _async_return([{"appid": 1, "name": "A", "cnyFen": 100, "discount": 0,
                            "positiveRate": None, "reviewCount": 0},
                           {"appid": 2, "name": "B", "cnyFen": 200, "discount": 0,
                            "positiveRate": None, "reviewCount": 0}]),
        )
        resp = await service.ask("把里奥的宝藏加进关注")
        assert resp["source"] == "facts"
        assert resp["reason"] == "need_target"

    @pytest.mark.asyncio
    async def test_fallback_alert_without_threshold_guide(self, monkeypatch):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(
            service.pilot_tools,
            "search_games",
            _async_return([{"appid": 8, "name": "星露谷物语", "cnyFen": 4800, "discount": 0,
                            "positiveRate": 0.98, "reviewCount": 90000}]),
        )
        resp = await service.ask("星露谷物语低于心里价位提醒我")
        assert resp["source"] == "guide"

    @pytest.mark.asyncio
    async def test_fallback_no_data(self, monkeypatch):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(service.pilot_tools, "price_facts", _async_return(None))
        monkeypatch.setattr(service.pilot_tools, "recommend_games", _async_return([]))
        resp = await service.ask("史低多少", appid=999)
        assert resp["source"] == "none"
        assert resp["reason"] == "no_data"


class TestFlywheel:
    @pytest.mark.asyncio
    async def test_decision_log_written(self, monkeypatch, tmp_path):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        monkeypatch.setattr(service.pilot_tools, "search_games", _async_return([]))
        await service.ask("今天天气怎么样", session_id="s-log")
        log = tmp_path / "pilot" / "decisions.jsonl"
        assert log.is_file()
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["question"] == "今天天气怎么样"
        assert entry["intent"] == pilot_intent.CHAT
        assert entry["mode"] == "fallback"


class TestProtocolAdapters:
    """各协议流解析的罐头帧单测（不触网）。"""

    async def _collect(self, gen):
        return [e async for e in gen]

    @pytest.mark.asyncio
    async def test_anthropic_parse(self):
        frames = [
            "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
            "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "thinking_delta", "thinking": "思考"}}),
            "data: " + json.dumps({"type": "content_block_delta", "index": 1,
                                   "delta": {"type": "text_delta", "text": "答案"}}),
            "data: " + json.dumps({"type": "content_block_start", "index": 2,
                                   "content_block": {"type": "tool_use", "id": "tu1", "name": "search_games"}}),
            "data: " + json.dumps({"type": "content_block_delta", "index": 2,
                                   "delta": {"type": "input_json_delta", "partial_json": "{\"q\": \"x\"}"}}),
            "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                   "usage": {"output_tokens": 5}}),
        ]

        async def lines():
            for f in frames:
                yield f

        events = [e async for e in pilot_llm._anthropic_parse(lines())]
        kinds = [k for k, _ in events]
        assert kinds[0] == "thinking" and kinds[1] == "answer"
        tool = [d for k, d in events if k == "tool_calls"][0]
        assert tool[0]["name"] == "search_games" and tool[0]["arguments"] == {"q": "x"}
        assert ("usage", (10, 5)) in events

    @pytest.mark.asyncio
    async def test_gemini_parse(self):
        frame = "data: " + json.dumps({"candidates": [{"content": {"parts": [
            {"text": "思考", "thought": True},
            {"functionCall": {"name": "get_price_briefing", "args": {"appid": 1}}},
        ]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})

        async def lines():
            yield frame

        events = [e async for e in pilot_llm._gemini_parse(lines())]
        kinds = [k for k, _ in events]
        assert kinds[0] == "thinking"
        tool = [d for k, d in events if k == "tool_calls"][0]
        assert tool[0]["name"] == "get_price_briefing" and tool[0]["arguments"] == {"appid": 1}
        assert ("usage", (10, 5)) in events

    @pytest.mark.asyncio
    async def test_ollama_parse(self):
        lines = [
            json.dumps({"message": {"thinking": "想"}}),
            json.dumps({"message": {"tool_calls": [{"function": {"name": "add_follow", "arguments": {"appid": 3}}}]}},
                       ensure_ascii=False),
            json.dumps({"done": True, "prompt_eval_count": 7, "eval_count": 3}),
        ]

        async def it():
            for l in lines:
                yield l

        events = [e async for e in pilot_llm._ollama_parse(it())]
        kinds = [k for k, _ in events]
        assert kinds[0] == "thinking"
        tool = [d for k, d in events if k == "tool_calls"][0]
        assert tool[0]["name"] == "add_follow" and tool[0]["arguments"] == {"appid": 3}
        assert ("usage", (7, 3)) in events

    def test_effective_base_url(self):
        assert pilot_llm.effective_base_url("anthropic", "") == "https://api.anthropic.com"
        assert pilot_llm.effective_base_url("openai", "https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"


class TestNavigate:
    @pytest.mark.asyncio
    async def test_navigate_card_emitted(self, monkeypatch):
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [{"id": "t1", "name": "navigate", "arguments": {"target": "alerts"}}]),
                ("answer", "已打开。"),
            ],
        )
        executed = []
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, {"kind": "navigate", "target": "alerts", "path": "/alerts"}),
        )
        events = [e async for e in service.ask_stream("打开价格提醒", session_id="s-nav")]
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert {"kind": "navigate", "target": "alerts", "path": "/alerts"} in done["cards"]

    @pytest.mark.asyncio
    async def test_navigate_unknown_target_rejected(self):
        from app.domains.pilot import tools as pilot_tools_mod

        result = await pilot_tools_mod.execute_tool("navigate", {"target": "etc/passwd"})
        assert result["path"] == ""
