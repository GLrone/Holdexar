"""pilot 配置：领航员 LLM 的多供应商设置与月度用量台账。

供应商账本（settings KV）：
- pilot.llm.providers          供应商清单（JSON 数组：id/name/protocol/base_url/models/models_disabled）
- pilot.llm.active             活跃供应商 id（领航台在用的那家）
- pilot.llm.provider_key.<id>  供应商 API Key（逐家独立派生加密，写时即密文）
- pilot.llm.enabled            bool，LLM 解读总开关（全局；关闭时领航台仍直出模板化事实摘要）
- pilot.llm.model              当前选用模型名（属于活跃供应商的清单）
- pilot.llm.monthly_cap        单月 token 用量上限（全局；0 = 不限，仅统计不熔断）
- pilot.ctx.tool_content_limit 历史轮工具结果保留预算（字符；头尾保留、中段省略）
- pilot.usage.<YYYYMM>         当月已用 token 累计（用量台账，跨月自然换键）

存量单配置由数据库升级一次迁入供应商账本并清源；load_config 输出保持平面
形态（protocol/base_url/api_key/model/models 解析自活跃供应商），消费方无感。
协议：openai（兼容生态）/ openai-responses / anthropic / ollama。
"""
from __future__ import annotations

from uuid import uuid4

from app.core.database import WritePriority, write_gate
from app.crawler.utils import get_beijing_time_obj
from app.domains.pilot import llm as pilot_llm
from app.domains.pilot import model_windows as MODEL_WINDOWS
from app.domains.settings import service as settings_service

DEFAULT_MONTHLY_CAP = 0
# 存量库里的旧默认上限：用于把已迁移为不限（0）的月度用量识别出来
_LEGACY_DEFAULT_CAP = 500_000

_ENABLED_KEY = "pilot.llm.enabled"
_MODEL_KEY = "pilot.llm.model"
_CAP_KEY = "pilot.llm.monthly_cap"
_PROVIDERS_KEY = "pilot.llm.providers"
_ACTIVE_KEY = "pilot.llm.active"
# 窗口上限：防手滑填爆（1M 级窗口已覆盖现役模型；未知填 0/清空即回落固定预算）
_CONTEXT_WINDOW_MAX = 2_000_000

# 工具输出留在上下文里的保留预算（字符，头尾保留、中段省略）：
# 历史轮里超长工具结果按它裁剪——调大更保真但更快撞上下文预算。
_TOOL_LIMIT_KEY = "pilot.ctx.tool_content_limit"
TOOL_CONTENT_LIMIT_DEFAULT = 3000
TOOL_CONTENT_LIMIT_MIN = 500
TOOL_CONTENT_LIMIT_MAX = 20000


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


def _clean_tool_limit(raw) -> int:
    """工具输出预算清洗：夹在 [MIN, MAX]；非法/非正回落默认。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return TOOL_CONTENT_LIMIT_DEFAULT
    if value <= 0:
        return TOOL_CONTENT_LIMIT_DEFAULT
    return max(TOOL_CONTENT_LIMIT_MIN, min(TOOL_CONTENT_LIMIT_MAX, value))


def _resolve_window(manual: int | None, auto: int | None) -> tuple[int | None, str]:
    """生效窗口 = 手填优先，其次模型名识别，都没有则按兜底窗口估算。

    手填的高于识别值：用户可能知道自家网关改造过窗口，而模型名只认得通用
    型号；识别只负责「用户没说话时给个合理的数」。表外模型不再回落固定
    预算——现代主流模型窗口都很大，按宽裕口径兜底比保守少给更接近真实，
    source 标为 fallback 供前端提示「这是估算值，可手填覆盖」。"""
    if manual:
        return manual, "manual"
    if auto:
        return auto, "auto"
    return MODEL_WINDOWS.FALLBACK_CONTEXT_WINDOW, "fallback"


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
    """读取唯一供应商账本；返回体不带密钥原文，带 has_key。"""
    raw = await settings_service.get_value(_PROVIDERS_KEY, [])
    providers = [_clean_provider(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    return [await _with_key_flag(p) for p in providers]


async def _with_key_flag(provider: dict) -> dict:
    stored = await settings_service.get_value(_provider_key_name(provider["id"]), None)
    return {**provider, "has_key": bool(stored)}


def _active_model(provider: dict, model: str) -> str:
    disabled = set(provider["models_disabled"])
    enabled = [m for m in provider["models"] if m not in disabled]
    return enabled[0] if enabled and model not in enabled else model


async def create_provider(record: dict) -> dict:
    """新增供应商，清单、密钥与首次激活在同一写边界提交。"""
    async with write_gate(WritePriority.INTERACTIVE, label="pilot_config"):
        provider = _clean_provider({**record, "id": uuid4().hex[:12]})
        providers = await load_providers()
        values = {_PROVIDERS_KEY: [*providers, provider]}
        if not providers:
            model = str(await settings_service.get_value(_MODEL_KEY, "") or "")
            values.update({_ACTIVE_KEY: provider["id"], _MODEL_KEY: _active_model(provider, model)})
        secrets = {_provider_key_name(provider["id"]): record["api_key"]} if record.get("api_key") else {}
        await settings_service.set_values(values, secret_values=secrets)
        return await _with_key_flag(provider)


async def update_provider(pid: str, update: dict) -> dict:
    """更新供应商；密钥缺省不改，空值清空；空地址不覆盖已生效地址。"""
    async with write_gate(WritePriority.INTERACTIVE, label="pilot_config"):
        providers = await load_providers()
        target = next((p for p in providers if p["id"] == pid), None)
        if target is None:
            raise LookupError(f"provider not found: {pid}")
        patch = {k: v for k, v in update.items() if k not in {"api_key", "id"}}
        if not str(patch.get("base_url") or "").strip() and target["base_url"]:
            patch["base_url"] = target["base_url"]
        merged = _clean_provider({**target, **patch})
        providers[providers.index(target)] = merged
        values = {_PROVIDERS_KEY: providers}
        active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
        if pid == active_id or not active_id:
            model = str(await settings_service.get_value(_MODEL_KEY, "") or "")
            values[_MODEL_KEY] = _active_model(merged, model)
        secrets = {_provider_key_name(pid): update["api_key"]} if "api_key" in update else {}
        await settings_service.set_values(values, secret_values=secrets)
        return await _with_key_flag(merged)


async def delete_provider(pid: str) -> None:
    """删除供应商及密钥；删活跃家时交接首家，无则清空当前模型。"""
    async with write_gate(WritePriority.INTERACTIVE, label="pilot_config"):
        providers = await load_providers()
        remaining = [p for p in providers if p["id"] != pid]
        values = {_PROVIDERS_KEY: remaining, _provider_key_name(pid): ""}
        active = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
        if active == pid:
            values[_ACTIVE_KEY] = remaining[0]["id"] if remaining else ""
            if not remaining:
                values[_MODEL_KEY] = ""
        await settings_service.set_values(values)


async def set_active_provider(pid: str) -> dict:
    """切换活跃家；仅在启用模型清单非空时回落首个模型。"""
    async with write_gate(WritePriority.INTERACTIVE, label="pilot_config"):
        providers = await load_providers()
        target = next((p for p in providers if p["id"] == pid), None)
        if target is None:
            raise LookupError(f"provider not found: {pid}")
        model = str(await settings_service.get_value(_MODEL_KEY, "") or "").strip()
        stored = _active_model(target, model)
        await settings_service.set_values({_ACTIVE_KEY: pid, _MODEL_KEY: stored})
        return {"active": pid, "model": stored}


async def load_config() -> dict:
    """平面配置（消费方形态不变）：解析自活跃供应商 + 全局开关/上限/台账。"""
    cap_raw = await settings_service.get_value(_CAP_KEY, DEFAULT_MONTHLY_CAP)
    try:
        cap = max(0, int(cap_raw))
    except (TypeError, ValueError):
        cap = DEFAULT_MONTHLY_CAP
    if cap == _LEGACY_DEFAULT_CAP:
        # 旧默认值不是用户的选择：一次性迁为不限，让「默认移除 token 上限」对存量库生效
        cap = 0
        await settings_service.set_value(_CAP_KEY, 0)
    providers = await load_providers()
    active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
    active = next((p for p in providers if p["id"] == active_id), providers[0] if providers else None)
    api_key = ""
    if active:
        api_key = str(await settings_service.get_secret_value(_provider_key_name(active["id"]), "") or "")
    model = str(await settings_service.get_value(_MODEL_KEY, "") or "").strip()
    stored_window = active["context_window"] if active else None
    # 手填优先，没手填就按模型名问内置表——填了 a 家地址却忘了窗口也能工作
    auto_window = MODEL_WINDOWS.guess_context_window(model)
    window, window_source = _resolve_window(stored_window, auto_window)
    return {
        "protocol": active["protocol"] if active else "openai",
        "enabled": bool(await settings_service.get_value(_ENABLED_KEY, False)),
        "base_url": active["base_url"] if active else "",
        "model": str(await settings_service.get_value(_MODEL_KEY, "") or "").strip(),
        "models": active["models"] if active else [],
        "models_disabled": active["models_disabled"] if active else [],
        "api_key": api_key,
        "monthly_cap": cap,
        "context_window": window,
        "context_window_source": window_source,
        "context_window_auto": auto_window,
        "active": active["id"] if active else "",
        "providers": providers,
        "tool_content_limit": _clean_tool_limit(await settings_service.get_value(_TOOL_LIMIT_KEY, None)),
    }


async def save_config(update: dict) -> None:
    """平面 API 的供方和全局配置在原设置账本一次提交，首次保存创建默认接入。"""
    async with write_gate(WritePriority.INTERACTIVE, label="pilot_config"):
        values = {}
        secrets = {}
        patch = {key: update[key] for key in (
            "protocol", "base_url", "models", "models_disabled", "context_window",
        ) if key in update and (key not in {"models", "models_disabled"} or isinstance(update[key], list))}
        providers = await load_providers()
        active_id = str(await settings_service.get_value(_ACTIVE_KEY, "") or "")
        selected = next((p for p in providers if p["id"] == str(update.get("active") or "")), None)
        if selected:
            active_id = selected["id"]
            values[_ACTIVE_KEY] = active_id
        if patch or "api_key" in update or selected:
            if not providers:
                providers = [_clean_provider({"id": uuid4().hex[:12], "name": "默认接入"})]
            target = next((p for p in providers if p["id"] == active_id), providers[0])
            merged = _clean_provider({**target, **patch})
            providers[providers.index(target)] = merged
            model = str(await settings_service.get_value(_MODEL_KEY, "") or "").strip()
            values.update({_PROVIDERS_KEY: providers, _ACTIVE_KEY: merged["id"],
                           _MODEL_KEY: _active_model(merged, model)})
            if "api_key" in update:
                secrets[_provider_key_name(merged["id"])] = update["api_key"]
        if "enabled" in update:
            values[_ENABLED_KEY] = bool(update["enabled"])
        if "model" in update:
            values[_MODEL_KEY] = str(update["model"] or "").strip()
        if "monthly_cap" in update:
            try:
                values[_CAP_KEY] = max(0, int(update["monthly_cap"]))
            except (TypeError, ValueError):
                values[_CAP_KEY] = DEFAULT_MONTHLY_CAP
        if "tool_content_limit" in update:
            values[_TOOL_LIMIT_KEY] = _clean_tool_limit(update["tool_content_limit"])
        await settings_service.set_values(values, secret_values=secrets)


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
    return cap > 0 and usage["total"] >= cap


def llm_ready(cfg: dict) -> bool:
    """开关、地址、模型齐备即可用；密钥可选（匿名免费端点不要求密钥）。"""
    return bool(cfg["enabled"] and cfg["base_url"] and cfg["model"])
