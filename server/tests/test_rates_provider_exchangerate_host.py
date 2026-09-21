"""exchangerate.host Provider 适配器离线验收。

覆盖：quotes 解析与 USD 交叉换算、请求参数形态（currencies 而非 symbols、
base 锁 USD）、窗口上限、错误分类（101 → auth / 104、429 → quota）、
HTTP 层失败路径（缺 key 返回 200 + success:false 也算失败）、1 req/s 节流。
全部离线（_get 打桩 / httpx MockTransport），不消耗真实配额。
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.domains.rates.http as rates_http  # noqa: E402
from app.domains.rates.providers import exchangerate_host as erh  # noqa: E402


def _provider() -> erh.ExchangerateHostProvider:
    return erh.ExchangerateHostProvider("test-key")


# ── 纯函数 ────────────────────────────────────────────────


def test_cross_to_cny_math():
    quotes = {"USDCNY": 6.5, "USDKZT": 500.0, "USDJPY": 150.0, "USDEUR": 0.0}
    out = erh._cross_to_cny(quotes, {"KZT", "JPY", "EUR", "CNY"})
    assert out["CNY"] == 1.0
    assert out["KZT"] == pytest.approx(6.5 / 500.0)
    assert out["JPY"] == pytest.approx(6.5 / 150.0)
    assert "EUR" not in out  # 值为 0 → 跳过
    assert "USD" not in out


def test_cross_to_cny_without_usdcny_is_empty():
    assert erh._cross_to_cny({"USDKZT": 500.0}, {"KZT"}) == {}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"error": {"code": 101, "type": "missing_access_key"}}, erh.ProviderAuthError),
        (401, {"error": {"code": 101, "type": "invalid_access_key"}}, erh.ProviderAuthError),
        (200, {"error": {"code": 104, "type": "monthly_usage_limit_reached"}}, erh.ProviderQuotaError),
        (429, {"error": {"code": 429, "type": "too_many_requests"}}, erh.ProviderQuotaError),
        (500, {"error": {"code": 500, "type": "server_error"}}, erh.ProviderError),
    ],
)
def test_classify_error(status, body, expected):
    err = erh._classify_error(status, body)
    assert type(err) is expected


def test_parse_day_accepts_iso_only():
    assert erh._parse_day("2026-09-18") == date(2026, 9, 18)
    assert erh._parse_day("not-a-date") is None
    assert erh._parse_day(None) is None


# ── 请求参数与解析（_get 打桩）────────────────────────────


@pytest.mark.asyncio
async def test_fetch_timeframe_parses_quotes_and_params(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def _fake_get(path, params):
        captured["path"], captured["params"] = path, params
        return {
            "success": True,
            "quotes": {
                "2026-09-18": {"USDCNY": 6.5, "USDKZT": 500.0, "USDEUR": 0.9},
                "2026-09-19": {"USDCNY": 6.51, "USDKZT": 501.0},
            },
        }

    monkeypatch.setattr(provider, "_get", _fake_get)
    out = await provider.fetch_timeframe(
        date(2026, 9, 18), date(2026, 9, 19), ["kzt", "CNY", "USD"]
    )

    assert out[date(2026, 9, 18)]["KZT"] == pytest.approx(6.5 / 500.0)
    assert out[date(2026, 9, 18)]["CNY"] == 1.0
    assert "EUR" not in out[date(2026, 9, 18)]  # 未请求的币种不返回
    assert out[date(2026, 9, 19)]["KZT"] == pytest.approx(6.51 / 501.0)
    assert captured["path"] == "/timeframe"
    params = captured["params"]
    # 生产实现必须用 currencies（symbols 被 Provider 静默忽略），base 锁 USD
    assert "symbols" not in params
    assert params["currencies"] == "CNY,KZT"  # USD（base 自身）被剔除
    assert params["base"] == "USD"
    assert params["start_date"] == "2026-09-18"
    assert params["end_date"] == "2026-09-19"


@pytest.mark.asyncio
async def test_fetch_historical_parses_flat_quotes(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def _fake_get(path, params):
        captured["path"], captured["params"] = path, params
        return {"success": True, "quotes": {"USDCNY": 6.7, "USDRUB": 84.0}}

    monkeypatch.setattr(provider, "_get", _fake_get)
    out = await provider.fetch_historical(date(2026, 9, 18), ["RUB", "CNY"])

    assert out["RUB"] == pytest.approx(6.7 / 84.0)
    assert out["CNY"] == 1.0
    assert captured["path"] == "/historical"
    assert captured["params"]["date"] == "2026-09-18"
    assert "symbols" not in captured["params"]


@pytest.mark.asyncio
async def test_fetch_timeframe_rejects_oversized_window(monkeypatch):
    provider = _provider()
    with pytest.raises(ValueError):
        await provider.fetch_timeframe(date(2025, 1, 1), date(2026, 1, 1), ["CNY"])
    with pytest.raises(ValueError):
        await provider.fetch_timeframe(date(2026, 9, 19), date(2026, 9, 18), ["CNY"])
    # 365 天（含首尾）合法：返回完整 365 天即可用
    async def _fake_get(path, params):
        return {
            "success": True,
            "quotes": {
                (date(2025, 1, 1) + timedelta(days=i)).isoformat(): {
                    "USDCNY": 6.5, "USDKZT": 500.0
                }
                for i in range(365)
            },
        }

    monkeypatch.setattr(provider, "_get", _fake_get)
    out = await provider.fetch_timeframe(date(2025, 1, 1), date(2025, 12, 31), ["KZT"])
    assert len(out) == 365


@pytest.mark.asyncio
async def test_fetch_timeframe_rejects_incomplete_window(monkeypatch):
    """完整性闸门：缺日 / 某日缺 USDCNY → 整批判失败（不把半批数据当成功）。"""
    provider = _provider()

    async def _missing_day(path, params):
        return {
            "success": True,
            "quotes": {
                "2026-09-18": {"USDCNY": 6.5, "USDKZT": 500.0},
                "2026-09-20": {"USDCNY": 6.5, "USDKZT": 500.0},  # 9/19 未返回
            },
        }

    monkeypatch.setattr(provider, "_get", _missing_day)
    with pytest.raises(erh.ProviderError, match="响应不完整"):
        await provider.fetch_timeframe(date(2026, 9, 18), date(2026, 9, 20), ["KZT"])

    async def _missing_usd_cny(path, params):
        return {"success": True, "quotes": {"2026-09-18": {"USDKZT": 500.0}}}

    monkeypatch.setattr(provider, "_get", _missing_usd_cny)
    with pytest.raises(erh.ProviderError, match="响应不完整"):
        await provider.fetch_timeframe(date(2026, 9, 18), date(2026, 9, 18), ["KZT"])


@pytest.mark.asyncio
async def test_fetch_timeframe_allows_missing_currency_but_keeps_gap(monkeypatch):
    """目标币种缺失不判失败：该币种当日不写，缺口继续保留。"""
    provider = _provider()

    async def _fake_get(path, params):
        return {"success": True, "quotes": {"2026-09-18": {"USDCNY": 6.5}}}

    monkeypatch.setattr(provider, "_get", _fake_get)
    out = await provider.fetch_timeframe(
        date(2026, 9, 18), date(2026, 9, 18), ["KZT", "RUB"]
    )
    assert out[date(2026, 9, 18)] == {"CNY": 1.0}  # KZT / RUB 缺失 → 不写、保留缺口


@pytest.mark.asyncio
async def test_fetch_timeframe_requires_quotes_structure(monkeypatch):
    provider = _provider()

    async def _fake_get(path, params):
        return {"success": True, "rates": {"2026-09-18": {}}}  # 旧脚本假设的键名不存在

    monkeypatch.setattr(provider, "_get", _fake_get)
    with pytest.raises(erh.ProviderError):
        await provider.fetch_timeframe(date(2026, 9, 18), date(2026, 9, 18), ["CNY"])


@pytest.mark.asyncio
async def test_empty_currencies_short_circuits(monkeypatch):
    provider = _provider()
    called = []

    async def _fake_get(path, params):
        called.append(path)
        return {"success": True, "quotes": {}}

    monkeypatch.setattr(provider, "_get", _fake_get)
    assert await provider.fetch_timeframe(date(2026, 9, 18), date(2026, 9, 18), []) == {}
    assert called == []


# ── HTTP 层（MockTransport）──────────────────────────────


def _mock_client_factory(status_code: int, text: str, content_type: str = "application/json"):
    async def _factory(timeout: float = 15.0) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code, text=text, headers={"content-type": content_type}
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return _factory


@pytest.mark.asyncio
async def test_get_missing_key_http_200_is_failure(monkeypatch):
    """缺 key 返回 HTTP 200 + success:false——只判状态码会误当成功。"""
    body = '{"success": false, "error": {"code": 101, "type": "missing_access_key"}}'
    monkeypatch.setattr(rates_http, "open_client", _mock_client_factory(200, body))
    provider = _provider()
    with pytest.raises(erh.ProviderAuthError):
        await provider._get("/timeframe", {"start_date": "x"})


@pytest.mark.asyncio
async def test_get_quota_error_http_429(monkeypatch):
    body = '{"success": false, "error": {"code": 104, "type": "usage_limit"}}'
    monkeypatch.setattr(rates_http, "open_client", _mock_client_factory(429, body))
    provider = _provider()
    with pytest.raises(erh.ProviderQuotaError):
        await provider._get("/timeframe", {"start_date": "x"})


@pytest.mark.asyncio
async def test_get_non_json_is_provider_error(monkeypatch):
    monkeypatch.setattr(
        rates_http, "open_client", _mock_client_factory(502, "<html>bad gateway</html>", "text/html")
    )
    provider = _provider()
    with pytest.raises(erh.ProviderError):
        await provider._get("/timeframe", {"start_date": "x"})


@pytest.mark.asyncio
async def test_get_network_error_is_provider_error(monkeypatch):
    async def _factory(timeout: float = 15.0) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(rates_http, "open_client", _factory)
    provider = _provider()
    with pytest.raises(erh.ProviderError):
        await provider._get("/timeframe", {"start_date": "x"})


@pytest.mark.asyncio
async def test_get_success_passthrough(monkeypatch):
    body = '{"success": true, "quotes": {"USDCNY": 6.5}}'
    monkeypatch.setattr(rates_http, "open_client", _mock_client_factory(200, body))
    provider = _provider()
    data = await provider._get("/historical", {"date": "2026-09-18"})
    assert data["quotes"]["USDCNY"] == 6.5


# ── 节流与 Key 解析 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_throttle_enforces_min_interval(monkeypatch):
    monkeypatch.setattr(erh, "_MIN_REQUEST_INTERVAL", 0.1)
    provider = _provider()
    t0 = time.monotonic()
    await provider._throttle()
    await provider._throttle()
    assert time.monotonic() - t0 >= 0.1


def test_resolve_api_key_env_priority(monkeypatch):
    monkeypatch.setenv("ERH_API_KEY", "k-from-env")
    assert erh.resolve_api_key() == "k-from-env"
