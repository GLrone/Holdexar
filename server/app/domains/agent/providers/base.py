"""Provider 契约与显式事件类型。

LlmEvent 流取代 pilot/llm.py 的 (kind, payload) 元组协议；ProviderError
以异常形态上抛（不入流），携带 retriable 供注册表单次重试判定。网络/
协议异常统一收敛为 ProviderError，由上层转用户语言提示。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol
from urllib.parse import urlsplit

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(180.0, connect=15.0, read=180.0, write=30.0)
MAX_OUTPUT_TOKENS = 4096
ANTHROPIC_THINKING_BUDGET = 2048

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ProviderError(Exception):
    """LLM 调用失败（网络 / 非 2xx / 响应形态不符 / 取消）。"""

    def __init__(self, code: str, *, retriable: bool = False):
        super().__init__(code)
        self.code = code
        self.retriable = retriable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class LlmRequest:
    model: str
    messages: tuple[Message, ...]
    base_url: str = ""
    api_key: str = ""
    tools: tuple[dict, ...] = ()
    max_tokens: int | None = None
    thinking_budget: int | None = None
    timeout_s: float | None = None
    cancel: asyncio.Event | None = None
    client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class AnswerDelta:
    text: str


@dataclass(frozen=True)
class ToolCallBlock:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CacheInfo:
    rate: float | None
    read: int | None
    write: int | None
    base: int | None


LlmEvent = ThinkingDelta | AnswerDelta | ToolCallBlock | Usage | CacheInfo


class Provider(Protocol):
    name: str

    def effective_base_url(self, raw: str) -> str: ...

    def build_request(self, base_url: str, req: LlmRequest) -> tuple[str, dict, dict]: ...

    async def stream(self, req: LlmRequest) -> AsyncIterator[LlmEvent]: ...

    async def list_models(self, base_url: str, api_key: str) -> list[str]: ...


def auth_headers(protocol: str, api_key: str) -> dict:
    """匿名端点（免费网关 / 本地 Ollama）不落鉴权头：空值头会被 httpx 拒绝。"""
    if protocol == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        return headers
    if protocol == "ollama" or not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def http_client(base_url: str, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
    """回环地址（自建/本地网关）绕过系统代理——httpx trust_env 会把环回
    请求也交给代理，代理对回环返回 502；外部地址照常跟随环境代理。"""
    return httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT, trust_env=urlsplit(base_url).hostname not in _LOOPBACK_HOSTS)


def make_timeout(timeout_s: float | None) -> httpx.Timeout:
    if timeout_s is None:
        return DEFAULT_TIMEOUT
    return httpx.Timeout(timeout_s, connect=15.0, read=timeout_s, write=30.0)


def check_cancel(cancel: asyncio.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ProviderError("cancelled")


def sse_data_lines(aiter_lines):
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


def assembled_calls(pending: dict) -> list[ToolCall]:
    """积攒表 → ToolCall 列表（无名称丢弃、坏 JSON 落空参）。"""
    out = []
    for idx in sorted(pending):
        item = pending[idx]
        if not item["name"]:
            continue
        try:
            args = json.loads(item["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append(ToolCall(id=item["id"], name=item["name"], arguments=args))
    return out


def wire_tool_calls(calls: tuple[ToolCall, ...]) -> list[dict]:
    """openai chat 线格式的 tool_calls（arguments 序列化为 JSON 串）。"""
    return [
        {"id": tc.id, "type": "function",
         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
        for tc in calls
    ]


def list_models_url(protocol: str, base: str) -> str:
    base = base.rstrip("/")
    if protocol == "anthropic":
        return base + "/v1/models"
    if protocol == "ollama":
        return base + "/api/tags"
    return base + "/models"


def parse_model_list(protocol: str, payload: dict) -> list[str]:
    if protocol in ("openai", "openai-responses", "anthropic"):
        return sorted(str(m["id"]) for m in payload.get("data") or [] if m.get("id"))
    if protocol == "ollama":
        return sorted(str(m["name"]) for m in payload.get("models") or [] if m.get("name"))
    return []


def _transport_retriable(e: httpx.HTTPError) -> bool:
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout))


async def _stream_response(client: httpx.AsyncClient, url: str, headers: dict, body: dict,
                           parse, cancel: asyncio.Event | None) -> AsyncIterator[LlmEvent]:
    async with client.stream("POST", url, json=body, headers=headers) as resp:
        if resp.status_code >= 400:
            # 响应体截断进异常：网关用 4xx 报真实原因（参数缺失/超窗），
            # 日志与溢出回收（looks_like_overflow）都依赖这段文本
            detail = (await resp.aread()).decode("utf-8", errors="replace")[:300]
            retriable = resp.status_code in (429, 500, 502, 503, 504)
            raise ProviderError(f"HTTP {resp.status_code}: {detail}", retriable=retriable)
        async for event in parse(resp.aiter_lines(), cancel):
            yield event


async def run_provider_stream(provider, req: LlmRequest, parse) -> AsyncIterator[LlmEvent]:
    """协议无关执行骨架：地址兜底 → 构请求 → 流式解析 → 异常收敛。"""
    base_url = provider.effective_base_url(req.base_url)
    url, headers, body = provider.build_request(base_url, req)
    try:
        if req.client is not None:
            async for event in _stream_response(req.client, url, headers, body, parse, req.cancel):
                yield event
        else:
            async with http_client(base_url, make_timeout(req.timeout_s)) as own:
                async for event in _stream_response(own, url, headers, body, parse, req.cancel):
                    yield event
    except httpx.HTTPError as e:
        raise ProviderError(str(e) or type(e).__name__, retriable=_transport_retriable(e)) from e
    except (TypeError, ValueError) as e:
        raise ProviderError(str(e) or type(e).__name__) from e
