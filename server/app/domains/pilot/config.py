"""pilot 配置：领航员 LLM 服务的设置键与月度用量台账。

设置键（settings KV，前缀 pilot.）：
- pilot.llm.enabled       bool，LLM 解读开关；关闭时领航台仍直出模板化事实摘要
- pilot.llm.base_url      OpenAI 兼容服务地址（含到 /v1 的路径前缀）
- pilot.llm.model         模型名
- pilot.llm.api_key       API Key（settings.SECRET_KEYS 成员，落库即密文）
- pilot.llm.monthly_cap   单月 token 用量上限（超限停用解读，模板摘要不受限）
- pilot.usage.<YYYYMM>    当月已用 token 累计（用量台账，跨月自然换键）
"""
from __future__ import annotations

from app.crawler.utils import get_beijing_time_obj
from app.domains.settings import service as settings_service

DEFAULT_MONTHLY_CAP = 500_000

_ENABLED_KEY = "pilot.llm.enabled"
_BASE_URL_KEY = "pilot.llm.base_url"
_MODEL_KEY = "pilot.llm.model"
_API_KEY_KEY = "pilot.llm.api_key"
_CAP_KEY = "pilot.llm.monthly_cap"


def _usage_key() -> str:
    return f"pilot.usage.{get_beijing_time_obj().strftime('%Y%m')}"


async def load_config() -> dict:
    cap_raw = await settings_service.get_value(_CAP_KEY, DEFAULT_MONTHLY_CAP)
    try:
        cap = max(0, int(cap_raw))
    except (TypeError, ValueError):
        cap = DEFAULT_MONTHLY_CAP
    return {
        "enabled": bool(await settings_service.get_value(_ENABLED_KEY, False)),
        "base_url": str(await settings_service.get_value(_BASE_URL_KEY, "") or "").strip(),
        "model": str(await settings_service.get_value(_MODEL_KEY, "") or "").strip(),
        "api_key": str(await settings_service.get_secret_value(_API_KEY_KEY, "") or ""),
        "monthly_cap": cap,
    }


async def save_config(update: dict) -> None:
    """按提交字段写入；api_key 空 = 清空（set_secret_value 的既有语义）。"""
    if "enabled" in update:
        await settings_service.set_value(_ENABLED_KEY, bool(update["enabled"]))
    if "base_url" in update:
        await settings_service.set_value(_BASE_URL_KEY, str(update["base_url"] or "").strip())
    if "model" in update:
        await settings_service.set_value(_MODEL_KEY, str(update["model"] or "").strip())
    if "api_key" in update:
        await settings_service.set_secret_value(_API_KEY_KEY, update["api_key"])
    if "monthly_cap" in update:
        try:
            cap = max(0, int(update["monthly_cap"]))
        except (TypeError, ValueError):
            cap = DEFAULT_MONTHLY_CAP
        await settings_service.set_value(_CAP_KEY, cap)


async def usage_month() -> int:
    return int(await settings_service.get_value(_usage_key(), 0) or 0)


async def add_usage(tokens: int) -> None:
    if tokens <= 0:
        return
    total = int(await settings_service.get_value(_usage_key(), 0) or 0) + tokens
    await settings_service.set_value(_usage_key(), total)


def llm_ready(cfg: dict) -> bool:
    """四项齐备才算可用（开关、地址、模型、密钥）。"""
    return bool(cfg["enabled"] and cfg["base_url"] and cfg["model"] and cfg["api_key"])
