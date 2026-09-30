"""pilot 配置：领航员 LLM 服务的设置键与月度用量台账。

设置键（settings KV，前缀 pilot.）：
- pilot.llm.protocol      协议：openai（兼容 DeepSeek/智谱/通义/Kimi 等）/
                          anthropic / gemini / ollama
- pilot.llm.enabled       bool，LLM 解读开关；关闭时领航台仍直出模板化事实摘要
- pilot.llm.base_url      服务地址；空值时用协议官方入口（llm.DEFAULT_BASE_URL）
- pilot.llm.model         当前选用的模型名
- pilot.llm.models        模型清单（JSON 数组；自动识别与手动添加共同维护）
- pilot.llm.api_key       API Key（settings.SECRET_KEYS 成员，落库即密文）
- pilot.llm.monthly_cap   单月 token 用量上限（超限停用解读，模板摘要不受限）
- pilot.usage.<YYYYMM>    当月已用 token 累计（用量台账，跨月自然换键）
"""
from __future__ import annotations

from app.crawler.utils import get_beijing_time_obj
from app.domains.pilot import llm as pilot_llm
from app.domains.settings import service as settings_service

DEFAULT_MONTHLY_CAP = 500_000

_ENABLED_KEY = "pilot.llm.enabled"
_PROTOCOL_KEY = "pilot.llm.protocol"
_BASE_URL_KEY = "pilot.llm.base_url"
_MODEL_KEY = "pilot.llm.model"
_MODELS_KEY = "pilot.llm.models"
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
    protocol = str(await settings_service.get_value(_PROTOCOL_KEY, "openai") or "openai").strip()
    if protocol not in pilot_llm.PROTOCOLS:
        protocol = "openai"
    return {
        "protocol": protocol,
        "enabled": bool(await settings_service.get_value(_ENABLED_KEY, False)),
        "base_url": str(await settings_service.get_value(_BASE_URL_KEY, "") or "").strip(),
        "model": str(await settings_service.get_value(_MODEL_KEY, "") or "").strip(),
        "models": _models_of(await settings_service.get_value(_MODELS_KEY, None)),
        "api_key": str(await settings_service.get_secret_value(_API_KEY_KEY, "") or ""),
        "monthly_cap": cap,
    }


async def save_config(update: dict) -> None:
    """按提交字段写入；api_key 空 = 清空（set_secret_value 的既有语义）。"""
    if "protocol" in update:
        protocol = str(update["protocol"] or "openai").strip()
        if protocol not in pilot_llm.PROTOCOLS:
            protocol = "openai"
        await settings_service.set_value(_PROTOCOL_KEY, protocol)
    if "enabled" in update:
        await settings_service.set_value(_ENABLED_KEY, bool(update["enabled"]))
    if "base_url" in update:
        await settings_service.set_value(_BASE_URL_KEY, str(update["base_url"] or "").strip())
    if "model" in update:
        await settings_service.set_value(_MODEL_KEY, str(update["model"] or "").strip())
    if "models" in update:
        models = update["models"]
        if isinstance(models, list):
            await settings_service.set_value(_MODELS_KEY, [str(m) for m in models if str(m).strip()][:50])
    if "api_key" in update:
        await settings_service.set_secret_value(_API_KEY_KEY, update["api_key"])
    if "monthly_cap" in update:
        try:
            cap = max(0, int(update["monthly_cap"]))
        except (TypeError, ValueError):
            cap = DEFAULT_MONTHLY_CAP
        await settings_service.set_value(_CAP_KEY, cap)


def _models_of(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(m) for m in raw if str(m).strip()][:50]


async def usage_month() -> dict:
    """当月用量台账：输入 / 输出 / 调用次数 / 合计。

    存量整数（旧口径只记合计）迁读为 total，输入输出缺省 0。"""
    raw = await settings_service.get_value(_usage_key(), None)
    if isinstance(raw, dict):
        inp = int(raw.get("inp") or 0)
        out = int(raw.get("out") or 0)
        calls = int(raw.get("calls") or 0)
    elif isinstance(raw, int):
        inp, out, calls = int(raw), 0, 0
    else:
        inp = out = calls = 0
    return {"inp": inp, "out": out, "calls": calls, "total": inp + out}


async def add_usage(inp: int, out: int) -> None:
    if inp <= 0 and out <= 0:
        return
    cur = await usage_month()
    await settings_service.set_value(_usage_key(), {
        "inp": cur["inp"] + max(0, inp),
        "out": cur["out"] + max(0, out),
        "calls": cur["calls"] + 1,
    })


def over_cap(usage: dict, cap: int) -> bool:
    return usage["total"] >= cap


def llm_ready(cfg: dict) -> bool:
    """四项齐备才算可用（开关、地址、模型、密钥）。"""
    return bool(cfg["enabled"] and cfg["base_url"] and cfg["model"] and cfg["api_key"])
