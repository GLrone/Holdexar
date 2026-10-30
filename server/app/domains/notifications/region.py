"""通知地区与币种来源解析。"""
from __future__ import annotations

from app.domains.games.pricing import REGION_TO_CURRENCY


def resolve_region(*, explicit: str | None = None, prefs: dict | None = None,
                   observed: dict | None = None) -> dict:
    region = str(explicit or (prefs or {}).get("manualRegion") or "").strip().upper()
    if not region:
        return {"region": None, "source": "unknown", "currency": None, "currencySource": "unknown"}
    if region not in REGION_TO_CURRENCY:
        raise ValueError("unsupported notification region")
    currency = str((observed or {}).get("currency") or "").strip().upper()
    observed_region = str((observed or {}).get("region") or (observed or {}).get("region_code") or "").strip().upper()
    use_observed = bool(currency and observed_region == region)
    return {"region": region, "source": "rule" if explicit else "manual",
            "currency": currency if use_observed else REGION_TO_CURRENCY[region],
            "currencySource": "observation" if use_observed else "region_mapping"}
