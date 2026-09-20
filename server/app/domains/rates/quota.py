"""Provider 配额账本（fx_provider_usage）：把「每月 100 次」变成系统状态。

响应 headers 不暴露月度余量（Provider 只暴露 1 req/s 级限速），本地记账是唯一手段。
计量口径：每个 HTTP 请求记 1 单位（timeframe 一次调用记 1 次，与覆盖天数无关）。
限额可配置：settings KV `rates.history_monthly_limit`（默认 100）——默认值不是
业务规则，换订阅档/换 Provider 时按配置跟随。
Key 明文只留在 `secrets/fx_maintenance.env`；库里只存哈希指纹。
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from .models import FxProviderUsage

logger = logging.getLogger(__name__)

DEFAULT_MONTHLY_LIMIT = 100
LIMIT_SETTING_KEY = "rates.history_monthly_limit"


def current_period() -> str:
    """当前账期（北京时间月，YYYY-MM）。"""
    return get_beijing_time_obj().strftime("%Y-%m")


def key_fingerprint(api_key: str) -> str:
    """Key 的存储指纹（sha256 前 16 位）——库里不落明文。"""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


async def monthly_limit() -> int:
    """账期请求上限（settings 可配；非法值回落默认，不硬编码进业务规则）。"""
    try:
        from app.domains.settings.service import get_value

        raw = await get_value(LIMIT_SETTING_KEY, DEFAULT_MONTHLY_LIMIT)
    except Exception:  # noqa: BLE001 —— 设置读失败不阻断判定（用默认）
        return DEFAULT_MONTHLY_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MONTHLY_LIMIT
    return value if value > 0 else DEFAULT_MONTHLY_LIMIT


def _row_to_dict(row: FxProviderUsage | None, provider: str, key_fp: str, period: str) -> dict:
    if row is None:
        return {
            "provider": provider,
            "keyFingerprint": key_fp,
            "period": period,
            "requestCount": 0,
            "requestLimit": None,
            "remaining": None,
            "lastRequestedAt": None,
            "lastError": None,
        }
    return {
        "provider": row.provider,
        "keyFingerprint": row.key_fingerprint,
        "period": row.period,
        "requestCount": row.request_count or 0,
        "requestLimit": row.request_limit,
        "remaining": row.last_remaining,
        "lastRequestedAt": row.last_requested_at.isoformat() if row.last_requested_at else None,
        "lastError": row.last_error,
    }


async def get_usage(provider: str, key_fp: str, *, period: str | None = None) -> dict:
    """账期用量（不存在时返回零值行）。"""
    period = period or current_period()
    async with get_session_factory()() as session:
        row = await session.get(FxProviderUsage, (provider, key_fp, period))
        return _row_to_dict(row, provider, key_fp, period)


async def can_request(provider: str, key_fp: str, *, limit: int | None = None) -> bool:
    """quota guard：账期剩余 > 0 才放行（剩余 0 绝不触网）。"""
    limit = await monthly_limit() if limit is None else limit
    usage = await get_usage(provider, key_fp)
    return usage["requestCount"] < limit


async def record_request(
    provider: str,
    key_fp: str,
    *,
    limit: int | None = None,
    error: str | None = None,
    status: str = "ok",
) -> dict:
    """记一笔请求（成功/失败都记——失败也可能已被平台计数）。

    `last_remaining` 存**本地推算的账期剩余**（Provider 不上报余量），供
    界面/脚本展示；`last_error` 成功时清空。
    """
    limit = await monthly_limit() if limit is None else limit
    period = current_period()
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        stmt = sqlite_insert(FxProviderUsage).values(
            provider=provider,
            key_fingerprint=key_fp,
            period=period,
            request_count=1,
            request_limit=limit,
            last_requested_at=now,
            last_remaining=limit - 1,
            last_error=None if status == "ok" else error,
        )
        set_: dict = {
            "request_count": FxProviderUsage.request_count + 1,
            "request_limit": limit,
            "last_requested_at": now,
            "last_remaining": limit - (FxProviderUsage.request_count + 1),
            "last_error": None if status == "ok" else error,
        }
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    FxProviderUsage.provider,
                    FxProviderUsage.key_fingerprint,
                    FxProviderUsage.period,
                ],
                set_=set_,
            )
        )
        await session.commit()
        row = await session.get(FxProviderUsage, (provider, key_fp, period))
        result = _row_to_dict(row, provider, key_fp, period)
    logger.info(
        "[quota] %s %s 账期 %s：%d/%d（剩余 %s）%s",
        provider, key_fp, period, result["requestCount"], limit,
        result["remaining"], f"错误：{error}" if error else "",
    )
    return result


async def quota_status(provider: str, key_fp: str) -> dict:
    """账期状态（脚本 `quota` 子命令展示用）。"""
    limit = await monthly_limit()
    usage = await get_usage(provider, key_fp)
    return {**usage, "requestLimit": limit, "exhausted": usage["requestCount"] >= limit}
