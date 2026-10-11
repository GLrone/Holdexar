"""价格刷新周期的覆盖率（Coverage）：本轮该刷多少 × 实际确认了多少。

分母**只**来自 Cycle 在 planning 冻结的期望集（`price_cycles.expected_json`），
不重查当前监控池——否则监控池增删会让同一轮的覆盖率分母漂移。

最小单元 = `(appid, region_code)`，与 `game_current_prices` 主键同形。每个期望
单元落进五个桶之一：`ok` / `locked` / `missing` / `blocked` / `unobserved`。
前四个沿用 `price_status` 的现状取值，第五个表示本轮窗口内没有任何结果。

两个口径并存，不合并成一个数：

    coverage           = ok / expected              真正拿到可购买价格的比例
    coverage_confirmed = (ok + locked) / expected   Steam 明确给了答复的比例

`locked` 既不是成功价格也不是缺失，单独计量——把锁区塞进普通 coverage 会把
「Steam 说这个区不卖」误报成「拿到了价格」。

读路径：统计列即冻结口径——统计与覆盖读数在 write_stats 同一事务落账，
收敛轮（units_expected 非 NULL）直接由统计列重建，不随活表滚动漂移；
活表窗口现算只兜底未收敛轮与统计机制上线前的存量轮——价格行会随任何
周期外写入滚动覆盖 updated_at，旧窗口在活表上不可复现。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.domains.crawl.cycle import TERMINAL_STATES, PriceCycle
from app.domains.games.models import GameCurrentPrice

logger = logging.getLogger(__name__)

# 计数桶顺序即结果字段顺序
BUCKETS = ("ok", "locked", "missing", "blocked", "unobserved")

# 期望单元里「Steam 给了明确答复」的两种状态
_CONFIRMED_STATUSES = ("ok", "locked")

# appid 分批宽度：避开 SQLite 变量上限，一条 IN 不装整池
_CHUNK = 500


def _bucket_of(price_status: str | None) -> str:
    """`price_status` → 覆盖率桶。未知取值按未确认计（不虚构价格状态）。"""
    status = (price_status or "").strip().lower()
    return status if status in ("ok", "locked", "missing", "blocked") else "missing"


def _in_window(
    updated_at: datetime | None, started_at: datetime | None, window_end: datetime
) -> bool:
    """窗口外的行等于本轮没结果；无更新时刻的行同理。"""
    if updated_at is None:
        return False
    if started_at is not None and updated_at < started_at:
        return False
    return updated_at <= window_end


def _scan(
    per_appid: dict[int, dict[str, int]],
    rows,
    regions: set[str],
    started_at: datetime | None,
    window_end: datetime,
) -> None:
    """价格行累加进「每个对象各桶的单元数」；所有消费方共用这一处判定。"""
    for appid, region_code, price_status, updated_at in rows:
        region = str(region_code).strip().lower()
        if region not in regions or not _in_window(updated_at, started_at, window_end):
            continue
        buckets = per_appid.setdefault(int(appid), {b: 0 for b in BUCKETS})
        buckets[_bucket_of(price_status)] += 1


def _result(
    cycle_id: int,
    status: str | None,
    expected_units: int,
    counts: dict[str, int],
    *,
    targets_total: int = 0,
    targets_done: int = 0,
    coverage: float | None = None,
    coverage_confirmed: float | None = None,
) -> dict:
    """计数 → 覆盖率结果。期望集为空时两个比值为 null（0/0 无意义）。"""
    ok = counts["ok"]
    confirmed = ok + counts["locked"]
    if coverage is None:
        coverage = round(ok / expected_units, 4) if expected_units else None
    if coverage_confirmed is None:
        coverage_confirmed = (
            round(confirmed / expected_units, 4) if expected_units else None
        )
    return {
        "cycleId": cycle_id,
        "status": status,
        "targetsTotal": targets_total,
        # 「处理完」= 该对象的全部期望区服都拿到明确终态；missing / blocked
        # 也算拿到结果，unobserved 不算——与「成功」是两件事
        "targetsDone": targets_done,
        "expectedUnits": expected_units,
        "ok": ok,
        "locked": counts["locked"],
        "missing": counts["missing"],
        "blocked": counts["blocked"],
        "unobserved": counts["unobserved"],
        "coverage": coverage,
        "coverageConfirmed": coverage_confirmed,
    }


async def cycle_coverage(cycle_id: int) -> dict | None:
    """本轮覆盖率；Cycle 不存在返回 None。

    收敛轮（统计已落账）直接读统计列重建——不随活表窗口漂移；未收敛轮
    与统计前的存量轮按活表窗口现算兜底。拆分列（units_missing/blocked）
    缺失的存量行按 missing=units_failed、blocked=0 合并计（合计精确）。
    """
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is None:
            return None
        cycle_status = cycle.status
        started_at = cycle.started_at
        # 窗口上界：收敛后取 finished_at，未收敛取当前时刻。上界是「本轮结果
        # 可复现」的前提——下一轮刷新会把同一行 updated_at 推到窗口外，
        # 历史 Cycle 的覆盖率才不会随之后的刷新变大。
        window_end = cycle.finished_at or datetime.now()
        expected = dict(cycle.expected_json or {})
        # 统计列与统计读数同事务落账：units_expected 非 NULL ⇒ 其余统计列
        # 同事务已写（write_stats 单事务不变量）
        frozen_units = cycle.units_expected
        frozen = (
            cycle.units_ok,
            cycle.units_locked,
            cycle.units_failed,
            cycle.units_missing,
            cycle.units_blocked,
            cycle.units_unobserved,
            cycle.coverage,
            cycle.coverage_confirmed,
            cycle.targets_total,
            cycle.targets_done,
        )

    if frozen_units is not None:
        units_ok, units_locked, units_failed, units_missing, units_blocked, \
            units_unobserved, coverage, coverage_confirmed, targets_total, \
            targets_done = frozen
        missing = units_missing
        blocked = units_blocked
        if missing is None and blocked is None:
            missing = units_failed
            blocked = 0
        return _result(
            cycle_id,
            cycle_status,
            frozen_units,
            {
                "ok": units_ok,
                "locked": units_locked,
                "missing": missing,
                "blocked": blocked,
                "unobserved": units_unobserved,
            },
            targets_total=targets_total,
            targets_done=targets_done,
            coverage=coverage,
            coverage_confirmed=coverage_confirmed,
        )

    appids = [int(a) for a in (expected.get("appids") or [])]
    # 区服码大小写不敏感：期望集来自 regions 域（小写 code），
    # game_current_prices.region_code 落库是大写，直接比会整批漏配
    regions = {str(r).strip().lower() for r in (expected.get("regions") or [])}
    per_unit = len(regions)
    expected_units = len(appids) * per_unit
    if expected_units == 0:
        return _result(cycle_id, cycle_status, 0, {b: 0 for b in BUCKETS})

    buckets_by_appid: dict[int, dict[str, int]] = {}
    async with get_session_factory()() as session:
        for start in range(0, len(appids), _CHUNK):
            rows = (
                await session.execute(
                    select(
                        GameCurrentPrice.appid,
                        GameCurrentPrice.region_code,
                        GameCurrentPrice.price_status,
                        GameCurrentPrice.updated_at,
                    ).where(GameCurrentPrice.appid.in_(appids[start : start + _CHUNK]))
                )
            ).all()
            _scan(buckets_by_appid, rows, regions, started_at, window_end)

    counts = {
        b: sum(buckets.get(b, 0) for buckets in buckets_by_appid.values())
        for b in BUCKETS
    }
    counts["unobserved"] = expected_units - sum(counts.values())
    # 「处理完」= 该对象的每个期望区服都拿到一条结果（每个单元最多一行）
    targets_done = sum(
        1 for a in appids if sum(buckets_by_appid.get(int(a), {}).values()) >= per_unit
    )
    return _result(
        cycle_id,
        cycle_status,
        expected_units,
        counts,
        targets_total=len(appids),
        targets_done=targets_done,
    )
