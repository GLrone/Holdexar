"""pilot 意图路由、agent 工具循环与降级路径单测（LLM 以替身注入，不触网、不依赖真实库）。"""
import asyncio
import json

import pytest

from app.domains.pilot import config as pilot_config
from app.domains.pilot import context as pilot_context
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

    async def _fn(name, arguments, guarded=False, sid=None):
        executed.append((name, arguments.get("appid"), guarded))
        return {**result, "appid": arguments.get("appid")}

    return _fn


@pytest.fixture(autouse=True)
def _fresh_sessions(tmp_path, monkeypatch):
    """会话账本指向临时目录，每个用例独立、跨用例零残留。"""
    monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
    service._store = None
    service._compact_failures.clear()
    yield
    service._store = None
    service._compact_failures.clear()


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
        # 上下文占用随 done 下发（前端输入区状态栏数据源）
        assert done["ctx_budget"] == pilot_context.HISTORY_BUDGET_TOKENS
        assert done["ctx_tokens"] > 0
        # 过程链数据：整轮工时与会话归档边界随 done 下发（链头与记忆条目数据源）
        assert isinstance(done["elapsed_ms"], int) and done["elapsed_ms"] >= 0
        assert done["archived_through"] == 0
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

        async def _multi(name, arguments, guarded=False, sid=None):
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
    async def test_phases_segment_thinking_and_text(self, monkeypatch):
        """阶段账本：step_start 定界，思考 / 前言 / 工具按步归位，thinking 与 steps 由 phases 派生。"""
        _patch_ready(monkeypatch)
        scripts = [
            [("thinking", "先看价格"), ("answer", "我去查一下。"),
             ("tool_calls", [{"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 7}}])],
            [("thinking", "再看史低"), ("answer", "结论：值得等史低。")],
        ]
        rounds = {"n": 0}

        async def _rounds(**kw):
            for item in scripts[rounds["n"]]:
                yield item
            rounds["n"] += 1

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _rounds)
        monkeypatch.setattr(service.pilot_tools, "execute_tool",
                            _tool_stub([], {**_FACTS, "appid": 7}))
        events = [e async for e in service.ask_stream("找一款试试", session_id="s-ph")]
        starts = [(e["type"], e["step"]) for e in events if e["type"] == "step_start"]
        assert starts == [("step_start", 1), ("step_start", 2)]
        done = events[-1]
        phases = done["phases"]
        assert [p["step"] for p in phases] == [1, 2]
        assert phases[0]["thinking"] == "先看价格" and phases[0]["text"] == "我去查一下。"
        assert [s["label"] for s in phases[0]["steps"]] == ["price"]
        assert phases[1]["thinking"] == "再看史低"
        assert phases[0]["terminal"] is False and phases[1]["terminal"] is True
        # phases 是权威记录：整轮 thinking / steps / answer 均由它派生
        assert done["thinking"] == "先看价格再看史低"
        assert done["steps"] == [s for p in phases for s in p["steps"]]
        assert done["answer"] == "我去查一下。结论：值得等史低。"

    @pytest.mark.asyncio
    async def test_phase_think_budget_truncates(self, monkeypatch):
        """单阶段思考超预算：该阶段停止累加与下发并标记 truncated（模型侧推理不受影响）。"""
        _patch_ready(monkeypatch)

        async def _scripted(**kw):
            yield ("thinking", "甲" * (service._THINK_BUDGET_CHARS + 5))
            yield ("answer", "答")

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _scripted)
        events = [e async for e in service.ask_stream("找一款试试", session_id="s-trunc")]
        assert "thinking" not in [e["type"] for e in events]
        done = events[-1]
        assert done["phases"][0]["truncated"] is True
        assert done["phases"][0]["thinking"] == ""
        assert done["thinking"] is None and done["answer"] == "答"

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
        assert kinds[0] == "cache" and kinds[1] == "thinking" and kinds[2] == "answer"
        cache = events[0][1]
        assert cache["base"] == 10 and cache["read"] is None and cache["write"] is None
        tool = [d for k, d in events if k == "tool_calls"][0]
        assert tool[0]["name"] == "search_games" and tool[0]["arguments"] == {"q": "x"}
        assert ("usage", (10, 5)) in events

    @pytest.mark.asyncio
    async def test_anthropic_parse_cache_buckets(self):
        """Anthropic 缓存三桶：命中率的分母含缓存读自身。"""
        frames = [
            "data: " + json.dumps({"type": "message_start", "message": {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 5}}}),
            "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 3}}),
            "data: " + json.dumps({"type": "message_stop"}),
        ]

        async def lines():
            for f in frames:
                yield f

        events = [e async for e in pilot_llm._anthropic_parse(lines())]
        cache = events[0][1]
        assert cache["read"] == 90 and cache["write"] == 5 and cache["base"] == 105
        assert abs(cache["rate"] - 90 / 105) < 1e-9

    @pytest.mark.asyncio
    async def test_responses_parse(self):
        """Responses API 罐头帧：思考/正文/工具调用组装/usage 四通道。"""
        frames = [
            "data: " + json.dumps({"type": "response.output_item.added", "item": {
                "type": "function_call", "item_id": "fc_1", "call_id": "call_9",
                "name": "get_price_briefing", "arguments": ""}}),
            "data: " + json.dumps({"type": "response.reasoning_summary_text.delta", "delta": "想"}),
            "data: " + json.dumps({"type": "response.output_text.delta", "delta": "结论"}),
            "data: " + json.dumps({"type": "response.function_call_arguments.delta",
                                   "item_id": "fc_1", "delta": "{\"appid\""}),
            "data: " + json.dumps({"type": "response.function_call_arguments.delta",
                                   "item_id": "fc_1", "delta": ": 292030}"}),
            "data: " + json.dumps({"type": "response.output_item.done", "item": {
                "type": "function_call", "item_id": "fc_1", "call_id": "call_9",
                "name": "get_price_briefing", "arguments": "{\"appid\": 292030}"}}),
            "data: " + json.dumps({"type": "response.completed", "response": {
                "usage": {"input_tokens": 88, "output_tokens": 9}}}),
        ]

        async def lines():
            for f in frames:
                yield f

        events = [e async for e in pilot_llm._responses_parse(lines())]
        kinds = [k for k, _ in events]
        assert kinds == ["thinking", "answer", "tool_calls", "usage", "cache"]
        calls = [d for k, d in events if k == "tool_calls"][0]
        assert calls == [{"id": "call_9", "name": "get_price_briefing", "arguments": {"appid": 292030}}]
        assert ("usage", (88, 9)) in events
        cache = events[-1][1]
        assert cache["base"] == 88 and cache["read"] is None

    @pytest.mark.asyncio
    async def test_responses_parse_stream_end_fallback(self):
        """无 completed 帧（兼容服务掐流）时流末兜底组装工具调用。"""
        frames = [
            "data: " + json.dumps({"type": "response.output_item.added", "item": {
                "type": "function_call", "item_id": "fc_2", "call_id": "call_7",
                "name": "search_games", "arguments": "{\"q\": \"x\"}"}}),
            "data: " + json.dumps({"type": "response.output_item.done", "item": {
                "type": "function_call", "item_id": "fc_2", "call_id": "call_7",
                "name": "search_games", "arguments": "{\"q\": \"x\"}"}}),
        ]

        async def lines():
            for f in frames:
                yield f

        events = [e async for e in pilot_llm._responses_parse(lines())]
        assert [d for k, d in events if k == "tool_calls"] == [
            [{"id": "call_7", "name": "search_games", "arguments": {"q": "x"}}]]

    def test_responses_input_mapping(self):
        """内部消息 → Responses input：system 提 instructions、工具调用/结果转项。"""
        messages = [
            {"role": "system", "content": "你是领航员"},
            {"role": "user", "content": "这游戏多少钱"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_price_briefing", "arguments": "{\"appid\": 1}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"name\": \"X\"}"},
        ]
        instructions, items = pilot_llm._responses_input(messages)
        assert instructions == "你是领航员"
        assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "这游戏多少钱"}]}
        assert items[1] == {"type": "function_call", "call_id": "call_1",
                            "name": "get_price_briefing", "arguments": "{\"appid\": 1}"}
        assert items[2] == {"type": "function_call_output", "call_id": "call_1",
                            "output": "{\"name\": \"X\"}"}

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
        # 非固定句式（无导航动词）不走直通，由模型决定调用
        events = [e async for e in service.ask_stream("帮我把提醒那页调出来", session_id="s-nav")]
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert {"kind": "navigate", "target": "alerts", "path": "/alerts"} in done["cards"]

    @pytest.mark.asyncio
    async def test_navigate_unknown_target_rejected(self):
        from app.domains.pilot import tools as pilot_tools_mod

        result = await pilot_tools_mod.execute_tool("navigate", {"target": "etc/passwd"})
        assert result["path"] == ""


class TestNavigateDirect:
    """导航确定性直通：动词+页面别名命中即执行，不过模型。"""

    def test_nav_target_matching(self):
        assert pilot_intent.nav_target("打开设置") == "settings"
        assert pilot_intent.nav_target("看看我的关注") == "follows"
        assert pilot_intent.nav_target("带我去账单页") == "bills"
        assert pilot_intent.nav_target("回到首页") == "dashboard"
        # 疑问/否定/守卫形态不直通
        assert pilot_intent.nav_target("怎么打开设置") is None
        assert pilot_intent.nav_target("别去设置页") is None
        assert pilot_intent.nav_target("批量打开所有页面") is None
        # 有动词无对象 / 有对象无动词
        assert pilot_intent.nav_target("打开看看") is None
        assert pilot_intent.nav_target("设置一下") is None

    def test_route_navigate(self):
        assert pilot_intent.route("打开设置") == pilot_intent.NAVIGATE
        assert pilot_intent.route("看看活动日历") == pilot_intent.NAVIGATE
        assert pilot_intent.route("打开设置吗") != pilot_intent.NAVIGATE

    @pytest.mark.asyncio
    async def test_deterministic_nav_short_circuits_llm(self, monkeypatch):
        # LLM 关闭也直通：导航是本地动作，不依赖模型可用性
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        executed = []
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, {"kind": "navigate", "target": "settings", "path": "/settings"}),
        )
        events = [e async for e in service.ask_stream("打开设置", session_id="s-fast")]
        assert [e["type"] for e in events] == ["ack", "tool_start", "tool", "done"]
        tool_ev = [e for e in events if e["type"] == "tool"][0]
        assert tool_ev["label"] == "navigate" and tool_ev["data"]["path"] == "/settings"
        done = events[-1]
        assert done["source"] == "facts" and done["reason"] is None
        assert done["facts"] == {"kind": "navigate", "target": "settings", "path": "/settings"}
        assert done["cards"] == [done["facts"]]
        assert done["steps"] == [{"label": "navigate", "status": "ok",
                                  "data": {"target": "settings", "path": "/settings"}}]
        assert executed == [("navigate", None, False)]

    @pytest.mark.asyncio
    async def test_direct_nav_logged_for_training(self, monkeypatch, tmp_path):
        _patch_ready(monkeypatch, cfg={**_CFG, "enabled": False})
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub([], {"kind": "navigate", "target": "logs", "path": "/logs"}),
        )
        await service.ask("打开日志", session_id="s-log2")
        entry = json.loads((tmp_path / "pilot" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["mode"] == "direct" and entry["nav_target"] == "logs"
        assert entry["appid"] is None


class TestSessionReference:
    """跨会话引用：摘要以 system 注入模型上下文；缺失引用静默跳过。"""

    async def _capture_messages(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[("answer", "好")])
        seen: dict = {}

        async def _scripted(**kw):
            seen["messages"] = [dict(m) for m in kw["messages"]]
            for item in [("answer", "好")]:
                yield item

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _scripted)
        return seen

    @staticmethod
    def _turn_record(q: str, answer: str) -> dict:
        return {"q": q, "resp": {"answer": answer, "source": "llm", "reason": None,
                                 "facts": None, "cards": [], "cached": False},
                "messages": [], "last_game": None}

    @pytest.mark.asyncio
    async def test_reference_injected_into_model_messages(self, monkeypatch):
        seen = await self._capture_messages(monkeypatch)
        store = service.get_store()
        await store.append_turn("ref-s1", self._turn_record("上次聊了巫师3的价格", "巫师3 现价 50 元"))
        events = [e async for e in service.ask_stream(
            "那它现在史低多少", session_id="cur-1", reference_sid="ref-s1")]
        assert events[-1]["type"] == "done"
        ref_msgs = [m for m in seen["messages"]
                    if m.get("role") == "system" and "另一段对话" in str(m.get("content") or "")]
        assert ref_msgs and "巫师3" in ref_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_missing_reference_skips_silently(self, monkeypatch):
        seen = await self._capture_messages(monkeypatch)
        events = [e async for e in service.ask_stream(
            "随便问问", session_id="cur-2", reference_sid="no-such-sid")]
        assert events[-1]["type"] == "done" and events[-1]["source"] == "llm"
        assert not [m for m in seen["messages"]
                    if "另一段对话" in str(m.get("content") or "")]

    @pytest.mark.asyncio
    async def test_done_carries_session_title(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[("answer", "好")])
        events = [e async for e in service.ask_stream("第一问句", session_id="s-tt")]
        assert events[-1]["title"] == "第一问句"
        await service.get_store().set_title("s-tt", "自定义标题")
        events2 = [e async for e in service.ask_stream("第二问句", session_id="s-tt")]
        assert events2[-1]["title"] == "自定义标题"

    @pytest.mark.asyncio
    async def test_reference_logged_in_decisions(self, monkeypatch, tmp_path):
        _patch_ready(monkeypatch, stream=[("answer", "好")])
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        await service.ask("问一句", session_id="s-reflog", reference_sid="ref-x")
        entry = json.loads((tmp_path / "pilot" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["reference_sid"] == "ref-x"


class TestSessionTools:
    @pytest.mark.asyncio
    async def test_list_and_read_session_tools(self, monkeypatch, tmp_path):
        from app.domains.pilot import tools as pilot_tools_mod

        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        store = service.get_store()
        await store.append_turn("tool-s1", TestSessionReference._turn_record("看看价格", "50 元"))
        listed = await pilot_tools_mod.execute_tool("list_sessions", {})
        assert listed["kind"] == "rows" and listed["total"] == 1
        assert listed["rows"][0]["k"] == "看看价格"
        read = await pilot_tools_mod.execute_tool("read_session", {"sid": "tool-s1"})
        assert read["kind"] == "rows" and read["total"] == 1
        assert "50 元" in (read.get("digest") or "")
        missing = await pilot_tools_mod.execute_tool("read_session", {"sid": "no-such"})
        assert missing["kind"] == "empty"
        service._store = None


class TestBulkProposal:
    """批量提议：提议不写、确认才写、取消作废、重复确认失效。"""

    @staticmethod
    def _stub_names(monkeypatch):
        async def _names(ids):
            return {i: f"游戏{i}" for i in ids}
        monkeypatch.setattr(service.pilot_tools.games_service, "names_for", _names)

    @staticmethod
    def _stub_writes(monkeypatch, log: list):
        async def _track(target_type, target_id, source):
            log.append(("track", int(target_id)))
            return "active"

        async def _add_alert(appid, region, target_type, target_value):
            log.append(("alert", int(appid)))
            return {"id": 1}

        async def _detail(appid):
            return {"name": f"游戏{appid}"}

        monkeypatch.setattr(service.pilot_tools.monitoring_service, "track", _track)
        monkeypatch.setattr(service.pilot_tools.alerts_service, "add_alert", _add_alert)
        monkeypatch.setattr(service.pilot_tools.games_service, "get_game_detail", _detail)

    @pytest.mark.asyncio
    async def test_propose_has_no_side_effect(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [1, 2, 3]}, sid="bulk-s1")
        assert out["kind"] == "proposal" and out["state"] == "pending"
        assert [it["appid"] for it in out["items"]] == [1, 2, 3]
        assert log == []

    @pytest.mark.asyncio
    async def test_propose_rejects_unknown_action_and_oversize(self, monkeypatch):
        self._stub_names(monkeypatch)
        bad = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "delete_all", "appids": [1]}, sid="bulk-s2")
        assert bad["note"] == "bad_action"
        big = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow",
                             "appids": list(range(1, service.pilot_tools._BULK_MAX_ITEMS + 2))},
            sid="bulk-s2")
        assert big["note"] == "too_many"
        nosession = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [1]}, sid=None)
        assert nosession["note"] == "no_session"

    @pytest.mark.asyncio
    async def test_confirm_executes_each_item(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [7, 8]}, sid="bulk-s3")
        res = await service.confirm_proposal("bulk-s3", out["pid"], True)
        assert res["ok"] is True and res["state"] == "confirmed"
        assert res["total"] == 2 and res["done"] == 2 and res["failed"] == []
        assert log == [("track", 7), ("track", 8)]

    @pytest.mark.asyncio
    async def test_dismiss_and_repeat_confirm(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [5]}, sid="bulk-s4")
        dismissed = await service.confirm_proposal("bulk-s4", out["pid"], False)
        assert dismissed["state"] == "rejected" and dismissed["done"] == 0
        assert log == []
        again = await service.confirm_proposal("bulk-s4", out["pid"], True)
        assert again["ok"] is False and again["reason"] == "proposal_gone"

    @pytest.mark.asyncio
    async def test_alert_proposal_passes_target_value(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "create_price_alert", "appids": [11],
                             "target_type": "price", "target_value_yuan": 48.5},
            sid="bulk-s5")
        res = await service.confirm_proposal("bulk-s5", out["pid"], True)
        assert res["done"] == 1
        assert log == [("alert", 11)]

    @pytest.mark.asyncio
    async def test_pending_proposal_survives_reload(self, monkeypatch):
        self._stub_names(monkeypatch)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [3]}, sid="bulk-s6")
        store = service.get_store()
        reloaded = await store.load("bulk-s6")
        assert reloaded is not None
        assert reloaded.pending_proposal is not None
        assert reloaded.pending_proposal["pid"] == out["pid"]


class TestBulkProposal:
    """批量提议：提议不写、确认才写、取消作废、重复确认失效。"""

    @staticmethod
    def _stub_names(monkeypatch):
        async def _names(ids):
            return {i: f"游戏{i}" for i in ids}
        monkeypatch.setattr(service.pilot_tools.games_service, "names_for", _names)

    @staticmethod
    def _stub_writes(monkeypatch, log: list):
        async def _track(target_type, target_id, source):
            log.append(("track", int(target_id)))
            return "active"

        async def _add_alert(appid, region, target_type, target_value):
            log.append(("alert", int(appid)))
            return {"id": 1}

        async def _detail(appid):
            return {"name": f"游戏{appid}"}

        monkeypatch.setattr(service.pilot_tools.monitoring_service, "track", _track)
        monkeypatch.setattr(service.pilot_tools.alerts_service, "add_alert", _add_alert)
        monkeypatch.setattr(service.pilot_tools.games_service, "get_game_detail", _detail)

    @pytest.mark.asyncio
    async def test_propose_has_no_side_effect(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [1, 2, 3]}, sid="bulk-s1")
        assert out["kind"] == "proposal" and out["state"] == "pending"
        assert [it["appid"] for it in out["items"]] == [1, 2, 3]
        assert log == []

    @pytest.mark.asyncio
    async def test_propose_rejects_unknown_action_and_oversize(self, monkeypatch):
        self._stub_names(monkeypatch)
        bad = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "delete_all", "appids": [1]}, sid="bulk-s2")
        assert bad["note"] == "bad_action"
        big = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow",
                             "appids": list(range(1, service.pilot_tools._BULK_MAX_ITEMS + 2))},
            sid="bulk-s2")
        assert big["note"] == "too_many"
        nosession = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [1]}, sid=None)
        assert nosession["note"] == "no_session"

    @pytest.mark.asyncio
    async def test_confirm_executes_each_item(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [7, 8]}, sid="bulk-s3")
        res = await service.confirm_proposal("bulk-s3", out["pid"], True)
        assert res["ok"] is True and res["state"] == "confirmed"
        assert res["total"] == 2 and res["done"] == 2 and res["failed"] == []
        assert log == [("track", 7), ("track", 8)]

    @pytest.mark.asyncio
    async def test_dismiss_and_repeat_confirm(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [5]}, sid="bulk-s4")
        dismissed = await service.confirm_proposal("bulk-s4", out["pid"], False)
        assert dismissed["state"] == "rejected" and dismissed["done"] == 0
        assert log == []
        again = await service.confirm_proposal("bulk-s4", out["pid"], True)
        assert again["ok"] is False and again["reason"] == "proposal_gone"

    @pytest.mark.asyncio
    async def test_alert_proposal_passes_target_value(self, monkeypatch):
        log: list = []
        self._stub_names(monkeypatch)
        self._stub_writes(monkeypatch, log)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "create_price_alert", "appids": [11],
                             "target_type": "price", "target_value_yuan": 48.5},
            sid="bulk-s5")
        res = await service.confirm_proposal("bulk-s5", out["pid"], True)
        assert res["done"] == 1
        assert log == [("alert", 11)]

    @pytest.mark.asyncio
    async def test_pending_proposal_survives_reload(self, monkeypatch):
        self._stub_names(monkeypatch)
        out = await service.pilot_tools.execute_tool(
            "propose_bulk", {"action": "add_follow", "appids": [3]}, sid="bulk-s6")
        store = service.get_store()
        reloaded = await store.load("bulk-s6")
        assert reloaded is not None
        assert reloaded.pending_proposal is not None
        assert reloaded.pending_proposal["pid"] == out["pid"]


class TestCtxBreakdown:
    """done 事件携带上下文分段与缓存计量（分段条/命中率行/会话统计的数据层）。"""

    def test_done_carries_breakdown_and_cache(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("cache", {"rate": 0.9, "read": 9, "write": None, "base": 10}),
            ("answer", "好"), ("usage", (10, 5)),
        ])
        result = asyncio.run(service.ask("巫师3值不值？"))
        assert result["cache_hit_rate"] == 0.9
        assert result["usage_in"] == 10 and result["usage_out"] == 5
        assert result["cache_read_tokens"] == 9
        assert result["cache_write_tokens"] is None
        assert result["cache_base_tokens"] == 10
        assert result["ttft_n"] == 1 and result["ttft_ms"] >= 0
        assert result["decode_ms"] >= 0 and result["decode_out"] == 5
        breakdown = {r["source"]: r["chars"] for r in result["ctx_breakdown"]}
        assert set(breakdown) == {"system", "tools", "summary", "history", "current"}
        assert breakdown["system"] > 0
        assert breakdown["tools"] > 0
        assert breakdown["current"] > 0
        assert breakdown["summary"] == 0

    def test_session_stats_folds_turns(self, monkeypatch):
        """会话统计折叠：轮/步/用量/计时按账本全轮求和，缓存桶 None 传导。"""
        _patch_ready(monkeypatch, stream=[
            ("cache", {"rate": 0.5, "read": 5, "write": None, "base": 10}),
            ("answer", "一"), ("usage", (10, 4)),
        ])
        asyncio.run(service.ask("问题一", session_id="stats-fold"))
        _patch_ready(monkeypatch, stream=[("answer", "二"), ("usage", (8, 6))])
        asyncio.run(service.ask("问题二", session_id="stats-fold"))
        state = asyncio.run(service.get_store().load("stats-fold"))
        s = service.session_stats(state)
        assert s["turns"] == 2
        assert s["usage_in"] == 18 and s["usage_out"] == 10
        assert s["cache_read_tokens"] == 5
        assert s["cache_base_tokens"] == 10
        assert s["ttft_n"] == 2  # 两轮都有内容增量且用量回传，各计一次首字
        assert s["decode_out"] == 10


class TestHistoryBudget:
    """历史预算派生：窗口未知回落固定值，已知窗口按公式派生并夹边。"""

    def test_unknown_window_falls_back(self):
        assert pilot_context.derive_history_budget(None) == pilot_context.HISTORY_BUDGET_TOKENS
        assert pilot_context.derive_history_budget(0) == pilot_context.HISTORY_BUDGET_TOKENS
        assert pilot_context.derive_history_budget(-5) == pilot_context.HISTORY_BUDGET_TOKENS

    def test_window_minus_reserve_and_overhead(self):
        # 不触顶区间：派生值 = 窗口 − 回答预留 − 安全垫 − 开销
        assert pilot_context.derive_history_budget(12000, 2000) == 12000 - 2048 - 1024 - 2000
        assert pilot_context.derive_history_budget(10000, -100) == 10000 - 2048 - 1024

    def test_clamps_small_and_large_windows(self):
        assert pilot_context.derive_history_budget(4096, 8000) == 2000
        assert pilot_context.derive_history_budget(1_000_000) == 12000
        assert pilot_context.derive_history_budget(3_000_000) == 12000

    def test_service_budget_uses_window_from_cfg(self):
        unknown = service._history_budget(_CFG)
        assert unknown == pilot_context.HISTORY_BUDGET_TOKENS
        assert service._history_budget({**_CFG, "context_window": 1_000_000}) == 12000
        assert service._history_budget({**_CFG, "context_window": 4096}) == 2000  # 开销吃满余量，夹下限
        # 期望值按当前开销动态推导：工具表体量随版本演化，测试钉公式不钉开销
        overhead = (
            pilot_context.estimate_tokens(service._SYSTEM_PROMPT)
            + pilot_context.estimate_tokens(
                json.dumps(service.pilot_tools.tool_specs(), ensure_ascii=False))
        )
        mid = service._history_budget({**_CFG, "context_window": 16384})
        assert mid == min(12000, max(2000, 16384 - 3072 - overhead))


class TestContextWindowConfig:
    """供应商窗口字段清洗：正整数生效，0/空/非法/超帽 = 未知。"""

    def test_clean_window(self):
        assert pilot_config._clean_window(None) is None
        assert pilot_config._clean_window(0) is None
        assert pilot_config._clean_window("0") is None
        assert pilot_config._clean_window(-1) is None
        assert pilot_config._clean_window("128k") is None
        assert pilot_config._clean_window(131072) == 131072
        assert pilot_config._clean_window("131072") == 131072
        assert pilot_config._clean_window(3_000_000) is None

    def test_provider_carries_window(self):
        cleaned = pilot_config._clean_provider({"id": "p1", "context_window": 65536})
        assert cleaned["context_window"] == 65536
        cleaned = pilot_config._clean_provider({"id": "p1", "context_window": None})
        assert cleaned["context_window"] is None


class TestCompactBreakerAndLedger:
    """压缩连败熔断与前后量落账：失败退整轮让位不中断问答，连败后停止自动压缩。"""

    @staticmethod
    def _patch_split_stream(monkeypatch, *, compact_fail=True, calls):
        """压缩调用与主循环调用分流：压缩（无 tools）按脚本失败/成功，主循环给终答。"""

        async def _fn(**kw):
            if "tools" not in kw:
                calls["compact"] += 1
                if compact_fail:
                    raise PilotLlmError("compact boom")
                    yield  # pragma: no cover
                yield ("answer", "要点存档")
                yield ("usage", (10, 5))
                return
            yield ("answer", "终答")
            yield ("usage", (100, 10))
        monkeypatch.setattr(service.pilot_llm, "chat_stream", _fn)

    async def _seed_big_turns(self, sid, count=3):
        """每轮 ~3 万 token 级模型消息：装配只剩最近 2 轮也远超预算，压缩必然触发。"""
        store = service.get_store()
        for i in range(count):
            await store.append_turn(sid, {
                "q": f"问题{i}", "resp": {"answer": "长" * 15000},
                "messages": [
                    {"role": "user", "content": f"问题{i}" + "长" * 15000},
                    {"role": "assistant", "content": "答" + "长" * 15000},
                ],
                "last_game": None,
            })
        return await store.load(sid)

    @pytest.mark.asyncio
    async def test_compact_failure_degrades_and_trips_breaker(self, monkeypatch):
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        self._patch_split_stream(monkeypatch, compact_fail=True, calls=calls)
        cfg = {**_CFG}
        state = await self._seed_big_turns("sess-breaker")
        for expected in (1, 2, 2):
            base, compacted = await service._assemble_for_model(state, "新问题", None, cfg)
            assert compacted is False
            assert calls["compact"] == expected
        assert service._compact_failures["sess-breaker"] == 2

    @pytest.mark.asyncio
    async def test_success_resets_breaker_and_writes_post_tokens(self, monkeypatch):
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        self._patch_split_stream(monkeypatch, compact_fail=False, calls=calls)
        cfg = {**_CFG}
        state = await self._seed_big_turns("sess-ledger")
        base, compacted = await service._assemble_for_model(state, "新问题", None, cfg)
        assert compacted is True
        assert calls["compact"] == 1
        assert "sess-ledger" not in service._compact_failures
        marker = state.markers[-1]
        assert marker["trigger"] == "auto"
        assert marker["pre_tokens"] > marker["post_tokens"] > 0
        # 蒸馏把装配体量压到压缩前之下（最近 2 轮仍逐字保留，可能仍超预算——设计如此）
        assert pilot_context.estimate_messages(base) < marker["pre_tokens"]

    @pytest.mark.asyncio
    async def test_manual_compact_resets_breaker(self, monkeypatch):
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        self._patch_split_stream(monkeypatch, compact_fail=True, calls=calls)
        cfg = {**_CFG}
        state = await self._seed_big_turns("sess-manual")
        await service._assemble_for_model(state, "问", None, cfg)
        await service._assemble_for_model(state, "问", None, cfg)
        assert calls["compact"] == 2  # 已达熔断线
        self._patch_split_stream(monkeypatch, compact_fail=False, calls=calls)
        result = await service.compact_session_now(state, cfg)
        assert result["ok"] is True
        assert result["post_tokens"] < result["pre_tokens"]
        assert calls["compact"] == 3  # 手动压缩不受熔断限制
        assert service._compact_failures.get("sess-manual") is None
        # 熔断已重置；但手动压缩后暂无可蒸馏轮次，自动压缩不再发起调用
        await service._assemble_for_model(state, "问", None, cfg)
        assert calls["compact"] == 3


def test_ask_stream_ack_and_same_sid_busy():
    """受理回执 + 同会话在飞防护：held 生成器占住闸 → 同 sid 第二条立即 busy，
    不同会话照常受理（ack.active 计全局在飞），关闭后闸释放。"""
    import asyncio

    async def run():
        gen1 = service.ask_stream("第一问", session_id="s-busy")
        first = await gen1.__anext__()
        assert first["type"] == "ack" and first["active"] == 1

        gen2 = service.ask_stream("第二问", session_id="s-busy")
        assert await gen2.__anext__() == {"type": "busy"}

        gen3 = service.ask_stream("第三问", session_id="s-other")
        third = await gen3.__anext__()
        assert third["type"] == "ack" and third["active"] == 2

        await gen1.aclose()
        await gen3.aclose()

        gen4 = service.ask_stream("第四问", session_id="s-busy")
        fourth = await gen4.__anext__()
        assert fourth["type"] == "ack" and fourth["active"] == 1
        await gen4.aclose()

    asyncio.run(run())
