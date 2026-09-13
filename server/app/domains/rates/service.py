"""rates 域服务：多源汇率刷新 + 查询。

数据源：
1. augmentedsteam  https://api.augmentedsteam.com/rates/v1?to=CNY
   → 实测响应为嵌套形态 {CUR: {"CNY": rate_to_cny}}；兼容旧平铺 {CUR: rate} 形态
2. open.er-api     https://open.er-api.com/v6/latest/CNY           → rates 为 per-CNY，取倒数
CNY 恒为 1.0。每次刷新写 fx_rates（UPSERT）+ fx_rate_history（INSERT）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.crawler.config import CC_LIST
from app.crawler.utils import get_beijing_time_obj
from app.domains.games.pricing import DEFAULT_EXCHANGE_RATES
from .models import FxRate, FxRateHistory

logger = logging.getLogger(__name__)

AUGSTEAM_URL = "https://api.augmentedsteam.com/rates/v1?to=CNY"
ERAPI_URL = "https://open.er-api.com/v6/latest/CNY"

# 41 区实际使用的货币白名单（由 CC_LIST 单点派生，41 区共 37 种唯一货币）。
# 另永久保留 TRY / ARS：当前无区服使用（土/阿已改 USD 定价），但后续拓展业务需要。
# 抓取落库、数据库清洗都只认这批；增减区服只改 CC_LIST，此处自动跟随。
RESERVED_CURRENCIES: frozenset[str] = frozenset({"TRY", "ARS"})
ALLOWED_CURRENCIES: frozenset[str] = frozenset(cur for _, _, cur in CC_LIST) | RESERVED_CURRENCIES


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def _client() -> httpx.AsyncClient:
    """共享 HTTP 客户端：策略引擎优先（代理优先为默认），回落环境变量代理。

    Steam 域/汇率源直连在国内网络下基本不可用（成功属侥幸），代理优先。
    """
    proxy = None
    try:
        from app.domains.proxies import service as proxies_service

        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        proxy = None
    if proxy is None:
        from app.crawler.proxy import resolve_proxy_url

        proxy = resolve_proxy_url()
    return httpx.AsyncClient(timeout=15, proxy=proxy) if proxy else httpx.AsyncClient(timeout=15)


async def _fetch_with_retry(url: str, attempts: int = 2) -> httpx.Response | None:
    """单源两次尝试（瞬时抖动容错），全败返回 None。"""
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            async with await _client() as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
    logger.warning("汇率源请求失败（%d 次）: %s: %s", attempts, url, last_err)
    return None


async def _fetch_augmentedsteam() -> dict[str, float] | None:
    try:
        resp = await _fetch_with_retry(AUGSTEAM_URL)
        if resp is None:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        rates: dict[str, float] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                r = v.get("CNY")
                if isinstance(r, (int, float)):
                    rates[str(k).upper()] = float(r)
            elif isinstance(v, (int, float)):
                rates[str(k).upper()] = float(v)
        if len(rates) < 5:
            return None
        return rates
    except Exception as e:  # noqa: BLE001
        logger.warning("augmentedsteam 汇率获取失败: %s", e)
        return None


async def _fetch_er_api() -> dict[str, float] | None:
    try:
        resp = await _fetch_with_retry(ERAPI_URL)
        if resp is None:
            return None
        data = resp.json()
        per_cny = data.get("rates", {})
        if not per_cny:
            return None
        return {str(k).upper(): 1.0 / float(v) for k, v in per_cny.items() if float(v) != 0}
    except Exception as e:  # noqa: BLE001
        logger.warning("er-api 汇率获取失败: %s", e)
        return None


async def refresh_rates() -> dict:
    """刷新全部汇率。返回 {source, count, updated}。"""
    rates = await _fetch_augmentedsteam()
    source = "augmentedsteam"
    if rates is None:
        rates = await _fetch_er_api()
        source = "er-api"
    if rates is None:
        raise RuntimeError("所有汇率源均失败")

    rates["CNY"] = 1.0
    # 只写白名单币种（41 区货币 + TRY/ARS 预留）；UI 自选追踪仅影响前端展示，不影响抓取范围
    rates = {code: rate for code, rate in rates.items() if code in ALLOWED_CURRENCIES}
    now = _naive(get_beijing_time_obj())

    async with get_session_factory()() as session:
        for code, rate in rates.items():
            stmt = sqlite_insert(FxRate).values(
                currency_code=code, rate_to_cny=rate, fetched_at=now
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[FxRate.currency_code],
                    set_={"rate_to_cny": stmt.excluded.rate_to_cny, "fetched_at": now},
                )
            )
            session.add(
                FxRateHistory(currency_code=code, rate_to_cny=rate, source=source, fetched_at=now)
            )
        await session.commit()
        # 顺带清洗历史遗留的白名单外币种（每次刷新幂等执行）
        await _delete_disallowed(session)

    # 汇率变更后立即使 games 域的进程内缓存失效
    from app.domains.games.service import invalidate_rates_cache

    invalidate_rates_cache()

    logger.info("汇率已刷新：source=%s 共 %d 币种", source, len(rates))
    return {"source": source, "count": len(rates), "fetchedAt": now.isoformat()}


STALE_THRESHOLD_HOURS = 12


async def refresh_if_stale() -> bool:
    """数据过期即补刷新（启动兜底）。返回是否触发了刷新。

    interval 调度从启动起算，服务频繁重启时 24h 永远到不了点——
    启动时检查最后快照龄，超过阈值（半天）就补一次，保证"每日自动抓取"承诺。
    """
    async with get_session_factory()() as session:
        row = (await session.execute(select(func.max(FxRate.fetched_at)))).scalar()
    if row is None:
        return False
    last = _naive(row)
    age_hours = (_naive(get_beijing_time_obj()) - last).total_seconds() / 3600
    if age_hours < STALE_THRESHOLD_HOURS:
        return False
    logger.info("汇率快照已 %.1f 小时未更新，启动补刷新", age_hours)
    try:
        await refresh_rates()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("启动汇率补刷新失败（等待下次调度）")
        return False


BACKFILL_SOURCE = "backfill"


async def backfill_history(dry_run: bool = False) -> dict:
    """历史缺口补齐：建档以来（该币种最早历史行日期）→ 今天，缺的交易日补行。

    口径（fx 维护脚本 backfill 子命令已委托本函数，单一实现）：
    - 只补周一~周五：外汇周末休市不造行，节假日同样无新报价、不造行；
    - 值 = 该币种上一已有行的汇率延续（forward-fill，库内已有行作锚点；
      同日多行按 id 取最后一行，与日线查询的日收语义一致）；
    - 已有任意历史行的日期一律跳过；无历史行的币种无锚点，跳过（序列由
      实时刷新从当天起建立）。
    幂等：只补「零历史行」的日期，重复执行零写入。仅处理白名单币种。
    返回 {"inserted": n, "currencies": {code: {count, first, last}}}。
    """
    async with get_session_factory()() as session:
        # 原生 SQL 直读（ORM 对象化 20 万+ 行不可接受）；fetched_at 存储形态
        # 混有 ORM datetime 文本与种子原文，统一按 ISO 前缀取日期部分
        rows = (
            await session.execute(
                text(
                    "SELECT currency_code, rate_to_cny, fetched_at "
                    "FROM fx_rate_history ORDER BY id"
                )
            )
        ).all()
    anchor: dict[str, dict[date, float]] = {}
    for code, rate, fa in rows:
        if fa is None:
            continue
        anchor.setdefault(str(code), {})[date.fromisoformat(str(fa)[:10])] = rate

    today = get_beijing_time_obj().date()
    plan: list[dict] = []
    detail: dict[str, dict[str, object]] = {}
    for code, series in sorted(anchor.items()):
        if code not in ALLOWED_CURRENCIES or not series:
            continue
        missing: list[tuple[date, float]] = []
        last_rate: float | None = None
        cursor = min(series)
        while cursor <= today:
            if cursor in series:
                last_rate = series[cursor]
            elif cursor.weekday() < 5 and last_rate is not None:
                missing.append((cursor, last_rate))
            cursor += timedelta(days=1)
        if missing:
            detail[code] = {
                "count": len(missing),
                "first": missing[0][0].isoformat(),
                "last": missing[-1][0].isoformat(),
            }
            plan.extend(
                {
                    "currency_code": code,
                    "rate_to_cny": rate,
                    "source": BACKFILL_SOURCE,
                    "fetched_at": datetime(d.year, d.month, d.day),
                }
                for d, rate in missing
            )

    if plan and not dry_run:
        async with get_session_factory()() as session:
            await session.execute(sqlite_insert(FxRateHistory), plan)
            await session.commit()
        logger.info("汇率历史缺口补齐：%d 币种 / %d 行", len(detail), len(plan))
    return {"inserted": len(plan), "currencies": detail}


async def list_rates() -> dict:
    async with get_session_factory()() as session:
        rows = (await session.execute(select(FxRate).order_by(FxRate.currency_code))).scalars().all()
    return {
        "rates": [
            {
                "currency": r.currency_code,
                "rateToCny": r.rate_to_cny,
                "fetchedAt": r.fetched_at.isoformat() if r.fetched_at else None,
            }
            for r in rows
        ]
    }


RANGE_MONTHS: dict[str, int | None] = {
    "1mo": 1,
    "6mo": 6,
    "1y": 12,
    "5y": 60,
    "10y": 120,
    "all": None,
}


async def rate_history(currency: str = "USD", limit: int = 0, range: str | None = None) -> list[dict]:
    """日线序列：同日多行取最后一行（日收语义），按日期升序返回。

    库内混存三类行：实时刷新（同日多行）、16 年档案导入、backfill 补齐行——
    补齐行 id 大于实时行而日期更早，按 id 排序会乱序，故按日期聚合。

    range（对齐原版 fx-trend 六档）：1mo/6mo/1y/5y/10y/all，只保留窗口内尾部。
    默认无窗口（16 年全量），limit>0 时再取尾段 limit 天（向后兼容旧调用）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(FxRateHistory)
                .where(FxRateHistory.currency_code == currency.upper())
                .order_by(FxRateHistory.fetched_at, FxRateHistory.id)
            )
        ).scalars().all()
    by_day: dict[date, FxRateHistory] = {}
    for r in rows:
        if r.fetched_at is None:
            continue
        by_day[r.fetched_at.date()] = r
    daily = list(by_day.items())
    if range is not None:
        key = range.lower()
        months = RANGE_MONTHS.get(key)
        if months is None and key != "all":
            raise ValueError(f"无效的时间范围: {range}")
        if months is not None:
            # 月数 → 天数折算（30.44 天/月），窗口过滤无需日历级月末精确
            cutoff = date.today() - timedelta(days=round(months * 30.44))
            daily = [(d, r) for d, r in daily if d >= cutoff]
    if limit > 0:
        daily = daily[-limit:]
    return [
        {
            "date": d.isoformat(),
            "rateToCny": r.rate_to_cny,
            "source": r.source,
            "fetchedAt": r.fetched_at.isoformat() if r.fetched_at else None,
        }
        for d, r in daily
    ]


async def _delete_disallowed(session) -> tuple[int, int]:
    """删除白名单外币种的快照与历史行。返回 (快照删除数, 历史删除数)。"""
    allowed = sorted(ALLOWED_CURRENCIES)
    res_rate = await session.execute(delete(FxRate).where(FxRate.currency_code.not_in(allowed)))
    res_hist = await session.execute(
        delete(FxRateHistory).where(FxRateHistory.currency_code.not_in(allowed))
    )
    return (res_rate.rowcount or 0, res_hist.rowcount or 0)


async def cleanup_disallowed() -> tuple[int, int]:
    """数据库清洗：41 区货币集（+ TRY/ARS 预留）之外的币种数据一律清除。

    启动时调用一次；refresh_rates 落库后也会顺带执行，保证幂等。
    """
    async with get_session_factory()() as session:
        removed = await _delete_disallowed(session)
        if any(removed):
            await session.commit()
            logger.info("汇率白名单清洗：删除快照 %d 条 / 历史 %d 条", *removed)
    return removed
