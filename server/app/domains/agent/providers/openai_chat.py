"""openai 兼容 /chat/completions 适配（DeepSeek / 智谱 / 通义 / Kimi 等）。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from app.domains.agent.providers import base
from app.domains.agent.providers.base import (
    AnswerDelta,
    CacheInfo,
    LlmEvent,
    LlmRequest,
    MAX_OUTPUT_TOKENS,
    ThinkingDelta,
    ToolCallBlock,
    Usage,
    assembled_calls,
    auth_headers,
    check_cancel,
    list_models_url,
    parse_model_list,
    run_provider_stream,
    sse_data_lines,
    wire_tool_calls,
)


def _wire_message(m) -> dict:
    item: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        item["tool_calls"] = wire_tool_calls(m.tool_calls)
    if m.tool_call_id is not None:
        item["tool_call_id"] = m.tool_call_id
    return item


async def _parse(aiter_lines, cancel) -> AsyncIterator[LlmEvent]:
    pending: dict[int, dict] = {}
    async for data in sse_data_lines(aiter_lines):
        check_cancel(cancel)
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = chunk.get("usage")
        if usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            yield Usage(input_tokens=prompt, output_tokens=int(usage.get("completion_tokens") or 0))
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            read = int(cached) if isinstance(cached, (int, float)) else None
            yield CacheInfo(rate=(min(1.0, read / prompt) if read is not None and prompt > 0 else None),
                            read=read, write=None, base=prompt or None)
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        think = delta.get("reasoning_content")
        if think:
            yield ThinkingDelta(text=str(think))
        content = delta.get("content")
        if content:
            yield AnswerDelta(text=str(content))
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
            assembled = assembled_calls(pending)
            pending = {}
            if assembled:
                yield ToolCallBlock(calls=tuple(assembled))
    if pending:
        # 流末兜底：部分实现不发 finish_reason
        assembled = assembled_calls(pending)
        if assembled:
            yield ToolCallBlock(calls=tuple(assembled))


class OpenAiChatProvider:
    name = "openai"
    default_base_url = ""

    def effective_base_url(self, raw: str) -> str:
        return (raw or "").strip() or self.default_base_url

    def build_request(self, base_url: str, req: LlmRequest) -> tuple[str, dict, dict]:
        url = base_url.rstrip("/") + "/chat/completions"
        # 兼容层收敛：部分网关（unisound 等）拒绝 assistant content=null（纯
        # tool_calls 轮的标准形态），统一规范为空串——对标准 OpenAI 同样合法。
        normalized = []
        for m in req.messages:
            item = _wire_message(m)
            if m.role == "assistant" and m.content is None:
                item["content"] = ""
            normalized.append(item)
        body = {
            "model": req.model,
            "messages": normalized,
            "temperature": 0.4,
            "max_tokens": req.max_tokens or MAX_OUTPUT_TOKENS,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            body["tools"] = list(req.tools)
        return url, auth_headers("openai", req.api_key), body

    async def stream(self, req: LlmRequest) -> AsyncIterator[LlmEvent]:
        async for event in run_provider_stream(self, req, _parse):
            yield event

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        resolved = self.effective_base_url(base_url)
        async with base.http_client(resolved) as client:
            resp = await client.get(list_models_url(self.name, resolved), headers=auth_headers(self.name, api_key))
            resp.raise_for_status()
            return parse_model_list(self.name, resp.json())
