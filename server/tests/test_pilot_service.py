"""pilot 意图路由、agent 工具循环与降级路径单测（LLM 以替身注入，不触网、不依赖真实库）。"""
import asyncio
import json

import pytest

from app.domains.pilot import config as pilot_config
from app.domains.pilot import context as pilot_context
from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import model_windows
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
    async def test_card_event_streams_before_done(self, monkeypatch):
        """卡片随工具完成即时下发：card 事件先于 done，tool_start 带类型预告，done.cards 同源。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [{"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030}}]),
                ("answer", "结论：值得。"),
                ("usage", (10, 5)),
            ],
        )
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, _FACTS),
        )
        events = [e async for e in service.ask_stream("这游戏值得入手吗", session_id="s-card")]
        types = [e["type"] for e in events]
        assert "card" in types
        started = next(e for e in events if e["type"] == "tool_start")
        assert started["card_kind"] == "price"
        card_ev = next(e for e in events if e["type"] == "card")
        assert card_ev["name"] == "get_price_briefing"
        assert card_ev["card"]["kind"] == "price"
        # 先见卡后收终态：card 落在 tool 与 done 之间
        assert types.index("tool") < types.index("card") < types.index("done")
        done = events[-1]
        assert done["cards"] == [card_ev["card"]]

    @pytest.mark.asyncio
    async def test_card_event_dedupes_repeated_result(self, monkeypatch):
        """同轮重复产出同一卡片只在终态账本记一份，card 事件也只发一次。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030}},
                    {"id": "t2", "name": "get_price_briefing", "arguments": {"appid": 292030}},
                ]),
                ("answer", "同一款不重复贴卡。"),
                ("usage", (10, 5)),
            ],
        )
        monkeypatch.setattr(
            service.pilot_tools,
            "execute_tool",
            _tool_stub(executed, _FACTS),
        )
        events = [e async for e in service.ask_stream("查两次同一个", session_id="s-dedupe")]
        card_events = [e for e in events if e["type"] == "card"]
        # 第二次同结果只在终态账本记一份：首帧带卡，重帧为空结算（前端撤骨架）
        assert [e["card"] for e in card_events] == [card_events[0]["card"], None]
        assert card_events[0]["card"] is not None
        done = events[-1]
        assert done["cards"] == [card_events[0]["card"]]

    @pytest.mark.asyncio
    async def test_stepper_and_proposal_both_stream(self, monkeypatch):
        """复合结果（进度 + 提议）两张卡各自即时下发，提议卡以 propose_bulk 名义发事件。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [{"id": "t1", "name": "audit_follows_workflow", "arguments": {}}]),
                ("answer", "扫描完成。"),
                ("usage", (10, 5)),
            ],
        )

        async def _audit(name, arguments, guarded=False, sid=None):
            executed.append(name)
            return {
                "kind": "stepper",
                "stepper": {"title": "T", "steps": [{"label": "a", "status": "ok"}]},
                "proposal": {"kind": "proposal", "pid": "p1", "action": "add",
                             "items": [{"appid": 1, "name": "X"}]},
            }

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _audit)
        events = [e async for e in service.ask_stream("体检一下关注", session_id="s-compound")]
        card_events = [e for e in events if e["type"] == "card"]
        assert [(e["name"], e["card"]["kind"]) for e in card_events] == [
            ("audit_follows_workflow", "stepper"),
            ("propose_bulk", "proposal"),
        ]
        started = next(e for e in events if e["type"] == "tool_start")
        assert started["card_kind"] == "stepper"
        done = events[-1]
        assert [c["kind"] for c in done["cards"]] == ["stepper", "proposal"]

    @pytest.mark.asyncio
    async def test_card_admission_caps_and_prioritizes_core_cards(self, monkeypatch):
        """单轮卡片上限两张且核心卡优先：超限或低优先级辅助卡被抑制。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030}},
                    {"id": "t2", "name": "get_price_briefing", "arguments": {"appid": 413150}},
                    {"id": "t3", "name": "list_follows", "arguments": {}},
                ]),
                ("answer", "对比完成。"),
                ("usage", (10, 5)),
            ],
        )

        async def _stub(name, arguments, guarded=False, sid=None):
            executed.append(name)
            if name == "get_price_briefing":
                appid = arguments.get("appid", 292030)
                return {"kind": "price", "appid": appid, "name": f"Game {appid}", "cn": {"cnyFen": 6000}}
            return {"kind": "rows", "titleKey": "follows", "rows": [{"k": "1", "vKey": "ok"}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub)
        events = [e async for e in service.ask_stream("对比这两款游戏", session_id="s-cap")]
        card_events = [e for e in events if e["type"] == "card"]
        assert len(card_events) == 3
        assert card_events[0]["card"] is not None and card_events[0]["card"]["appid"] == 292030
        assert card_events[1]["card"] is not None and card_events[1]["card"]["appid"] == 413150
        assert card_events[2]["card"] is None
        done = events[-1]
        assert len(done["cards"]) == 2
        assert [c["kind"] for c in done["cards"]] == ["price", "price"]

    @pytest.mark.asyncio
    async def test_card_admission_dedupes_multiple_rows_cards(self, monkeypatch):
        """多张同类辅助行卡去重：同一轮只保留首张行卡。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "list_tasks", "arguments": {}},
                    {"id": "t2", "name": "list_follows", "arguments": {}},
                ]),
                ("answer", "排查完成。"),
                ("usage", (10, 5)),
            ],
        )

        async def _stub_rows(name, arguments, guarded=False, sid=None):
            executed.append(name)
            return {"kind": "rows", "titleKey": name, "rows": [{"k": "status", "vKey": "ok"}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub_rows)
        events = [e async for e in service.ask_stream("排查状态", session_id="s-rows-dedupe")]
        card_events = [e for e in events if e["type"] == "card"]
        assert len(card_events) == 2
        assert card_events[0]["card"] is not None
        assert card_events[1]["card"] is None
        done = events[-1]
        assert len(done["cards"]) == 1
        assert done["cards"][0]["titleKey"] == "list_tasks"

    @pytest.mark.asyncio
    async def test_decision_agent_suppresses_card_on_explicit_emit_card_false(self, monkeypatch):
        """外层模型显式控制：传入 emit_card=False 时抑制卡片渲染并在回灌中明确标识。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030, "emit_card": False}},
                ]),
                ("answer", "价格核实完成。"),
                ("usage", (10, 5)),
            ],
        )

        async def _stub(name, arguments, guarded=False, sid=None):
            executed.append((name, arguments))
            return {"kind": "price", "appid": 292030, "name": "Witcher 3", "currentPrice": 4000}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub)
        events = [e async for e in service.ask_stream("查询巫师3价格", session_id="s-suppress-explicit")]
        card_events = [e for e in events if e["type"] == "card"]
        assert len(card_events) == 1
        assert card_events[0]["card"] is None
        done = events[-1]
        assert done["cards"] == []

    @pytest.mark.asyncio
    async def test_decision_agent_suppresses_price_card_when_question_is_non_price(self, monkeypatch):
        """非价格咨询门禁：用户咨询显卡配置等硬件问题时，内部代理自动抑制价格走势卡片。"""
        executed = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 2358720}},
                ]),
                ("answer", "GTX 1060 可以运行低画质。"),
                ("usage", (10, 5)),
            ],
        )

        async def _stub(name, arguments, guarded=False, sid=None):
            executed.append(name)
            return {"kind": "price", "appid": 2358720, "name": "Black Myth", "currentPrice": 26800}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub)
        events = [e async for e in service.ask_stream("黑神话悟空显卡配置要求高吗？GTX 1060 能玩吗？", session_id="s-non-price")]
        card_events = [e for e in events if e["type"] == "card"]
        assert len(card_events) == 1
        assert card_events[0]["card"] is None
        done = events[-1]
        assert done["cards"] == []

    @pytest.mark.asyncio
    async def test_decision_agent_informs_model_view_with_ui_card_status(self, monkeypatch):
        """双向认知对齐：回灌模型的工具结果携带 _ui_card 呈现状态。"""
        captured_messages = []
        _patch_ready(
            monkeypatch,
            stream=[
                ("tool_calls", [
                    {"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 292030}},
                ]),
                ("answer", "正如卡片所示，当前售价打折。"),
                ("usage", (10, 5)),
            ],
        )
        orig_stream = service.pilot_llm.chat_stream

        async def _spy(**kw):
            messages = kw.get("messages") or []
            captured_messages.append(list(messages))
            async for item in orig_stream(**kw):
                yield item

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _spy)

        async def _stub(name, arguments, guarded=False, sid=None):
            return {"kind": "price", "appid": 292030, "name": "Witcher 3", "currentPrice": 4000, "discount": 70}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub)
        events = [e async for e in service.ask_stream("巫师3现在多少钱？打折吗？", session_id="s-feedback")]
        card_events = [e for e in events if e["type"] == "card"]
        assert card_events[0]["card"] is not None
        second_req_msgs = captured_messages[1]
        tool_msg = [m for m in second_req_msgs if m.get("role") == "tool"][0]
        content_json = json.loads(tool_msg["content"])
        assert "_ui_card" in content_json
        assert content_json["_ui_card"]["status"] == "rendered"
        assert content_json["_ui_card"]["kind"] == "price"
        assert "价格卡片已在用户界面呈现" in content_json["_ui_card"]["summary"]

    @pytest.mark.asyncio
    async def test_denied_tool_hints_rows_but_no_card(self, monkeypatch):
        """守卫拒绝无卡可发：tool_start 仍按行卡类预告，事件流不出现 card。"""
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
        # 守卫拒绝无卡可发：card 结算帧仍下发给前端撤骨架，但 card 为空
        card_events = [e for e in events if e["type"] == "card"]
        assert len(card_events) == 1 and card_events[0]["card"] is None
        started = next(e for e in events if e["type"] == "tool_start")
        assert started["card_kind"] == "rows"

    def test_card_kind_hint_groups(self):
        assert service._card_kind_hint("get_price_briefing") == "price"
        assert service._card_kind_hint("search_games") == "games"
        assert service._card_kind_hint("list_follows") == "rows"
        assert service._card_kind_hint("refresh_rates") == "rows"
        assert service._card_kind_hint("list_tasks") == "rows"
        assert service._card_kind_hint("read_profile") is None

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
        # system + 环境事实（user 位）+ 上一轮问答（user/assistant）+ 当前问题
        assert roles.count("user") == 3
        assert roles.count("system") >= 1
        assert any(m.get("role") == "assistant" and m.get("content") == "好" for m in second)

    @pytest.mark.asyncio
    async def test_turn_prefix_replayed_byte_identical(self, monkeypatch):
        """跨轮前缀稳定：本轮请求的头部与上一轮实际请求逐字节一致（缓存复用地基）。

        意图等注入笔记随发送原样入账本，下一轮重放时原位重现——任何一条
        缺席或变形都会把上一轮助手与工具往返全部打出缓存。"""
        seen_messages = []
        scripts = [
            [("answer", "第一轮结论。"), ("usage", (10, 5))],
            [("answer", "第二轮结论。"), ("usage", (10, 5))],
        ]
        rounds = {"n": 0}

        async def _rounds(**kw):
            seen_messages.append([dict(m) for m in kw["messages"]])
            for item in scripts[rounds["n"]]:
                yield item
            rounds["n"] += 1

        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_llm, "chat_stream", _rounds)
        await service.ask("找一款值得一试的角色扮演大作", session_id="s-prefix")
        await service.ask("再推荐一款策略游戏", session_id="s-prefix")
        first, second = seen_messages[0], seen_messages[1]
        # 上一轮请求的完整序列在本轮请求头部逐字节重现（含意图笔记）
        assert len(second) > len(first)
        for i, m in enumerate(first):
            assert second[i] == m, f"前缀在第 {i} 条分叉：{m.get('role')}"
        # 账本侧：本轮入账 = 问句 + 意图笔记 + 终答
        state = await service.get_store().load("s-prefix")
        assert [m.get("role") for m in state.turns[0]["messages"]] == ["user", "system", "assistant"]


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
        assert kinds == ["thinking", "answer", "tool_calls", "usage", "context", "cache"]
        assert [d for k, d in events if k == "context"] == [88]
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
                                  "data": {"target": "settings", "path": "/settings", "section": ""}}]
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

    @pytest.mark.asyncio
    async def test_read_session_pages_back_into_compacted_region(self, monkeypatch, tmp_path):
        """压缩后的回读通道：offset/count 能翻到被蒸馏掉的旧区间，摘要外的原文取得到。"""
        from app.domains.pilot import tools as pilot_tools_mod

        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        store = service.get_store()
        for i in range(12):
            await store.append_turn("tool-s2", TestSessionReference._turn_record(
                f"问题{i}", f"回答{i} 价格 {1000 + i} 元"))
        await store.append_summary("tool-s2", "存档：早期都在问价格", 9)

        head = await pilot_tools_mod.execute_tool(
            "read_session", {"sid": "tool-s2", "count": 3})
        assert head["startTurn"] == 9 and head["turnCount"] == 3
        assert head["summaryThrough"] == 9

        # 往回翻：跳过最新 3 轮，读 4 轮 → 覆盖被蒸馏区间
        back = await pilot_tools_mod.execute_tool(
            "read_session", {"sid": "tool-s2", "offset": 3, "count": 4})
        assert back["startTurn"] == 5 and back["turnCount"] == 4
        assert "价格 1005 元" in back["digest"] and "价格 1008 元" in back["digest"]
        assert "价格 1011 元" not in back["digest"]

        # 越界不报错，取得到多少给多少
        edge = await pilot_tools_mod.execute_tool(
            "read_session", {"sid": "tool-s2", "offset": 50, "count": 4})
        assert edge["turnCount"] >= 0 and edge["kind"] == "rows"
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

    def test_cache_hit_rate_aggregates_whole_turn(self, monkeypatch):
        """命中率取本轮聚合：末次请求落空（0）不改写整轮读数。"""
        _patch_ready(monkeypatch)
        scripts = [
            [("cache", {"rate": 0.9, "read": 9000, "write": None, "base": 10000}),
             ("tool_calls", [{"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 7}}])],
            [("cache", {"rate": 0.0, "read": 0, "write": None, "base": 12000}),
             ("answer", "结论"), ("usage", (12000, 6))],
        ]
        rounds = {"n": 0}

        async def _rounds(**kw):
            for item in scripts[rounds["n"]]:
                yield item
            rounds["n"] += 1

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _rounds)

        async def _stub(name, arguments, guarded=False, sid=None):
            return {**_FACTS, "appid": arguments.get("appid")}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _stub)
        result = asyncio.run(service.ask("巫师3值不值？", session_id="cache-agg"))
        assert result["cache_read_tokens"] == 9000
        assert result["cache_base_tokens"] == 22000
        assert result["cache_hit_rate"] == pytest.approx(9000 / 22000)

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


class TestToolCatalog:
    """工具表切面：默认只给核心集，list_tools 解锁其余，解锁是会话级单向的。

    工具表是请求前缀的一部分，按轮增减会让整段历史的前缀缓存作废——所以
    只解锁、不回退。这里钉住三件事：首请求只有核心集、解锁后立刻展开、
    解锁事实落账并在下一轮延续。"""

    @staticmethod
    def _capture(monkeypatch, scripts):
        """脚本化 chat_stream，记录每次请求收到的工具名集合。"""
        seen: list[set[str]] = []
        rounds = {"n": 0}

        async def _rounds(**kw):
            seen.append({t["function"]["name"] for t in kw["tools"]})
            for item in scripts[rounds["n"]]:
                yield item
            rounds["n"] += 1

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _rounds)
        return seen

    @staticmethod
    def _catalog_stub(monkeypatch):
        async def _fn(name, arguments, guarded=False, sid=None):
            return {"kind": "tool_catalog", "tools": [], "count": 0}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _fn)

    def test_unexpanded_session_keeps_core_slice(self, monkeypatch):
        _patch_ready(monkeypatch)
        seen = self._capture(monkeypatch, [[("answer", "好"), ("usage", (10, 2))]])
        result = asyncio.run(service.ask("巫师3多少钱", session_id="catalog-plain"))
        assert seen[0] == set(service.pilot_tools.CORE_TOOLS)
        assert "bills_summary" not in seen[0]
        assert "tool_catalog" not in result  # 没解锁就不写这条账

    def test_list_tools_expands_mid_turn(self, monkeypatch):
        _patch_ready(monkeypatch)
        seen = self._capture(monkeypatch, [
            [("tool_calls", [{"id": "t1", "name": "list_tools", "arguments": {}}])],
            [("answer", "好了"), ("usage", (100, 5))],
        ])
        self._catalog_stub(monkeypatch)
        result = asyncio.run(service.ask("同步一下账单", session_id="catalog-expand"))
        core = set(service.pilot_tools.CORE_TOOLS)
        extra = set(service.pilot_tools.expandable_tools())
        assert seen[0] == core            # 首次请求：只有核心切面
        assert seen[1] == core | extra    # 解锁后：本步之后的请求立刻带上其余工具
        assert "bills_summary" in seen[1] and "bills_summary" not in seen[0]
        assert set(result["tool_catalog"]) == extra

    def test_expansion_survives_next_turn(self, monkeypatch):
        _patch_ready(monkeypatch)
        self._capture(monkeypatch, [
            [("tool_calls", [{"id": "t1", "name": "list_tools", "arguments": {}}])],
            [("answer", "好了"), ("usage", (100, 5))],
        ])
        self._catalog_stub(monkeypatch)
        asyncio.run(service.ask("同步一下账单", session_id="catalog-keep"))
        state = asyncio.run(service.get_store().load("catalog-keep"))
        assert (service.session_expanded_tools(state)
                == service.pilot_tools.expandable_tools())

        _patch_ready(monkeypatch)
        seen = self._capture(monkeypatch, [[("answer", "再答"), ("usage", (100, 5))]])
        asyncio.run(service.ask("那再看看", session_id="catalog-keep"))
        assert "bills_summary" in seen[0]  # 下一轮仍带展开后的工具表

    def test_session_expanded_tools_ignores_empty_and_missing(self):
        class _St:
            turns: list[dict] = []

        assert service.session_expanded_tools(None) == frozenset()
        assert service.session_expanded_tools(_St()) == frozenset()
        _St.turns = [{"resp": {}}, {"resp": {"tool_catalog": []}},
                     {"resp": {"tool_catalog": ["bills_summary", "read_logs"]}}]
        assert service.session_expanded_tools(_St()) == {"bills_summary", "read_logs"}


class TestHistoryBudget:
    """历史预算派生：窗口未知回落固定值；已知窗口按比例切给历史，再受成本线约束。"""

    def test_unknown_window_falls_back(self):
        assert pilot_context.derive_history_budget(None) == pilot_context.HISTORY_BUDGET_TOKENS
        assert pilot_context.derive_history_budget(0) == pilot_context.HISTORY_BUDGET_TOKENS
        assert pilot_context.derive_history_budget(-5) == pilot_context.HISTORY_BUDGET_TOKENS

    def test_scales_with_window_below_cost_line(self):
        # 成本线以下：有效窗口（窗口 − 回答预留 − 安全垫）按比例归历史，再扣非历史开销
        window = 16384
        usable = window - pilot_context._OUTPUT_RESERVE_TOKENS - pilot_context._WINDOW_BUFFER_TOKENS
        scaled = int(usable * pilot_context.HISTORY_BUDGET_RATIO)
        assert scaled < pilot_context.HISTORY_COST_TOKENS
        assert pilot_context.derive_history_budget(window) == scaled
        assert pilot_context.derive_history_budget(window, 2000) == scaled - 2000
        assert pilot_context.derive_history_budget(window, -100) == scaled

    def test_cost_line_caps_large_windows(self):
        # 窗口决定「装得下多少」，成本线决定「值不值得装这么多」——大窗口不再线性放开。
        # 每轮每个请求都要重发整段历史，一次缓存未命中就是整段全量重算的账单。
        assert pilot_context.derive_history_budget(200_000) == pilot_context.HISTORY_COST_TOKENS
        assert pilot_context.derive_history_budget(1_000_000) == pilot_context.HISTORY_COST_TOKENS
        # 成本线是「一次请求的总量」：固定开销照扣，扣完剩下的才是历史
        assert (pilot_context.derive_history_budget(200_000, 5000)
                == pilot_context.HISTORY_COST_TOKENS - 5000)
        # 小窗口本来就在线以下，成本线不动它
        assert (pilot_context.derive_history_budget(16384)
                < pilot_context.derive_history_budget(200_000))

    def test_clamps_small_windows(self):
        assert pilot_context.derive_history_budget(4096, 8000) == pilot_context._HISTORY_BUDGET_MIN
        assert pilot_context.derive_history_budget(8000) == pilot_context._HISTORY_BUDGET_MIN

    def test_service_budget_uses_window_from_cfg(self):
        unknown = service._history_budget(_CFG)
        assert unknown == pilot_context.HISTORY_BUDGET_TOKENS
        # 期望值按当前开销动态推导：工具表体量随版本演化，测试钉公式不钉开销
        overhead = (
            pilot_context.estimate_tokens(service._SYSTEM_PROMPT)
            + pilot_context.estimate_tokens(
                json.dumps(service.pilot_tools.tool_specs(), ensure_ascii=False))
        )
        usable = 16384 - pilot_context._OUTPUT_RESERVE_TOKENS - pilot_context._WINDOW_BUFFER_TOKENS
        assert service._history_budget({**_CFG, "context_window": 16384}) == max(
            pilot_context._HISTORY_BUDGET_MIN,
            min(int(usable * pilot_context.HISTORY_BUDGET_RATIO) - overhead,
                pilot_context.HISTORY_COST_TOKENS),
        )


class TestSystemPromptCapabilities:
    """系统提示词能力宣告：钉住自主性与证据纪律的硬条款，防提示词回退成工具清单枚举。"""

    def test_declares_autonomy_and_evidence_discipline(self):
        prompt = service._SYSTEM_PROMPT
        # 证据纪律：日期与数字必须有工具证据，禁止按惯例补
        assert "日期、价格、折扣" in prompt
        assert "严禁按「历年惯例」" in prompt
        # 开放问题不自我设限：说做不到之前必须先查完整能力
        assert "list_tools" in prompt
        assert "绝不要说「我没有这个能力」" in prompt
        # 呈现与导航能力仍在（模型不得声称无渲染/无法跳转能力）
        assert "<svg>" in prompt
        assert "navigate" in prompt
        assert "Markdown 表格" in prompt
        # 批量动作走提议确认
        assert "propose_bulk" in prompt

    def test_prompt_is_not_a_tool_catalog(self):
        """提示词不得退化成工具清单：不得逐个罗列冷门工具名。

        工具用法跟着工具走（工具表里带 description），提示词只讲方法论——
        否则加一个工具就要改一次提示词，且清单会把方法论淹掉。"""
        prompt = service._SYSTEM_PROMPT
        for cold in ("system_diagnostics", "read_logs", "proxy_latency_test",
                     "proxy_exit_detail", "sync_subscriptions", "read_attachment",
                     "list_sessions", "get_user_preferences"):
            assert cold not in prompt


class TestPruneOldToolResults:
    """老工具结果清理：整轮让位之前那道免费减负（只改发给模型的视图）。"""

    @staticmethod
    def _groups(count: int, size: int = 400) -> list[dict]:
        msgs = []
        for i in range(count):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "arguments": {}}]})
            msgs.append({"role": "tool", "content": "价" * size})
        return msgs

    def test_keeps_recent_groups_verbatim(self):
        msgs = self._groups(8)
        out = pilot_context.prune_old_tool_results(msgs)
        cleared = [m for m in out if m["content"] == pilot_context.CLEARED_TOOL_RESULT]
        kept = [m for m in out if m["role"] == "tool" and m["content"] != pilot_context.CLEARED_TOOL_RESULT]
        assert len(cleared) == 8 - pilot_context.KEEP_RECENT_TOOL_GROUPS
        assert len(kept) == pilot_context.KEEP_RECENT_TOOL_GROUPS
        # 传入的列表不被改动：这是视图，不是存储
        assert all(m["content"] != pilot_context.CLEARED_TOOL_RESULT for m in msgs)

    def test_noop_within_keep_window(self):
        msgs = self._groups(3)
        assert pilot_context.prune_old_tool_results(msgs) == msgs

    def test_assemble_clears_before_dropping_turns(self):
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s1")
        for i in range(6):
            st.turns.append({
                "q": f"q{i}",
                "messages": [
                    {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "arguments": {}}]},
                    {"role": "tool", "content": "价格数据" * 200},
                    {"role": "assistant", "content": f"回答{i}"},
                ],
                "resp": {"answer": f"回答{i}"},
            })

        full = pilot_context.assemble(st, "现在呢", None, budget=10 ** 9)
        pruned = pilot_context.prune_old_tool_results(full)
        # 预算卡在「清得动、装不下整轮」之间：该清老工具结果，而不是丢轮次
        budget = pilot_context.estimate_messages(pruned) + 10
        assert budget < pilot_context.estimate_messages(full)
        out = pilot_context.assemble(st, "现在呢", None, budget=budget)
        assert any(m.get("content") == pilot_context.CLEARED_TOOL_RESULT for m in out)
        assert any("回答0" in str(m.get("content")) for m in out)  # 最早那轮没被让位丢掉

    @staticmethod
    def _named_groups(count: int, name: str, *, kind: str = "rows", size: int = 400) -> list[dict]:
        """带工具名的真实形态：名字只在同组 assistant 的 tool_calls 里。"""
        msgs = []
        for i in range(count):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": name, "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": json.dumps({"kind": kind, "payload": "价" * size},
                                               ensure_ascii=False)})
        return msgs

    def test_only_replayable_tool_results_are_cleared(self):
        """只清「能重新调回来」的只读工具结果；动作类结果不可复现，留着。"""
        read = self._named_groups(8, "price_drops")
        out = pilot_context.prune_old_tool_results(read)
        assert sum(1 for m in out if m["content"] == pilot_context.CLEARED_TOOL_RESULT) == 3

        write = self._named_groups(8, "add_follow")
        assert pilot_context.prune_old_tool_results(write) == write

    def test_failed_results_are_kept(self):
        """失败结果承载「这一步没成」的事实，清掉模型就没法决定要不要重试。"""
        msgs = self._named_groups(8, "price_drops", kind="failed")
        assert pilot_context.prune_old_tool_results(msgs) == msgs

    def test_placeholder_tells_model_how_to_recover(self):
        assert "重新调用" in pilot_context.CLEARED_TOOL_RESULT

    def test_low_trigger_ratio_clears_before_budget(self):
        """闲置回落：触发线降到九成，体量还没超预算就先清（此刻缓存已过期，清了不亏）。"""
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s1")
        for i in range(6):
            st.turns.append({
                "q": f"q{i}",
                "messages": [
                    {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "arguments": {}}]},
                    {"role": "tool", "content": "价格数据" * 200},
                    {"role": "assistant", "content": f"回答{i}"},
                ],
                "resp": {"answer": f"回答{i}"},
            })
        full = pilot_context.assemble(st, "现在呢", None, budget=10 ** 9)
        full_tokens = pilot_context.estimate_messages(full)
        # 触发线上：正常触发线是 1.0（体量恰好等于预算 → 不动）
        plain = pilot_context.assemble(st, "现在呢", None, budget=full_tokens)
        # 闲置触发线 0.9：同样体量下先把老工具结果清掉
        idle = pilot_context.assemble(st, "现在呢", None, budget=full_tokens,
                                      prune_ratio=pilot_context.IDLE_PRUNE_TRIGGER_RATIO)
        assert not any(m.get("content") == pilot_context.CLEARED_TOOL_RESULT for m in plain)
        assert any(m.get("content") == pilot_context.CLEARED_TOOL_RESULT for m in idle)


class TestRetainBoundary:
    """逐字保留的近期历史按窗口比例算（DSH retainRatio），不再钉死两轮。"""

    @staticmethod
    def _turns(count: int, chars: int) -> list[dict]:
        return [{"q": f"q{i}", "messages": [{"role": "assistant", "content": "长" * chars}],
                 "resp": {}} for i in range(count)]

    def test_unknown_window_keeps_recent_two_turns(self):
        assert pilot_context.retain_boundary(self._turns(10, 200), None) == 8

    def test_large_window_retains_much_more(self):
        turns = self._turns(500, 200)
        boundary = pilot_context.retain_boundary(turns, 200_000)
        retained = sum(pilot_context.estimate_messages(t["messages"]) for t in turns[boundary:])
        assert boundary < 490                     # 远早于「留两轮」(498)，这才是大窗口的意义
        assert retained >= pilot_context.derive_retain_tokens(200_000)
        assert retained < pilot_context.derive_retain_tokens(200_000) * 1.5

    def test_never_distills_the_current_turn(self):
        turns = self._turns(4, 10)
        turns[-1]["messages"] = [{"role": "assistant", "content": "长" * 300_000}]
        assert pilot_context.retain_boundary(turns, 200_000) == 2

    def test_retain_scales_with_window_then_caps_at_cost_line(self):
        # 窗口未知 → 0（调用方退回轮数下限）
        assert pilot_context.derive_retain_tokens(None) == 0
        assert pilot_context.derive_retain_tokens(0) == 0
        # 成本线以下按窗口比例；保留段恒在历史预算以内（压缩才有腾挪空间）
        small = pilot_context.derive_retain_tokens(16384)
        assert small == pytest.approx(
            (16384 - pilot_context._OUTPUT_RESERVE_TOKENS) * pilot_context.RETAIN_RATIO, abs=1)
        assert small < pilot_context.derive_history_budget(16384)
        # 成本线以上：200k 与 1M 的逐字保留段一样厚——否则保留段自己就把预算吃满
        assert (pilot_context.derive_retain_tokens(200_000)
                == pilot_context.derive_retain_tokens(1_000_000)
                == int(pilot_context.HISTORY_COST_TOKENS * pilot_context.RETAIN_RATIO))
        assert (pilot_context.derive_retain_tokens(200_000)
                < pilot_context.derive_history_budget(200_000))


class TestSummaryBudget:
    """摘要输出上限跟窗口走：被蒸馏的历史是十万量级，摘要却是它之后唯一的载体。"""

    def test_scales_and_clamps(self):
        assert pilot_context.derive_summary_max_tokens(None) == pilot_context.SUMMARY_MAX_TOKENS_MIN
        assert pilot_context.derive_summary_max_tokens(200_000) > 2_000
        assert pilot_context.derive_summary_max_tokens(1_000_000) == pilot_context.SUMMARY_MAX_TOKENS_MAX
        assert (pilot_context.derive_summary_max_tokens(400_000)
                > pilot_context.derive_summary_max_tokens(200_000))

    def test_old_hard_cap_is_gone(self):
        # 历史上摘要被钉在 380 token / 300 字；大窗口下那等于把历史丢掉
        assert pilot_context.derive_summary_max_tokens(1_000_000) > 380 * 10


class TestCompactPlan:
    """蒸馏计划：边界来自保留预算，区间太小不蒸（省一次模型调用，不是失败）。"""

    def test_no_plan_when_nothing_above_boundary(self):
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s")
        st.turns = TestRetainBoundary._turns(2, 200)
        assert pilot_context.compact_plan(st, 200_000) is None
        assert pilot_context.compact_plan(None, 200_000) is None

    def test_region_too_small_is_not_worth_compacting(self):
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s")
        st.turns = TestRetainBoundary._turns(3, 10)   # 区间远小于摘要上限的倍数
        assert pilot_context.compact_plan(st, None) is None

    def test_plan_carries_boundary_and_budget(self):
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s")
        st.turns = TestRetainBoundary._turns(600, 200)
        plan = pilot_context.compact_plan(st, 200_000)
        assert plan is not None
        start, through, max_tokens = plan
        assert start == 0 and 0 < through < 600
        assert max_tokens == pilot_context.derive_summary_max_tokens(200_000)


class TestUsageCorrection:
    """用提供方回传的真实输入量收紧本地估值——只紧不松。"""

    class _St:
        def __init__(self, turns):
            self.turns = turns

    def test_no_sample_means_no_correction(self):
        assert service._measure_correction(None, 1000) == 1.0
        assert service._measure_correction(self._St([]), 1000) == 1.0
        assert service._measure_correction(self._St([{"resp": {}}]), 1000) == 1.0

    def test_small_sample_is_ignored(self):
        st = self._St([{"resp": {"first_in": 900, "ctx_tokens": 10}}])
        assert service._measure_correction(st, 0) == 1.0

    def test_underestimate_is_corrected_upwards(self):
        # 真实跑了 2000，本地只估到 800（低估）→ 预算按 2.0 收紧，但夹在上限
        st = self._St([{"resp": {"first_in": 2_000, "ctx_tokens": 800}}])
        assert service._measure_correction(st, 0) == service._CORRECTION_MAX

    def test_overestimate_never_loosens_below_one(self):
        st = self._St([{"resp": {"first_in": 500, "ctx_tokens": 900}}])
        assert service._measure_correction(st, 0) == 1.0

    def test_absurd_ratio_falls_back(self):
        st = self._St([{"resp": {"first_in": 10_000_000, "ctx_tokens": 900}}])
        assert service._measure_correction(st, 0) == 1.0

    def test_budget_is_tightened_by_correction(self):
        assert (pilot_context.derive_history_budget(200_000, 5000, correction=1.4)
                < pilot_context.derive_history_budget(200_000, 5000))

    def test_history_budget_passes_correction_through(self):
        # 工具表体量随版本演化：按当前真实开销反推一个「真实/估值 = 1.3」的样本
        tools_tokens = pilot_context.estimate_tokens(json.dumps(
            service.pilot_tools.tool_specs(), ensure_ascii=False))
        st = self._St([{"resp": {"first_in": int((1_000 + tools_tokens) * 1.3),
                                 "ctx_tokens": 1_000}}])
        assert service._measure_correction(st, tools_tokens) == pytest.approx(1.3, abs=0.01)
        tight = service._history_budget({**_CFG, "context_window": 200_000}, state=st)
        loose = service._history_budget({**_CFG, "context_window": 200_000})
        assert tight < loose


class TestIdlePrune:
    """闲置回落：前缀缓存早已过期，此刻清理不额外损失缓存。"""

    def test_minutes_from_turn_timestamp(self):
        import time as _time
        from app.domains.pilot.session import SessionState

        st = SessionState(sid="s")
        assert service._idle_minutes(st) is None
        st.turns = [{"ts": _time.strftime("%Y-%m-%dT%H:%M:%S")}]
        assert service._idle_minutes(st) == pytest.approx(0, abs=0.2)
        st.turns = [{"ts": "2020-01-01T00:00:00"}]
        assert service._idle_minutes(st) > pilot_context.IDLE_PRUNE_MINUTES
        st.turns = [{"ts": "不是时间"}]
        assert service._idle_minutes(st) is None



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

    def test_manual_window_beats_auto(self):
        assert pilot_config._resolve_window(32000, 200000) == (32000, "manual")
        assert pilot_config._resolve_window(None, 200000) == (200000, "auto")
        # 表外模型不拒绝猜测：按兜底窗口估算（source=fallback，前端提示可手填覆盖）
        assert pilot_config._resolve_window(None, None) == (
            model_windows.FALLBACK_CONTEXT_WINDOW,
            "fallback",
        )

    def test_guess_matches_known_models(self):
        from app.domains.pilot import model_windows

        guess = model_windows.guess_context_window
        assert guess("deepseek-chat") == 131072
        assert guess("glm-4-plus") == 131072
        assert guess("claude-3-5-sonnet-20241022") == 200000
        assert guess("gpt-4o-mini") == 128000
        assert guess("kimi-k2-0711-preview") == 131072
        # 新一代型号沿用同系列窗口（未发布型号的保守取值）
        assert guess("glm-5.3-flash") == 131072
        assert guess("kimi-k3") == 131072
        assert guess("deepseek-v4-pro") == 131072
        # 先具体后宽泛：短后缀规则不能被通用规则吃掉
        assert guess("moonshot-v1-8k") == 8192
        assert guess("moonshot-v1-128k") == 131072
        assert guess("gpt-4-turbo") == 128000

    def test_guess_returns_none_for_unknown(self):
        from app.domains.pilot import model_windows

        guess = model_windows.guess_context_window
        # 网关自定义名认不出就不要猜：回落固定预算，行为与从前一致
        assert guess("u2-flash") is None
        assert guess("some-private-model") is None
        assert guess("") is None
        assert guess(None) is None

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
    async def test_trigger_measures_full_view_not_dropped_one(self, monkeypatch):
        """触发看全量视图：装配让位到装得下就不再压缩，等于存档永远不更新。

        这里装配结果（让位到最近 2 轮）已在预算内，但未让位的全量历史超了——
        按旧口径不会压缩，实际会让位硬扛、把更早的对话静默丢掉。"""
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        self._patch_split_stream(monkeypatch, compact_fail=False, calls=calls)
        store = service.get_store()
        sid = "sess-full-view"
        for i in range(3):  # 每轮约 3000 token：3 轮超预算，让位到 2 轮正好装得下
            await store.append_turn(sid, {
                "q": f"问题{i}",
                "resp": {"answer": "答" * 1875},
                "messages": [
                    {"role": "user", "content": f"问题{i}" + "长" * 1875},
                    {"role": "assistant", "content": "答" * 1875},
                ],
                "last_game": None,
            })
        state = await store.load(sid)
        base, compacted = await service._assemble_for_model(state, "新问题", None, {**_CFG})
        assert compacted is True and calls["compact"] == 1
        # 压缩后带存档的视图明显小于未压缩的全量历史
        assert pilot_context.estimate_messages(base) < state.markers[-1]["pre_tokens"]

    @pytest.mark.asyncio
    async def test_free_prune_first_skips_the_paid_compaction(self, monkeypatch):
        """先免费清理、清得动就不摘要：省下一次真花钱的模型调用（DSH 的顺序）。"""
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        self._patch_split_stream(monkeypatch, compact_fail=False, calls=calls)
        store = service.get_store()
        sid = "sess-prune-shortcut"
        for i in range(9):  # 9 组工具结果：留下最近 5 组，清掉 4 组即回到预算内
            await store.append_turn(sid, {
                "q": f"问题{i}",
                "resp": {"answer": f"回答{i}"},
                "messages": [
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"c{i}", "type": "function",
                         "function": {"name": "price_drops", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": f"c{i}",
                     "content": json.dumps({"kind": "rows", "payload": "价" * 1400},
                                           ensure_ascii=False)},
                    {"role": "assistant", "content": f"回答{i}"},
                ],
                "last_game": None,
            })
        state = await store.load(sid)
        full = pilot_context.full_view(state, "新问题", None)
        budget = pilot_context.HISTORY_BUDGET_TOKENS
        assert pilot_context.estimate_messages(full) > budget          # 压力确实越线
        assert pilot_context.estimate_messages(
            pilot_context.prune_old_tool_results(full)) <= budget       # 但清得动
        base, compacted = await service._assemble_for_model(state, "新问题", None, {**_CFG})
        assert compacted is False and calls["compact"] == 0
        assert any(m.get("content") == pilot_context.CLEARED_TOOL_RESULT for m in base)

    @pytest.mark.asyncio
    async def test_manual_compact_resets_breaker(self, monkeypatch):
        _patch_ready(monkeypatch)
        calls = {"compact": 0}
        cfg = {**_CFG}
        self._patch_split_stream(monkeypatch, compact_fail=True, calls=calls)
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


class TestToolExecutionWatch:
    """工具执行期心跳、预算收口与失败归属：执行不再静默，异常不吞整轮。"""

    @pytest.mark.asyncio
    async def test_progress_frames_during_slow_tool(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
            ("answer", "完成"),
        ])

        async def _slow(name, arguments, guarded=False, sid=None):
            await asyncio.sleep(0.3)
            return {"kind": "games", "items": [{"appid": 1}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _slow)
        monkeypatch.setattr(service, "_TOOL_TICK_S", 0.05)
        events = [e async for e in service.ask_stream("我关注了哪些游戏", session_id="s-tick")]
        progress = [e for e in events if e["type"] == "tool_progress"]
        assert progress and all(e["name"] == "list_follows" for e in progress)
        stamps = [e["elapsed_ms"] for e in progress]
        assert stamps == sorted(stamps) and stamps[0] >= 50
        tool_ev = [e for e in events if e["type"] == "tool"][0]
        assert tool_ev["status"] == "ok" and tool_ev["duration_ms"] >= 250
        # 心跳只走流式事件，不改变账本形态（步骤与 done 与既有一致）
        assert events[-1]["steps"] == [{"label": "follows", "status": "ok", "data": {"count": 1}}]

    @pytest.mark.asyncio
    async def test_budget_exceeded_yields_timeout_step(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
            ("answer", "没查到，稍后再试。"),
        ])
        cancelled = {"v": False}

        async def _hang(name, arguments, guarded=False, sid=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise
            return {"kind": "games", "items": []}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _hang)
        monkeypatch.setattr(service, "_TOOL_TICK_S", 0.05)
        monkeypatch.setattr(service.pilot_tools, "tool_budget_s", lambda name: 0.2)
        events = [e async for e in service.ask_stream("我关注了哪些游戏", session_id="s-timeout")]
        tool_ev = [e for e in events if e["type"] == "tool"][0]
        assert tool_ev["status"] == "timeout" and tool_ev["label"] == "follows"
        assert tool_ev["duration_ms"] >= 200
        assert cancelled["v"] is True
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert done["steps"] == [{"label": "follows", "status": "timeout", "data": {}}]

    @pytest.mark.asyncio
    async def test_tool_failure_attributed_and_fed_back(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
            ("answer", "查询失败了。"),
        ])

        async def _boom(name, arguments, guarded=False, sid=None):
            raise RuntimeError("db down")

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _boom)
        events = [e async for e in service.ask_stream("我关注了哪些游戏", session_id="s-fail")]
        tool_ev = [e for e in events if e["type"] == "tool"][0]
        assert tool_ev["status"] == "failed"
        assert events[-1]["type"] == "done" and events[-1]["source"] == "llm"

    @pytest.mark.asyncio
    async def test_disconnect_cancels_inflight_tool(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
            ("answer", "完成"),
        ])
        cancelled = {"v": False}

        async def _hang(name, arguments, guarded=False, sid=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _hang)
        monkeypatch.setattr(service, "_TOOL_TICK_S", 0.05)
        gen = service.ask_stream("我关注了哪些游戏", session_id="s-cut")
        async for e in gen:
            if e["type"] == "tool_progress":
                break
        await gen.aclose()
        await asyncio.sleep(0.05)
        assert cancelled["v"] is True

    def test_budget_by_category(self):
        budgets = service.pilot_tools.tool_budget_s
        assert budgets("navigate") == service.pilot_tools._BUDGET_DEFAULT_S
        assert budgets("bills_summary") == 60.0
        assert budgets("audit_follows_workflow") == 90.0
        assert budgets("add_follow") == service.pilot_tools._BUDGET_SLOW_S
        assert budgets("sync_library") == service.pilot_tools._BUDGET_SLOW_S
        assert budgets("list_tasks") == service.pilot_tools._BUDGET_TASK_S


class TestAgentNudge:
    """追问机制：模型空转 / 只宣告不行动时循环内回灌系统提示再给一轮（每轮至多一次）。"""

    @staticmethod
    def _patch_scripts(monkeypatch, scripts, calls):
        _patch_ready(monkeypatch)  # 配置/用量替身（本类再覆写 chat_stream 为逐请求脚本）
        it = iter(scripts)

        async def _chat(**kw):
            calls.append(list(kw.get("messages") or []))  # 快照：循环会原地追加消息
            for item in next(it):
                yield item

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _chat)

    @pytest.mark.asyncio
    async def test_empty_phase_nudges_then_answers(self, monkeypatch):
        calls = []
        self._patch_scripts(
            monkeypatch,
            [[], [("answer", "补上结论。"), ("usage", (10, 5))]],
            calls,
        )
        events = [e async for e in service.ask_stream("最近有什么值得关注的高分史低大作？", session_id="s-nudge1")]
        done = events[-1]
        assert done["type"] == "done" and done["answer"] == "补上结论。" and done["reason"] is None
        # 两阶段：第一次空转被追问，第二次给出终答
        assert [e for e in events if e["type"] == "step_start"][-1]["step"] == 2
        # 追问消息随发送原样入账（账本重放保前缀缓存）：问句 + 意图笔记 + 追问 + 终答
        state = await service.get_store().load("s-nudge1")
        roles = [m.get("role") for m in state.turns[-1]["messages"]]
        assert roles == ["user", "system", "system", "assistant"]

    @pytest.mark.asyncio
    async def test_announced_without_tools_nudges_then_answers(self, monkeypatch):
        calls = []
        self._patch_scripts(
            monkeypatch,
            [[("answer", "好问题，我来深入查一下。")], [("answer", "真实结论。"), ("usage", (10, 5))]],
            calls,
        )
        events = [e async for e in service.ask_stream("最近有什么值得关注的高分史低大作？", session_id="s-nudge2")]
        done = events[-1]
        assert "真实结论。" in done["answer"]
        # 第一次请求之后收到追问系统消息（在对话末尾），第二次请求正常执行
        first_msgs, second_msgs = calls[0], calls[1]
        assert len(second_msgs) == len(first_msgs) + 1
        assert "没有实际调用工具" in second_msgs[-1]["content"]
        # 宣告文本不进模型对话（只留展示层）
        assert all("我来深入查一下" not in str(m.get("content")) for m in second_msgs)

    @pytest.mark.asyncio
    async def test_conclusive_answer_not_nudged(self, monkeypatch):
        calls = []
        self._patch_scripts(
            monkeypatch,
            [[("answer", "史低 199 元，近一年中位 299。"), ("usage", (10, 5))]],
            calls,
        )
        events = [e async for e in service.ask_stream("赛博朋克2077 现在史低多少", session_id="s-nudge3")]
        done = events[-1]
        assert done["answer"].startswith("史低") and done["reason"] is None
        assert [e for e in events if e["type"] == "step_start"][-1]["step"] == 1

    @pytest.mark.asyncio
    async def test_intent_note_reaches_model(self, monkeypatch):
        calls = []
        self._patch_scripts(
            monkeypatch,
            [[("answer", "好的。"), ("usage", (10, 5))]],
            calls,
        )
        events = [e async for e in service.ask_stream("最近有什么值得关注的高分史低大作？", session_id="s-note1")]
        assert events[-1]["type"] == "done"
        note = calls[0][-1]
        assert note["role"] == "system" and "意图路由" in note["content"]

    @pytest.mark.asyncio
    async def test_guarded_question_note_mentions_propose_bulk(self, monkeypatch):
        calls = []
        self._patch_scripts(
            monkeypatch,
            [[("answer", "好的。"), ("usage", (10, 5))]],
            calls,
        )
        events = [e async for e in service.ask_stream("清空所有提醒规则", session_id="s-note2")]
        assert events[-1]["type"] == "done"
        assert "propose_bulk" in calls[0][-1]["content"]


class TestMonthlyCap:
    def test_zero_means_unlimited(self):
        assert pilot_config.DEFAULT_MONTHLY_CAP == 0
        assert pilot_config.over_cap({"inp": 10**9, "out": 0, "calls": 1, "total": 10**9}, 0) is False
        assert pilot_config.over_cap({"inp": 5, "out": 0, "calls": 1, "total": 5}, 5) is True
        assert pilot_config.over_cap({"inp": 4, "out": 0, "calls": 1, "total": 4}, 5) is False


class TestMessagelessTurnReplay:
    def test_replay_line_synthesized_for_messageless_turn(self):
        turn = {"q": "打开代理页", "resp": {"source": "facts", "answer": "",
                                            "tools": ["navigate: proxies"],
                                            "cards": [{"kind": "navigate", "target": "proxies", "path": "/proxies"}]}}
        msgs = pilot_context._turn_view(turn)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "系统回放" in msgs[0]["content"] and "navigate" in msgs[0]["content"]

    def test_messageless_turn_without_facts_stays_silent(self):
        turn = {"q": "在吗", "resp": {"source": "none", "reason": "no_data", "answer": "", "tools": [], "cards": []}}
        assert pilot_context._turn_view(turn) == []

    def test_assemble_includes_replay(self):
        state = pilot_context.__dict__ and None
        from app.domains.pilot.session import SessionState
        st = SessionState(sid="s1")
        st.turns = [{"q": "打开代理页", "resp": {"source": "facts", "answer": "",
                                                 "tools": ["navigate: proxies"], "cards": [{"kind": "navigate"}]}}]
        msgs = pilot_context.assemble(st, "现在呢", None)
        assert any("系统回放" in str(m.get("content")) for m in msgs)


class TestDecisionAgent:
    """内部快速决策引擎（System 1 Decision Agent）测试。"""

    def test_card_suppressed_when_model_passes_emit_card_false(self):
        from app.domains.pilot.decision import DECISION_AGENT
        base = {"kind": "price", "name": "黑神话", "currentPrice": 26800}
        dec = DECISION_AGENT.decide_card(
            tool_name="get_price_briefing",
            result={"kind": "price", "cnyFen": 26800},
            arguments={"appid": 2358720, "emit_card": False},
            question="黑神话配置要求高吗",
            current_cards=[],
            base_card=base,
        )
        assert dec.should_emit is False
        assert dec.status == "suppressed"
        assert dec.reason == "suppressed_by_model"

    def test_card_suppressed_on_non_price_question(self):
        from app.domains.pilot.decision import DECISION_AGENT
        base = {"kind": "price", "name": "黑神话", "currentPrice": 26800}
        dec = DECISION_AGENT.decide_card(
            tool_name="get_price_briefing",
            result={"kind": "price", "cnyFen": 26800},
            arguments={"appid": 2358720},
            question="黑神话卡顿闪退怎么办",
            current_cards=[],
            base_card=base,
        )
        assert dec.should_emit is False
        assert dec.reason == "no_price_intent"

    def test_model_view_enhancement_contains_ui_card_status(self):
        from app.domains.pilot.decision import DECISION_AGENT, CardDecision
        dec = CardDecision(
            should_emit=True,
            card={"kind": "price"},
            kind="price",
            status="rendered",
            reason="ok",
            summary="价格卡片已在用户界面呈现",
        )
        view = DECISION_AGENT.enhance_model_view({"kind": "price", "cnyFen": 26800, "trend": [1, 2]}, dec)
        assert "_ui_card" in view
        assert view["_ui_card"]["status"] == "rendered"
        assert "trend" not in view

    def test_single_game_query_suppresses_list_cards(self):
        from app.domains.pilot.decision import DECISION_AGENT
        card = {"kind": "games", "items": [{"name": "赛博朋克2077"}, {"name": "黑神话"}]}
        # 1. 针对单款游戏的问句：抑制全量列表卡片
        dec = DECISION_AGENT.decide_card(
            tool_name="list_follows",
            result=card,
            arguments={},
            question="黑神话在不在我的关注里？",
            current_cards=[],
            base_card=card,
        )
        assert dec.should_emit is False
        assert dec.reason == "single_game_query"

        # 2. 明确请求列表的问句：正常放行渲染
        dec_list = DECISION_AGENT.decide_card(
            tool_name="list_follows",
            result=card,
            arguments={},
            question="查看我的关注列表",
            current_cards=[],
            base_card=card,
        )
        assert dec_list.should_emit is True
        assert dec_list.status == "rendered"

    def test_diagnostic_tool_suppression(self):
        from app.domains.pilot.decision import DECISION_AGENT
        card = {"kind": "rows", "titleKey": "proxies", "rows": [{"k": "node1"}]}
        # 1. 普通非诊断问句：抑制技术诊断卡片
        dec = DECISION_AGENT.decide_card(
            tool_name="proxy_pool_status",
            result=card,
            arguments={},
            question="黑神话为什么价格没刷新？",
            current_cards=[],
            base_card=card,
        )
        assert dec.should_emit is False
        assert dec.reason == "internal_diagnostic"

        # 2. 技术诊断问句：正常放行渲染
        dec_diag = DECISION_AGENT.decide_card(
            tool_name="proxy_pool_status",
            result=card,
            arguments={},
            question="体检网络节点代理池状态",
            current_cards=[],
            base_card=card,
        )
        assert dec_diag.should_emit is True
        assert dec_diag.status == "rendered"

    def test_family_and_achievements_rules_suppression(self):
        from app.domains.pilot.decision import DECISION_AGENT
        fam_card = {"kind": "family", "members": [{"steamId": "123", "personaName": "Tom"}]}
        ach_card = {"kind": "achievements", "unlocked": 10, "total": 81, "completionRate": 12}

        # 规则/攻略问句抑制个人信息卡片
        dec_fam = DECISION_AGENT.decide_card(
            tool_name="family_status",
            result=fam_card,
            arguments={},
            question="Steam家庭共享怎么开启？最多支持几个人？",
            current_cards=[],
            base_card=fam_card,
        )
        assert dec_fam.should_emit is False
        assert dec_fam.reason == "family_rules_query"

        dec_ach = DECISION_AGENT.decide_card(
            tool_name="achievements_summary",
            result=ach_card,
            arguments={},
            question="黑神话全成就难吗？需要几周目？",
            current_cards=[],
            base_card=ach_card,
        )
        assert dec_ach.should_emit is False
        assert dec_ach.reason == "game_achievement_query"

        # 查看个人进度放行渲染
        dec_fam_ok = DECISION_AGENT.decide_card(
            tool_name="family_status",
            result=fam_card,
            arguments={},
            question="看看我的家庭成员列表",
            current_cards=[],
            base_card=fam_card,
        )
        assert dec_fam_ok.should_emit is True

        dec_ach_ok = DECISION_AGENT.decide_card(
            tool_name="achievements_summary",
            result=ach_card,
            arguments={},
            question="查看我的成就进度",
            current_cards=[],
            base_card=ach_card,
        )
        assert dec_ach_ok.should_emit is True

    def test_nav_target_blocked_on_compound_and_action_queries(self):
        from app.domains.pilot.intent import nav_target
        # 复合动作或提问不直通导航
        assert nav_target("去设置修改密钥") is None
        assert nav_target("看看我的关注里有没有打折") is None
        assert nav_target("去关注列表找找黑神话") is None
        # 纯粹导航直通
        assert nav_target("打开设置") == "settings"
        assert nav_target("去关注列表") == "follows"

    def test_registry_views_exposes_emit_card_schema(self):
        from app.domains.agent.tools.registry import REGISTRY
        views = REGISTRY.views()
        for v in views:
            props = v["function"]["parameters"].get("properties") or {}
            assert "emit_card" in props
            assert props["emit_card"]["type"] == "boolean"

    def test_skeleton_hint_accurate_and_not_leaking(self):
        # 决策代理按意图与参数抑制骨架占位
        assert service._stream_card_hint("list_follows", [], arguments={"emit_card": False}, question="查看关注") is None
        assert service._stream_card_hint("list_follows", [], arguments={}, question="黑神话在不在我的关注里？") is None
        assert service._stream_card_hint("proxy_pool_status", [], arguments={}, question="黑神话为什么价格没刷新？") is None
        assert service._stream_card_hint("family_status", [], arguments={}, question="家庭共享怎么开？") is None
        assert service._stream_card_hint("steam_free", [], arguments={}, question="目前有什么可以领取的免费喜加一游戏吗？") is None
        # 放行情况
        assert service._stream_card_hint("list_follows", [], arguments={}, question="查看我的关注列表") == "rows"
        assert service._stream_card_hint("proxy_pool_status", [], arguments={}, question="体检网络节点代理池") == "rows"
        assert service._stream_card_hint("epic_free", [], arguments={}, question="目前有什么可以领取的免费喜加一游戏吗？") == "epic"

    def test_free_offers_rows_suppressed_by_default_unless_explicit(self):
        from app.domains.pilot.decision import DECISION_AGENT
        steam_card = {"kind": "rows", "titleKey": "steamFree", "rows": [{"k": "System Shock 2", "vKey": "steamFree"}]}
        # 1. 默认聊天对话：作为内部推导事实抑制原始数据表发卡
        dec = DECISION_AGENT.decide_card(
            tool_name="steam_free",
            result=steam_card,
            arguments={},
            question="目前有什么可以领取的免费喜加一游戏吗？",
            current_cards=[],
            base_card=steam_card,
        )
        assert dec.should_emit is False
        assert dec.reason == "internal_data_for_synthesis"

        # 2. 显式要求发卡：emit_card=True 放行
        dec_explicit = DECISION_AGENT.decide_card(
            tool_name="steam_free",
            result=steam_card,
            arguments={"emit_card": True},
            question="目前有什么可以领取的免费喜加一游戏吗？",
            current_cards=[],
            base_card=steam_card,
        )
        assert dec_explicit.should_emit is True
        assert dec_explicit.status == "rendered"

    def test_epic_free_emits_epic_card_by_default(self):
        from app.domains.pilot.decision import DECISION_AGENT
        epic_card = {
            "kind": "epic",
            "titleKey": "epic",
            "offers": [
                {"title": "The Falconeer", "titleCn": "空战猎鹰", "upcoming": False, "start": "2026-10-03", "end": "2026-10-10", "url": "https://store.epicgames.com/p/the-falconeer"},
            ],
            "total": 1,
        }
        dec = DECISION_AGENT.decide_card(
            tool_name="epic_free",
            result=epic_card,
            arguments={},
            question="目前有什么可以领取的免费喜加一游戏吗？",
            current_cards=[],
            base_card=epic_card,
        )
        assert dec.should_emit is True
        assert dec.status == "rendered"
        assert dec.kind == "epic"
        assert "空战猎鹰" in (dec.summary or "")




class TestCardLedger:
    """跨轮卡片账本：卡片出现挂账（phases.cards）、近轮账本去重与模型同步。"""

    @pytest.mark.asyncio
    async def test_phase_cards_ledgered_and_streamed(self, monkeypatch):
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
            ("answer", "这是你的关注。"),
        ])

        async def _games(name, arguments, guarded=False, sid=None):
            return {"kind": "games", "items": [{"appid": 1, "name": "黑神话"}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _games)
        events = [e async for e in service.ask_stream("关注里都有什么游戏", session_id="s-ledger")]
        card_evs = [e for e in events if e["type"] == "card" and e.get("card")]
        assert card_evs and card_evs[0]["card"]["kind"] == "games"
        done = events[-1]
        phase_cards = done["phases"][0]["cards"]
        assert phase_cards and phase_cards[0]["kind"] == "games"
        assert done["cards"] == phase_cards

    def test_recent_cards_digest_reads_turn_ledger(self):
        from types import SimpleNamespace

        state = SimpleNamespace(turns=[
            {"resp": {"cards": [{"kind": "price", "name": "黑神话"}]}},
            {"resp": {"cards": [{"kind": "games", "items": [{"name": "艾尔登法环"}]}]}},
        ])
        digest = service._recent_cards_digest(state)
        assert ("price", "黑神话") in digest
        assert ("games", "艾尔登法环") in digest
        assert service._recent_cards_digest(None) == []

    def test_ledger_note_lists_recent_cards(self):
        note = service._card_ledger_note([("games", "艾尔登法环"), ("price", "黑神话")])
        assert note is not None and "艾尔登法环" in note["content"]
        assert service._card_ledger_note([]) is None

    @pytest.mark.asyncio
    async def test_ledger_note_reaches_model_and_dedups_static_lists(self, monkeypatch):
        state = await service.get_store().load_or_new("s-cross")
        await service._record_turn(state, "旧问题", {
            "answer": "", "source": "llm",
            "cards": [{"kind": "games", "items": [{"appid": 2, "name": "艾尔登法环"}]}],
        })
        _patch_ready(monkeypatch, stream=[("answer", "占位")])
        seen: dict = {}

        async def _capture(messages=None, **kw):
            seen["messages"] = list(messages or [])
            yield ("answer", "好的。")

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _capture)

        async def _games(name, arguments, guarded=False, sid=None):
            return {"kind": "games", "items": [{"appid": 2, "name": "艾尔登法环"}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _games)
        events = [e async for e in service.ask_stream("关注里都有什么游戏", session_id="s-cross")]
        # 词条锁定「已向用户呈现过」而非泛化的「界面卡片」——后者会连系统提示一起命中
        note = [m for m in seen["messages"]
                if m.get("role") == "system" and "已向用户呈现过" in str(m.get("content"))]
        assert note and "艾尔登法环" in note[0]["content"]
        # 静态清单卡近轮已呈现：本轮不再发卡（模型可用 emit_card=true 强制）
        assert not [e for e in events if e["type"] == "card" and e.get("card")]
        assert not events[-1]["phases"][0]["cards"]

    def test_price_card_not_deduped_across_turns(self):
        from app.domains.pilot.decision import DECISION_AGENT

        base = {"kind": "price", "name": "黑神话", "cnyFen": 26800}
        dec = DECISION_AGENT.decide_card(
            tool_name="get_price_briefing", result={"kind": "price", "cnyFen": 26800},
            arguments={"appid": 2358720}, question="黑神话现在多少钱",
            current_cards=[], base_card=base,
            recent_cards=[("price", "黑神话")],
        )
        assert dec.should_emit is True

    def test_explicit_emit_card_overrides_recent_dedup(self):
        from app.domains.pilot.decision import DECISION_AGENT

        base = {"kind": "games", "items": [{"name": "艾尔登法环"}]}
        dec = DECISION_AGENT.decide_card(
            tool_name="list_follows", result={"kind": "games", "items": [{"name": "艾尔登法环"}]},
            arguments={"emit_card": True}, question="再列一次我的关注",
            current_cards=[], base_card=base,
            recent_cards=[("games", "艾尔登法环")],
        )
        assert dec.should_emit is True and dec.status == "rendered"


class TestParallelReadTools:
    """模型一次给多个只读调用时的并行批次。"""

    @pytest.mark.asyncio
    async def test_read_tools_run_concurrently(self, monkeypatch):
        import time as _time

        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [
                {"id": "t1", "name": "list_follows", "arguments": {}},
                {"id": "t2", "name": "list_alerts", "arguments": {}},
            ]),
            ("answer", "两边都查完了。"),
        ])
        started: dict[str, float] = {}

        async def _slow(name, arguments, guarded=False, sid=None):
            started[name] = _time.monotonic()
            await asyncio.sleep(0.3 if name == "list_follows" else 0.05)
            if name == "list_follows":
                return {"kind": "games", "items": [{"appid": 1, "name": name}]}
            return {"kind": "rows", "rows": [{"k": "提醒", "v": "x"}]}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _slow)
        events = [e async for e in service.ask_stream("黑神话在关注里吗", session_id="s-par")]
        # 并行证明：第二个工具在第一个还在跑时就已启动（顺序执行会差 ≥0.3s）
        assert started["list_alerts"] - started["list_follows"] < 0.15
        tool_evs = [e for e in events if e["type"] == "tool"]
        assert [e["name"] for e in tool_evs] == ["list_follows", "list_alerts"]
        assert all(e["status"] == "ok" for e in tool_evs)
        done = events[-1]
        assert [s["label"] for s in done["phases"][0]["steps"]] == ["follows", "alerts"]
        # 两个工具结果都回灌模型（账本里两条 tool 消息；turn_messages 不下发 SSE）
        state = await service.get_store().load("s-par")
        tool_msgs = [m for t in state.turns for m in (t.get("messages") or [])
                     if m.get("role") == "tool"]
        assert len(tool_msgs) == 2


class TestTurnStepClosure:
    """回合收口：tool_start 声明的运行态占位在结算时原位回填，异常收场统一归为 stopped。"""

    @staticmethod
    def _spy_captured(monkeypatch):
        """包裹收口助手，捕获收口前后的 phases 深快照。"""
        captured: dict = {}
        original = service._settle_running_steps

        def _spy(phases):
            snap = lambda ps: [{**p, "steps": [dict(s) for s in p["steps"]]} for p in ps]
            captured["before"] = snap(phases)
            original(phases)
            captured["after"] = snap(phases)

        monkeypatch.setattr(service, "_settle_running_steps", _spy)
        return captured

    def test_settle_running_steps_flips_only_running(self):
        phases = [{"steps": [
            {"label": "a", "status": "ok", "data": {"count": 1}},
            {"label": "b", "status": "running", "data": {}},
        ]}]
        service._settle_running_steps(phases)
        assert phases[0]["steps"][0] == {"label": "a", "status": "ok", "data": {"count": 1}}
        assert phases[0]["steps"][1] == {"label": "b", "status": "stopped", "data": {}}

    @pytest.mark.asyncio
    async def test_cancelled_mid_tool_settles_running_to_stopped(self, monkeypatch):
        """工具执行中途回合被取消：收口把运行态占位归为 stopped，中断轮不落账。"""
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_follows", "arguments": {}}]),
        ])
        monkeypatch.setattr(service, "_TOOL_TICK_S", 0.02)
        captured = self._spy_captured(monkeypatch)
        entered = asyncio.Event()

        async def _hang(name, arguments, guarded=False, sid=None):
            entered.set()
            await asyncio.Event().wait()  # 永不返回：等收口取消

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _hang)
        state = await service.get_store().load_or_new("s-stop")
        agen = service._agent_stream(
            [{"role": "user", "content": "查一下关注"}], state, "查一下关注", None, _CFG)
        assert (await agen.__anext__())["type"] == "step_start"
        assert (await agen.__anext__())["type"] == "tool_start"
        assert (await agen.__anext__())["type"] == "tool_progress"
        for _ in range(20):
            await asyncio.sleep(0.01)
            if entered.is_set():
                break
        assert entered.is_set()
        await agen.aclose()
        await asyncio.sleep(0.05)  # 让被取消的在飞任务走完取消流程
        # 收口前：tool_start 已声明、尚未结算 → 运行态占位在场
        assert captured["before"][0]["steps"] == [{"label": "follows", "status": "running", "data": {}}]
        # 收口后：统一归为 stopped 终态，phases 无 running 残留
        assert captured["after"][0]["steps"] == [{"label": "follows", "status": "stopped", "data": {}}]
        # 中断轮不落账：账本文件从未创建
        assert not (service.get_store().base / "s-stop.jsonl").exists()

    @pytest.mark.asyncio
    async def test_parallel_batch_cancel_settles_all_declared_steps(self, monkeypatch):
        """并行批次中途取消：全部已声明的运行态占位一并收口为 stopped。"""
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [
                {"id": "t1", "name": "list_follows", "arguments": {}},
                {"id": "t2", "name": "list_alerts", "arguments": {}},
            ]),
        ])
        monkeypatch.setattr(service, "_TOOL_TICK_S", 0.02)
        captured = self._spy_captured(monkeypatch)
        entered = asyncio.Event()

        async def _hang(name, arguments, guarded=False, sid=None):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _hang)
        state = await service.get_store().load_or_new("s-par-stop")
        agen = service._agent_stream(
            [{"role": "user", "content": "都查一下"}], state, "都查一下", None, _CFG)
        assert (await agen.__anext__())["type"] == "step_start"
        assert (await agen.__anext__())["type"] == "tool_start"
        assert (await agen.__anext__())["type"] == "tool_start"
        assert (await agen.__anext__())["type"] == "tool_progress"
        for _ in range(20):
            await asyncio.sleep(0.01)
            if entered.is_set():
                break
        assert entered.is_set()
        await agen.aclose()
        await asyncio.sleep(0.05)
        assert captured["before"][0]["steps"] == [
            {"label": "follows", "status": "running", "data": {}},
            {"label": "alerts", "status": "running", "data": {}},
        ]
        assert captured["after"][0]["steps"] == [
            {"label": "follows", "status": "stopped", "data": {}},
            {"label": "alerts", "status": "stopped", "data": {}},
        ]
        assert not (service.get_store().base / "s-par-stop.jsonl").exists()

    @pytest.mark.asyncio
    async def test_tool_cancelled_error_propagates_without_ledger_write(self, monkeypatch):
        """桩工具抛取消：取消异常上抛不吞，收口已把占位归为 stopped，轮次不落账。"""
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "list_alerts", "arguments": {}}]),
        ])
        captured = self._spy_captured(monkeypatch)

        async def _raise_cancel(name, arguments, guarded=False, sid=None):
            raise asyncio.CancelledError()

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _raise_cancel)
        with pytest.raises(asyncio.CancelledError):
            async for _ in service.ask_stream("查提醒", session_id="s-cancel"):
                pass
        assert captured["before"][0]["steps"] == [{"label": "alerts", "status": "running", "data": {}}]
        assert captured["after"][0]["steps"] == [{"label": "alerts", "status": "stopped", "data": {}}]
        assert not (service.get_store().base / "s-cancel.jsonl").exists()

    @pytest.mark.asyncio
    async def test_done_phases_settled_and_ledger_free_of_running(self, monkeypatch):
        """正常收尾回归：结算原位回填占位，done 与账本 phases 全为既有终态、无 stopped 误伤。"""
        _patch_ready(monkeypatch, stream=[
            ("tool_calls", [{"id": "t1", "name": "get_price_briefing", "arguments": {"appid": 7}}]),
            ("answer", "结论：值得。"),
        ])
        monkeypatch.setattr(service.pilot_tools, "execute_tool",
                            _tool_stub([], {**_FACTS, "appid": 7}))
        events = [e async for e in service.ask_stream("这游戏值得吗", session_id="s-term")]
        done = events[-1]
        assert done["type"] == "done"
        all_steps = [s for p in done["phases"] for s in p["steps"]]
        assert all_steps == [{"label": "price", "status": "ok", "data": {"name": "测试游戏"}}]
        state = await service.get_store().load("s-term")
        ledger_steps = [s for t in state.turns
                        for p in (t.get("resp", {}).get("phases") or [])
                        for s in p.get("steps") or []]
        assert ledger_steps == all_steps


class TestDeterministicPrefetch:
    """确定性预取：数据型问句在首轮模型请求前先把事实取回注入（步骤挂第 1 阶段）。"""

    @staticmethod
    def _spy_stream(seen_notes):
        async def _spy(**kw):
            for m in kw["messages"]:
                c = m.get("content")
                if isinstance(c, str) and service._PREFETCH_NOTE_HEAD in c:
                    seen_notes.append(c)
            yield ("answer", "结论：现价 50 元。")
            yield ("usage", (100, 20))
        return _spy

    @pytest.mark.asyncio
    async def test_price_question_prefetches_briefing_before_first_request(self, monkeypatch):
        executed = []
        seen_notes = []
        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_llm, "chat_stream", self._spy_stream(seen_notes))

        async def _search(question, query=None):
            return [{"appid": 292030, "name": "测试游戏"}]

        monkeypatch.setattr(service.pilot_tools, "search_games", _search)
        monkeypatch.setattr(service.pilot_tools, "execute_tool", _tool_stub(executed, _FACTS))
        events = [e async for e in service.ask_stream("《测试游戏》现在多少钱", session_id="s-prefetch")]
        kinds = [e["type"] for e in events]
        assert "tool_start" in kinds and "card" in kinds
        first_tool = [e for e in events if e["type"] == "tool_start"][0]
        assert first_tool["name"] == "get_price_briefing" and first_tool["card_kind"] == "price"
        assert executed[0] == ("get_price_briefing", 292030, False)
        assert seen_notes and "get_price_briefing" in seen_notes[0]
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert done["steps"][0] == {"label": "price", "status": "ok", "data": {"name": "测试游戏"}}
        assert done["cards"] and done["cards"][0]["kind"] == "price"
        state = await service.get_store().load("s-prefetch")
        ledger_steps = [s for t in state.turns
                        for p in (t.get("resp", {}).get("phases") or [])
                        for s in p.get("steps") or []]
        assert ledger_steps == done["steps"]

    @pytest.mark.asyncio
    async def test_price_prefetch_abstains_on_ambiguous_target(self, monkeypatch):
        executed = []
        seen_notes = []
        _patch_ready(monkeypatch, stream=[("answer", "需要先确认是哪一款。")])
        _spy = self._spy_stream(seen_notes)
        monkeypatch.setattr(service.pilot_llm, "chat_stream", _spy)

        async def _search(question, query=None):
            return [{"appid": 1}, {"appid": 2}]

        monkeypatch.setattr(service.pilot_tools, "search_games", _search)
        monkeypatch.setattr(service.pilot_tools, "execute_tool", _tool_stub(executed, _FACTS))
        events = [e async for e in service.ask_stream("《赛博朋克2077》和《巫师3》多少钱", session_id="s-ambig")]
        assert not [e for e in events if e["type"] in ("tool_start", "tool", "card")]
        assert not seen_notes and not executed

    @pytest.mark.asyncio
    async def test_free_words_prefetch_offers_without_search(self, monkeypatch):
        executed = []
        seen_notes = []
        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_llm, "chat_stream", self._spy_stream(seen_notes))

        async def _exec(name, arguments, guarded=False, sid=None):
            executed.append(name)
            if name == "epic_free":
                return {"kind": "epic", "titleKey": "epic",
                        "offers": [{"title": "Free", "titleCn": "免费游戏"}], "total": 1}
            return {"kind": "rows", "titleKey": "steamFree",
                    "rows": [{"k": "A", "v": "", "vKey": "x"}], "total": 1}

        monkeypatch.setattr(service.pilot_tools, "execute_tool", _exec)
        events = [e async for e in service.ask_stream("最近有什么免费游戏可以白嫖", session_id="s-free")]
        assert executed == ["epic_free", "steam_free"]
        cards = [e["card"] for e in events if e["type"] == "card" and e["card"]]
        assert [c["kind"] for c in cards] == ["epic"]
        done = events[-1]
        assert [s["label"] for s in done["phases"][0]["steps"]] == ["epicFree", "steamFree"]
        assert done["cards"] and done["cards"][0]["kind"] == "epic"
        assert seen_notes and "epic_free" in seen_notes[0] and "steam_free" in seen_notes[0]

    @pytest.mark.asyncio
    async def test_region_words_prefetch_region_prices(self, monkeypatch):
        executed = []
        seen_notes = []
        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_llm, "chat_stream", self._spy_stream(seen_notes))

        async def _search(question, query=None):
            return [{"appid": 292030, "name": "测试游戏"}]

        monkeypatch.setattr(service.pilot_tools, "search_games", _search)
        monkeypatch.setattr(service.pilot_tools, "execute_tool", _tool_stub(executed, _FACTS))
        events = [e async for e in service.ask_stream("《测试游戏》全区多少钱", session_id="s-region")]
        assert executed[0] == ("get_region_prices", 292030, False)
        first_tool = [e for e in events if e["type"] == "tool_start"][0]
        assert first_tool["name"] == "get_region_prices"
        assert seen_notes and "get_region_prices" in seen_notes[0]

    @pytest.mark.asyncio
    async def test_forced_single_pass_omits_tools_on_first_request(self, monkeypatch):
        """目标零歧义（appid 直给）+ 纯数据咨询：首轮请求不带工具表，模型只能基于预取事实作答。"""
        executed = []
        seen_tools = []
        _patch_ready(monkeypatch)

        async def _spy(**kw):
            seen_tools.append(kw.get("tools"))
            yield ("answer", "结论：现价 50 元。")
            yield ("usage", (100, 20))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _spy)
        monkeypatch.setattr(service.pilot_tools, "execute_tool", _tool_stub(executed, _FACTS))
        events = [e async for e in service.ask_stream(
            "它现在多少钱", session_id="s-force", appid=292030)]
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm"
        assert seen_tools and seen_tools[0] is None
        assert [e for e in events if e["type"] == "tool_start"][0]["name"] == "get_price_briefing"
        assert done["cards"] and done["cards"][0]["kind"] == "price"
        assert done["ttft_n"] == 1

    @pytest.mark.asyncio
    async def test_compound_question_keeps_full_tool_table(self, monkeypatch):
        """复合问句（价格 + 推荐）：预取照发，但保留完整工具表不禁工具。"""
        seen_tools = []
        _patch_ready(monkeypatch)

        async def _spy(**kw):
            seen_tools.append(kw.get("tools"))
            yield ("answer", "结论。")
            yield ("usage", (100, 20))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _spy)

        async def _search(question, query=None):
            return [{"appid": 292030, "name": "测试游戏"}]

        monkeypatch.setattr(service.pilot_tools, "search_games", _search)
        monkeypatch.setattr(service.pilot_tools, "execute_tool",
                            _tool_stub([], _FACTS))
        events = [e async for e in service.ask_stream(
            "《测试游戏》多少钱？顺便推荐几个类似的游戏", session_id="s-compound")]
        assert seen_tools and seen_tools[0]  # 工具表仍在场
        names = [e["name"] for e in events if e["type"] == "tool_start"]
        assert names and names[0] == "get_price_briefing"
