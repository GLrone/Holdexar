"""历史汇率统一出口：缺口扫描 / 修复 / 查询 / 依赖方重估。

数据语义（v8 起，见 models.FxRateHistory）：
- `observed` = Provider 实际返回的汇率（含 Provider 自做的周末 carry 填充，
  按天落行）；
- `carried` = 历史遗留的 forward-fill 延续值（保留展示，不被历史业务当真值消费）；
- `derived` = 由合法来源计算得到。

Bills / Games / 维护脚本一律走本模块，不直接读 `fx_rate_history`——「什么才算
有效历史汇率」只有本模块知道。

修复原则：
- 缺口由本地扫描确定（缺行日 ∪ carried 日），数据由 Provider 决定——不维护
  工作日历、不跳过周末、不猜节假日；
- 先本地 scan → 日期区间合并 → 每窗一次 Provider 拉取 → 校验 → 只写缺失日
  与 carried 日（observed 日永不重拉）；单源 bing.currencyapi（免 Key）；
- 外网失败 / 额度不足即停：不重试风暴、不造假数据。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, get_session_factory
from app.core.database import write_gate
from app.core.logging import log_event
from app.crawler.utils import get_beijing_time_obj
from .models import FxRateHistory
from .providers.bing_currency import (
    PROVIDER_NAME as BING_PROVIDER_NAME,
    BingCurrencyProvider,
    ProviderError,
)

logger = logging.getLogger(__name__)

# 缺口日聚类阈值：相邻待修复日间隔 ≤ 该值并入同一窗口段（多拉几天数据零成本，
# 换来更少的请求数）；超过则各自成段。
_CLUSTER_GAP_DAYS = 7

# 全新安装 bootstrap 起点 = Provider 全历史档位起点（bing chartType=9 自 2012
# 年起）：种子不再携带汇率档案，无任何历史行的币种由扫描以该锚点建窗，
# 交修复链现抓——否则新装库永远「无锚点跳过」，历史 CNY 换算无从建立。
FULL_HISTORY_START = date(2012, 1, 1)

# 修复窗口上限：批次化单窗数据量（Bing 按币种请求，窗口大小不影响请求数，
# 批次语义保留用于逐窗落账与失败定位）
MAX_WINDOW_DAYS = 365


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ── 查询（Bills / Games 的唯一入口）────────────────────────


async def load_observed_series(
    session, currency: str, start: date, end: date
) -> list[tuple[str, float]]:
    """[start, end] 内已观测日线，升序 `[(YYYY-MM-DD, rate_to_cny)]`。

    只含 `source_kind='observed'` 行——carried 是历史延续值，不能当真实
    历史汇率消费。调用方负责传日期对象。
    """
    rows = (
        await session.execute(
            select(FxRateHistory.rate_date, FxRateHistory.rate_to_cny)
            .where(
                FxRateHistory.currency_code == currency.upper(),
                FxRateHistory.source_kind == "observed",
                FxRateHistory.rate_date.is_not(None),
                FxRateHistory.rate_date >= start,
                FxRateHistory.rate_date <= end,
            )
            .order_by(FxRateHistory.rate_date)
        )
    ).all()
    out: list[tuple[str, float]] = []
    for rate_date, rate in rows:
        day = _as_date(rate_date)
        if day is not None and rate is not None:
            out.append((day.isoformat(), float(rate)))
    return out


async def observed_rate_map(
    currencies: Iterable[str], start: date, end: date
) -> dict[tuple[str, str], float]:
    """多币种窗口的 observed 汇率表 `{(CODE, 'YYYY-MM-DD'): rate_to_cny}`。

    Games 历史价格换算用：一次取齐窗口内全部涉及币种，内存查表。
    """
    codes = sorted({str(c).strip().upper() for c in currencies if c})
    if not codes:
        return {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    FxRateHistory.currency_code,
                    FxRateHistory.rate_date,
                    FxRateHistory.rate_to_cny,
                ).where(
                    FxRateHistory.currency_code.in_(codes),
                    FxRateHistory.source_kind == "observed",
                    FxRateHistory.rate_date.is_not(None),
                    FxRateHistory.rate_date >= start,
                    FxRateHistory.rate_date <= end,
                )
            )
        ).all()
    out: dict[tuple[str, str], float] = {}
    for code, rate_date, rate in rows:
        day = _as_date(rate_date)
        if day is not None and rate is not None:
            out[(str(code).upper(), day.isoformat())] = float(rate)
    return out


def pick_observed_rate(
    rows: list[tuple[str, float]], currency: str, day: str, *, lookback_days: int = 15
) -> float | None:
    """从 observed 日线取 day 的汇率：精确日 → 逐日回溯 ≤ lookback_days 天。

    rows 必须是 observed-only（本模块出口保证）。CNY 恒 1.0；查不到返回
    None——明示缺失，不兜底、不猜当前汇率。
    """
    currency = currency.upper()
    if currency == "CNY":
        return 1.0
    if not rows:
        return None
    by_day = {d: rate for d, rate in rows}
    try:
        base = datetime.strptime(day, "%Y-%m-%d")
    except (TypeError, ValueError):
        return by_day.get(str(day))
    for delta in range(max(1, lookback_days)):
        key = (base - timedelta(days=delta)).strftime("%Y-%m-%d")
        if key in by_day:
            return by_day[key]
    return None


# ── 缺口扫描（纯本地）──────────────────────────────────────


def _cluster_days(days: set[date]) -> list[tuple[date, date]]:
    """待修复日聚成宽区间：相邻间隔 ≤ _CLUSTER_GAP_DAYS 合并。"""
    if not days:
        return []
    ordered = sorted(days)
    clusters: list[tuple[date, date]] = []
    start = prev = ordered[0]
    for day in ordered[1:]:
        if (day - prev).days <= _CLUSTER_GAP_DAYS:
            prev = day
            continue
        clusters.append((start, prev))
        start = prev = day
    clusters.append((start, prev))
    return clusters


def _split_windows(start: date, end: date) -> list[tuple[date, date]]:
    """区间拆成 ≤ MAX_WINDOW_DAYS 天的窗口。"""
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


# 语义可信度：同一 (币种, 日) 多行并存时取更可信的一条
_KIND_RANK = {"carried": 1, "derived": 2, "observed": 3}


def _kind_from_source(source: object) -> str:
    """语义列缺失时的回退推导：backfill 延续值 → carried，其余 → observed。

    与 v8 迁移、种子合并同一口径——过渡期写入（不带语义列）的 forward-fill
    延续值不得被当成真实观测。
    """
    return "carried" if str(source or "").lower() == "backfill" else "observed"


def _stronger_kind(current: str, incoming: str) -> str:
    """同一 (币种, 日) 多行的取胜语义：observed > derived > carried（同级保留当前）。"""
    if _KIND_RANK.get(incoming, 0) > _KIND_RANK.get(current, 0):
        return incoming
    return current


async def _load_marks() -> dict[str, dict[date, str]]:
    """每币种 `{day: source_kind}`（只读白名单币种）。

    读取兼容新旧两代行格式——旧版本实例写入的行不带 canonical 列
    （`rate_date` / `source_kind` 均为空）：

    - 日期：`rate_date` 有值即用它，否则从 `fetched_at` 取日期部分推导
      （两者都为空的行放弃）；
    - 语义：`source_kind` 有值即用它，否则按 `source` 推导（见
      `_kind_from_source`）；
    - 冲突：同一 (币种, 日) 可能同时存在 canonical 行与旧格式行，
      取更可信的一条（见 `_stronger_kind`）。
    """
    from .service import ALLOWED_CURRENCIES

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT currency_code, rate_date, fetched_at, source_kind, source"
                    " FROM fx_rate_history"
                )
            )
        ).all()
    marks: dict[str, dict[date, str]] = {}
    for code, rate_date, fetched_at, kind, source in rows:
        code = str(code).upper()
        if code not in ALLOWED_CURRENCIES:
            continue
        day = _as_date(rate_date) or _as_date(fetched_at)
        if day is None:
            continue
        resolved = str(kind).lower() if kind else _kind_from_source(source)
        bucket = marks.setdefault(code, {})
        bucket[day] = (
            resolved if day not in bucket else _stronger_kind(bucket[day], resolved)
        )
    return marks


async def scan_history_gaps(
    *,
    today: date | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """扫 canonical 历史缺口：缺行日 ∪ carried 日。

    扫描范围 = 各币种最早历史行 → 昨天（今天由实时链负责）；`start`/`end`
    可再收窄（运维限定区间修复）。**全库零历史行**（全新安装）时以
    FULL_HISTORY_START 为每个白名单币种锚定（bootstrap：种子不再带汇率
    档案，全史由修复链现抓）；存量库无行币种照旧跳过。

    返回 {currencies, windows, totalDays, totalPairs, horizon}：
    - currencies：{code: {missing, carried, first, last}}
    - windows：[{start, end, days}]，全局日期并集聚类后的 ≤365 天窗口
    - totalDays：待修复日期并集大小；totalPairs：(币种, 日) 对数
    """
    today = today or get_beijing_time_obj().date()
    horizon = today - timedelta(days=1)
    if end is not None and end < horizon:
        horizon = end
    marks = await _load_marks()
    from .service import ALLOWED_CURRENCIES

    # 全新安装 bootstrap：**全库零历史行**时以 Provider 全历史起点给每个
    # 白名单币种建窗（种子不再带汇率档案，全史由修复链现抓）；存量库保持
    # 旧语义——个别无行币种跳过，序列由实时刷新从当天起建立（不给老用户
    # 突然排 2012 起的全量回填）。
    bootstrap = not marks
    per_currency: dict[str, dict] = {}
    pending_days: set[date] = set()
    total_pairs = 0
    for code in sorted(ALLOWED_CURRENCIES) if bootstrap else sorted(marks):
        days = marks.get(code, {})
        span_start = min(days) if days else FULL_HISTORY_START
        if start is not None and span_start < start:
            span_start = start
        if span_start > horizon:
            continue
        span_days = (horizon - span_start).days + 1
        all_days = {span_start + timedelta(days=i) for i in range(span_days)}
        missing = all_days - set(days)
        carried = {
            d for d, kind in days.items() if kind == "carried" and span_start <= d <= horizon
        }
        pending = missing | carried
        if not pending:
            continue
        per_currency[code] = {
            "missing": len(missing),
            "carried": len(carried),
            "first": min(pending).isoformat(),
            "last": max(pending).isoformat(),
        }
        pending_days |= pending
        total_pairs += len(pending)

    windows: list[dict] = []
    for cluster_start, cluster_end in _cluster_days(pending_days):
        for win_start, win_end in _split_windows(cluster_start, cluster_end):
            windows.append(
                {
                    "start": win_start.isoformat(),
                    "end": win_end.isoformat(),
                    "days": (win_end - win_start).days + 1,
                }
            )
    return {
        "currencies": per_currency,
        "windows": windows,
        "totalDays": len(pending_days),
        "totalPairs": total_pairs,
        "horizon": horizon.isoformat(),
    }


# ── 修复 ───────────────────────────────────────────────────


async def _write_observed(data: dict[date, dict[str, float]]) -> tuple[int, dict[str, set[date]]]:
    """写入 observed 行：只写缺失日与 carried 日，observed 日跳过。

    返回 (写入行数, {code: {date}})——touched 供依赖方重估。
    """
    from .service import ALLOWED_CURRENCIES

    if not data:
        return 0, {}
    days = sorted(data)
    lo, hi = days[0], days[-1]
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT currency_code, rate_date, source_kind FROM fx_rate_history"
                    " WHERE rate_date >= :lo AND rate_date <= :hi"
                ),
                {"lo": lo.isoformat(), "hi": hi.isoformat()},
            )
        ).all()
    existing: dict[tuple[str, date], str] = {}
    for code, rate_date, kind in rows:
        day = _as_date(rate_date)
        if day is not None:
            existing[(str(code).upper(), day)] = str(kind or "").lower()

    now = get_beijing_time_obj().replace(tzinfo=None)
    values: list[dict] = []
    touched: dict[str, set[date]] = {}
    for day in days:
        row = data[day]
        for code, rate in row.items():
            if code not in ALLOWED_CURRENCIES:
                continue
            if existing.get((code, day)) == "observed":
                continue  # observed 日永不重拉/覆盖
            values.append(
                {
                    "currency_code": code,
                    "rate_to_cny": float(rate),
                    "source": BING_PROVIDER_NAME,
                    "source_kind": "observed",
                    "rate_date": day,
                    "fetched_at": now,
                    "observed_at": None,
                }
            )
            touched.setdefault(code, set()).add(day)
    if not values:
        return 0, {}
    async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
        stmt = sqlite_insert(FxRateHistory).values(values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    FxRateHistory.currency_code,
                    FxRateHistory.rate_date,
                ],
                set_={
                    "rate_to_cny": stmt.excluded.rate_to_cny,
                    "source": stmt.excluded.source,
                    "source_kind": stmt.excluded.source_kind,
                    "fetched_at": stmt.excluded.fetched_at,
                    "observed_at": None,
                },
            )
        )
        await session.commit()
    return len(values), touched


async def revalue_dependents(touched: dict[str, set[date]]) -> dict:
    """修复落库后重估依赖方。

    - Bills：受影响时段的交易 fx_rate / cny_fen 重算 + 汇总重算（否则旧汇率
      会固化在账单里）；
    - Games：历史价格 CNY 由读取层按 snapshot_at 的 observed 汇率即时换算
      （见 games.service.get_game_history），无需回写。
    """
    if not touched:
        return {}
    from app.domains.bills import service as bills_service

    result = await bills_service.revalue_affected(touched)
    if result.get("bills"):
        log_event(
            logger,
            "历史汇率修复后账单重估完成",
            detail={"账单": result.get("bills"), "交易": result.get("transactions")},
        )
    return result


def _new_bing_provider() -> BingCurrencyProvider:
    return BingCurrencyProvider()


async def repair_history_gaps(
    *,
    dry_run: bool = False,
    max_windows: int | None = None,
    today: date | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """拉取真实历史修复缺口：scan → 窗口分批 → Provider 拉取 → 写 observed → 重估。

    - `dry_run`：只出计划不触网不写库；
    - `max_windows`：单轮最多消费的窗口数（调度侧节流用；None = 不限，仍受
      quota guard 硬拦）；
    - `start`/`end`：限定修复区间（运维用）；
    - 主源免 Key（bing.currencyapi），无任何配置即可修复。任一次外网失败
      即停（不重试风暴）→ `provider_error`。
    """
    scan = await scan_history_gaps(today=today, start=start, end=end)
    windows = scan["windows"]
    result: dict = {
        "status": "no_gaps",
        "windowsPlanned": len(windows),
        "windows": [],
        "requests": 0,
        "written": 0,
        "currencies": scan["currencies"],
        "totalPairs": scan["totalPairs"],
    }
    if not windows:
        return result

    planned = windows if max_windows is None else windows[: max(0, max_windows)]
    if dry_run:
        return {**result, "status": "dry_run", "planned": planned}

    bing = _new_bing_provider()
    from .service import ALLOWED_CURRENCIES

    targets = sorted(ALLOWED_CURRENCIES)
    touched: dict[str, set[date]] = {}
    for window in planned:
        start = date.fromisoformat(window["start"])
        end = date.fromisoformat(window["end"])
        try:
            data = await bing.fetch_timeframe(start, end, targets)
        except ProviderError as e:
            result["status"] = "provider_error"
            result["error"] = str(e)[:200]
            break
        except ValueError as e:  # 窗口参数非法（不应发生，防御）
            result["status"] = "provider_error"
            result["error"] = str(e)[:200]
            break
        written, part_touched = await _write_observed(data)
        result["requests"] += 1
        result["written"] += written
        result["windows"].append(
            {"start": window["start"], "end": window["end"], "written": written,
             "days": len(data)}
        )
        for code, days in part_touched.items():
            touched.setdefault(code, set()).update(days)
    else:
        result["status"] = "ok"

    if result["written"]:
        result["revalued"] = await revalue_dependents(touched)
    log_event(
        logger,
        "历史汇率缺口修复本轮结束",
        detail={
            "状态": result["status"],
            "请求": result["requests"],
            "写入行": result["written"],
            "覆盖币种": len(touched),
        },
    )
    return result


async def bootstrap_full_history(*, max_windows: int | None = None) -> dict:
    """全新安装的汇率全史 bootstrap（种子不再携带汇率档案）。

    判定：全库零历史行 → 扫描以 FULL_HISTORY_START 给全部白名单币种建窗 →
    复用修复链现抓（bing 免 Key 全历史档位）；已有任何历史行的存量库直接
    返回 no_bootstrap，缺口照常归每日 04:00 修复任务。「汇率历史补全」
    开关关闭时跳过（与定时修复同一闸）。
    """
    from app.core.scheduler import content_fetch_enabled

    if not await content_fetch_enabled("fetch.fx_history"):
        return {"status": "disabled"}
    marks = await _load_marks()
    if marks:
        return {"status": "no_bootstrap"}
    log_event(logger, "全新安装汇率全史补全启动，全库零历史行，现抓全时间范围")
    return await repair_history_gaps(max_windows=max_windows)
