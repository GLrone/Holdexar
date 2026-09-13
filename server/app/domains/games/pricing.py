"""games 域定价工具。"""
from __future__ import annotations

from app.crawler.config import CC_LIST

# cc 大写 → 货币
REGION_TO_CURRENCY: dict[str, str] = {
    code.upper(): currency for code, _, currency in CC_LIST
}

# 所有币种存储统一 2 位小数（cents），但显示小数位不同
DISPLAY_DECIMALS: dict[str, int] = {
    "CLP": 0, "VND": 0, "IDR": 0, "JPY": 0, "KZT": 0, "UAH": 0,
    "CNY": 2, "USD": 2, "RUB": 2, "INR": 2, "BRL": 2, "HKD": 2,
    "PHP": 2, "TWD": 2,
}

CURRENCY_SYMBOLS: dict[str, str] = {
    "CNY": "¥", "RUB": "₽", "KZT": "₸", "UAH": "₴",
    "USD": "$", "VND": "₫", "IDR": "Rp", "INR": "₹",
    "BRL": "R$", "CLP": "$", "JPY": "¥", "HKD": "HK$", "PHP": "₱",
    "TWD": "NT$", "KWD": "KD", "SAR": "SR", "ZAR": "R", "QAR": "QR",
    "MYR": "RM", "THB": "฿", "PEN": "S/", "MXN": "MX$", "SGD": "S$",
    "AED": "AED", "UYU": "$U", "COP": "COL$", "KRW": "₩", "NZD": "NZ$",
    "PLN": "zł", "CRC": "₡", "CAD": "C$", "AUD": "A$", "EUR": "€",
    "GBP": "£", "NOK": "kr", "ILS": "₪", "CHF": "CHF",
}

# 符号后置的货币（如 "55.00 ₽" / "119,00 zł" / "149,00 kr"）
_SUFFIX_CURRENCIES = {"RUB", "KZT", "UAH", "PLN", "NOK"}

# 汇率兜底表（fx_rates 表缺失时使用；来源 constants.ts DEFAULT_EXCHANGE_RATES）
DEFAULT_EXCHANGE_RATES: dict[str, float] = {
    "CNY": 1.0, "USD": 7.25, "RUB": 0.079, "KZT": 0.015,
    "UAH": 0.175, "TRY": 0.22, "ARS": 0.0083, "VND": 0.00029,
    "IDR": 0.00046, "INR": 0.087, "BRL": 1.42, "CLP": 0.0076,
    "JPY": 0.048, "HKD": 0.928, "PHP": 0.126, "AZN": 4.26,
    "TWD": 0.21, "KWD": 3.31, "SAR": 1.93, "ZAR": 0.38,
    "QAR": 1.97, "MYR": 1.55, "THB": 0.20, "PEN": 2.12,
    "MXN": 0.42, "SGD": 5.40, "AED": 1.97, "UYU": 0.21,
    "COP": 0.0018, "KRW": 0.0052, "NZD": 4.50, "PLN": 2.15,
    "CRC": 0.13, "CAD": 5.20, "AUD": 4.70, "EUR": 7.90,
    "GBP": 9.20, "NOK": 0.66, "ILS": 1.95, "CHF": 8.10,
}


def format_minor_units(amount_minor: int | None, currency_code: str) -> str:
    """cents → 本币字符串（如 "¥42.00" / "225.00 ₴" / "₫299,000"）。"""
    if amount_minor is None:
        return ""
    currency = currency_code.upper()
    symbol = CURRENCY_SYMBOLS.get(currency, "")
    decimals = DISPLAY_DECIMALS.get(currency, 2)
    major = amount_minor / 100
    if decimals == 0:
        formatted = f"{round(major):,}"
    else:
        formatted = f"{major:,.{decimals}f}"
    if currency in _SUFFIX_CURRENCIES:
        return f"{formatted} {symbol}".strip()
    return f"{symbol}{formatted}"


def format_cny_fen(fen: int | None) -> str:
    if fen is None:
        return "—"
    return f"¥{fen / 100:.2f}"


def convert_minor_to_cny_fen(amount_minor: int, currency: str, rates: dict[str, float]) -> int | None:
    rate = rates.get(currency.upper())
    if rate is None:
        return None
    return round(amount_minor * rate)


def build_steam_header_url(appid: int) -> str:
    return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"


def build_steam_store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"
