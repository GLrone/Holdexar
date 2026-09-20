"""rates 域服务：当前汇率刷新（实时层）+ 查询。

数据源：
1. augmentedsteam  https://api.augmentedsteam.com/rates/v1?to=CNY
   → 响应为嵌套形态 {CUR: {"CNY": rate_to_cny}}；兼容平铺 {CUR: rate} 形态
2. open.er-api     https://open.er-api.com/v6/latest/CNY           → rates 为 per-CNY，取倒数
CNY 恒为 1.0。每次刷新写 fx_rates（UPSERT）+ fx_rate_history
（UPSERT：`(currency_code, rate_date)` 唯一，同日多次刷新收敛为最后一行）。

**本模块只管实时层**：历史缺口修复 / 历史查询 / 依赖方重估一律走
`rates/history.py`（Provider 细节见 `rates/providers/`）。
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
    """共享 HTTP 客户端（代理优先；实现见 rates/http.py）。"""
    from .http import open_client

    return await open_client(15.0)


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
    """刷新全部汇率（原子刷新边界：汇率 + 全部 cny_fen + 两域排序快照）。

    返回 {source, count, updated, recomputed}。

    单事务语义（见 rates/snapshot.py 的模块说明）：
        BEGIN
          fx_rates / fx_rate_history
          → 白名单清洗（幂等）
          → recompute_cny_fen_all（本事务内的汇率快照，不读 get_rates 缓存）
          → games 排序快照 / bundles 排序快照
        COMMIT
    GET 只可能读到旧快照或新快照，绝不出现「rates 新 / cny_fen 旧 / diff 旧」
    的半刷新状态。任一步失败整体回滚——宁可汇率也不更新，也不留半态。
    """
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
    today = now.date()

    from . import snapshot as snapshot_service

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
            # 历史行 UPSERT（(currency_code, rate_date) 唯一）：同日多次刷新收敛为
            # 最后一行——与日收语义的既有读值一致，表不再堆积同日多行
            hist = sqlite_insert(FxRateHistory).values(
                currency_code=code,
                rate_to_cny=rate,
                source=source,
                source_kind="observed",
                rate_date=today,
                fetched_at=now,
            )
            await session.execute(
                hist.on_conflict_do_update(
                    index_elements=[
                        FxRateHistory.currency_code,
                        FxRateHistory.rate_date,
                    ],
                    set_={
                        "rate_to_cny": hist.excluded.rate_to_cny,
                        "source": hist.excluded.source,
                        "source_kind": "observed",
                        "fetched_at": hist.excluded.fetched_at,
                    },
                )
            )
        # 白名单清洗并入同一事务（幂等）。排在 commit 之后会随会话关闭被
        # 静默回滚——只剩启动链的 cleanup_disallowed 兜底
        await _delete_disallowed(session)
        # ── 原子刷新边界：cny_fen 重算 + 两域排序快照 ──
        recomputed = await snapshot_service.recompute_cny_fen_all(session)
        await snapshot_service.rebuild_sort_snapshots(session)
        await session.commit()

    # 进程内缓存在提交后失效（不得指向半刷新状态）
    from app.domains.games.service import invalidate_rates_cache

    invalidate_rates_cache()
    from app.domains.bundles.service import invalidate_bundles_cache

    invalidate_bundles_cache()

    logger.info(
        "汇率已刷新：source=%s 共 %d 币种（cny_fen 重算 games %d / bundles %d）",
        source, len(rates), recomputed["games"], recomputed["bundles"],
    )
    return {
        "source": source,
        "count": len(rates),
        "recomputed": recomputed,
        "fetchedAt": now.isoformat(),
    }


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
    """日线序列：按 `rate_date` 升序，一币种一天一行（canonical）。

    `(currency_code, rate_date)` 唯一索引保证同日只有一行；`sourceKind`
    （observed/carried）随行返回——前端据此区分真实观测与历史延续值。

    range（六档）：1mo/6mo/1y/5y/10y/all，只保留窗口内尾部。
    默认无窗口（16 年全量），limit>0 时再取尾段 limit 天（向后兼容旧调用）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(FxRateHistory)
                .where(
                    FxRateHistory.currency_code == currency.upper(),
                    FxRateHistory.rate_date.is_not(None),
                )
                .order_by(FxRateHistory.rate_date)
            )
        ).scalars().all()
    daily = [(r.rate_date, r) for r in rows]
    if range is not None:
        key = range.lower()
        months = RANGE_MONTHS.get(key)
        if months is None and key != "all":
            raise ValueError(f"无效的时间范围: {range}")
        if months is not None:
            # 月数 → 天数折算（30.44 天/月），窗口过滤无需日历级月末精确
            cutoff = get_beijing_time_obj().date() - timedelta(days=round(months * 30.44))
            daily = [(d, r) for d, r in daily if d >= cutoff]
    if limit > 0:
        daily = daily[-limit:]
    return [
        {
            "date": d.isoformat(),
            "rateToCny": r.rate_to_cny,
            "source": r.source,
            "sourceKind": r.source_kind,
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
