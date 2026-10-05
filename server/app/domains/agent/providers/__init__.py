"""Agent V2 P2 Provider/Model 层：显式契约 + 四协议适配 + 注册表。

与 pilot/llm.py 双轨并存（pilot 不切换，P7 退役）；行为等价由
test_agent_providers.py 新旧同帧对比钉住。"""
from app.domains.agent.providers.base import (
    AnswerDelta,
    CacheInfo,
    LlmEvent,
    LlmRequest,
    Message,
    Provider,
    ProviderError,
    ThinkingDelta,
    ToolCall,
    ToolCallBlock,
    Usage,
)
from app.domains.agent.providers.registry import (
    DEFAULT_BASE_URL,
    PROVIDERS,
    PROTOCOLS,
    detect_provider,
    effective_base_url,
    get_provider,
    list_models,
    match_host,
    model_caps,
    stream_events,
)

__all__ = [
    "AnswerDelta", "CacheInfo", "LlmEvent", "LlmRequest", "Message", "Provider",
    "ProviderError", "ThinkingDelta", "ToolCall", "ToolCallBlock", "Usage",
    "DEFAULT_BASE_URL", "PROVIDERS", "PROTOCOLS", "detect_provider",
    "effective_base_url", "get_provider", "list_models", "match_host",
    "model_caps", "stream_events",
]
