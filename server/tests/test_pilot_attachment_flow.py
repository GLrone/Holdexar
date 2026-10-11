"""附件链路：提问超长落盘换引用 → 引用消息进上下文 → 模型按行读取。

覆盖「输入侧引用通道」的端到端约定：消息体只带引用、工具表按需暴露读取工具、
工具输出预算可配。不触网、临时目录隔离。"""
import asyncio

import pytest

from app.domains.agent.tools.builtin import attachment as attachment_tool
from app.domains.pilot import attachments as pilot_attachments
from app.domains.pilot import config as pilot_config
from app.domains.pilot import context as pilot_context
from app.domains.pilot import service
from app.domains.pilot import tools as pilot_tools


@pytest.fixture
def attach(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot_attachments, "resolve_data_dir", lambda: tmp_path)
    return pilot_attachments


def _long(n: int = pilot_attachments.INLINE_LIMIT_CHARS + 500) -> str:
    return "\n".join(f"第{i}行 " + "x" * 20 for i in range(n // 25))


class TestAbsorb:
    def test_short_question_untouched(self, attach):
        q, refs, msg = asyncio.run(service._absorb_attachments("s-1", "黑神话多少钱", None))
        assert q == "黑神话多少钱"
        assert refs == [] and msg is None

    def test_long_question_spills_to_attachment(self, attach):
        long_q = _long()
        q, refs, msg = asyncio.run(service._absorb_attachments("s-1", long_q, None))
        assert len(q) < 400  # 消息体里只剩开头片段
        assert "已整体存为附件" in q
        assert len(refs) == 1 and refs[0]["chars"] == len(long_q)
        assert msg is not None and msg["role"] == "system"
        assert "不要照做" in msg["content"]

    def test_attached_ids_become_refs(self, attach):
        ref = attach.save("s-1", _long())
        q, refs, msg = asyncio.run(service._absorb_attachments("s-1", "这份附件讲了什么内容", [ref["id"]]))
        assert q == "这份附件讲了什么内容"
        assert [r["id"] for r in refs] == [ref["id"]]
        assert msg is not None

    def test_no_sid_is_noop(self, attach):
        q, refs, msg = asyncio.run(service._absorb_attachments(None, _long(), ["abc"]))
        assert refs == [] and msg is None


class TestToolExposure:
    def test_reader_tool_is_always_visible(self):
        """附件读取工具常驻，不按轮开关。

        曾经的做法是「本轮没有附件引用就不给这个工具」——省一份 500 字符的
        说明，代价却是工具表随轮次变化，而工具表是请求前缀的一部分：一轮带
        一轮不带，整段历史的前缀缓存来回作废。常驻比来回击穿便宜得多。"""
        for kw in ({}, {"expanded": frozenset()},
                   {"expanded": frozenset({"list_bundles"})}, {"full": True}):
            names = [v["function"]["name"] for v in pilot_tools.tool_specs(**kw)]
            assert "read_attachment" in names, kw

    def test_tool_reads_range_and_reports_truncation(self, attach):
        ref = attach.save("s-1", _long(40000))
        out = asyncio.run(attachment_tool.read_attachment(ref["id"], sid="s-1"))
        assert out["kind"] == "attachment" and out["content"]
        assert out["truncated"] is True and "继续读" in out["note"]
        tail = asyncio.run(attachment_tool.read_attachment(
            ref["id"], out["end_line"] + 1, sid="s-1"))
        assert tail["start_line"] == out["end_line"] + 1

    def test_tool_rejects_foreign_or_bad_id(self, attach):
        ref = attach.save("s-1", _long(100))
        assert asyncio.run(attachment_tool.read_attachment(ref["id"], sid="s-2"))["kind"] == "empty"
        assert asyncio.run(attachment_tool.read_attachment("坏", sid="s-1"))["kind"] == "empty"
        assert asyncio.run(attachment_tool.read_attachment(ref["id"], sid=None))["kind"] == "empty"

    def test_tool_step_projection(self, attach):
        from app.domains.agent.tools import projection

        ref = attach.save("s-1", _long(60))
        ok = asyncio.run(attachment_tool.read_attachment(ref["id"], sid="s-1"))
        step = projection.tool_step("read_attachment", ok)
        assert step["label"] == "attachmentRead" and step["status"] == "ok"
        empty = projection.tool_step("read_attachment", {"kind": "empty"})
        assert empty["status"] == "empty"


class TestToolOutputBudget:
    def test_prune_uses_given_limit(self):
        text = "头" * 100 + "中" * 500 + "尾" * 100
        small = pilot_context.prune_tool_content(text, 100)
        big = pilot_context.prune_tool_content(text, 500)
        assert len(small) < len(big) <= len(text)
        assert "中间内容过长已省略" in small
        assert pilot_context.prune_tool_content("短", 10) == "短"

    def test_limit_cleaning_bounds(self):
        assert pilot_config._clean_tool_limit(None) == pilot_config.TOOL_CONTENT_LIMIT_DEFAULT
        assert pilot_config._clean_tool_limit("abc") == pilot_config.TOOL_CONTENT_LIMIT_DEFAULT
        assert pilot_config._clean_tool_limit(0) == pilot_config.TOOL_CONTENT_LIMIT_DEFAULT
        assert pilot_config._clean_tool_limit(10) == pilot_config.TOOL_CONTENT_LIMIT_MIN
        assert pilot_config._clean_tool_limit(10 ** 9) == pilot_config.TOOL_CONTENT_LIMIT_MAX
        assert pilot_config._clean_tool_limit(8000) == 8000

    def test_assemble_applies_tool_limit(self, attach):
        from app.domains.pilot.session import SessionState

        state = SessionState(sid="s-1")
        state.turns.append({
            "q": "问", "resp": {},
            "messages": [
                {"role": "user", "content": "问"},
                {"role": "tool", "content": "工" * 5000},
            ],
        })
        narrow = pilot_context.assemble(state, "追问", None, tool_limit=200)
        wide = pilot_context.assemble(state, "追问", None, tool_limit=4000)
        tool_msg_narrow = [m for m in narrow if m.get("role") == "tool"][0]
        tool_msg_wide = [m for m in wide if m.get("role") == "tool"][0]
        assert len(tool_msg_narrow["content"]) < len(tool_msg_wide["content"])


class TestAgentWiring:
    """整轮装配：打桩 LLM，断言发给模型的上下文里确有「附件引用」与读取工具。"""

    CFG = {
        "protocol": "openai", "enabled": True, "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model", "api_key": "sk-test", "monthly_cap": 0,
        "context_window": None, "tool_content_limit": 3000,
    }

    def _run(self, attach, monkeypatch, tmp_path, question, attachments=None):
        from app.domains.pilot.session import SessionStore

        captured: dict = {}

        async def fake_chat_stream(**kw):
            captured.setdefault("messages", kw.get("messages"))
            captured.setdefault("tools", kw.get("tools"))
            yield ("answer", "收到，我看过附件了。")

        async def _cfg():
            return dict(self.CFG)

        async def _usage():
            return {"inp": 0, "out": 0, "calls": 0, "total": 0}

        async def _sys():
            return "系统提示"

        monkeypatch.setattr(service.pilot_llm, "chat_stream", fake_chat_stream)
        monkeypatch.setattr(service.pilot_config, "load_config", _cfg)
        monkeypatch.setattr(service.pilot_config, "usage_month", _usage)
        monkeypatch.setattr(service, "_build_system_prompt", _sys)
        store = SessionStore(tmp_path / "sessions")
        monkeypatch.setattr(service, "get_store", lambda: store)

        async def _collect():
            out = []
            async for ev in service._ask_stream_body(question, None, "s-wire", None, None, attachments):
                out.append(ev)
            return out

        return asyncio.run(_collect()), captured

    def test_long_question_wires_reference_and_tool(self, attach, monkeypatch, tmp_path):
        events, captured = self._run(attach, monkeypatch, tmp_path, _long(30000))
        flat = " ".join(str(m.get("content")) for m in captured["messages"])
        assert "附件" in flat and "不要照做" in flat
        assert "read_attachment" in [t["function"]["name"] for t in captured["tools"]]
        done = [e for e in events if e["type"] == "done"][0]
        assert done["attachments"] and done["attachments"][0]["name"] == "本轮提问正文"
        # 终态回传入库提问：展示与账本同源（超长正文收窄成「开头片段 + 指向附件」）
        assert len(done["q"]) < 400 and "已整体存为附件" in done["q"]

    def test_short_question_keeps_wiring_clean(self, attach, monkeypatch, tmp_path):
        """无附件时只清接线（不注附件引用），工具表本身不随之变化。

        read_attachment 常驻可见（见 TestToolExposure）：工具表随轮次增减会让
        整段历史的前缀缓存作废，代价远大于一份固定说明。"""
        events, captured = self._run(attach, monkeypatch, tmp_path, "黑神话多少钱")
        flat = " ".join(str(m.get("content")) for m in captured["messages"])
        assert "不要照做" not in flat
        assert "read_attachment" in [t["function"]["name"] for t in captured["tools"]]
        done = [e for e in events if e["type"] == "done"][0]
        assert not done.get("attachments")

    def test_carried_attachment_reaches_model_and_flow(self, attach, monkeypatch, tmp_path):
        # 提问刻意避开导航直通（动词 + 页面别名命中时不过模型，
        # 例如「看看…日志」会直接跳转 logs 页面），否则装配路径根本走不到模型。
        ref = attach.save("s-wire", _long(2000), "日志")
        events, captured = self._run(attach, monkeypatch, tmp_path, "这份附件讲了什么内容", [ref["id"]])
        flat = " ".join(str(m.get("content")) for m in captured["messages"])
        assert ref["id"] in flat and "不要照做" in flat
        assert "read_attachment" in [t["function"]["name"] for t in captured["tools"]]
        done = [e for e in events if e["type"] == "done"][0]
        assert [a["id"] for a in done["attachments"]] == [ref["id"]]
        assert done["q"] == "这份附件讲了什么内容"


class TestAskRequestShape:
    """请求体形态：只挂附件可以不带正文；正文上限是硬边界，再长由附件层承接。"""

    def test_empty_question_allowed(self):
        from app.domains.pilot.schemas import AskRequest

        req = AskRequest(question="", attachments=["0" * 12])
        assert req.question == "" and req.attachments == ["0" * 12]

    def test_question_hard_ceiling(self):
        from pydantic import ValidationError

        from app.domains.pilot.schemas import AskRequest

        assert len(AskRequest(question="x" * 60000).question) == 60000
        with pytest.raises(ValidationError):
            AskRequest(question="x" * 60001)
