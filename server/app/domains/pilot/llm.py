"""pilot LLM 客户端：OpenAI 兼容 chat/completions（非流式 + 流式 + 工具调用）。

provider 无关：base_url + api_key + model 三项即可接入任何 OpenAI 兼容
服务。流式形态区分三通道——reasoning_content 增量进 thinking（推理模型
才有）、content 增量进 answer、tool_calls 分片装配成完整调用后一次性
产出（agent 循环内核）。网络/协议异常统一收敛为 PilotLlmError，由服务
层转用户语言提示，原始原因只进日志。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_S = 60.0
_MAX_OUTPUT_TOKENS = 700

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _client(base_url: str) -> httpx.AsyncClient:
    """回环地址（自建/本地网关）绕过系统代理——httpx trust_env 会把环回
    请求也交给代理，代理对回环返回 502；外部地址照常跟随环境代理。"""
    return httpx.AsyncClient(timeout=_TIMEOUT_S, trust_env=not _is_loopback(base_url))


class PilotLlmError(RuntimeError):
    """LLM 调用失败（网络 / 非 2xx / 响应形态不符）。"""


def _is_loopback(base_url: str) -> bool:
    return urlsplit(base_url).hostname in _LOOPBACK_HOSTS


def _payload(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _body(
    model: str,
    messages: list[dict],
    *,
    stream: bool,
    tools: list[dict] | None = None,
) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    if stream:
        # 思维链 token 计入用量台账的前提：让 provider 在流末尾回传 usage
        body["stream_options"] = {"include_usage": True}
    return body


async def chat_complete(
    *, base_url: str, api_key: str, model: str, system: str, user: str
) -> tuple[str, int, int]:
    """单次补全。返回 (回答文本, 输入 tokens, 输出 tokens)。"""
    try:
        async with _client(base_url) as client:
            resp = await client.post(
                _payload(base_url),
                json=_body(model, [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ], stream=False),
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        return (
            str(text).strip(),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        logger.warning("pilot LLM 调用失败: %r", e)
        raise PilotLlmError(str(e)) from e


async def chat_stream(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
):
    """流式对话（agent 循环内核）。逐段产出：

    ("thinking", 增量)            reasoning_content 通道（推理模型才有）；
    ("answer", 增量)              正文通道；
    ("tool_calls", [assembled])   finish_reason=tool_calls 时一次性产出完整
                                  调用列表，每项 {id, name, arguments(dict)}；
    ("usage", (输入, 输出))        provider 回传时才有——缺省宁少勿多。

    arguments 解析失败的 tool_call 整条丢弃；网络/协议异常收敛为
    PilotLlmError。"""
    try:
        async with _client(base_url) as client:
            async with client.stream(
                "POST",
                _payload(base_url),
                json=_body(model, messages, stream=True, tools=tools),
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                resp.raise_for_status()
                pending: dict[int, dict] = {}
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        yield (
                            "usage",
                            (
                                int(usage.get("prompt_tokens") or 0),
                                int(usage.get("completion_tokens") or 0),
                            ),
                        )
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
                        assembled = []
                        for idx in sorted(pending):
                            item = pending[idx]
                            if not item["name"]:
                                continue
                            try:
                                args = json.loads(item["args"] or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            assembled.append({"id": item["id"], "name": item["name"], "arguments": args})
                        pending = {}
                        if assembled:
                            yield ("tool_calls", assembled)
                # 流结束兜底：部分 provider 不发 finish_reason，流末尾仍有待发
                # 的装配结果——不产出的话这轮工具调用就丢了
                if pending:
                    assembled = []
                    for idx in sorted(pending):
                        item = pending[idx]
                        if not item["name"]:
                            continue
                        try:
                            args = json.loads(item["args"] or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        assembled.append({"id": item["id"], "name": item["name"], "arguments": args})
                    if assembled:
                        yield ("tool_calls", assembled)
    except (httpx.HTTPError, TypeError, ValueError) as e:
        logger.warning("pilot LLM 流式调用失败: %r", e)
        raise PilotLlmError(str(e)) from e
