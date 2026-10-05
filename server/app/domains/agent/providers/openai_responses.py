"""openai-responses（Responses API /responses）适配。"""
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
    ToolCall,
    ToolCallBlock,
    Usage,
    auth_headers,
    check_cancel,
    list_models_url,
    parse_model_list,
    run_provider_stream,
    sse_data_lines,
)


def _input_items(messages: tuple) -> tuple[str | None, list[dict]]:
    """内部消息 → Responses input 项：system 提为 instructions，
    工具调用/结果转 function_call / function_call_output 项。"""
    system_parts = [m.content for m in messages if m.role == "system" and m.content]
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user":
            out.append({"role": "user", "content": [{"type": "input_text", "text": m.content or ""}]})
        elif m.role == "assistant":
            if m.content:
                out.append({"role": "assistant", "content": [{"type": "output_text", "text": m.content}]})
            for tc in m.tool_calls:
                out.append({
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                })
        elif m.role == "tool":
            out.append({
                "type": "function_call_output",
                "call_id": m.tool_call_id or "",
                "output": m.content or "",
            })
    instructions = "\n".join(system_parts) or None
    return instructions, out


def _flush_pending(pending: dict[str, dict], order: list[str]) -> list[tuple[ToolCall, ...]]:
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
        calls.append(ToolCall(id=acc["id"], name=acc["name"], arguments=args))
    order.clear()
    return [tuple(calls)] if calls else []


async def _parse(aiter_lines, cancel) -> AsyncIterator[LlmEvent]:
    """Responses SSE：output_text.delta=正文、reasoning_summary_text.delta=思考、
    function_call 项按 item 组装（added 起头、arguments.delta 追加、done 成call）。"""
    pending: dict[str, dict] = {}
    order: list[str] = []
    async for data in sse_data_lines(aiter_lines):
        check_cancel(cancel)
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "response.output_text.delta":
            if ev.get("delta"):
                yield AnswerDelta(text=str(ev["delta"]))
        elif etype == "response.reasoning_summary_text.delta":
            if ev.get("delta"):
                yield ThinkingDelta(text=str(ev["delta"]))
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
                yield ToolCallBlock(calls=calls)
            usage = (ev.get("response") or {}).get("usage") or {}
            inp = int(usage.get("input_tokens") or 0)
            yield Usage(input_tokens=inp, output_tokens=int(usage.get("output_tokens") or 0))
            cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            read = int(cached) if isinstance(cached, (int, float)) else None
            yield CacheInfo(rate=(min(1.0, read / inp) if read is not None and inp > 0 else None),
                            read=read, write=None, base=inp or None)
    for calls in _flush_pending(pending, order):
        # 流末兜底：部分兼容服务不发 completed
        yield ToolCallBlock(calls=calls)


class OpenAiResponsesProvider:
    name = "openai-responses"
    default_base_url = ""

    def effective_base_url(self, raw: str) -> str:
        return (raw or "").strip() or self.default_base_url

    def build_request(self, base_url: str, req: LlmRequest) -> tuple[str, dict, dict]:
        url = base_url.rstrip("/") + "/responses"
        instructions, items = _input_items(req.messages)
        body: dict = {
            "model": req.model,
            "input": items,
            "stream": True,
            "max_output_tokens": req.max_tokens or MAX_OUTPUT_TOKENS,
            "temperature": 0.4,
        }
        if instructions:
            body["instructions"] = instructions
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "parameters": t["function"].get("parameters", {"type": "object"}),
                }
                for t in req.tools
            ]
        return url, {"Authorization": f"Bearer {req.api_key}"}, body

    async def stream(self, req: LlmRequest) -> AsyncIterator[LlmEvent]:
        async for event in run_provider_stream(self, req, _parse):
            yield event

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        resolved = self.effective_base_url(base_url)
        async with base.http_client(resolved) as client:
            resp = await client.get(list_models_url(self.name, resolved), headers=auth_headers(self.name, api_key))
            resp.raise_for_status()
            return parse_model_list(self.name, resp.json())
