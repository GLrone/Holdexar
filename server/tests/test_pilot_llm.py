"""pilot LLM 适配层单测：协议请求构造、鉴权头与探测失败分类（不触网）。"""
import json
import logging

import httpx
import pytest

from app.domains.pilot import llm as pilot_llm
from app.domains.pilot import router as pilot_router
from app.domains.pilot.schemas import DetectRequest


@pytest.mark.asyncio
async def test_chat_stream_logs_http_provider_failure(caplog):
    async def handler(request):
        return httpx.Response(
            503, request=request,
            json={"error": {"message": "Authorization: Bearer sk-test-secret"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level(logging.ERROR, logger=pilot_llm.__name__):
            with pytest.raises(pilot_llm.PilotLlmError, match="HTTP 503") as raised:
                async for _ in pilot_llm.chat_stream(
                    protocol="openai", base_url="https://provider.test/v1",
                    api_key="sk-test-secret", model="test-model",
                    messages=[{"role": "user", "content": "hello"}], client=client,
                ):
                    pass

    failures = [record for record in caplog.records
                if record.name == pilot_llm.__name__ and record.levelno == logging.ERROR]
    assert len(failures) == 1
    assert failures[0].hl_detail["HTTP状态"] == 503
    assert failures[0].hl_detail["诊断编号"]
    assert "sk-test-secret" not in failures[0].hl_detail["原因"]
    assert "sk-test-secret" not in raised.value.to_diagnostic()["message"]


@pytest.mark.asyncio
async def test_usage_on_every_frame_still_yields_answer():
    """部分中转把 usage 挂在每一帧上（与正文同帧下发）。

    解析器若见 usage 就 continue，正文会随这些帧一起被丢掉——整轮只剩用量事件、
    答案全空。只有「无任何 choices 的纯用量帧」才可以到此为止。
    """
    def _frame(content, usage):
        chunk = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
        if usage is not None:
            chunk["usage"] = usage
        return f"data: {json.dumps(chunk)}\n\n"

    body = (
        _frame("你", {"prompt_tokens": 10, "completion_tokens": 1})
        + _frame("好", {"prompt_tokens": 10, "completion_tokens": 2})
        + _frame(None, {"prompt_tokens": 10, "completion_tokens": 2})  # 纯用量帧
    )

    async def handler(request):
        return httpx.Response(200, request=request, text=body,
                              headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = ""
        async for kind, delta in pilot_llm.chat_stream(
            protocol="openai", base_url="https://provider.test/v1",
            api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], client=client,
        ):
            if kind == "answer":
                answer += delta
    assert answer == "你好"


def test_anonymous_endpoints_send_no_auth_header():
    """免费匿名端点 / 本地网关无密钥：空值鉴权头会被 httpx 拒绝。"""
    assert pilot_llm._auth_headers("openai", "") == {}
    assert pilot_llm._auth_headers("ollama", "") == {}
    assert pilot_llm._auth_headers("ollama", "unused") == {}
    assert pilot_llm._auth_headers("anthropic", "") == {"anthropic-version": "2023-06-01"}


def test_keyed_endpoints_send_auth_header():
    assert pilot_llm._auth_headers("openai", "sk-x") == {"Authorization": "Bearer sk-x"}
    assert pilot_llm._auth_headers("anthropic", "sk-x") == {
        "x-api-key": "sk-x",
        "anthropic-version": "2023-06-01",
    }


def test_request_builders_use_auth_headers():
    url, headers, _ = pilot_llm._openai_request("https://x/v1", "", "m", [], None)
    assert url == "https://x/v1/chat/completions"
    assert "Authorization" not in headers
    _, headers, _ = pilot_llm._openai_request("https://x/v1", "sk-x", "m", [], None)
    assert headers["Authorization"] == "Bearer sk-x"
    _, headers, _ = pilot_llm._anthropic_request("https://x", "sk-x", "m", [], None)
    assert headers["x-api-key"] == "sk-x"


def test_list_models_url_per_protocol():
    assert pilot_llm._list_models_url("openai", "https://x/v1") == "https://x/v1/models"
    assert pilot_llm._list_models_url("anthropic", "https://x") == "https://x/v1/models"
    assert pilot_llm._list_models_url("ollama", "http://127.0.0.1:11434") == "http://127.0.0.1:11434/api/tags"


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x/v1/models")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


async def _stub_list_models(outcome):
    async def _fake(protocol, base_url, api_key):
        if outcome == "ok":
            return ["m1", "m2"]
        if isinstance(outcome, int):
            raise _status_error(outcome)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://x/v1/models"))
    return _fake


@pytest.mark.asyncio
async def test_detect_reason_success(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models("ok"))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["key_valid"] is True
    assert r["reason"] is None
    assert r["models"] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_detect_reason_key_invalid(monkeypatch):
    for code in (401, 403):
        monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(code))
        r = await pilot_llm.detect_provider("https://x/v1", "bad")
        assert r["key_valid"] is False
        assert r["reason"] == "key_invalid"


@pytest.mark.asyncio
async def test_detect_reason_not_found(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(404))
    r = await pilot_llm.detect_provider("https://x", "sk-x")
    assert r["key_valid"] is None
    assert r["reason"] == "not_found"


@pytest.mark.asyncio
async def test_detect_reason_upstream_and_unreachable(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(503))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["reason"] == "upstream"
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models("net"))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["reason"] == "unreachable"
    assert r["key_valid"] is None


@pytest.mark.asyncio
async def test_detect_uses_provider_key_not_active(monkeypatch):
    """编辑态识别：带 provider_id 用该供应商密钥；无 provider_id 才回落活跃配置。"""
    captured: dict = {}

    async def fake_provider_key(pid: str) -> str:
        return {"p-1": "sk-p1"}.get(pid, "")

    async def fake_load_config() -> dict:
        return {"api_key": "sk-active"}

    async def fake_detect(base_url: str, api_key: str, protocol: str | None) -> dict:
        captured["key"] = api_key
        return {"protocol": "openai", "vendor": "", "models": ["m"], "suggested": [],
                "key_valid": True, "reason": None}

    monkeypatch.setattr(pilot_router.pilot_config, "provider_key", fake_provider_key)
    monkeypatch.setattr(pilot_router.pilot_config, "load_config", fake_load_config)
    monkeypatch.setattr(pilot_router.pilot_llm, "detect_provider", fake_detect)

    r = await pilot_router.detect(DetectRequest(base_url="https://x/v1", provider_id="p-1"))
    assert captured["key"] == "sk-p1"
    await pilot_router.detect(DetectRequest(base_url="https://x/v1"))
    assert captured["key"] == "sk-active"
    # 供应商无密钥（匿名网关）或 id 不存在：保持匿名，不跨家回落活跃密钥
    await pilot_router.detect(DetectRequest(base_url="https://x/v1", provider_id="ghost"))
    assert captured["key"] == ""
    assert r.models == ["m"]
    assert r.reason is None


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

RESPONSES_FRAMES = [
    'data: {"type":"response.reasoning_summary_text.delta","delta":"思"}',
    'data: {"type":"response.output_text.delta","delta":"文"}',
    'data: {"type":"response.output_item.added","item":{"type":"function_call","item_id":"i1","call_id":"c1","name":"f","arguments":""}}',
    'data: {"type":"response.function_call_arguments.delta","item_id":"i1","delta":"{\\"x\\":1}"}',
    'data: {"type":"response.output_item.done","item":{"type":"function_call","item_id":"i1","call_id":"c1","name":"f","arguments":"{\\"x\\":1}"}}',
    'data: {"type":"response.completed","response":{"usage":{"input_tokens":50,"output_tokens":9,"input_tokens_details":{"cached_tokens":10}}}}',
]

ANTHROPIC_FRAMES = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":80,"cache_read_input_tokens":20,"cache_creation_input_tokens":5}}}',
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"推"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"f"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"p\\":2}"}}',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"文"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":6}}',
    'data: {"type":"message_stop"}',
]

OLLAMA_FRAMES = [
    '{"message":{"thinking":"想"}}',
    '{"message":{"content":"答"}}',
    '{"message":{"tool_calls":[{"function":{"name":"f","arguments":{"a":1}}},{"function":{"name":"g","arguments":"{\\"b\\":2}"}}]}}',
    '{"done":true,"prompt_eval_count":11,"eval_count":3}',
]


@pytest.mark.parametrize("protocol,frames,calls,usage,cache", [
    ("openai", OPENAI_FRAMES, [{"id": "c1", "name": "f", "arguments": {"a": 1}}], (100, 7), 0.6),
    ("responses", RESPONSES_FRAMES, [{"id": "c1", "name": "f", "arguments": {"x": 1}}], (50, 9), 0.2),
    ("anthropic", ANTHROPIC_FRAMES, [{"id": "t1", "name": "f", "arguments": {"p": 2}}], (80, 6), 20 / 105),
    ("ollama", OLLAMA_FRAMES, [
        {"id": "ol-f-0", "name": "f", "arguments": {"a": 1}},
        {"id": "ol-g-1", "name": "g", "arguments": {"b": 2}},
    ], (11, 3), None),
])
@pytest.mark.asyncio
async def test_protocol_stream_preserves_answer_tools_and_usage(protocol, frames, calls, usage, cache):
    async def lines():
        for frame in frames:
            yield frame

    events = [event async for event in getattr(pilot_llm, f"_{protocol}_parse")(lines())]
    values = dict(events)
    assert values["answer"] in ("答", "文")
    assert values["thinking"] in ("想", "思", "推")
    assert values["tool_calls"] == calls
    assert values["usage"] == usage
    assert values["context"] == (105 if protocol == "anthropic" else usage[0])
    if cache is not None:
        assert values["cache"]["rate"] == pytest.approx(cache)


@pytest.mark.parametrize("protocol,frames,expected", [
    ("openai", ['data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c9","function":{"name":"g","arguments":"{}"}}]}}]}'],
     {"id": "c9", "name": "g", "arguments": {}}),
    ("responses", RESPONSES_FRAMES[:5], {"id": "c1", "name": "f", "arguments": {"x": 1}}),
])
@pytest.mark.asyncio
async def test_protocol_stream_flushes_tools_without_finish_frame(protocol, frames, expected):
    async def lines():
        for frame in frames:
            yield frame

    events = [event async for event in getattr(pilot_llm, f"_{protocol}_parse")(lines())]
    assert dict(events)["tool_calls"] == [expected]


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic", "ollama"])
def test_protocol_requests_preserve_tool_calls_and_results(protocol):
    messages = [
        {"role": "system", "content": "s"}, {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "res"},
    ]
    tools = [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"type": "object"}}}]
    _, _, body = getattr(pilot_llm, f"_{protocol}_request")("https://x/v1", "sk", "m", messages, tools)
    assert body["stream"] is True
    if protocol == "responses":
        assert body["input"][-2:] == [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
            {"type": "function_call_output", "call_id": "c1", "output": "res"},
        ]
        assert body["instructions"] == "s"
    elif protocol == "anthropic":
        assert body["messages"][-2]["content"] == [{"type": "tool_use", "id": "c1", "name": "f", "input": {"a": 1}}]
        assert body["messages"][-1]["content"] == [{"type": "tool_result", "tool_use_id": "c1", "content": "res"}]
        assert body["max_tokens"] == 6144
    else:
        assert body["messages"][-2]["content"] == ""
        assert body["messages"][-1]["content"] == "res"
        expected = {"name": "f", "arguments": {"a": 1}} if protocol == "ollama" else {"name": "f", "arguments": '{"a":1}'}
        assert body["messages"][-2]["tool_calls"][0]["function"] == expected
