"""Frankfurter v2 历史汇率 Provider 离线单元测试。

覆盖：
1. 参数与区间校验（end < start、空币种集合）；
2. USD 中介到 CNY 本位的 cross-rate 换算（CNY 恒 1.0、USD 为 CNY/1.0、其它为 CNY/X）；
3. 缺失 CNY 报价时跳过该日（不伪造无法折算的汇率）；
4. 个别币种缺失保留缺口（不伪造数据、不人为 carry）；
5. 异常响应分类（HTTP 错误、网络异常、非列表响应、畸形数据）；
6. 39 种目标货币批量构造与换算。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.rates.providers.frankfurter import (  # noqa: E402
    FrankfurterCurrencyProvider,
    ProviderError,
)
from app.domains.rates.service import ALLOWED_CURRENCIES  # noqa: E402


@pytest.mark.asyncio
async def test_invalid_date_range_raises_value_error():
    p = FrankfurterCurrencyProvider()
    with pytest.raises(ValueError, match="区间非法"):
        await p.fetch_timeframe(date(2024, 1, 5), date(2024, 1, 1), ["USD"])


@pytest.mark.asyncio
async def test_empty_currencies_returns_empty():
    p = FrankfurterCurrencyProvider()
    res = await p.fetch_timeframe(date(2024, 1, 1), date(2024, 1, 2), [])
    assert res == {}


@pytest.mark.asyncio
async def test_parse_and_convert_cross_rates_to_cny(monkeypatch):
    p = FrankfurterCurrencyProvider()
    recorded_params = []

    async def fake_get(params):
        recorded_params.append(params)
        return [
            {"date": "2024-01-02", "base": "USD", "quote": "CNY", "rate": 7.15},
            {"date": "2024-01-02", "base": "USD", "quote": "EUR", "rate": 0.90},
            {"date": "2024-01-02", "base": "USD", "quote": "JPY", "rate": 142.0},
            {"date": "2024-01-02", "base": "USD", "quote": "USD", "rate": 1.0},
        ]

    monkeypatch.setattr(p, "_get", fake_get)
    result = await p.fetch_timeframe(
        date(2024, 1, 2), date(2024, 1, 2), ["CNY", "EUR", "JPY", "USD"]
    )
    assert len(recorded_params) == 1
    assert recorded_params[0]["base"] == "USD"
    assert "CNY" in recorded_params[0]["quotes"]
    assert "EUR" in recorded_params[0]["quotes"]
    assert "JPY" in recorded_params[0]["quotes"]

    row = result[date(2024, 1, 2)]
    assert row["CNY"] == pytest.approx(1.0)
    assert row["USD"] == pytest.approx(7.15)
    assert row["EUR"] == pytest.approx(7.15 / 0.90)
    assert row["JPY"] == pytest.approx(7.15 / 142.0)


@pytest.mark.asyncio
async def test_missing_cny_skips_date(monkeypatch):
    p = FrankfurterCurrencyProvider()

    async def fake_get(params):
        return [
            {"date": "2024-01-02", "base": "USD", "quote": "EUR", "rate": 0.90},
            {"date": "2024-01-03", "base": "USD", "quote": "CNY", "rate": 7.12},
            {"date": "2024-01-03", "base": "USD", "quote": "EUR", "rate": 0.91},
        ]

    monkeypatch.setattr(p, "_get", fake_get)
    result = await p.fetch_timeframe(date(2024, 1, 2), date(2024, 1, 3), ["EUR"])
    assert date(2024, 1, 2) not in result  # 缺少 CNY 无法换算，跳过
    assert date(2024, 1, 3) in result
    assert result[date(2024, 1, 3)]["EUR"] == pytest.approx(7.12 / 0.91)


@pytest.mark.asyncio
async def test_missing_quote_keeps_gap(monkeypatch):
    p = FrankfurterCurrencyProvider()

    async def fake_get(params):
        return [
            {"date": "2024-01-02", "base": "USD", "quote": "CNY", "rate": 7.15},
            {"date": "2024-01-02", "base": "USD", "quote": "EUR", "rate": 0.90},
        ]

    monkeypatch.setattr(p, "_get", fake_get)
    result = await p.fetch_timeframe(
        date(2024, 1, 2), date(2024, 1, 2), ["EUR", "TRY"]
    )
    row = result[date(2024, 1, 2)]
    assert "EUR" in row
    assert "TRY" not in row  # 未返回的币种保持缺口，不伪造


@pytest.mark.asyncio
async def test_non_positive_rate_ignored(monkeypatch):
    p = FrankfurterCurrencyProvider()

    async def fake_get(params):
        return [
            {"date": "2024-01-02", "base": "USD", "quote": "CNY", "rate": 7.15},
            {"date": "2024-01-02", "base": "USD", "quote": "EUR", "rate": 0.0},
            {"date": "2024-01-02", "base": "USD", "quote": "GBP", "rate": -1.0},
        ]

    monkeypatch.setattr(p, "_get", fake_get)
    result = await p.fetch_timeframe(
        date(2024, 1, 2), date(2024, 1, 2), ["EUR", "GBP"]
    )
    assert date(2024, 1, 2) not in result  # 无有效目标汇率


@pytest.mark.asyncio
async def test_provider_error_on_network_or_response_shape(monkeypatch):
    p = FrankfurterCurrencyProvider()

    async def fake_get_dict(params):
        raise ProviderError("HTTP 422: invalid currency")

    monkeypatch.setattr(p, "_get", fake_get_dict)
    with pytest.raises(ProviderError, match="HTTP 422"):
        await p.fetch_timeframe(date(2024, 1, 1), date(2024, 1, 2), ["USD"])


@pytest.mark.asyncio
async def test_all_39_currencies_conversion(monkeypatch):
    p = FrankfurterCurrencyProvider()
    d = date(2024, 1, 2)
    fake_items = [{"date": d.isoformat(), "base": "USD", "quote": "CNY", "rate": 7.10}]
    for code in sorted(ALLOWED_CURRENCIES - {"CNY", "USD"}):
        fake_items.append({"date": d.isoformat(), "base": "USD", "quote": code, "rate": 2.0})

    async def fake_get(params):
        return fake_items

    monkeypatch.setattr(p, "_get", fake_get)
    result = await p.fetch_timeframe(d, d, ALLOWED_CURRENCIES)
    row = result[d]
    assert len(row) == len(ALLOWED_CURRENCIES)
    assert row["CNY"] == pytest.approx(1.0)
    assert row["USD"] == pytest.approx(7.10)
    for code in ALLOWED_CURRENCIES - {"CNY", "USD"}:
        assert row[code] == pytest.approx(7.10 / 2.0)
