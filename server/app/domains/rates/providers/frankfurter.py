"""Frankfurter v2 Provider：多币种免 Key 历史汇率源。

职责边界：HTTP / 参数构造 / 多币种批量响应解析 / USD 中介到 CNY 本位的交叉换算。
落库语义：统一为 derived（混合推导汇率），缺日保留缺口，不人为延续。
"""
from __future__ import annotations

import asyncio
from datetime import date
import logging
import time
from typing import Iterable

import httpx

from app.core.logging import log_event

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Provider 调用失败（网络 / 解析 / 数据不可用）。"""


PROVIDER_NAME = "frankfurter.v2"
DEFAULT_BASE_URL = "https://api.frankfurter.dev/v2"

_MIN_REQUEST_INTERVAL = 0.2
_REQUEST_TIMEOUT = 30.0


class FrankfurterCurrencyProvider:
    """Frankfurter v2 历史汇率客户端（CNY 本位，免 Key，批量 quotes）。"""

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def fetch_timeframe(
        self, start: date, end: date, currencies: Iterable[str]
    ) -> dict[date, dict[str, float]]:
        """窗口内多币种逐日 {date: {currency: rate_to_cny}}。

        请求以 USD 为基准批量获取目标币种与 CNY 报价，通过 cross-rate 换算为
        CNY 本位；未返回的币种或日期保留缺口，不伪造数据。
        """
        if end < start:
            raise ValueError(f"区间非法：{start} → {end}")
        wanted = {str(c).strip().upper() for c in currencies if str(c).strip()}
        if not wanted:
            return {}

        include_cny = "CNY" in wanted
        include_usd = "USD" in wanted
        quote_codes = sorted((wanted - {"USD"}) | {"CNY"})
        if not quote_codes:
            return {}

        params = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "base": "USD",
            "quotes": ",".join(quote_codes),
        }
        items = await self._get(params)
        return self._parse_and_convert(items, wanted, include_cny, include_usd)

    def _parse_and_convert(
        self,
        items: list[dict],
        wanted: set[str],
        include_cny: bool,
        include_usd: bool,
    ) -> dict[date, dict[str, float]]:
        raw_by_date: dict[date, dict[str, float]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            date_str = item.get("date")
            quote = str(item.get("quote", "")).strip().upper()
            rate_raw = item.get("rate")
            if not date_str or not quote or rate_raw is None:
                continue
            try:
                d = date.fromisoformat(str(date_str)[:10])
                r = float(rate_raw)
            except (ValueError, TypeError):
                continue
            if r <= 0:
                continue
            raw_by_date.setdefault(d, {})[quote] = r

        result: dict[date, dict[str, float]] = {}
        non_usd_cny = wanted - {"USD", "CNY"}
        for d, quotes in raw_by_date.items():
            r_cny = quotes.get("CNY")
            if r_cny is None or r_cny <= 0:
                continue
            row: dict[str, float] = {}
            if include_cny:
                row["CNY"] = 1.0
            if include_usd:
                row["USD"] = float(r_cny)
            for code in non_usd_cny:
                q_rate = quotes.get(code)
                if q_rate is not None and q_rate > 0:
                    row[code] = float(r_cny / q_rate)
            if row:
                result[d] = row
        return result

    async def _get(self, params: dict[str, str]) -> list[dict]:
        from app.domains.rates.http import open_client

        await self._throttle()
        url = f"{self._base_url}/rates"
        try:
            async with await open_client(_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络请求失败：{e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            data = resp.json()
        except Exception as e:
            raise ProviderError(f"JSON 解析失败：{e}") from e
        if not isinstance(data, list):
            raise ProviderError(f"响应非列表结构：{type(data)}")
        return data

    async def _throttle(self) -> None:
        async with self._lock:
            wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()
