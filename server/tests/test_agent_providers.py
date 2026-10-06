"""Agent V2 providers 单测：新旧协议解析同帧对比等价 + 取消 / 重试 / 超时（不触网）。"""
import asyncio
import json

import httpx
import pytest

from app.domains.agent.providers import base, registry
from app.domains.agent.providers.anthropic import AnthropicProvider, _convert, _parse as anthropic_parse
from app.domains.agent.providers.base import (
    AnswerDelta,
    CacheInfo,
    LlmRequest,
    Message,
    ProviderError,
    ThinkingDelta,
    ToolCall,
    ToolCallBlock,
    Usage,
)
from app.domains.agent.providers.ollama import OllamaProvider, _parse as ollama_parse
from app.domains.agent.providers.openai_chat import OpenAiChatProvider, _parse as openai_parse
from app.domains.agent.providers.openai_responses import OpenAiResponsesProvider, _parse as responses_parse
from app.domains.pilot import llm as pilot_llm


def _norm_old(tuples):
    out = []
    for kind, payload in tuples:
        if kind == "thinking":
            out.append(ThinkingDelta(text=payload))
        elif kind == "answer":
            out.append(AnswerDelta(text=payload))
        elif kind == "tool_calls":
            out.append(ToolCallBlock(calls=tuple(ToolCall(**c) for c in payload)))
        elif kind == "usage":
            out.append(Usage(input_tokens=payload[0], output_tokens=payload[1]))
        elif kind == "cache":
            out.append(CacheInfo(**payload))
    return out


async def _collect(gen):
    return [e async for e in gen]


def _lines(frames):
    async def _gen():
        for f in frames:
            yield f
    return _gen()


def _req(**kw):
    defaults = dict(model="m", messages=(Message(role="user", content="hi"),))
    defaults.update(kw)
    return LlmRequest(**defaults)


# ── 解析等价：同一罐头帧喂旧元组解析器与新 LlmEvent 解析器 ────────────────

OPENAI_FRAMES = [
    'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}',
    'data: {"choices":[{"delta":{"content":"答"}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{\\"a\\""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":1}"}}]}}]}',
    'data: not-json',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: {"usage":{"prompt_tokens":100,"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":60}}}',
    'data: [DONE]',
]


@pytest.mark.asyncio
async def test_openai_parse_equivalence():
    old = await _collect(pilot_llm._openai_parse(_lines(OPENAI_FRAMES)))
    new = await _collect(openai_parse(_lines(OPENAI_FRAMES), None))
    assert _norm_old(old) == new
    kinds = [type(e).__name__ for e in new]
    assert kinds == ["ThinkingDelta", "AnswerDelta", "ToolCallBlock", "Usage", "CacheInfo"]
    assert new[2].calls == (ToolCall(id="c1", name="f", arguments={"a": 1}),)
    assert new[3].input_tokens == 100 and new[4].rate == 0.6 and new[4].base == 100


@pytest.mark.asyncio
async def test_openai_parse_stream_end_fallback():
    """流末兜底：无 finish_reason 也产出积攒的 tool_calls（新旧一致）。"""
    frames = ['data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c9","function":{"name":"g","arguments":"{}"}}]}}]}']
    old = await _collect(pilot_llm._openai_parse(_lines(frames)))
    new = await _collect(openai_parse(_lines(frames), None))
    assert _norm_old(old) == new
    assert new[0].calls[0].name == "g"


RESPONSES_FRAMES = [
    'data: {"type":"response.reasoning_summary_text.delta","delta":"思"}',
    'data: {"type":"response.output_text.delta","delta":"文"}',
    'data: {"type":"response.output_item.added","item":{"type":"function_call","item_id":"i1","call_id":"c1","name":"f","arguments":""}}',
    'data: {"type":"response.function_call_arguments.delta","item_id":"i1","delta":"{\\"x\\":1}"}',
    'data: {"type":"response.output_item.done","item":{"type":"function_call","item_id":"i1","call_id":"c1","name":"f","arguments":"{\\"x\\":1}"}}',
    'data: {"type":"response.completed","response":{"usage":{"input_tokens":50,"output_tokens":9,"input_tokens_details":{"cached_tokens":10}}}}',
]


@pytest.mark.asyncio
async def test_responses_parse_equivalence():
    old = await _collect(pilot_llm._responses_parse(_lines(RESPONSES_FRAMES)))
    new = await _collect(responses_parse(_lines(RESPONSES_FRAMES), None))
    assert _norm_old(old) == new
    assert new[2].calls == (ToolCall(id="c1", name="f", arguments={"x": 1}),)
    assert new[4].rate == 0.2 and new[4].read == 10


@pytest.mark.asyncio
async def test_responses_parse_stream_end_fallback():
    """流末兜底：不发 completed 也按序冲出积攒 call（新旧一致）。"""
    frames = RESPONSES_FRAMES[:5]
    old = await _collect(pilot_llm._responses_parse(_lines(frames)))
    new = await _collect(responses_parse(_lines(frames), None))
    assert _norm_old(old) == new


ANTHROPIC_FRAMES = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":80,"cache_read_input_tokens":20,"cache_creation_input_tokens":5}}}',
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"推"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"f"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"p\\":2}"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"文"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":6}}',
    'data: {"type":"message_stop"}',
]


@pytest.mark.asyncio
async def test_anthropic_parse_equivalence():
    old = await _collect(pilot_llm._anthropic_parse(_lines(ANTHROPIC_FRAMES)))
    new = await _collect(anthropic_parse(_lines(ANTHROPIC_FRAMES), None))
    assert _norm_old(old) == new
    assert new[0].base == 105 and new[0].rate == pytest.approx(20 / 105)
    assert new[2] == AnswerDelta(text="文")
    assert new[3].calls == (ToolCall(id="t1", name="f", arguments={"p": 2}),)
    assert new[-1] == Usage(input_tokens=80, output_tokens=6)


OLLAMA_FRAMES = [
    '{"message":{"thinking":"想"}}',
    '{"message":{"content":"答"}}',
    '{"message":{"tool_calls":[{"function":{"name":"f","arguments":{"a":1}}},{"function":{"name":"g","arguments":"{\\"b\\":2}"}}]}}',
    '{"done":true,"prompt_eval_count":11,"eval_count":3}',
]


@pytest.mark.asyncio
async def test_ollama_parse_equivalence():
    old = await _collect(pilot_llm._ollama_parse(_lines(OLLAMA_FRAMES)))
    new = await _collect(ollama_parse(_lines(OLLAMA_FRAMES), None))
    assert _norm_old(old) == new
    assert new[2].calls == (
        ToolCall(id="ol-f-0", name="f", arguments={"a": 1}),
        ToolCall(id="ol-g-1", name="g", arguments={"b": 2}),
    )


# ── 请求构造等价：旧 dict 消息 vs 新 Message → 同一 url/headers/body ────────

def _to_messages(dicts):
    msgs = []
    for m in dicts:
        tcs = tuple(
            ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=json.loads(tc["function"]["arguments"] or "{}"))
            for tc in m.get("tool_calls") or []
        )
        msgs.append(Message(role=m["role"], content=m.get("content"), tool_calls=tcs, tool_call_id=m.get("tool_call_id")))
    return tuple(msgs)


ASSISTANT_TOOL_MSG = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "f", "arguments": json.dumps({"a": 1}, ensure_ascii=False)}},
]}
TOOL_RESULT_MSG = {"role": "tool", "tool_call_id": "c1", "content": "res"}

TOOLS_ENV = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}]


def test_request_parity_openai():
    dicts = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, ASSISTANT_TOOL_MSG, TOOL_RESULT_MSG]
    old = pilot_llm._openai_request("https://x/v1", "sk", "m", dicts, TOOLS_ENV)
    p = OpenAiChatProvider()
    new = p.build_request("https://x/v1", _req(base_url="https://x/v1", api_key="sk",
                                               messages=_to_messages(dicts), tools=tuple(TOOLS_ENV)))
    assert old == new


def test_request_parity_openai_null_content_normalized():
    old = pilot_llm._openai_request("https://x/v1", "", "m", [ASSISTANT_TOOL_MSG], None)
    new = OpenAiChatProvider().build_request("https://x/v1", _req(api_key="", messages=_to_messages([ASSISTANT_TOOL_MSG])))
    assert old == new
    assert new[2]["messages"][0]["content"] == ""


def test_request_parity_responses():
    dicts = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, ASSISTANT_TOOL_MSG, TOOL_RESULT_MSG]
    old = pilot_llm._responses_request("https://x", "sk", "m", dicts, TOOLS_ENV)
    new = OpenAiResponsesProvider().build_request("https://x", _req(base_url="https://x", api_key="sk",
                                                                    messages=_to_messages(dicts), tools=tuple(TOOLS_ENV)))
    assert old == new


def test_request_parity_anthropic():
    dicts = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, ASSISTANT_TOOL_MSG, TOOL_RESULT_MSG]
    old = pilot_llm._anthropic_request("https://x/v1", "sk", "m", dicts, TOOLS_ENV)
    new = AnthropicProvider().build_request("https://x/v1", _req(base_url="https://x/v1", api_key="sk",
                                                                 messages=_to_messages(dicts), tools=tuple(TOOLS_ENV)))
    assert old == new
    assert new[2]["max_tokens"] == 2048 + 4096


def test_request_parity_ollama():
    dicts = [{"role": "user", "content": "u"}, ASSISTANT_TOOL_MSG]
    old = pilot_llm._ollama_request("http://127.0.0.1:11434", "", "m", dicts, TOOLS_ENV)
    new = OllamaProvider().build_request("http://127.0.0.1:11434", _req(base_url="http://127.0.0.1:11434",
                                                                        messages=_to_messages(dicts), tools=tuple(TOOLS_ENV)))
    assert old == new


def test_convert_and_auth_headers_parity():
    # 序列不出现相邻 user/tool：连续同角色合并是适配器有意新增的行为，
    # 单独在 test_tool_result_merges_into_user_turn 断言
    dicts = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
             {"role": "assistant", "content": "a"}, TOOL_RESULT_MSG]
    old_system, old_out = pilot_llm._anthropic_convert(dicts)
    new_system, new_out = _convert(_to_messages(dicts))
    assert (old_system, old_out) == (new_system, new_out)
    assert base.auth_headers("openai", "sk") == pilot_llm._auth_headers("openai", "sk")
    assert base.auth_headers("anthropic", "") == pilot_llm._auth_headers("anthropic", "")


def test_tool_result_merges_into_user_turn():
    """工具结果以 tool_result 块收进 user 轮（Anthropic 无 tool 角色）：并入相邻
    user 消息为单条多块，孤立时自成 user 轮。"""
    dicts = [{"role": "user", "content": "u"}, TOOL_RESULT_MSG, {"role": "user", "content": "v"}]
    _, out = _convert(_to_messages(dicts))
    assert out == [
        {"role": "user", "content": [
            {"type": "text", "text": "u"},
            {"type": "tool_result", "tool_use_id": "c1", "content": "res"},
            {"type": "text", "text": "v"},
        ]},
    ]


def test_list_models_url_parity():
    for protocol, raw in (("openai", "https://x/v1"), ("anthropic", "https://x"), ("ollama", "http://h:11434")):
        assert base.list_models_url(protocol, raw) == pilot_llm._list_models_url(protocol, raw)
    assert base.parse_model_list("openai", {"data": [{"id": "b"}, {"id": "a"}]}) == ["a", "b"]
    assert base.parse_model_list("ollama", {"models": [{"name": "x"}]}) == ["x"]


# ── 探测与注册表 ─────────────────────────────────────────────────────────

def test_match_host():
    assert registry.match_host("https://API.DEEPSEEK.com/v1") == ("openai", "DeepSeek", ["deepseek-chat", "deepseek-reasoner"])
    assert registry.match_host("http://localhost:11434") == ("ollama", "Ollama 本地", [])
    assert registry.match_host("https://unknown.example") is None
    assert registry.match_host("") is None


def test_unknown_protocol_rejected():
    with pytest.raises(ProviderError, match="未知协议"):
        registry.effective_base_url("nope", "")


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x/v1/models")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def _stub_registry_list(outcome):
    async def _fake(protocol, base_url, api_key):
        if outcome == "ok":
            return ["m1"]
        if isinstance(outcome, int):
            raise _status_error(outcome)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://x/v1/models"))
    return _fake


@pytest.mark.asyncio
async def test_detect_reason_classification(monkeypatch):
    monkeypatch.setattr(registry, "list_models", _stub_registry_list("ok"))
    r = await registry.detect_provider("https://x/v1", "sk")
    assert (r["key_valid"], r["reason"], r["models"]) == (True, None, ["m1"])
    monkeypatch.setattr(registry, "list_models", _stub_registry_list(403))
    r = await registry.detect_provider("https://x/v1", "bad")
    assert (r["key_valid"], r["reason"]) == (False, "key_invalid")
    monkeypatch.setattr(registry, "list_models", _stub_registry_list(404))
    r = await registry.detect_provider("https://x/v1", "sk")
    assert r["reason"] == "not_found"
    monkeypatch.setattr(registry, "list_models", _stub_registry_list(503))
    r = await registry.detect_provider("https://x/v1", "sk")
    assert r["reason"] == "upstream"
    monkeypatch.setattr(registry, "list_models", _stub_registry_list("net"))
    r = await registry.detect_provider("https://x/v1", "sk")
    assert (r["reason"], r["key_valid"]) == ("unreachable", None)


def test_effective_base_url_defaults():
    assert registry.effective_base_url("anthropic", "") == "https://api.anthropic.com"
    assert registry.effective_base_url("ollama", " ") == "http://127.0.0.1:11434"
    assert registry.effective_base_url("openai", " https://x/ ") == "https://x/"


def test_model_caps():
    caps = registry.model_caps("openai", "deepseek-reasoner")
    assert caps.supports_tools is True and caps.supports_thinking is True
    assert registry.model_caps("openai", "deepseek-chat").supports_thinking is None
    assert registry.model_caps("anthropic", "claude-x").supports_thinking is True
    assert registry.model_caps("openai", "m", context_window=128000).context_window == 128000


# ── 执行骨架：HTTP 错误收敛 / 取消 / 超时 ─────────────────────────────────

class _FakeResp:
    def __init__(self, status_code, lines, body=b"err"):
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, *args, **kwargs):
        return _FakeStreamCM(self._resp)


@pytest.mark.asyncio
async def test_http_error_carries_detail_and_retriable():
    with pytest.raises(ProviderError) as ei:
        async for _ in base._stream_response(_FakeClient(_FakeResp(400, [])), "u", {}, {}, None, None):
            pass
    assert ei.value.code.startswith("HTTP 400: ") and not ei.value.retriable
    with pytest.raises(ProviderError) as ei:
        async for _ in base._stream_response(_FakeClient(_FakeResp(503, [])), "u", {}, {}, None, None):
            pass
    assert ei.value.retriable


@pytest.mark.asyncio
async def test_cancel_between_frames():
    cancel = asyncio.Event()

    async def lines():
        yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
        cancel.set()
        yield 'data: {"choices":[{"delta":{"content":"b"}}]}'

    evs = []
    with pytest.raises(ProviderError) as ei:
        async for e in openai_parse(lines(), cancel):
            evs.append(e)
    assert evs == [AnswerDelta(text="a")]
    assert ei.value.code == "cancelled" and not ei.value.retriable
    with pytest.raises(ProviderError):
        async for _ in openai_parse(lines(), cancel):
            pass


def test_make_timeout():
    assert base.make_timeout(None) == base.DEFAULT_TIMEOUT
    t = base.make_timeout(5.0)
    assert t.read == 5.0 and t.connect == 15.0


# ── 注册表重试：仅 retriable 且首个事件前重试一次 ─────────────────────────

class _ScriptedProvider:
    name = "openai"
    default_base_url = ""

    def __init__(self, script):
        self._script = script
        self.calls = 0

    def effective_base_url(self, raw):
        return raw

    def build_request(self, base_url, req):
        return "http://stub", {}, {}

    async def stream(self, req):
        step = self._script[self.calls]
        self.calls += 1
        for item in step:
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.mark.asyncio
async def test_retry_once_before_first_event(monkeypatch):
    p = _ScriptedProvider([
        [ProviderError("connect fail", retriable=True)],
        [AnswerDelta(text="ok")],
    ])
    monkeypatch.setitem(registry.PROVIDERS, "openai", p)
    evs = await _collect(registry.stream_events("openai", _req()))
    assert p.calls == 2 and evs == [AnswerDelta(text="ok")]


@pytest.mark.asyncio
async def test_no_retry_mid_stream(monkeypatch):
    p = _ScriptedProvider([
        [AnswerDelta(text="a"), ProviderError("mid fail", retriable=True)],
    ])
    monkeypatch.setitem(registry.PROVIDERS, "openai", p)
    with pytest.raises(ProviderError):
        await _collect(registry.stream_events("openai", _req()))
    assert p.calls == 1


@pytest.mark.asyncio
async def test_no_retry_non_retriable(monkeypatch):
    p = _ScriptedProvider([
        [ProviderError("bad request", retriable=False)],
        [AnswerDelta(text="ok")],
    ])
    monkeypatch.setitem(registry.PROVIDERS, "openai", p)
    with pytest.raises(ProviderError):
        await _collect(registry.stream_events("openai", _req()))
    assert p.calls == 1
