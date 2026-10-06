"""anthropic Messages API 适配（thinking 块、tool_use/tool_result）。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from app.domains.agent.providers import base
from app.domains.agent.providers.base import (
    ANTHROPIC_THINKING_BUDGET,
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
)


def _convert(messages: tuple) -> tuple[str | None, list[dict]]:
    """内部消息 → Anthropic 形态：system 提顶、工具结果转 tool_result 块、
    连续同角色合并为单条多块消息（tool_result 必须收进 user 消息）。"""
    system = "\n".join(m.content for m in messages if m.role == "system") or None
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            # Anthropic 无 tool 角色：工具结果以 tool_result 块收进 user 轮
            blocks = [{
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content or "",
            }]
            if out and out[-1]["role"] == "user":
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": "user", "content": blocks})
            continue
        if m.role == "user":
            blocks = [{"type": "text", "text": m.content or ""}]
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
        else:
            continue
        if out and out[-1]["role"] == m.role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": m.role, "content": blocks})
    return system, out


async def _parse(aiter_lines, cancel) -> AsyncIterator[LlmEvent]:
    tools_acc: dict[int, dict] = {}
    usage = [0, 0]
    async for data in sse_data_lines(aiter_lines):
        check_cancel(cancel)
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
            yield CacheInfo(rate=(min(1.0, read / input_side) if input_side > 0 else None),
                            read=read or None, write=write or None, base=input_side or None)
        elif etype == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "thinking_delta" and d.get("thinking"):
                yield ThinkingDelta(text=str(d["thinking"]))
            elif dt == "text_delta" and d.get("text"):
                yield AnswerDelta(text=str(d["text"]))
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
                assembled = assembled_calls(tools_acc)
                tools_acc = {}
                if assembled:
                    yield ToolCallBlock(calls=tuple(assembled))
        elif etype == "message_stop":
            break
    yield Usage(input_tokens=usage[0], output_tokens=usage[1])


class AnthropicProvider:
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"

    def effective_base_url(self, raw: str) -> str:
        return (raw or "").strip() or self.default_base_url

    def build_request(self, base_url: str, req: LlmRequest) -> tuple[str, dict, dict]:
        resolved = base_url.rstrip("/")
        if resolved.endswith("/v1"):
            resolved = resolved[: -len("/v1")]
        url = resolved + "/v1/messages"
        system, converted = _convert(req.messages)
        budget = req.thinking_budget if req.thinking_budget is not None else ANTHROPIC_THINKING_BUDGET
        body: dict = {
            "model": req.model,
            "max_tokens": req.max_tokens if req.max_tokens else budget + MAX_OUTPUT_TOKENS,
            "messages": converted,
            "stream": True,
            # 扩展思维链：预算内先思考后作答（thinking 时 temperature 必须缺省）
            "thinking": {"type": "enabled", "budget_tokens": budget},
        }
        if system:
            body["system"] = system
        if req.tools:
            body["tools"] = [
                {"name": t["function"]["name"], "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters", {"type": "object"})}
                for t in req.tools
            ]
        return url, auth_headers("anthropic", req.api_key), body

    async def stream(self, req: LlmRequest) -> AsyncIterator[LlmEvent]:
        async for event in run_provider_stream(self, req, _parse):
            yield event

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        resolved = self.effective_base_url(base_url)
        async with base.http_client(resolved) as client:
            resp = await client.get(list_models_url(self.name, resolved), headers=auth_headers(self.name, api_key))
            resp.raise_for_status()
            return parse_model_list(self.name, resp.json())
