"""ollama 本地 /api/chat 适配（ndjson，thinking 字段）。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from app.domains.agent.providers import base
from app.domains.agent.providers.base import (
    AnswerDelta,
    LlmEvent,
    LlmRequest,
    ThinkingDelta,
    ToolCall,
    ToolCallBlock,
    Usage,
    auth_headers,
    check_cancel,
    list_models_url,
    parse_model_list,
    run_provider_stream,
)


async def _parse(aiter_lines, cancel) -> AsyncIterator[LlmEvent]:
    async for line in aiter_lines:
        check_cancel(cancel)
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
            yield ThinkingDelta(text=str(think))
        content = msg.get("content")
        if content:
            yield AnswerDelta(text=str(content))
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
            yield ToolCallBlock(calls=tuple(ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"]) for c in calls))
        if ev.get("done"):
            yield Usage(input_tokens=int(ev.get("prompt_eval_count") or 0),
                        output_tokens=int(ev.get("eval_count") or 0))


class OllamaProvider:
    name = "ollama"
    default_base_url = "http://127.0.0.1:11434"

    def effective_base_url(self, raw: str) -> str:
        return (raw or "").strip() or self.default_base_url

    def build_request(self, base_url: str, req: LlmRequest) -> tuple[str, dict, dict]:
        url = base_url.rstrip("/") + "/api/chat"
        converted = []
        for m in req.messages:
            item = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                item["tool_calls"] = [{"function": {"name": tc.name, "arguments": tc.arguments}}
                                      for tc in m.tool_calls]
            converted.append(item)
        body: dict = {"model": req.model, "messages": converted, "stream": True}
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = list(req.tools)
        return url, auth_headers("ollama", req.api_key), body

    async def stream(self, req: LlmRequest) -> AsyncIterator[LlmEvent]:
        async for event in run_provider_stream(self, req, _parse):
            yield event

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        resolved = self.effective_base_url(base_url)
        async with base.http_client(resolved) as client:
            resp = await client.get(list_models_url(self.name, resolved), headers=auth_headers(self.name, api_key))
            resp.raise_for_status()
            return parse_model_list(self.name, resp.json())
