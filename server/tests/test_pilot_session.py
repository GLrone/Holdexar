"""pilot 会话账本（session.py）与上下文装配（context.py）单测（不触网、临时目录隔离）。"""
import asyncio
import json
import os

import pytest

from app.domains.pilot import config as pilot_config
from app.domains.pilot import context as pilot_context
from app.domains.pilot import service
from app.domains.pilot.llm import PilotLlmError
from app.domains.pilot.session import (SESSION_MAX_FILES,
                                       SessionState, SessionStore, valid_sid)

_CFG = {
    "protocol": "openai",
    "enabled": True,
    "base_url": "http://127.0.0.1:9/v1",
    "model": "test-model",
    "api_key": "sk-test",
    "monthly_cap": 1000000,
}


def _async_return(value):
    async def _fn(*a, **kw):
        return value
    return _fn


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


def _turn(q: str, answer: str, **extra) -> dict:
    return {"q": q, "resp": {"answer": answer, "source": "llm", "reason": None,
                             "facts": None, "cards": [], "cached": False},
            "messages": [{"role": "user", "content": q},
                         {"role": "assistant", "content": answer}],
            "last_game": extra.get("last_game")}


class TestValidSid:
    def test_accepts_uuid_and_short_ids(self):
        assert valid_sid("3f2a9c0e-1234-5678-9abc-def012345678")
        assert valid_sid("s-1728000000-ab12cd")

    def test_rejects_traversal_and_empty(self):
        assert not valid_sid("../etc/passwd")
        assert not valid_sid("")
        assert not valid_sid("a" * 65)
        assert not valid_sid("白名单外")


class TestSessionStore:
    @pytest.mark.asyncio
    async def test_turn_roundtrip_across_store_instances(self, store, tmp_path):
        """重启等价：新实例从文件折叠出同一会话状态。"""
        await store.append_turn("sid-1", _turn("第一个问题", "第一个回答", last_game={"appid": 7, "name": "G"}))
        await store.append_turn("sid-1", _turn("第二个问题", "第二个回答"))
        reopened = SessionStore(tmp_path / "sessions")
        state = await reopened.load("sid-1")
        assert [t["q"] for t in state.turns] == ["第一个问题", "第二个问题"]
        assert state.last_game == {"appid": 7, "name": "G"}
        assert state.summary is None

    @pytest.mark.asyncio
    async def test_load_missing_returns_none_and_lazy_create(self, store):
        assert await store.load("nope") is None
        state = await store.load_or_new("nope")
        assert isinstance(state, SessionState)
        # 未写入前零足迹（懒创建）
        assert not (store.base / "nope.jsonl").exists()

    @pytest.mark.asyncio
    async def test_summary_fold_latest_wins_and_clamped(self, store, tmp_path):
        await store.append_turn("sid-2", _turn("q1", "a1"))
        await store.append_turn("sid-2", _turn("q2", "a2"))
        await store.append_summary("sid-2", "旧存档", 1)
        await store.append_summary("sid-2", "新存档", 99)
        reopened = SessionStore(tmp_path / "sessions")
        state = await reopened.load("sid-2")
        assert state.summary == "新存档"
        assert state.summary_through == 2  # 超出轮数的 through 收敛到 len(turns)

    @pytest.mark.asyncio
    async def test_torn_tail_tolerated(self, store):
        await store.append_turn("sid-3", _turn("完整轮", "回答"))
        path = store.base / "sid-3.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write('{"t":"turn","q":"撕')  # 模拟崩溃留下的半行
        state = await store.load("sid-3")
        assert len(state.turns) == 1 and state.turns[0]["q"] == "完整轮"
        # 后续追加照常
        assert await store.append_turn("sid-3", _turn("再问", "再答")) is True
        assert len((await store.load("sid-3")).turns) == 2

    @pytest.mark.asyncio
    async def test_invalid_sid_never_touches_disk(self, store):
        assert await store.append_turn("../evil", _turn("q", "a")) is False
        assert await store.load("../evil") is None
        assert not (store.base / "evil.jsonl").exists()

    def test_sweep_keeps_idle_and_only_caps_count(self, tmp_path):
        base = tmp_path / "sessions"
        base.mkdir(parents=True)
        old_path = base / "old.jsonl"
        old_path.write_text('{"t":"turn"}\n', encoding="utf-8")
        stale = 9999999999  # 远早于现在的 mtime，模拟闲置已久的会话
        os.utime(old_path, (stale, stale))
        for i in range(SESSION_MAX_FILES + 5):
            (base / f"s{i}.jsonl").write_text('{"t":"turn"}\n', encoding="utf-8")
        SessionStore(base)
        assert old_path.exists()  # 闲置会话保留（不再按 TTL 静默删除）
        leftovers = sorted(base.glob("*.jsonl"))
        assert len(leftovers) == SESSION_MAX_FILES  # 仅超上限时删最旧

    @pytest.mark.asyncio
    async def test_concurrent_appends_serialize(self, store):
        """同一会话的并发追加逐条入账，互不覆盖。"""
        async def _bang(i: int):
            await store.append_turn("conc", _turn(f"q{i}", f"a{i}"))
        await asyncio.gather(*(_bang(i) for i in range(20)))
        state = await store.load("conc")
        assert len(state.turns) == 20


class TestContextEstimate:
    def test_cjk_aware_estimation(self):
        assert pilot_context.estimate_tokens("") == 0
        ascii_tokens = pilot_context.estimate_tokens("abcd" * 25)
        cjk_tokens = pilot_context.estimate_tokens("测" * 100)
        assert ascii_tokens < cjk_tokens  # 同 token 计量下中文密度更高
        assert pilot_context.estimate_messages([{"role": "user", "content": "x"}]) > 0

    def test_prune_keeps_head_and_tail(self):
        text = "A" * 5000 + "MIDDLE" + "B" * 5000
        pruned = pilot_context.prune_tool_content(text, limit=3000)
        assert len(pruned) < 4000
        assert pruned.startswith("A") and pruned.endswith("B")
        assert "已省略" in pruned
        short = pilot_context.prune_tool_content("短结果", limit=3000)
        assert short == "短结果"


def _state_with_turns(n: int, summary: str | None = None, through: int = 0) -> SessionState:
    state = SessionState(sid="s")
    for i in range(n):
        state.turns.append(_turn(f"问题{i}" + "长" * 400, f"回答{i}" + "长" * 400))
    state.summary = summary
    state.summary_through = through
    return state


class TestAssemble:
    def test_summary_injected_as_user_and_turns_respected(self):
        """压缩摘要以 user 消息衔接（摘要占 user 位），system 位只留系统提示。"""
        state = _state_with_turns(3, summary="存档要点", through=1)
        msgs = pilot_context.assemble(state, "现在呢", None)
        assert msgs[0]["role"] == "user" and "存档要点" in msgs[0]["content"]
        roles = [m["role"] for m in msgs]
        # 存档覆盖轮 0；轮 1、2 原文保留（2 个历史 user）+ 摘要 user + 当前问题
        assert roles.count("user") == 4
        assert msgs[-1]["content"] == "现在呢"

    def test_budget_head_cut_keeps_recent_turns(self, monkeypatch):
        monkeypatch.setattr(pilot_context, "HISTORY_BUDGET_TOKENS", 1700)
        state = _state_with_turns(6)
        msgs = pilot_context.assemble(state, "最新问题", None)
        assert pilot_context.estimate_messages(msgs) <= 1700
        # 最近 RECENT_KEEP_TURNS 轮逐字保留
        bodies = [m.get("content") for m in msgs]
        assert any("问题4" in (b or "") for b in bodies)
        assert not any("问题0" in (b or "") for b in bodies)

    def test_appid_context_appended(self):
        msgs = pilot_context.assemble(None, "多少钱", 427520)
        assert msgs[-1]["content"].endswith("AppID 427520 的游戏详情）")


_CFG_MIN = {"protocol": "openai", "base_url": "http://127.0.0.1:9/v1",
            "api_key": "k", "model": "m"}


_PLAN = (0, 2, 900)  # 显式计划：单测这两条路径时不需要真去推边界（边界另有专测）


class TestCompact:
    @pytest.mark.asyncio
    async def test_nothing_to_distill(self):
        state = _state_with_turns(2)
        assert await pilot_context.compact(state, _CFG_MIN) is None  # 轮数 ≤ KEEP，无可蒸馏

    @pytest.mark.asyncio
    async def test_distills_older_turns_with_merge(self, monkeypatch):
        seen = {}

        async def _scripted(**kw):
            seen["messages"] = kw["messages"]
            seen["max_tokens"] = kw.get("max_tokens")
            yield ("answer", "存档：用户在问价格。")
            yield ("usage", (50, 12))

        monkeypatch.setattr(pilot_context.pilot_llm, "chat_stream", _scripted)
        state = _state_with_turns(4, summary="旧存档", through=0)
        text, through, usage = await pilot_context.compact(state, _CFG_MIN, plan=_PLAN)
        assert text == "存档：用户在问价格。"
        assert through == 2
        assert usage == (50, 12)
        # 首条是合并用的既有存档，末条是压缩指令，中间是被蒸馏区间的真实消息
        assert "旧存档" in seen["messages"][0]["content"]
        assert "合并" in seen["messages"][0]["content"]
        assert seen["messages"][-1]["content"] == pilot_context._COMPACT_INSTRUCTION
        assert seen["max_tokens"] == 900

    @pytest.mark.asyncio
    async def test_compaction_input_replays_real_tool_results(self, monkeypatch):
        """压缩输入必须是真实消息：精确价格只存在于工具返回里，压平只剩问答就丢了。"""
        seen = {}

        async def _scripted(**kw):
            seen["messages"] = kw["messages"]
            yield ("answer", "存档")
            yield ("usage", (1, 1))

        monkeypatch.setattr(pilot_context.pilot_llm, "chat_stream", _scripted)
        state = SessionState(sid="s")
        for i in range(3):
            state.turns.append({
                "q": f"问题{i}",
                "messages": [
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": f"c{i}", "type": "function",
                         "function": {"name": "price_drops", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": f"c{i}",
                     "content": f'{{"kind":"rows","price":{1000 + i}}}'},
                    {"role": "assistant", "content": f"回答{i}"},
                ],
                "resp": {"answer": f"回答{i}"},
            })
        await pilot_context.compact(state, _CFG_MIN, sys_prompt="系统提示", plan=(0, 2, 900))
        blob = "\n".join(str(m.get("content") or "") for m in seen["messages"])
        assert '"price":1001' in blob          # 工具返回原样在输入里
        assert seen["messages"][0]["content"] == "系统提示"

    @pytest.mark.asyncio
    async def test_empty_answer_is_none(self, monkeypatch):
        async def _empty(**kw):
            yield ("answer", "")
        monkeypatch.setattr(pilot_context.pilot_llm, "chat_stream", _empty)
        assert await pilot_context.compact(_state_with_turns(4), _CFG_MIN, plan=_PLAN) is None

    @pytest.mark.asyncio
    async def test_overflowing_compaction_input_drops_oldest_and_retries(self, monkeypatch):
        """压缩输入自己撑爆窗口：丢掉最早一段重试，而不是放弃整个会话。"""
        calls: list[list[dict]] = []

        async def _scripted(**kw):
            calls.append(kw["messages"])
            if len(calls) == 1:
                raise PilotLlmError("prompt is too long: maximum context length exceeded")
            yield ("answer", "存档")
            yield ("usage", (1, 1))

        monkeypatch.setattr(pilot_context.pilot_llm, "chat_stream", _scripted)
        state = _state_with_turns(8)
        text, through, _ = await pilot_context.compact(state, _CFG_MIN, plan=(0, 6, 900))
        assert text == "存档"
        assert len(calls) == 2
        assert len(calls[1]) < len(calls[0])
        # 截断标记在场，且首条不会是落单的 tool 消息
        assert any(pilot_context._COMPACT_RETRY_MARKER in str(m.get("content") or "")
                   for m in calls[1])


class TestOverflow:
    def test_markers(self):
        assert pilot_context.looks_like_overflow("This model's maximum context length is 4096 tokens")
        assert pilot_context.looks_like_overflow("input length exceeds limit")
        assert not pilot_context.looks_like_overflow("connection timed out")

    @pytest.mark.asyncio
    async def test_service_overflow_retry_drops_history(self, monkeypatch, tmp_path):
        """首轮上下文超窗报错 → 丢弃历史仅存档+当前问题重试一次并成功。"""
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        store = SessionStore(tmp_path / "pilot" / "sessions")
        await store.append_turn("s-of", _turn("历史问题", "历史回答"))

        calls: list[list[dict]] = []
        state_flag = {"n": 0}

        async def _scripted(**kw):
            calls.append([dict(m) for m in kw["messages"]])
            state_flag["n"] += 1
            if state_flag["n"] == 1:
                raise PilotLlmError("This model's maximum context length is 4096 tokens")
            yield ("answer", "好")
            yield ("usage", (10, 2))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _scripted)
        monkeypatch.setattr(pilot_config, "load_config", _async_return({**_CFG}))
        monkeypatch.setattr(pilot_config, "usage_month",
                            _async_return({"inp": 0, "out": 0, "calls": 0, "total": 0}))

        async def _add(inp, out):
            pass

        monkeypatch.setattr(pilot_config, "add_usage", _add)
        events = [e async for e in service.ask_stream("再来一次", session_id="s-of")]
        done = events[-1]
        assert done["type"] == "done" and done["source"] == "llm" and done["answer"] == "好"
        assert len(calls) == 2
        # 第二次调用不再含历史轮次
        assert not any("历史问题" in (m.get("content") or "") for m in calls[1])


class TestCompactionInService:
    @pytest.mark.asyncio
    async def test_over_budget_turn_distills_and_persists(self, monkeypatch, tmp_path):
        """装配超预算 → 蒸馏存档落盘 → 下一轮装配带存档且历史让位。"""
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        monkeypatch.setattr(pilot_config, "load_config", _async_return({**_CFG}))
        monkeypatch.setattr(pilot_config, "usage_month",
                            _async_return({"inp": 0, "out": 0, "calls": 0, "total": 0}))

        async def _add(inp, out):
            pass

        monkeypatch.setattr(pilot_config, "add_usage", _add)
        # 预算压到极小，迫使首轮后就触发压缩
        monkeypatch.setattr(pilot_context, "HISTORY_BUDGET_TOKENS", 150)
        compact_calls = {"n": 0}

        async def _compact(state, cfg, **kw):
            compact_calls["n"] += 1
            through = len(state.turns) - pilot_context.RECENT_KEEP_TURNS
            if through <= state.summary_through:
                return None
            return f"存档第{compact_calls['n']}版", through, (30, 8)

        monkeypatch.setattr(pilot_context, "compact", _compact)

        import itertools

        answers = itertools.cycle(["一", "二", "三"])

        async def _stream(**kw):
            yield ("answer", next(answers))
            yield ("usage", (10, 2))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _stream)
        # 轮次内容按真实体量给（按窗口比例的保留边界 + 区间最小体量门槛
        # 都需要被蒸馏区间真的有东西，微型轮次本来就不该触发压缩）
        for i in range(6):
            events = [e async for e in service.ask_stream(f"问题{i}" + "长" * 900, session_id="s-c")]
            assert events[-1]["type"] == "done"

        assert compact_calls["n"] >= 1
        state = await service.get_store().load("s-c")
        assert state.summary and state.summary.startswith("存档第")
        assert (tmp_path / "pilot" / "sessions" / "s-c.jsonl").is_file()


class TestManualCompact:
    @pytest.mark.asyncio
    async def test_compact_session_now_distills_and_persists(self, monkeypatch, tmp_path):
        """手动归档：蒸馏落盘 + 状态更新，重启等价（新实例可读）。"""
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        store = SessionStore(tmp_path / "pilot" / "sessions")
        for i in range(5):
            await store.append_turn("s-m", _turn(f"问题{i}" + "长" * 900, f"回答{i}" + "长" * 900))
        state = await store.load("s-m")

        async def _compact(st, cfg, **kw):
            return "存档文本", 3, (20, 5)

        monkeypatch.setattr(pilot_context, "compact", _compact)
        # 本用例只验落盘与状态流转，蒸馏计划另行专测——给一个确定的计划
        monkeypatch.setattr(pilot_context, "compact_plan", lambda *a, **k: (0, 3, 900))

        async def _add(inp, out):
            pass

        monkeypatch.setattr(pilot_config, "add_usage", _add)
        out = await service.compact_session_now(state, {})
        assert out["ok"] is True and out["summary_through"] == 3 and out["turn_total"] == 5
        assert isinstance(out["pre_tokens"], int) and isinstance(out["post_tokens"], int)
        # 摘要确实把源内容缩小了才允许落盘（不缩小的结果被拒）
        assert out["post_tokens"] < out["pre_tokens"]
        assert state.summary == "存档文本" and state.summary_through == 3
        reopened = SessionStore(tmp_path / "pilot" / "sessions")
        st2 = await reopened.load("s-m")
        assert st2.summary == "存档文本" and st2.summary_through == 3

    @pytest.mark.asyncio
    async def test_compact_session_now_nothing_to_distill(self, monkeypatch, tmp_path):
        """轮数不足 KEEP 时无可蒸馏：机器码返回，状态不动。"""
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        store = SessionStore(tmp_path / "pilot" / "sessions")
        await store.append_turn("s-n", _turn("唯一一问", "答"))
        state = await store.load("s-n")
        out = await service.compact_session_now(state, {})
        assert out == {"ok": False, "reason": "nothing_to_distill"}
        assert state.summary is None and state.summary_through == 0


class TestRestartResume:
    @pytest.mark.asyncio
    async def test_history_reaches_model_after_store_rebuild(self, monkeypatch, tmp_path):
        """store 实例重建（等价进程重启）后，历史轮次仍能进入模型上下文。"""
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        monkeypatch.setattr(pilot_config, "load_config", _async_return({**_CFG}))
        monkeypatch.setattr(pilot_config, "usage_month",
                            _async_return({"inp": 0, "out": 0, "calls": 0, "total": 0}))

        async def _add(inp, out):
            pass

        monkeypatch.setattr(pilot_config, "add_usage", _add)
        seen: list[list[dict]] = []

        async def _stream(**kw):
            seen.append([dict(m) for m in kw["messages"]])
            yield ("answer", "收到")
            yield ("usage", (5, 1))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _stream)
        await service.ask("巫师 3 值得入手吗", session_id="s-r")
        service._store = None  # 模拟进程重启：内存态清零，账本在盘上
        await service.ask("把它加进关注", session_id="s-r")
        second = seen[1]
        assert any(m.get("role") == "user" and "巫师 3" in (m.get("content") or "") for m in second)
        assert any(m.get("role") == "assistant" and m.get("content") == "收到" for m in second)


class TestSessionTitle:
    @pytest.mark.asyncio
    async def test_first_turn_auto_titles_and_rename_wins(self, store):
        sid = "t-title-1"
        await store.append_turn(sid, _turn("打开设置看看", "好"))
        state = await store.load(sid)
        assert state.title == "打开设置看看"
        await store.append_turn(sid, _turn("第二问不改变标题", "好"))
        assert (await store.load(sid)).title == "打开设置看看"
        await store.set_title(sid, "我的设置会话")
        # 重启（新实例）后折叠还原标题
        state2 = await SessionStore(store.base).load(sid)
        assert state2.title == "我的设置会话"

    def test_derive_title_rules(self):
        from app.domains.pilot.session import derive_title

        assert derive_title("短问") == "短问"
        assert derive_title("  多  行\n空格  ") == "多 行 空格"
        t = derive_title("很" * 30)
        assert len(t) == 25 and t.endswith("…")

    @pytest.mark.asyncio
    async def test_list_recent_projection_and_legacy_fallback(self, store):
        await store.append_turn("t-list-a", _turn("甲乙丙丁", "答"))
        await store.append_turn("t-list-b", _turn("戊己庚辛壬癸", "答"))
        items = {i["sid"]: i for i in store.list_recent(10)}
        assert set(items) == {"t-list-a", "t-list-b"}
        assert items["t-list-a"]["title"] == "甲乙丙丁"
        assert items["t-list-b"]["turn_total"] == 1
        assert items["t-list-b"]["updated_at"]
        # 旧文件无 title 行：投影按首问兜底
        import json as _json

        store.base.mkdir(parents=True, exist_ok=True)
        rec = {"t": "turn", **_turn("老会话的首问在这里", "答"), "ts": "2026-10-01T00:00:00"}
        (store.base / "t-legacy.jsonl").write_text(
            _json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        legacy = [i for i in store.list_recent(10) if i["sid"] == "t-legacy"]
        assert legacy and legacy[0]["title"] == "老会话的首问在这里"

    @pytest.mark.asyncio
    async def test_delete_removes_file_and_cache(self, store):
        await store.append_turn("t-del", _turn("问", "答"))
        assert (store.base / "t-del.jsonl").is_file()
        assert await store.delete("t-del") is True
        assert not (store.base / "t-del.jsonl").exists()
        assert await store.load("t-del") is None
        assert await store.delete("t-del") is False
        assert await store.delete("../x") is False


class TestReferenceDigest:
    def test_summary_takes_priority(self):
        state = SessionState(sid="x", summary="存档要点", turns=[{"q": "问", "resp": {}}])
        assert pilot_context.reference_digest(state) == "存档要点"

    def test_budget_bounds_digest(self):
        state = SessionState(sid="x")
        for _ in range(50):
            state.turns.append({"q": "问" * 80, "resp": {"answer": "答" * 80, "tools": []}})
        digest = pilot_context.reference_digest(state)
        assert digest and pilot_context.estimate_tokens(digest) <= pilot_context.REFERENCE_BUDGET_TOKENS

    def test_reference_message_frame(self):
        msg = pilot_context.reference_message("旧标题", "要点")
        assert msg["role"] == "system" and "旧标题" in msg["content"] and "要点" in msg["content"]


class TestRewrite:
    """编辑重问：账本软截断（保留前 N 轮，历史行不删）。"""

    @pytest.mark.asyncio
    async def test_truncate_keeps_prefix_turns(self, store):
        for i in range(3):
            await store.append_turn("s1", _turn(f"q{i}", f"a{i}"))
        assert await store.truncate("s1", 1) is True
        state = await store.load("s1")
        assert [t["q"] for t in state.turns] == ["q0"]

    @pytest.mark.asyncio
    async def test_truncate_survives_reload(self, tmp_path):
        """截断是账本语义而非内存态：换实例重折叠仍只见前 N 轮，历史行不删。"""
        first = SessionStore(tmp_path / "s")
        for i in range(3):
            await first.append_turn("s1", _turn(f"q{i}", f"a{i}"))
        await first.truncate("s1", 2)
        second = SessionStore(tmp_path / "s")
        state = await second.load("s1")
        assert [t["q"] for t in state.turns] == ["q0", "q1"]
        lines = (tmp_path / "s" / "s1.jsonl").read_text(encoding="utf-8").strip().splitlines()
        # 首轮自动标题 1 行 + 3 轮原文 + 1 条截断记录：历史一行未少，截断只追加
        assert len(lines) == 5
        assert any('"t": "truncate"' in line for line in lines)

    @pytest.mark.asyncio
    async def test_truncate_tightens_compaction_views(self, store):
        for i in range(3):
            await store.append_turn("s2", _turn(f"q{i}", f"a{i}"))
        await store.append_summary("s2", "摘要", 2)
        assert (await store.load("s2")).summary_through == 2
        await store.truncate("s2", 1)
        state = await store.load("s2")
        assert state.summary_through == 1
        assert state.markers == []

    @pytest.mark.asyncio
    async def test_truncate_beyond_length_is_kept_verbatim(self, store):
        await store.append_turn("s3", _turn("q0", "a0"))
        await store.truncate("s3", 5)
        assert [t["q"] for t in (await store.load("s3")).turns] == ["q0"]

    @pytest.mark.asyncio
    async def test_rewind_reports_missing_session(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        service._INFLIGHT_TURNS.clear()
        try:
            out = await service.rewind_session("nope", 1)
        finally:
            service._store = None
        assert out["ok"] is False and out["reason"] == "no_session"


class TestResumeNote:
    """中断续跑：上一轮已完成的事实回灌（只随本请求在场，不入账本）。"""

    def test_carries_tools_and_tail(self):
        note = service._resume_note({
            "text": "已经查到史低是 31 元",
            "tools": [{"name": "get_price", "label": "查价格", "summary": "CN ¥62"}],
        })
        assert note is not None and note["role"] == "system"
        content = note["content"]
        assert "查价格" in content and "CN ¥62" in content and "31 元" in content

    def test_none_when_nothing_to_resume(self):
        assert service._resume_note(None) is None
        assert service._resume_note({"text": "   ", "tools": []}) is None

    def test_tool_only_resume_is_valid(self):
        note = service._resume_note({"tools": [{"label": "查关注清单"}]})
        assert note is not None and "查关注清单" in note["content"]

    def test_oversized_payload_is_bounded(self):
        note = service._resume_note({
            "text": "正文" * 2000,
            "tools": [{"label": f"工具{i}", "summary": "x" * 5000} for i in range(40)],
        })
        assert note is not None
        assert len(note["content"]) < 12 * 500 + 600 + 2000


class TestResumeInjection:
    """续跑载荷经路由透传到模型消息（不是只测零件）。"""

    @pytest.mark.asyncio
    async def test_resume_facts_reach_model(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        monkeypatch.setattr(pilot_config, "load_config", _async_return({**_CFG}))
        monkeypatch.setattr(pilot_config, "usage_month",
                            _async_return({"inp": 0, "out": 0, "calls": 0, "total": 0}))

        async def _add(inp, out):
            pass

        monkeypatch.setattr(pilot_config, "add_usage", _add)
        seen: list[list[dict]] = []

        async def _stream(**kw):
            seen.append([dict(m) for m in kw["messages"]])
            yield ("answer", "接着讲")
            yield ("usage", (5, 1))

        monkeypatch.setattr(service.pilot_llm, "chat_stream", _stream)
        service._store = None
        service._INFLIGHT_TURNS.clear()
        try:
            await service.ask(
                "继续", session_id="s-rs",
                resume={"text": "已查到史低 31 元",
                        "tools": [{"label": "查价格", "summary": "CN ¥62"}]},
            )
        finally:
            service._store = None
            service._INFLIGHT_TURNS.clear()
        msgs = seen[0]
        system_text = "\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
        assert "查价格" in system_text and "31 元" in system_text
        # 装配契约不被续跑打破：末位仍是当前问题
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert user_msgs and "继续" in user_msgs[-1]["content"]


class TestSessionFlags:
    """置顶 / 归档：会话级布尔账本事实（追加记录，后者胜）。"""

    @pytest.mark.asyncio
    async def test_pin_toggles_and_persists(self, tmp_path):
        first = SessionStore(tmp_path / "s")
        await first.append_turn("s1", _turn("q0", "a0"))
        assert await first.set_pinned("s1", True) is True
        # 换实例重折叠：置顶是账本语义，不是内存态
        second = SessionStore(tmp_path / "s")
        assert (await second.load("s1")).pinned is True
        assert await second.set_pinned("s1", False) is True
        assert (await second.load("s1")).pinned is False

    @pytest.mark.asyncio
    async def test_archive_flag_round_trips(self, store):
        await store.append_turn("s2", _turn("q0", "a0"))
        await store.set_archived("s2", True)
        assert (await store.load("s2")).archived is True

    @pytest.mark.asyncio
    async def test_list_projects_flags_and_sorts_pinned_first(self, tmp_path):
        st = SessionStore(tmp_path / "s")
        await st.append_turn("old", _turn("旧会话", "a"))
        await st.append_turn("new", _turn("新会话", "a"))
        # 置顶较旧的那个：列表里必须排在新会话之前
        await st.set_pinned("old", True)
        items = st.list_recent(10)
        assert [i["sid"] for i in items] == ["old", "new"]
        assert items[0]["pinned"] is True and items[1]["pinned"] is False
        assert "archived" in items[0] and "snippet" in items[0]


class TestSessionSearch:
    """会话搜索：命中标题或问答正文；正文命中带片段。"""

    @pytest.mark.asyncio
    async def test_matches_title_question_and_answer(self, store):
        await store.append_turn("s1", _turn("地平线价格", "目前 99 元"))
        await store.append_turn("s2", _turn("别的游戏", "史低 31 元"))
        by_title = store.list_recent(10, q="地平线")
        assert [i["sid"] for i in by_title] == ["s1"]
        by_answer = store.list_recent(10, q="31")
        assert [i["sid"] for i in by_answer] == ["s2"]
        assert "31" in by_answer[0]["snippet"]

    @pytest.mark.asyncio
    async def test_no_match_is_excluded(self, store):
        await store.append_turn("s1", _turn("甲", "乙"))
        assert store.list_recent(10, q="完全不存在的词") == []


class TestFork:
    """话题分叉：新会话复制源会话前 N 轮，源账本零改动。"""

    @pytest.mark.asyncio
    async def test_fork_copies_prefix_and_records_provenance(self, store):
        for i in range(3):
            await store.append_turn("src", _turn(f"q{i}", f"a{i}"))
        assert await store.fork("src", "dst", 2) is True
        dst = await store.load("dst")
        assert [t["q"] for t in dst.turns] == ["q0", "q1"]
        assert dst.forked_from == "src"
        assert dst.title == "q0"  # 生效标题随分叉带过来

    @pytest.mark.asyncio
    async def test_fork_leaves_source_untouched(self, tmp_path):
        st = SessionStore(tmp_path / "s")
        for i in range(3):
            await st.append_turn("src", _turn(f"q{i}", f"a{i}"))
        await st.fork("src", "dst", 1)
        assert [t["q"] for t in (await st.load("src")).turns] == ["q0", "q1", "q2"]
        src_lines = (tmp_path / "s" / "src.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(src_lines) == 4  # 自动标题 1 + 3 轮，分叉不写源账本

    @pytest.mark.asyncio
    async def test_fork_refuses_existing_target(self, store):
        await store.append_turn("src", _turn("q0", "a0"))
        await store.append_turn("dst", _turn("别人的问题", "别人的回答"))
        assert await store.fork("src", "dst", 1) is False
        assert [t["q"] for t in (await store.load("dst")).turns] == ["别人的问题"]

    @pytest.mark.asyncio
    async def test_fork_keep_beyond_length_is_clamped(self, store):
        await store.append_turn("src", _turn("q0", "a0"))
        assert await store.fork("src", "dst", 99) is True
        assert [t["q"] for t in (await store.load("dst")).turns] == ["q0"]

    @pytest.mark.asyncio
    async def test_service_fork_returns_new_sid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        service._INFLIGHT_TURNS.clear()
        try:
            store = SessionStore(tmp_path / "pilot" / "sessions")
            for i in range(2):
                await store.append_turn("s-f", _turn(f"q{i}", f"a{i}"))
            out = await service.fork_session("s-f", 1)
            assert out["ok"] is True and out["sid"] and out["turn_total"] == 1
            assert [t["q"] for t in (await store.load(out["sid"])).turns] == ["q0"]
        finally:
            service._store = None
            service._INFLIGHT_TURNS.clear()

    @pytest.mark.asyncio
    async def test_service_fork_reports_missing_session(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        service._INFLIGHT_TURNS.clear()
        try:
            out = await service.fork_session("nope", 1)
        finally:
            service._store = None
        assert out["ok"] is False and out["reason"] == "no_session"

    @pytest.mark.asyncio
    async def test_service_fork_rejected_while_streaming(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        service._INFLIGHT_TURNS["s-busy"] = 1
        try:
            out = await service.fork_session("s-busy", 1)
        finally:
            service._store = None
            service._INFLIGHT_TURNS.clear()
        assert out["ok"] is False and out["reason"] == "busy"

    @pytest.mark.asyncio
    async def test_service_pin_missing_session(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service, "resolve_data_dir", lambda: tmp_path)
        service._store = None
        try:
            out = await service.pin_session("nope", True)
        finally:
            service._store = None
        assert out["ok"] is False and out["reason"] == "no_session"
