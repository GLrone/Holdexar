"""pilot 配置：领航员 LLM 的多供应商设置与月度用量台账。

供应商账本（settings KV）：
- pilot.llm.providers          供应商清单（JSON 数组：id/name/protocol/base_url/models/models_disabled）
- pilot.llm.active             活跃供应商 id（领航台在用的那家）
- pilot.llm.provider_key.<id>  供应商 API Key（逐家独立派生加密，写时即密文）
- pilot.llm.enabled            bool，LLM 解读总开关（全局；关闭时领航台仍直出模板化事实摘要）
- pilot.llm.model              当前选用模型名（属于活跃供应商的清单）
- pilot.llm.monthly_cap        单月 token 用量上限（全局；超限停用解读，模板摘要不受限）
- pilot.usage.<YYYYMM>         当月已用 token 累计（用量台账，跨月自然换键）

存量单配置（pilot.llm.protocol/base_url/models/api_key）在账本键缺失时
幂等迁移为一条「默认接入」供应商，密钥随迁；load_config 输出保持平面
形态（protocol/base_url/api_key/model/models 解析自活跃供应商），消费方无感。
协议：openai（兼容生态）/ openai-responses / anthropic / ollama。
"""
from __future__ import annotations

from uuid import uuid4

from app.crawler.utils import get_beijing_time_obj
from app.domains.pilot import llm as pilot_llm
from app.domains.settings import service as settings_service

DEFAULT_MONTHLY_CAP = 500_000

_ENABLED_KEY = "pilot.llm.enabled"
_PROTOCOL_KEY = "pilot.llm.protocol"
_BASE_URL_KEY = "pilot.llm.base_url"
_MODEL_KEY = "pilot.llm.model"
_MODELS_KEY = "pilot.llm.models"
_MODELS_DISABLED_KEY = "pilot.llm.models_disabled"
_API_KEY_KEY = "pilot.llm.api_key"
_CAP_KEY = "pilot.llm.monthly_cap"
_PROVIDERS_KEY = "pilot.llm.providers"
_ACTIVE_KEY = "pilot.llm.active"
_CONTEXT_WINDOW_KEY = "pilot.llm.context_window"
# 窗口上限：防手滑填爆（1M 级窗口已覆盖现役模型；未知填 0/清空即回落固定预算）
_CONTEXT_WINDOW_MAX = 2_000_000


def _provider_key_name(pid: str) -> str:
    return f"pilot.llm.provider_key.{pid}"


async def provider_key(pid: str) -> str:
    """读指定供应商已存密钥；无密钥返回空串。"""
    return str(await settings_service.get_secret_value(_provider_key_name(pid), "") or "")


def _clean_window(raw) -> int | None:
    """窗口清洗：正整数生效（封顶防手滑），0/空/非法 = 未知（None）。"""
    try:
        window = int(raw)
    except (TypeError, ValueError):
        return None
    return window if 0 < window <= _CONTEXT_WINDOW_MAX else None


def _clean_provider(raw: dict) -> dict:
    """供应商记录清洗（账本口径）：字段齐备、协议合法、清单截断。"""
    protocol = str(raw.get("protocol") or "openai").strip()
    if protocol not in pilot_llm.PROTOCOLS:
        protocol = "openai"
    return {
        "id": str(raw.get("id") or uuid4().hex[:12]),
        "name": str(raw.get("name") or "").strip()[:60],
        "protocol": protocol,
        "base_url": str(raw.get("base_url") or "").strip(),
        "models": _models_of(raw.get("models")),
        "models_disabled": _models_of(raw.get("models_disabled")),
        "context_window": _clean_window(raw.get("context_window")),
    }


def _usage_key() -> str:
    return f"pilot.usage.{get_beijing_time_obj().strftime('%Y%m')}"


async def load_providers() -> list[dict]:
    """供应商账本（含存量单配置幂等迁移；返回体不带密钥原文，带 has_key）。"""
    raw = await settings_service.get_value(_PROVIDERS_KEY, None)
    if isinstance(raw, list) and raw:
        providers = [_clean_provider(r) for r in raw if isinstance(r, dict)]
    else:
        providers = await _migrate_legacy_provider()
    return [await _with_key_flag(p) for p in providers]


async def _migrate_legacy_provider() -> list[dict]:
    """存量单配置 → 供应商账本（幂等：仅账本键缺失时执行一次）。"""
    legacy_base = str(await settings_service.get_value(_BASE_URL_KEY, "") or "").strip()
    legacy_models = _models_of(await settings_service.get_value(_MODELS_KEY, None))
    if not (legacy_base or legacy_models):
        await settings_service.set_value(_PROVIDERS_KEY, [])
        return []
    pid = "main"
    provider = _clean_provider({
        "id": pid, "name": "默认接入",
        "protocol": await settings_service.get_value(_PROTOCOL_KEY, "openai"),
        "base_url": legacy_base, "models": legacy_models,
        "models_disabled": await settings_service.get_value(_MODELS_DISABLED_KEY, None),
    })
    legacy_key = await settings_service.get_secret_value(_API_KEY_KEY, "")
    if legacy_key:
        await settings_service.set_secret_value(_provider_key_name(pid), legacy_key)
    await settings_service.set_value(_PROVIDERS_KEY, [provider])
    await settings_service.set_value(_ACTIVE_KEY, pid)
    return [provider]


async def _with_key_flag(provider: dict) -> dict:
    stored = await settings_service.get_value(_provider_key_name(provider["id"]), None)
    return {**provider, "has_key": bool(stored)}


async def _write_providers(providers: list[dict]) -> None:
    await settings_service.set_value(_PROVIDERS_KEY, providers)


async def create_provider(record: dict) -> dict:
    """新增供应商（id 服务端生成；api_key 有值即加密落库）。返回清洗后记录。"""
    provider = _clean_provider({"id": uuid4().hex[:12], **record})
    providers = await load_providers()
    api_key = record.get("api_key")
    if api_key:
        await settings_service.set_secret_value(_provider_key_name(provider["id"]), str(api_key))
    providers.append(await _with_key_flag(provider))
    await _write_providers(providers)
    if len(providers) == 1:
        await set_active_provider(provider["id"])
    return await _with_key_flag(provider)


async def update_provider(pid: str, update: dict) -> dict:
    """更新供应商字段；api_key 三态（缺省不改 / 空串清空 / 有值重写）。

    base_url 这类端点字段空值不静默覆盖既有生效配置：编辑其他字段（如模型
    清单）时即使端点框为空也不该把已配好的地址冲掉，否则供应商整体失活。"""
    providers = await load_providers()
    target = next((p for p in providers if p["id"] == pid), None)
    if target is None:
        raise LookupError(f"provider not found: {pid}")
    patch = {k: v for k, v in update.items() if k != "api_key"}
    if not str(patch.get("base_url") or "").strip() and str(target.get("base_url") or "").strip():
        patch["base_url"] = target["base_url"]
    merged = _clean_provider({**target, **patch})
    if "api_key" in update:
        await settings_service.set_secret_value(_provider_key_name(pid), update["api_key"])
    providers[providers.index(target)] = merged
    await _write_providers(providers)
    active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
    if pid == active_id or not active_id:
        await _ensure_active_model(merged)
    return await _with_key_flag(merged)


async def delete_provider(pid: str) -> None:
    """删除供应商（密钥键随删）；删的是活跃家时交接给第一家（无则清空模型）。"""
    providers = await load_providers()
    remaining = [p for p in providers if p["id"] != pid]
    await _write_providers(remaining)
    await settings_service.set_value(_provider_key_name(pid), "")
    active = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
    if active == pid:
        await settings_service.set_value(_ACTIVE_KEY, remaining[0]["id"] if remaining else "")
        if not remaining:
            await settings_service.set_value(_MODEL_KEY, "")


async def set_active_provider(pid: str) -> dict:
    """切换活跃供应商；当前模型不属于新家启用清单时回落其首个启用模型。

    已生效的模型不被静默清空：启用清单为空（模型全被禁用 / 尚未配置）时保留
    既有模型，避免切换动作把正在用的模型冲成空导致领航员整体失活。"""
    providers = await load_providers()
    target = next((p for p in providers if p["id"] == pid), None)
    if target is None:
        raise LookupError(f"provider not found: {pid}")
    await settings_service.set_value(_ACTIVE_KEY, pid)
    disabled = set(target["models_disabled"])
    enabled = [m for m in target["models"] if m not in disabled]
    model = str(await settings_service.get_value(_MODEL_KEY, "") or "").strip()
    stored = model
    if model not in enabled and enabled:
        stored = enabled[0]
    # 启用清单为空时不重置：保留既有模型，不静默清空
    if stored != model:
        await settings_service.set_value(_MODEL_KEY, stored)
    return {"active": pid, "model": stored}


async def load_config() -> dict:
    """平面配置（消费方形态不变）：解析自活跃供应商 + 全局开关/上限/台账。"""
    cap_raw = await settings_service.get_value(_CAP_KEY, DEFAULT_MONTHLY_CAP)
    try:
        cap = max(0, int(cap_raw))
    except (TypeError, ValueError):
        cap = DEFAULT_MONTHLY_CAP
    providers = await load_providers()
    active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
    active = next((p for p in providers if p["id"] == active_id), providers[0] if providers else None)
    api_key = ""
    if active:
        api_key = str(await settings_service.get_secret_value(_provider_key_name(active["id"]), "") or "")
    else:
        # 无供应商账本时回落存量单配置读取（全新库两路皆空）
        api_key = str(await settings_service.get_secret_value(_API_KEY_KEY, "") or "")
    return {
        "protocol": active["protocol"] if active else str(await settings_service.get_value(_PROTOCOL_KEY, "openai") or "openai").strip(),
        "enabled": bool(await settings_service.get_value(_ENABLED_KEY, False)),
        "base_url": active["base_url"] if active else str(await settings_service.get_value(_BASE_URL_KEY, "") or "").strip(),
        "model": str(await settings_service.get_value(_MODEL_KEY, "") or "").strip(),
        "models": active["models"] if active else _models_of(await settings_service.get_value(_MODELS_KEY, None)),
        "models_disabled": active["models_disabled"] if active else _models_of(await settings_service.get_value(_MODELS_DISABLED_KEY, None)),
        "api_key": api_key,
        "monthly_cap": cap,
        "context_window": active["context_window"] if active else _clean_window(
            await settings_service.get_value(_CONTEXT_WINDOW_KEY, None)),
        "active": active["id"] if active else "",
        "providers": providers,
    }


async def save_config(update: dict) -> None:
    """按提交字段写入；api_key 空 = 清空（set_secret_value 的既有语义）。

    供应商级字段（protocol/base_url/models/models_disabled/api_key）在多供应
    商语义下写入**活跃供应商**账本；无账本（全新库）回落存量单键。"""
    if "active" in update and update["active"]:
        try:
            await set_active_provider(str(update["active"]))
        except LookupError:
            pass
    if "protocol" in update:
        protocol = str(update["protocol"] or "openai").strip()
        if protocol not in pilot_llm.PROTOCOLS:
            protocol = "openai"
        await _patch_active_provider({"protocol": protocol}) or await settings_service.set_value(_PROTOCOL_KEY, protocol)
    if "enabled" in update:
        await settings_service.set_value(_ENABLED_KEY, bool(update["enabled"]))
    if "base_url" in update:
        await _patch_active_provider({"base_url": str(update["base_url"] or "").strip()}) or await settings_service.set_value(_BASE_URL_KEY, str(update["base_url"] or "").strip())
    if "model" in update:
        await settings_service.set_value(_MODEL_KEY, str(update["model"] or "").strip())
    if "models" in update:
        models = update["models"]
        if isinstance(models, list):
            cleaned = [str(m) for m in models if str(m).strip()][:50]
            await _patch_active_provider({"models": cleaned}) or await settings_service.set_value(_MODELS_KEY, cleaned)
    if "models_disabled" in update:
        disabled = update["models_disabled"]
        if isinstance(disabled, list):
            cleaned = [str(m) for m in disabled if str(m).strip()][:50]
            await _patch_active_provider({"models_disabled": cleaned}) or await settings_service.set_value(_MODELS_DISABLED_KEY, cleaned)
    if "api_key" in update:
        active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
        if active_id:
            await settings_service.set_secret_value(_provider_key_name(active_id), update["api_key"])
        else:
            await settings_service.set_secret_value(_API_KEY_KEY, update["api_key"])
    if "monthly_cap" in update:
        try:
            cap = max(0, int(update["monthly_cap"]))
        except (TypeError, ValueError):
            cap = DEFAULT_MONTHLY_CAP
        await settings_service.set_value(_CAP_KEY, cap)
    if "context_window" in update:
        cleaned = _clean_window(update["context_window"])
        # 显式空 = 清除（回落固定预算）；无供应商账本时落存量单键
        if not await _patch_active_provider({"context_window": cleaned}):
            await settings_service.set_value(_CONTEXT_WINDOW_KEY, cleaned or 0)


def _models_of(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(m) for m in raw if str(m).strip()][:50]


async def _patch_active_provider(patch: dict) -> bool:
    """把供应商级字段补丁写到活跃供应商账本；无账本返回 False（调用方回落单键）。"""
    providers = await load_providers()
    if not providers:
        return False
    active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
    target = next((p for p in providers if p["id"] == active_id), providers[0])
    merged = _clean_provider({**target, **patch})
    providers[providers.index(target)] = merged
    await _write_providers(providers)
    if merged["id"] == active_id or not active_id:
        await _ensure_active_model(merged)
    return True


async def _ensure_active_model(provider: dict) -> None:
    """添加即生效：活跃供应商有启用模型而当前模型为空/失效时，自动选中首个启用模型。

    只在模型「不可用」时兜底，不覆盖用户已做的选择。"""
    disabled = set(provider["models_disabled"])
    enabled = [m for m in provider["models"] if m not in disabled]
    if not enabled:
        return
    model = str(await settings_service.get_value(_MODEL_KEY, "") or "").strip()
    if model not in enabled:
        await settings_service.set_value(_MODEL_KEY, enabled[0])


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
    """开关、地址、模型齐备即可用；密钥可选（匿名免费端点不要求密钥）。"""
    return bool(cfg["enabled"] and cfg["base_url"] and cfg["model"])
