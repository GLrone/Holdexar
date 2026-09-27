"""Bing currencyapi Provider 适配器离线验收。

覆盖：内嵌脚本解析（graphData 括号配对、字符串内花括号不干扰）、同日多点
去重（保留最晚）、档位选择（按窗口起始日锚定今天）、日线档周末 carry、
周线/月线档只落采样日、CNY 本位不请求恒 1.0、单币失败不拖垮整批、
档位级缓存（同档位二窗零请求）。全部离线（_get 打桩）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.rates.providers import bing_currency as bing  # noqa: E402
from app.domains.rates.providers.bing_currency import ProviderError  # noqa: E402


TZ = timezone(timedelta(hours=8))
TODAY = date(2026, 9, 26)  # 周六


def _ms(d: date, hour: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=TZ).timestamp() * 1000)


def _body(points: list[dict]) -> str:
    return (
        '<script type="text/javascript" nonce="n">//<![CDATA[\n"use strict";\n'
        "var Finance; (function (Finance) { Finance.graphData ="
        + json.dumps({"Points": [points], "Meta": 'braces {"inside": 1} string'})
        + ";;Finance.graph.initializeDataAndDrawGraphs(Finance.graphData);;\n//]]></script>"
    )


def _pt(d: date, rate: float, hour: int = 0) -> dict:
    return {"XLabel": d.isoformat(), "XLong": _ms(d, hour), "XIndex": 0, "Y": rate}


def _provider(monkeypatch: pytest.MonkeyPatch, bodies: dict[str, str], today=None):
    """打桩 _get：按 symbol 返回脚本体，记录请求。"""
    calls: list[str] = []
    provider = bing.BingCurrencyProvider(today=today or TODAY)

    async def _fake_get(symbol: str, chart: int) -> str:
        calls.append(f"{symbol}#{chart}")
        if symbol not in bodies:
            raise ProviderError(f"无桩数据：{symbol}")
        return bodies[symbol]

    monkeypatch.setattr(provider, "_get", _fake_get)
    return provider, calls


# ── 纯函数 ────────────────────────────────────────────────


def test_extract_graphdata_tolerates_braces_in_strings():
    body = _body([_pt(date(2026, 9, 25), 0.043)])
    graph = bing._extract_graphdata(body)
    assert graph is not None
    assert graph["Meta"] == 'braces {"inside": 1} string'
    assert graph["Points"][0][0]["Y"] == pytest.approx(0.043)


def test_extract_graphdata_missing_marker():
    assert bing._extract_graphdata("<script>var Other = {};</script>") is None


def test_select_chart_anchored_to_today():
    today = date(2026, 9, 26)
    assert bing._select_chart(date(2026, 8, 1), today) == bing.CHART_DAILY
    assert bing._select_chart(date(2024, 1, 1), today) == bing.CHART_WEEKLY
    assert bing._select_chart(date(2013, 1, 1), today) == bing.CHART_MONTHLY


# ── fetch_timeframe ──────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_chart_carries_weekends(monkeypatch):
    """日线档：业务日点之间与之后的休市日延续前一采样值。"""
    fri, mon = date(2026, 9, 25), date(2026, 9, 21)
    provider, calls = _provider(
        monkeypatch, {"JPYCNY": _body([_pt(mon, 0.0430), _pt(fri, 0.0435)])}
    )
    out = await provider.fetch_timeframe(
        date(2026, 9, 23), date(2026, 9, 27), ["JPY"]
    )
    assert out[date(2026, 9, 23)]["JPY"] == pytest.approx(0.0430)  # 周三 ← 周一采样
    assert out[date(2026, 9, 25)]["JPY"] == pytest.approx(0.0435)
    assert out[date(2026, 9, 26)]["JPY"] == pytest.approx(0.0435)  # 周六 ← 周五
    assert out[date(2026, 9, 27)]["JPY"] == pytest.approx(0.0435)  # 周日 ← 周五
    assert calls == ["JPYCNY#6"]


@pytest.mark.asyncio
async def test_monthly_chart_samples_only(monkeypatch):
    """月线档：只落采样日，中间日不造 observed 数据。"""
    provider, calls = _provider(
        monkeypatch,
        {"USDCNY": _body([_pt(date(2020, 1, 31), 7.10), _pt(date(2020, 2, 29), 7.20)])},
    )
    out = await provider.fetch_timeframe(
        date(2020, 1, 1), date(2020, 3, 31), ["USD"]
    )
    assert set(out) == {date(2020, 1, 31), date(2020, 2, 29)}
    assert out[date(2020, 1, 31)]["USD"] == pytest.approx(7.10)
    assert calls == ["USDCNY#9"]


@pytest.mark.asyncio
async def test_cny_is_constant_and_never_requested(monkeypatch):
    provider, calls = _provider(monkeypatch, {"USDCNY": _body([_pt(date(2026, 9, 25), 7.0)])})
    out = await provider.fetch_timeframe(date(2026, 9, 25), date(2026, 9, 25), ["CNY", "USD"])
    assert out[date(2026, 9, 25)]["CNY"] == 1.0
    assert calls == ["USDCNY#6"]  # CNY 本位不请求


@pytest.mark.asyncio
async def test_same_day_points_keep_latest(monkeypatch):
    """同日多点（近端小时级采样）保留最晚一条。"""
    provider, _ = _provider(
        monkeypatch,
        {
            "USDCNY": _body(
                [
                    _pt(date(2026, 9, 25), 7.00, hour=2),
                    _pt(date(2026, 9, 25), 7.01, hour=10),
                ]
            )
        },
    )
    out = await provider.fetch_timeframe(date(2026, 9, 25), date(2026, 9, 25), ["USD"])
    assert out[date(2026, 9, 25)]["USD"] == pytest.approx(7.01)


@pytest.mark.asyncio
async def test_single_symbol_failure_tolerated(monkeypatch):
    """个别币种响应异常不判整体失败：其余币种照常产出，缺口保留。"""
    provider, _ = _provider(
        monkeypatch,
        {
            "USDCNY": _body([_pt(date(2026, 9, 25), 7.0)]),
            "EURCNY": "<script>no graph data here</script>",
        },
    )
    out = await provider.fetch_timeframe(date(2026, 9, 25), date(2026, 9, 25), ["USD", "EUR"])
    assert out[date(2026, 9, 25)]["USD"] == pytest.approx(7.0)
    assert "EUR" not in out[date(2026, 9, 25)]


@pytest.mark.asyncio
async def test_all_symbols_failure_raises(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {"USDCNY": "<script>broken</script>", "EURCNY": "<script>broken</script>"},
    )
    with pytest.raises(ProviderError):
        await provider.fetch_timeframe(date(2026, 9, 25), date(2026, 9, 25), ["USD", "EUR"])


@pytest.mark.asyncio
async def test_chart_cache_reuses_between_windows(monkeypatch):
    """档位级缓存：同档位的第二个窗口零请求（一次修复跑只拉一遍）。"""
    provider, calls = _provider(
        monkeypatch, {"USDCNY": _body([_pt(date(2026, 9, 25), 7.0)])}
    )
    await provider.fetch_timeframe(date(2026, 9, 20), date(2026, 9, 25), ["USD"])
    await provider.fetch_timeframe(date(2026, 9, 22), date(2026, 9, 24), ["USD"])
    assert calls == ["USDCNY#6"]


@pytest.mark.asyncio
async def test_window_before_history_returns_empty(monkeypatch):
    """窗口早于该币种首批采样：无行可写，不报错不造数。"""
    provider, _ = _provider(monkeypatch, {"USDCNY": _body([_pt(date(2012, 1, 31), 6.3)])})
    out = await provider.fetch_timeframe(date(2011, 1, 1), date(2011, 6, 30), ["USD"])
    assert out == {}
