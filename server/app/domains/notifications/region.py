"""通知地区与币种来源解析。"""
from __future__ import annotations

from app.domains.games.pricing import REGION_TO_CURRENCY


def resolve_region(*, explicit: str | None = None, prefs: dict | None = None,
                   fallback: str | None = None, observed: dict | None = None) -> dict:
    region = str(explicit or (prefs or {}).get("manualRegion") or "").strip().upper()
    source = "rule" if explicit else "manual"
    if not region:
        # 回退区来自钱包快照（机器派生），不在区币映射内时不作数，而不是报错
        account_region = str(fallback or "").strip().upper()
        if account_region in REGION_TO_CURRENCY:
            region, source = account_region, "account"
    if not region:
        return {"region": None, "source": "unknown", "currency": None, "currencySource": "unknown"}
    if region not in REGION_TO_CURRENCY:
        raise ValueError("unsupported notification region")
    currency = str((observed or {}).get("currency") or "").strip().upper()
    observed_region = str((observed or {}).get("region") or (observed or {}).get("region_code") or "").strip().upper()
    use_observed = bool(currency and observed_region == region)
    return {"region": region, "source": source,
            "currency": currency if use_observed else REGION_TO_CURRENCY[region],
            "currencySource": "observation" if use_observed else "region_mapping"}


async def effective_region(*, explicit: str | None = None, prefs: dict | None = None,
                           observed: dict | None = None) -> dict:
    """完整解析链出口：显式指定 → 手动通知地区 → 主账号钱包地区 → 未知。

    通知判断、预览与偏好视图必须共用本入口，保证地区来源一致。
    """
    from app.domains.account import service as account_service

    fallback = ""
    try:
        fallback = await account_service.primary_wallet_region()
    except Exception:  # noqa: BLE001 — 账号域异常不阻断通知地区解析
        pass
    return resolve_region(explicit=explicit, prefs=prefs, fallback=fallback, observed=observed)
