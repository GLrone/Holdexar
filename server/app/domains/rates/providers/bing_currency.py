"""Bing currencyapi Provider：免 Key 历史汇率源（必应 SERP 汇率组件数据接口）。

职责边界：HTTP / 响应脚本解析 / 档位选择 / 采样点到日线的映射 / CNY 本位。
业务层不感知本模块的专属概念（chartType / graphData / Points / symbol 拼接）。

接口口径：
- `?symbol=<CCY>CNY&chartType=<n>` 返回内嵌脚本 `Finance.graphData = {...}`，
  Points 扁平化后每点 {XLabel, XLong, XIndex, Y}，Y 即该币种兑 CNY 汇率
  （XLong 为 epoch 毫秒，日期按北京时间取整）；
- 档位锚定「今天」、一次请求覆盖固定跨度：6 = 近 1 年（业务日采样）、
  8 = 近 5 年（周线）、9 = 全历史（2012 年起月线）；窗口修复按窗口起始日
  选择能完整覆盖窗口的最细档位；
- 数据为采样点：日线档的业务日点做周末/假日 carry 成逐日（FX 休市日汇率
  即前一收盘值，与 exchangerate.host 的 Provider 侧 carry 同一语义）；
  周线/月线档只落采样日本身——中间日不造 observed 数据，缺口保留；
- 每请求一个币种（symbol 拼接），CNY 本位不请求（恒 1.0，业务层约定）；
- 无 Key、无配额概念；0.4s 节流仅为礼貌性限速。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import httpx

from app.core.logging import log_event

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Provider 调用失败（网络 / 解析 / 数据不可用）。"""
PROVIDER_NAME = "bing.currencyapi"
DEFAULT_BASE_URL = "https://cn.bing.com/currencyapi/quote"

CHART_DAILY = 6  # 近 1 年，业务日采样
CHART_WEEKLY = 8  # 近 5 年，周线
CHART_MONTHLY = 9  # 全历史（2012 年起），月线

_DAILY_SPAN_DAYS = 360  # 档位 6 覆盖跨度（官方口径 1 年，留余量）
_WEEKLY_SPAN_DAYS = 1800  # 档位 8 覆盖跨度（官方口径 5 年，留余量）
_MIN_REQUEST_INTERVAL = 0.4
_REQUEST_TIMEOUT = 20.0
_TZ = timezone(timedelta(hours=8))  # 汇率日按北京时间取整（组件请求固定 tz=-480）

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _extract_graphdata(body: str) -> dict | None:
    """从内嵌脚本提取 graphData JSON（括号配对截取，忽略字符串内的花括号）。"""
    i0 = body.find("Finance.graphData")
    if i0 < 0:
        return None
    start = body.find("{", i0)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(body[start : i + 1])
    return None


def _select_chart(window_start: date, today: date) -> int:
    """按窗口起始日选能完整覆盖窗口的最细档位（档位跨度锚定今天）。"""
    if window_start >= today - timedelta(days=_DAILY_SPAN_DAYS):
        return CHART_DAILY
    if window_start >= today - timedelta(days=_WEEKLY_SPAN_DAYS):
        return CHART_WEEKLY
    return CHART_MONTHLY


class BingCurrencyProvider:
    """必应 currencyapi 客户端（CNY 本位采样点，免 Key）。"""

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, today: date | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._today = today  # 测试注入固定「今天」；生产按北京时间取日
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        # 档位级采样缓存：一次修复跑里多个窗口共用同一档位数据，不重复触网
        self._charts: dict[int, dict[str, list[tuple[date, float]]]] = {}

    async def fetch_timeframe(
        self, start: date, end: date, currencies: Iterable[str]
    ) -> dict[date, dict[str, float]]:
        """窗口内逐日 {date: {currency: rate_to_cny}}。

        与 exchangerate.host 的关键差异：请求按币种展开（每币种一请求，结果
        按档位缓存），且档位跨度锚定今天——早于近 1 年的窗口只能拿到周线/
        月线采样，只落采样日；个别币种拉取失败不判整体失败（该币种缺口
        保留，记日志），全部失败才抛 ProviderError。
        """
        if end < start:
            raise ValueError(f"区间非法：{start} → {end}")
        wanted = {str(c).strip().upper() for c in currencies if str(c).strip()}
        include_cny = "CNY" in wanted
        codes = sorted(wanted - {"CNY"})  # 本位不请求
        if not codes:
            return {}
        today = self._today or datetime.now(tz=_TZ).date()
        chart = _select_chart(start, today)
        series: dict[str, list[tuple[date, float]]] = {}
        failed: list[str] = []
        for code in codes:
            try:
                series[code] = await self._chart_points(code, chart)
            except ProviderError as e:
                log_event(
                    logger,
                    "单个币种汇率采样拉取失败，该币种缺口保留",
                    detail={"币种": code, "原因": e},
                    level=logging.WARNING,
                )
                failed.append(code)
        if failed and len(failed) == len(codes):
            raise ProviderError(f"全部币种采样拉取失败（示例 {failed[:3]}）")
        if chart == CHART_DAILY:
            return self._carry_daily(series, start, end, include_cny)
        return self._samples_only(series, start, end, include_cny)

    # ── 内部件 ──

    def _carry_daily(
        self,
        series: dict[str, list[tuple[date, float]]],
        start: date,
        end: date,
        include_cny: bool,
    ) -> dict[date, dict[str, float]]:
        """业务日采样 → 逐日：休市日延续前一采样值（FX 休市语义）。"""
        out: dict[date, dict[str, float]] = {}
        cursors = {code: -1 for code in series}
        day = start
        while day <= end:
            row: dict[str, float] = {}
            for code, pts in series.items():
                idx = cursors[code]
                while idx + 1 < len(pts) and pts[idx + 1][0] <= day:
                    idx += 1
                cursors[code] = idx
                if idx >= 0:
                    row[code] = pts[idx][1]
            if row:
                if include_cny:
                    row["CNY"] = 1.0
                out[day] = row
            day += timedelta(days=1)
        return out

    @staticmethod
    def _samples_only(
        series: dict[str, list[tuple[date, float]]],
        start: date,
        end: date,
        include_cny: bool,
    ) -> dict[date, dict[str, float]]:
        """周线/月线档：只落采样日（中间日造数等于编造 observed）。"""
        out: dict[date, dict[str, float]] = {}
        for code, pts in series.items():
            for d, v in pts:
                if start <= d <= end:
                    out.setdefault(d, {})[code] = v
        if include_cny:
            for row in out.values():
                row["CNY"] = 1.0
        return out

    async def _chart_points(self, code: str, chart: int) -> list[tuple[date, float]]:
        """单币种采样序列 `[(日期, rate_to_cny)]`（同日多点保留最晚一条）。"""
        cached = self._charts.setdefault(chart, {})
        if code in cached:
            return cached[code]
        body = await self._get(f"{code}CNY", chart)
        graph = _extract_graphdata(body)
        if graph is None:
            raise ProviderError(f"响应不含 graphData（{code} chartType={chart}）")
        points = graph.get("Points")
        by_day: dict[date, tuple[int, float]] = {}
        for arr in points if isinstance(points, list) else []:
            for p in arr if isinstance(arr, list) else []:
                if not isinstance(p, dict):
                    continue
                xlong, y = p.get("XLong"), p.get("Y")
                if (
                    not isinstance(xlong, (int, float))
                    or not isinstance(y, (int, float))
                    or y <= 0
                ):
                    continue
                d = datetime.fromtimestamp(xlong / 1000, tz=_TZ).date()
                if d not in by_day or xlong > by_day[d][0]:
                    by_day[d] = (int(xlong), float(y))
        result = sorted((d, v) for d, (_, v) in by_day.items())
        self._charts[chart][code] = result
        return result

    async def _get(self, symbol: str, chart: int) -> str:
        from app.domains.rates.http import open_client

        await self._throttle()
        try:
            async with await open_client(_REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    self._base_url,
                    params={
                        "symbol": symbol,
                        "chartType": str(chart),
                        "tz": "-480",
                        "ajaxreq": "1",
                    },
                    headers={
                        "User-Agent": _UA,
                        "Referer": "https://cn.bing.com/search?q=currency",
                    },
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"网络请求失败：{e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}（{symbol} chartType={chart}）")
        return resp.text

    async def _throttle(self) -> None:
        """0.4s 礼貌性节流（锁保证并发调用串行排队）。"""
        async with self._lock:
            wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()
