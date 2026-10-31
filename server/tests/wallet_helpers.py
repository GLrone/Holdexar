"""钱包测试夹具：分单位原值 → WalletInfo 构造器。

生产侧同名构造已随 JSON 通道退役删除（HTML 解析路径内联同规则）；
本夹具只服务 fake 数据构造，/100 与未知币种规则由生产路径测试钉住。
"""
from __future__ import annotations

from app.domains.account.steam_wallet import (
    NO_DIVIDE_CURRENCY_IDS,
    STEAM_CURRENCY_IDS,
    UnknownCurrencyError,
    WalletInfo,
    _normalize_country,
)


def from_raw_amounts(
    balance_raw: int, delayed_raw: int, currency_id: int, country: str
) -> WalletInfo:
    """Steam 返回的分单位原值 → WalletInfo（含 /100 规则）。"""
    info = STEAM_CURRENCY_IDS.get(currency_id)
    if info is None:
        raise UnknownCurrencyError(f"未知 Steam 钱包币种 ID: {currency_id}")
    code, symbol = info
    total_fen = balance_raw + delayed_raw
    amount = float(total_fen) if currency_id in NO_DIVIDE_CURRENCY_IDS else total_fen / 100.0
    return WalletInfo(
        balance=round(amount, 2),
        currency_code=code,
        currency_symbol=symbol,
        currency_id=currency_id,
        country_code=_normalize_country(country),
    )
