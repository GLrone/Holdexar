"""exchangerate.host Provider：历史汇率修复源（USD base + EOD）。

职责边界：HTTP / 认证 / 1 req/s 节流 / 响应解析 / 错误识别 / USD 交叉换算。
业务层不感知本模块的专属概念（access_key / currencies / quotes 键名）。

接口口径：
- `/timeframe` 返回 `{success, timeframe, start_date, end_date, source, quotes}`，
  `quotes = {<date>: {"<BASE><CCY>": rate}}`，窗口上限 365 天；
- `currencies` 是唯一生效的币种过滤参数（`symbols` 被静默忽略返回全量）；
- `base` 锁定 USD（传 base=CNY 无效），CNY 价交叉换算：1 target = USDCNY / USDtarget；
- 非交易日（周末/假日）Provider 自做 carry 填充，逐日返回；
- 缺 key 返回 HTTP 200 + `success:false`（只判状态码会误当成功）；
- 响应 headers 无月度余量（仅 1 req/s 级限速）→ 本地节流 + 本地配额账本。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

PROVIDER_NAME = "exchangerate.host"
DEFAULT_BASE_URL = "https://api.exchangerate.host"
ENV_API_KEY = "ERH_API_KEY"

MAX_WINDOW_DAYS = 365  # /timeframe 窗口含首尾上限（Provider 约束，非业务规则）
_MIN_REQUEST_INTERVAL = 1.05  # 1 req/s 限速下限（留少量余量）
_REQUEST_TIMEOUT = 30.0  # 365 天窗口约 3s，留足余量


class ProviderError(RuntimeError):
    """Provider 调用失败（网络 / 协议 / 业务错误）。"""


class ProviderAuthError(ProviderError):
    """Key 缺失或无效（error.code=101）。"""


class ProviderQuotaError(ProviderError):
    """配额 / 频率上限耗尽。"""


def resolve_api_key() -> str | None:
    """解析 ERH API Key：环境变量优先，再读工作树 `secrets/fx_maintenance.env`。

    secrets 文件命中 .gitignore（不进库、不进 git）；打包态无 secrets
    文件时走系统环境变量，均缺失返回 None（修复任务安全降级为 no_key）。
    """
    key = (os.environ.get(ENV_API_KEY) or "").strip()
    if key:
        return key
    secrets = Path(__file__).resolve().parents[5] / "secrets" / "fx_maintenance.env"
    if not secrets.is_file():
        return None
    try:
        for line in secrets.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == ENV_API_KEY and value.strip():
                value = value.strip()
                os.environ.setdefault(ENV_API_KEY, value)
                return value
    except OSError:
        return None
    return None


class ExchangerateHostProvider:
    """exchangerate.host 客户端（USD base，EOD 历史）。"""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ProviderAuthError("未配置 exchangerate.host API Key")
        self._key = key
        self._base_url = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def fetch_timeframe(
        self, start: date, end: date, currencies: Iterable[str]
    ) -> dict[date, dict[str, float]]:
        """按日期区间拉日线：{date: {currency: rate_to_cny}}。

        只含「Provider 返回且可交叉换算」的 (日期, 币种)；CNY 恒为 1.0。
        """
        if end < start:
            raise ValueError(f"区间非法：{start} → {end}")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"窗口超过 {MAX_WINDOW_DAYS} 天上限：{start} → {end}")
        wanted = self._normalize_currencies(currencies)
        if not wanted:
            return {}
        data = await self._get(
            "/timeframe",
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "base": "USD",
                "currencies": ",".join(sorted(wanted)),
            },
        )
        quotes = data.get("quotes")
        if not isinstance(quotes, dict):
            raise ProviderError("响应缺少 quotes 结构")
        out: dict[date, dict[str, float]] = {}
        for day_str, row in quotes.items():
            day = _parse_day(day_str)
            if day is None or not isinstance(row, dict):
                continue
            converted = _cross_to_cny(row, wanted)
            if converted:
                out[day] = converted
        return out

    async def fetch_historical(
        self, day: date, currencies: Iterable[str]
    ) -> dict[str, float]:
        """单日历史：{currency: rate_to_cny}。"""
        wanted = self._normalize_currencies(currencies)
        if not wanted:
            return {}
        data = await self._get(
            "/historical",
            {
                "date": day.isoformat(),
                "base": "USD",
                "currencies": ",".join(sorted(wanted)),
            },
        )
        quotes = data.get("quotes")
        if not isinstance(quotes, dict):
            raise ProviderError("响应缺少 quotes 结构")
        return _cross_to_cny(quotes, wanted)

    # ── 内部件 ──

    @staticmethod
    def _normalize_currencies(currencies: Iterable[str]) -> set[str]:
        wanted = {str(c).strip().upper() for c in currencies if c}
        wanted.discard("USD")  # base 自身不在返回里
        if wanted:
            wanted.add("CNY")  # 交叉换算分子，有目标币种才需要随请求
        return wanted

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        from app.domains.rates.http import open_client

        query = {**params, "access_key": self._key}
        await self._throttle()
        try:
            async with await open_client(_REQUEST_TIMEOUT) as client:
                resp = await client.get(f"{self._base_url}{path}", params=query)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络请求失败：{e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError(
                f"响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:120]!r}"
            ) from e
        if not isinstance(data, dict):
            raise ProviderError("响应结构异常（非对象）")
        if data.get("success") is not True:
            raise _classify_error(resp.status_code, data)
        return data

    async def _throttle(self) -> None:
        """1 req/s 本地节流（Provider 频率限速口径；锁保证并发调用串行排队）。"""
        async with self._lock:
            wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


def _classify_error(status_code: int, data: dict) -> ProviderError:
    err = data.get("error")
    code = None
    etype = info = ""
    if isinstance(err, dict):
        code = err.get("code")
        etype = str(err.get("type") or "")
        info = str(err.get("info") or err.get("message") or "")
    elif err is not None:
        info = str(err)
    detail = f"HTTP {status_code} code={code} type={etype} {info}".strip()
    if code == 101 or "access_key" in etype or "access_key" in info:
        return ProviderAuthError(f"API Key 无效或缺失：{detail}")
    if status_code == 429 or code in (104, 429) or "limit" in etype or "quota" in etype:
        return ProviderQuotaError(f"配额或频率上限：{detail}")
    return ProviderError(f"Provider 返回失败：{detail}")


def _parse_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _cross_to_cny(quotes: dict, wanted: set[str]) -> dict[str, float]:
    """`{USDxxx: rate}` → `{xxx: rate_to_cny}`：1 target = USDCNY / USDtarget。

    USDCNY 缺失或非法时整日不可换算（返回空 dict）；CNY 恒 1.0。
    """
    usd_cny = quotes.get("USDCNY")
    if not isinstance(usd_cny, (int, float)) or usd_cny <= 0:
        return {}
    out: dict[str, float] = {}
    if "CNY" in wanted:
        out["CNY"] = 1.0
    for code in wanted:
        if code == "CNY":
            continue
        value = quotes.get(f"USD{code}")
        if isinstance(value, (int, float)) and value > 0:
            out[code] = float(usd_cny) / float(value)
    return out
