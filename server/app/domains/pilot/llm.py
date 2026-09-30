"""pilot LLM 客户端：多协议适配层。

四种协议各自适配请求构造、鉴权头、流式解析与工具调用格式，归一化为
统一事件流供 agent 循环消费：
  ("thinking", 增量)            推理通道（协议/模型支持时才有）；
  ("answer", 增量)              正文通道；
  ("tool_calls", [assembled])   工具调用（arguments 已解析为 dict）；
  ("usage", (输入, 输出))        用量（provider 回传时才有）。

协议：
- openai     OpenAI 兼容 /chat/completions（DeepSeek / 智谱 / 通义 / Kimi 等）
- anthropic  Messages API（thinking 块、tool_use/tool_result）
- gemini     Google generateContent（thought 部件、functionCall/functionResponse）
- ollama     本地 Ollama /api/chat（ndjson，thinking 字段）

网络/协议异常统一收敛为 PilotLlmError，由服务层转用户语言提示。"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 60.0
_MAX_OUTPUT_TOKENS = 700
_ANTHROPIC_THINKING_BUDGET = 1024

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

PROTOCOLS = ("openai", "anthropic", "gemini", "ollama")

DEFAULT_BASE_URL = {
    "openai": "",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://127.0.0.1:11434",
}


class PilotLlmError(RuntimeError):
    """LLM 调用失败（网络 / 非 2xx / 响应形态不符）。"""


def _is_loopback(base_url: str) -> bool:
    return urlsplit(base_url).hostname in _LOOPBACK_HOSTS


def _client(base_url: str) -> httpx.AsyncClient:
    """回环地址（自建/本地网关）绕过系统代理——httpx trust_env 会把环回
    请求也交给代理，代理对回环返回 502；外部地址照常跟随环境代理。"""
    return httpx.AsyncClient(timeout=_TIMEOUT_S, trust_env=not _is_loopback(base_url))


def _assembled(pending: dict[int, dict]) -> list[dict]:
    out = []
    for idx in sorted(pending):
        item = pending[idx]
        if not item["name"]:
            continue
        try:
            args = json.loads(item["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append({"id": item["id"], "name": item["name"], "arguments": args})
    return out


def _sse_data_lines(aiter_lines):
    """SSE 帧的 data 行迭代（跳过空行 / event 行 / [DONE]）。"""
    async def _gen():
        async for line in aiter_lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data and data != "[DONE]":
                yield data
    return _gen()


# ── openai（兼容 DeepSeek / 智谱 / 通义 / Kimi 等）────────────────────

def _openai_request(base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None) -> tuple[str, dict, dict]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    return url, {"Authorization": f"Bearer {api_key}"}, body


async def _openai_parse(aiter_lines):
    pending: dict[int, dict] = {}
    async for data in _sse_data_lines(aiter_lines):
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if usage:
            yield ("usage", (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)))
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        think = delta.get("reasoning_content")
        if think:
            yield ("thinking", str(think))
        content = delta.get("content")
        if content:
            yield ("answer", str(content))
        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index") or 0)
            acc = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                acc["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc["name"] = fn["name"]
            if fn.get("arguments"):
                acc["args"] += str(fn["arguments"])
        if choice.get("finish_reason") == "tool_calls" or chunk.get("finish_reason") == "tool_calls":
            # finish_reason 标准位置在 choice 内；部分实现放 chunk 顶层，一并兼容
            assembled = _assembled(pending)
            pending = {}
            if assembled:
                yield ("tool_calls", assembled)
    if pending:
        # 流末兜底：部分实现不发 finish_reason
        assembled = _assembled(pending)
        if assembled:
            yield ("tool_calls", assembled)


# ── anthropic（Messages API）────────────────────────────────────────

def _anthropic_convert(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """内部消息 → Anthropic 形态：system 提顶、工具结果转 tool_result 块。"""
    system = "\n".join(m["content"] for m in messages if m["role"] == "system") or None
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m["content"]}]})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"] or "{}"),
                })
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") or "",
            }]})
    return system, out


def _anthropic_request(base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None) -> tuple[str, dict, dict]:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = base + "/v1/messages"
    system, converted = _anthropic_convert(messages)
    body: dict = {
        "model": model,
        "max_tokens": _ANTHROPIC_THINKING_BUDGET + _MAX_OUTPUT_TOKENS,
        "messages": converted,
        "stream": True,
        # 扩展思维链：预算内先思考后作答（thinking 时 temperature 必须缺省）
        "thinking": {"type": "enabled", "budget_tokens": _ANTHROPIC_THINKING_BUDGET},
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters", {"type": "object"})}
            for t in tools
        ]
    return url, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, body


async def _anthropic_parse(aiter_lines):
    tools_acc: dict[int, dict] = {}
    usage = [0, 0]
    async for data in _sse_data_lines(aiter_lines):
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            usage[0] = int(u.get("input_tokens") or 0)
        elif etype == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "thinking_delta" and d.get("thinking"):
                yield ("thinking", str(d["thinking"]))
            elif dt == "text_delta" and d.get("text"):
                yield ("answer", str(d["text"]))
            elif dt == "input_json_delta":
                acc = tools_acc.setdefault(int(ev.get("index") or 0), {"id": "", "name": "", "args": ""})
                acc["args"] += str(d.get("partial_json") or "")
        elif etype == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                acc = tools_acc.setdefault(int(ev.get("index") or 0), {"id": "", "name": "", "args": ""})
                acc["id"] = block.get("id") or ""
                acc["name"] = block.get("name") or ""
        elif etype == "message_delta":
            d = ev.get("delta") or {}
            u = ev.get("usage") or {}
            usage[1] += int(u.get("output_tokens") or 0)
            if d.get("stop_reason") == "tool_use":
                assembled = _assembled(tools_acc)
                tools_acc = {}
                if assembled:
                    yield ("tool_calls", assembled)
        elif etype == "message_stop":
            break
    yield ("usage", (usage[0], usage[1]))


# ── gemini（Google generateContent）───────────────────────────────────

def _gemini_convert(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system = "\n".join(m["content"] for m in messages if m["role"] == "system") or None
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        grole = "model" if role == "assistant" else "user"
        parts: list[dict] = []
        if role == "assistant":
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in m.get("tool_calls") or []:
                parts.append({"functionCall": {"name": tc["function"]["name"],
                                               "args": json.loads(tc["function"]["arguments"] or "{}")}})
        elif role == "tool":
            parts.append({"functionResponse": {
                "name": m.get("name") or m.get("tool_call_id") or "tool",
                "response": {"result": m.get("content") or ""},
            }})
        else:
            parts.append({"text": m["content"]})
        out.append({"role": grole, "parts": parts})
    return system, out


def _gemini_request(base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None) -> tuple[str, dict, dict]:
    base = base_url.rstrip("/")
    url = base + f"/v1beta/models/{model}:streamGenerateContent?alt=sse"
    system, contents = _gemini_convert(messages)
    body: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": _MAX_OUTPUT_TOKENS, "temperature": 0.4},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"function_declarations": [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "parameters": t["function"].get("parameters", {"type": "object"})}
            for t in tools
        ]}]
    return url, {"x-goog-api-key": api_key}, body


async def _gemini_parse(aiter_lines):
    async for data in _sse_data_lines(aiter_lines):
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        meta = ev.get("usageMetadata") or {}
        calls: list[dict] = []
        for cand in ev.get("candidates") or []:
            parts = ((cand.get("content") or {}).get("parts")) or []
            for p in parts:
                if p.get("thought") and p.get("text"):
                    yield ("thinking", str(p["text"]))
                elif p.get("functionCall"):
                    calls.append({"id": f"gc-{p['functionCall'].get('name')}-{len(calls)}",
                                  "name": p["functionCall"].get("name") or "",
                                  "arguments": p["functionCall"].get("args") or {}})
                elif p.get("text"):
                    yield ("answer", str(p["text"]))
        if calls:
            yield ("tool_calls", calls)
        if meta:
            yield ("usage", (int(meta.get("promptTokenCount") or 0), int(meta.get("candidatesTokenCount") or 0)))


# ── ollama（本地 /api/chat，ndjson）───────────────────────────────────

def _ollama_request(base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None) -> tuple[str, dict, dict]:
    url = base_url.rstrip("/") + "/api/chat"
    converted = []
    for m in messages:
        item = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            item["tool_calls"] = [{"function": {
                "name": tc["function"]["name"],
                "arguments": json.loads(tc["function"]["arguments"] or "{}"),
            }} for tc in m["tool_calls"]]
        converted.append(item)
    body: dict = {"model": model, "messages": converted, "stream": True}
    if tools:
        body["tools"] = tools
    return url, {}, body


async def _ollama_parse(aiter_lines):
    async for line in aiter_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        think = msg.get("thinking")
        if think:
            yield ("thinking", str(think))
        content = msg.get("content")
        if content:
            yield ("answer", str(content))
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if not fn.get("name"):
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": f"ol-{fn['name']}-{len(calls)}", "name": fn["name"], "arguments": args or {}})
        if calls:
            yield ("tool_calls", calls)
        if ev.get("done"):
            yield ("usage", (int(ev.get("prompt_eval_count") or 0), int(ev.get("eval_count") or 0)))


# ── 分发 ─────────────────────────────────────────────────────────────

_PARSE = {"openai": _openai_parse, "anthropic": _anthropic_parse, "gemini": _gemini_parse, "ollama": _ollama_parse}
_REQUEST = {"openai": _openai_request, "anthropic": _anthropic_request, "gemini": _gemini_request, "ollama": _ollama_request}


def effective_base_url(protocol: str, base_url: str) -> str:
    """协议默认地址兜底：用户未填 base_url 时用协议官方入口。"""
    return (base_url or "").strip() or DEFAULT_BASE_URL.get(protocol, "")


async def chat_stream(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
):
    """按协议构造请求并流式解析，产出统一事件（见模块 docstring）。"""
    if protocol not in _PARSE:
        raise PilotLlmError(f"未知协议: {protocol}")
    url, headers, body = _REQUEST[protocol](base_url, api_key, model, messages, tools)
    try:
        async with _client(base_url) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for event in _PARSE[protocol](resp.aiter_lines()):
                    yield event
    except (httpx.HTTPError, TypeError, ValueError) as e:
        logger.warning("pilot LLM 流式调用失败（%s）: %r", protocol, e)
        raise PilotLlmError(str(e)) from e
