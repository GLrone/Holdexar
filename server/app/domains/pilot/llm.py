"""pilot LLM 客户端：多协议适配层。

三种协议各自适配请求构造、鉴权头、流式解析与工具调用格式，归一化为
统一事件流供 agent 循环消费：
  ("thinking", 增量)            推理通道（协议/模型支持时才有）；
  ("answer", 增量)              正文通道；
  ("tool_calls", [assembled])   工具调用（arguments 已解析为 dict）；
  ("usage", (输入, 输出))        用量（provider 回传时才有）；
  ("cache", {"rate","read","write","base"})
                                 输入缓存计量：rate=命中率(0-1)，read/write=缓存读/写
                                 token 数，base=计费输入总量（含缓存部分）；provider
                                 未报告缓存时 base 仍给出（计费口径的分母）。

协议：
- openai            OpenAI 兼容 /chat/completions
- openai-responses  OpenAI Responses API /responses（instructions/input 工具即 function_call 项）
- anthropic         Messages API（thinking 块、tool_use/tool_result）
- ollama            本地 Ollama /api/chat（ndjson，thinking 字段）

网络/协议异常统一收敛为 PilotLlmError，由服务层转用户语言提示。"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx

from app.core.logging import log_event

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(180.0, connect=15.0, read=180.0, write=30.0)
_MAX_OUTPUT_TOKENS = 4096
_ANTHROPIC_THINKING_BUDGET = 2048

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

PROTOCOLS = ("openai", "openai-responses", "anthropic", "ollama")

DEFAULT_BASE_URL = {
    "openai": "",
    "openai-responses": "",
    "anthropic": "https://api.anthropic.com",
    "ollama": "http://127.0.0.1:11434",
}


class PilotLlmError(RuntimeError):
    """LLM 调用失败（网络 / 非 2xx / 响应形态不符）。"""


def _is_loopback(base_url: str) -> bool:
    return urlsplit(base_url).hostname in _LOOPBACK_HOSTS


def http_client(base_url: str) -> httpx.AsyncClient:
    """回环地址（自建/本地网关）绕过系统代理——httpx trust_env 会把环回
    请求也交给代理，代理对回环返回 502；外部地址照常跟随环境代理。"""
    return httpx.AsyncClient(timeout=_TIMEOUT, trust_env=not _is_loopback(base_url))


def _auth_headers(protocol: str, api_key: str) -> dict:
    """匿名端点（免费网关 / 本地 Ollama）不落鉴权头：空值头会被 httpx 拒绝。"""
    if protocol == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    if protocol == "ollama" or not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


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
    # 兼容层收敛：部分网关（unisound 等）拒绝 assistant content=null（纯 tool_calls
    # 轮的标准形态），统一规范为空串——对标准 OpenAI 同样合法。
    normalized = [
        {**m, "content": ""} if m.get("role") == "assistant" and m.get("content") is None else m
        for m in messages
    ]
    body = {
        "model": model,
        "messages": normalized,
        "temperature": 0.4,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    return url, _auth_headers("openai", api_key), body


async def _openai_parse(aiter_lines):
    pending: dict[int, dict] = {}
    async for data in _sse_data_lines(aiter_lines):
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            yield ("usage", (prompt, int(usage.get("completion_tokens") or 0)))
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            read = int(cached) if isinstance(cached, (int, float)) else None
            yield ("cache", {"rate": (min(1.0, read / prompt) if read is not None and prompt > 0 else None),
                             "read": read, "write": None, "base": prompt or None})
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


# ── openai-responses（Responses API）─────────────────────────────────

def _responses_input(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """内部消息 → Responses input 项：system 提为 instructions，
    工具调用/结果转 function_call / function_call_output 项。"""
    system_parts = [m["content"] for m in messages if m["role"] == "system" and m.get("content")]
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            out.append({"role": "user", "content": [{"type": "input_text", "text": m.get("content") or ""}]})
        elif role == "assistant":
            if m.get("content"):
                out.append({"role": "assistant", "content": [{"type": "output_text", "text": m["content"]}]})
            for tc in m.get("tool_calls") or []:
                out.append({
                    "type": "function_call",
                    "call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"] or "{}",
                })
        elif role == "tool":
            out.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or "",
                "output": m.get("content") or "",
            })
    instructions = "\n".join(system_parts) or None
    return instructions, out


def _responses_request(base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None) -> tuple[str, dict, dict]:
    url = base_url.rstrip("/") + "/responses"
    instructions, items = _responses_input(messages)
    body: dict = {
        "model": model,
        "input": items,
        "stream": True,
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
        "temperature": 0.4,
    }
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "parameters": t["function"].get("parameters", {"type": "object"}),
            }
            for t in tools
        ]
    return url, {"Authorization": f"Bearer {api_key}"}, body


def _flush_pending(pending: dict[str, dict], order: list[str]) -> list[list[dict]]:
    """按出现顺序组装积攒的 function_call 项并清空积攒（坏 JSON 落空参）。"""
    if not order:
        return []
    calls = []
    for item_id in order:
        acc = pending.pop(item_id, None)
        if not acc or not acc["name"]:
            continue
        try:
            args = json.loads(acc["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": acc["id"], "name": acc["name"], "arguments": args})
    order.clear()
    return [calls] if calls else []


async def _responses_parse(aiter_lines):
    """Responses SSE：output_text.delta=正文、reasoning_summary_text.delta=思考、
    function_call 项按 item 组装（added 起头、arguments.delta 追加、done 成call）。"""
    pending: dict[str, dict] = {}
    order: list[str] = []
    async for data in _sse_data_lines(aiter_lines):
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "response.output_text.delta":
            if ev.get("delta"):
                yield ("answer", str(ev["delta"]))
        elif etype == "response.reasoning_summary_text.delta":
            if ev.get("delta"):
                yield ("thinking", str(ev["delta"]))
        elif etype == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("item_id"):
                item_id = str(item["item_id"])
                order.append(item_id)
                pending[item_id] = {
                    "id": item.get("call_id") or item_id,
                    "name": item.get("name") or "",
                    "args": item.get("arguments") or "",
                }
        elif etype == "response.function_call_arguments.delta":
            item_id = str(ev.get("item_id") or "")
            if item_id in pending and ev.get("delta"):
                pending[item_id]["args"] += str(ev["delta"])
        elif etype == "response.output_item.done":
            item = ev.get("item") or {}
            item_id = str(ev.get("item_id") or item.get("id") or "")
            if item.get("type") == "function_call" and item_id in pending:
                acc = pending[item_id]
                acc["name"] = item.get("name") or acc["name"]
                acc["id"] = item.get("call_id") or acc["id"]
                if item.get("arguments"):
                    acc["args"] = item["arguments"]
        elif etype == "response.completed":
            for calls in _flush_pending(pending, order):
                yield ("tool_calls", calls)
            usage = (ev.get("response") or {}).get("usage") or {}
            inp = int(usage.get("input_tokens") or 0)
            yield ("usage", (inp, int(usage.get("output_tokens") or 0)))
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            read = int(cached) if isinstance(cached, (int, float)) else None
            yield ("cache", {"rate": (min(1.0, read / inp) if read is not None and inp > 0 else None),
                             "read": read, "write": None, "base": inp or None})
    for calls in _flush_pending(pending, order):
        # 流末兜底：部分兼容服务不发 completed
        yield ("tool_calls", calls)


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
    return url, _auth_headers("anthropic", api_key), body


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
            cache_read = u.get("cache_read_input_tokens")
            cache_write = u.get("cache_creation_input_tokens")
            read = int(cache_read) if isinstance(cache_read, (int, float)) else 0
            write = int(cache_write) if isinstance(cache_write, (int, float)) else 0
            # 计费输入 = 未缓存输入 + 缓存读 + 缓存写（Anthropic 三桶分开计费）；
            # 命中率分母含缓存读自身，避免部分命中被高估到 100%
            input_side = usage[0] + read + write
            yield ("cache", {"rate": (min(1.0, read / input_side) if input_side > 0 else None),
                             "read": read or None, "write": write or None, "base": input_side or None})
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
    return url, _auth_headers("ollama", api_key), body


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

_PARSE = {"openai": _openai_parse, "openai-responses": _responses_parse, "anthropic": _anthropic_parse, "ollama": _ollama_parse}
_REQUEST = {"openai": _openai_request, "openai-responses": _responses_request, "anthropic": _anthropic_request, "ollama": _ollama_request}


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
    max_tokens: int | None = None,
    client: httpx.AsyncClient | None = None,
):
    """按协议构造请求并流式解析，产出统一事件（见模块 docstring）。

    client 传入时由调用方持有并关闭（agent 循环多轮共用一个连接池，免每轮
    重新握手）；缺省自建一次性客户端。"""
    if protocol not in _PARSE:
        raise PilotLlmError(f"未知协议: {protocol}")
    url, headers, body = _REQUEST[protocol](base_url, api_key, model, messages, tools)
    if max_tokens:
        if protocol == "openai-responses":
            body["max_output_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens

    async def _run(own: httpx.AsyncClient):
        async with own.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                # 响应体截断进异常：网关用 4xx 报真实原因（参数缺失/超窗），
                # 日志与溢出回收（looks_like_overflow）都依赖这段文本
                detail = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                raise PilotLlmError(f"HTTP {resp.status_code}: {detail}")
            async for event in _PARSE[protocol](resp.aiter_lines()):
                yield event

    try:
        if client is not None:
            async for event in _run(client):
                yield event
        else:
            async with http_client(base_url) as own:
                async for event in _run(own):
                    yield event
    except (httpx.HTTPError, TypeError, ValueError) as e:
        log_event(
            logger,
            f"模型来源「{protocol}」的流式调用失败",
            level=logging.WARNING,
            detail={"模型来源": protocol, "原因": repr(e)},
        )
        # 超时异常 str 为空，回落类名，避免上层拿到空白诊断
        raise PilotLlmError(str(e) or type(e).__name__) from e


# ── 服务商智能识别：网址 + 密钥 → 协议 / 服务商 / 可用模型清单 ──────────
# host 指纹表给协议与服务商默认值（对齐「目录服务商免配置」的思路）；
# 真实模型清单以端点探测为准（GET 各协议模型列表），比静态目录新鲜。

_KNOWN_HOSTS: list[tuple[str, str, str, list[str]]] = [
    ("api.deepseek.com", "openai", "DeepSeek", ["deepseek-chat", "deepseek-reasoner"]),
    ("api.moonshot.cn", "openai", "Kimi", []),
    ("api.moonshot.ai", "openai", "Kimi", []),
    ("dashscope.aliyuncs.com", "openai", "通义千问", []),
    ("open.bigmodel.cn", "openai", "智谱 GLM", []),
    ("api.siliconflow.cn", "openai", "硅基流动", []),
    ("openrouter.ai", "openai", "OpenRouter", []),
    ("api.groq.com", "openai", "Groq", []),
    ("api.x.ai", "openai", "Grok", []),
    ("api.openai.com", "openai", "OpenAI", []),
    ("api.anthropic.com", "anthropic", "Anthropic", []),
    (":11434", "ollama", "Ollama 本地", []),
]


def match_host(base_url: str) -> tuple[str, str, list[str]] | None:
    """host 指纹 → (协议, 服务商, 推荐模型)；未命中返回 None。"""
    raw = (base_url or "").lower()
    if not raw:
        return None
    for host_key, protocol, vendor, models in _KNOWN_HOSTS:
        if host_key in raw:
            return protocol, vendor, models
    return None


def _parse_model_list(protocol: str, payload: dict) -> list[str]:
    if protocol in ("openai", "openai-responses", "anthropic"):
        return sorted(str(m["id"]) for m in payload.get("data") or [] if m.get("id"))
    if protocol == "ollama":
        return sorted(str(m["name"]) for m in payload.get("models") or [] if m.get("name"))
    return []


def _list_models_url(protocol: str, base: str) -> str:
    base = base.rstrip("/")
    if protocol == "anthropic":
        return base + "/v1/models"
    if protocol == "ollama":
        return base + "/api/tags"
    return base + "/models"


async def list_models(protocol: str, base_url: str, api_key: str) -> list[str]:
    """探测该账号可用的模型清单（短超时 GET；异常上抛由调用方处置）。"""
    async with http_client(base_url) as client:
        resp = await client.get(
            _list_models_url(protocol, base_url),
            headers=_auth_headers(protocol, api_key),
        )
        resp.raise_for_status()
        return _parse_model_list(protocol, resp.json())


async def detect_provider(base_url: str, api_key: str, protocol_hint: str | None = None) -> dict:
    """网址 + 密钥 → 协议 / 服务商 / 可用模型清单 / 密钥有效性 / 失败原因。

    协议判定：显式提示 > host 指纹 > 缺省 openai（第三方网关多为兼容形态）。
    模型清单以端点探测为准，探测失败回退 host 表推荐值；密钥有效性只在
    探测拿到明确 401/403 时判 False，reason 分类：key_invalid（401/403）/
    not_found（404，地址与协议不配对）/ upstream（其余 HTTP 错误）/
    unreachable（网络不通），成功为 None。"""
    hit = match_host(base_url)
    protocol = protocol_hint or (hit[0] if hit else "openai")
    vendor = hit[1] if hit else ""
    suggested = hit[2] if hit else []
    base = effective_base_url(protocol, base_url)

    key_valid: bool | None = None
    models = suggested
    reason: str | None = None
    try:
        models = await list_models(protocol, base, api_key)
        key_valid = True
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            key_valid = False
            reason = "key_invalid"
        elif code == 404:
            reason = "not_found"
        else:
            reason = "upstream"
    except httpx.HTTPError:
        key_valid = None
        reason = "unreachable"

    return {
        "protocol": protocol,
        "vendor": vendor,
        "models": models,
        "suggested": suggested,
        "key_valid": key_valid,
        "reason": reason,
    }
