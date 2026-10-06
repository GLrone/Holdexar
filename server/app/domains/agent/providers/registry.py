"""协议注册表：Provider 实例解析、host 指纹、模型探测、单次重试与能力基座。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from app.core.logging import log_event
from app.domains.agent.providers.anthropic import AnthropicProvider
from app.domains.agent.providers.base import LlmEvent, LlmRequest, Provider, ProviderError
from app.domains.agent.providers.ollama import OllamaProvider
from app.domains.agent.providers.openai_chat import OpenAiChatProvider
from app.domains.agent.providers.openai_responses import OpenAiResponsesProvider

logger = logging.getLogger(__name__)

PROTOCOLS = ("openai", "openai-responses", "anthropic", "ollama")

PROVIDERS: dict[str, Provider] = {
    p.name: p for p in (OpenAiChatProvider(), OpenAiResponsesProvider(), AnthropicProvider(), OllamaProvider())
}

DEFAULT_BASE_URL = {name: p.default_base_url for name, p in PROVIDERS.items()}


def get_provider(protocol: str) -> Provider:
    if protocol not in PROVIDERS:
        raise ProviderError(f"未知协议: {protocol}")
    return PROVIDERS[protocol]


def effective_base_url(protocol: str, base_url: str) -> str:
    """协议默认地址兜底：用户未填 base_url 时用协议官方入口。"""
    return get_provider(protocol).effective_base_url(base_url)


async def stream_events(protocol: str, req: LlmRequest) -> AsyncIterator[LlmEvent]:
    """协议分发 + 单次重试：仅 retriable 且首个事件产出前的失败重试一次，
    流中途失败不重试（防事件重复）。"""
    provider = get_provider(protocol)
    for attempt in (0, 1):
        emitted = False
        try:
            async for event in provider.stream(req):
                emitted = True
                yield event
            return
        except ProviderError as e:
            if not e.retriable or emitted or attempt:
                raise
            log_event(
                logger,
                f"模型来源「{protocol}」的流式调用失败，正在重试一次",
                level=logging.WARNING,
                detail={"模型来源": protocol, "原因码": e.code},
            )


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


async def list_models(protocol: str, base_url: str, api_key: str) -> list[str]:
    """探测该账号可用的模型清单（异常上抛由调用方处置）。"""
    return await get_provider(protocol).list_models(base_url, api_key)


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


# ── 模型能力基座：协议缺省 + 模型名启发式；消费方在 P5+（审批 / 上下文预算）──

_THINKING_HINTS = ("reasoner", "o1", "o3", "r1", "thinking", "qwq")


@dataclass(frozen=True)
class ModelCaps:
    supports_tools: bool | None = None
    supports_thinking: bool | None = None
    context_window: int | None = None


def model_caps(protocol: str, model: str, context_window: int | None = None) -> ModelCaps:
    """能力探测基座：窗口以显式设置为准，tool/thinking 按协议与模型名推导。"""
    model_l = (model or "").lower()
    if protocol in ("anthropic", "openai-responses", "ollama"):
        return ModelCaps(supports_tools=True, supports_thinking=True, context_window=context_window)
    thinking = any(h in model_l for h in _THINKING_HINTS)
    return ModelCaps(supports_tools=True, supports_thinking=thinking or None, context_window=context_window)
