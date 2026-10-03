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

本轮结果只认 `updated_at` 落在 `[cycle.started_at, cycle.finished_at]`（未收敛
取当前时刻为上界）的行；上界让历史 Cycle 的覆盖率不随之后的刷新变大。价格行
没有 `cycle_id`，窗口内并行的 repair / 手动抓取区分不出来源——该限制是当前
口径的一部分，不引入第二套生命周期去掩盖它。
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
    *,
    regions_by_appid: dict[int, dict[str, str]] | None = None,
) -> None:
    """价格行累加进「每个对象各桶的单元数」。

    所有消费方共用这一处判定，覆盖率在不同粒度上不会算出两套数字。
    regions_by_appid 传入时同步记录「区 → 观察状态」（收敛冻结区级明细用）。
    """
    for appid, region_code, price_status, updated_at in rows:
        region = str(region_code).strip().lower()
        if region not in regions or not _in_window(updated_at, started_at, window_end):
            continue
        buckets = per_appid.setdefault(int(appid), {b: 0 for b in BUCKETS})
        status = _bucket_of(price_status)
        buckets[status] += 1
        if regions_by_appid is not None:
            regions_by_appid.setdefault(int(appid), {})[region] = status


def _snapshot_entry(
    buckets: dict[str, int] | None,
    observed: dict[str, str] | None,
    expected_regions: set[str],
) -> dict:
    """对象级快照条目（收敛冻结的格式）：五桶计数 + 区级明细。

    regions 只记**非 ok** 的区（大写码 → locked/missing/blocked），本轮没有
    观察到任何行的区记成 unobserved——卡片悬停点名问题地区的数据源。
    """
    entry = {b: (buckets or {}).get(b, 0) for b in BUCKETS if b != "unobserved"}
    entry["unobserved"] = len(expected_regions) - sum(entry.values())
    seen = observed or {}
    problems = {r.upper(): st for r, st in seen.items() if st != "ok"}
    for r in expected_regions:
        if r not in seen:
            problems[r.upper()] = "unobserved"
    entry["regions"] = problems
    return entry


def _read_snapshot_entry(
    raw: dict | None, per_unit: int, expected_regions: set[str]
) -> dict:
    """读已冻结的快照条目：计数与区明细照存取用，缺区明细（旧格式条目）
    记空表——前端没有点名数据时退回计数措辞，不虚构区名。"""
    if raw is None:
        return _snapshot_entry(None, None, expected_regions)
    entry = {b: raw.get(b, 0) for b in BUCKETS if b != "unobserved"}
    entry["unobserved"] = raw.get("unobserved", per_unit - sum(entry.values()))
    entry["regions"] = dict(raw.get("regions") or {})
    return entry


def _result(
    cycle_id: int,
    status: str | None,
    expected_units: int,
    counts: dict[str, int],
    *,
    targets_total: int = 0,
    targets_done: int = 0,
    per_appid: dict[str, dict[str, int]] | None = None,
) -> dict:
    """计数 → 覆盖率结果。期望集为空时两个比值为 null（0/0 无意义）。"""
    ok = counts["ok"]
    confirmed = ok + counts["locked"]
    result = {
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
        "coverage": round(ok / expected_units, 4) if expected_units else None,
        "coverageConfirmed": (
            round(confirmed / expected_units, 4) if expected_units else None
        ),
    }
    if per_appid is not None:
        result["perAppid"] = per_appid
    return result


async def cycle_coverage(cycle_id: int, *, per_appid: bool = False) -> dict | None:
    """本轮覆盖率；Cycle 不存在返回 None。

    证据来源是**收敛时冻结在 Cycle 行上的对象级快照**（coverage_json）：
    `game_current_prices` 是当前价表，行会随任何周期外写入（手动抓取 / repair /
    下一轮进行中）滚动覆盖 updated_at，旧窗口在活表上不可复现——按窗口现算会把
    已收敛轮的覆盖率改成 0/5 之类的假塌陷。快照缺失（冻结机制上线前的存量轮）
    才按窗口现算，数字可能已被周期外写入污染。

    per_appid=True 时附带 `"perAppid"`：每个对象的五桶计数（键为 appid 字符串，
    JSON 落库用）——收敛统计冻结快照走的就是这个出口。
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
        snapshot_raw = dict(cycle.coverage_json or {})

    appids = [int(a) for a in (expected.get("appids") or [])]
    # 区服码大小写不敏感：期望集来自 regions 域（小写 code），
    # game_current_prices.region_code 落库是大写，直接比会整批漏配
    regions = {str(r).strip().lower() for r in (expected.get("regions") or [])}
    per_unit = len(regions)
    expected_units = len(appids) * per_unit
    if expected_units == 0:
        return _result(cycle_id, cycle_status, 0, {b: 0 for b in BUCKETS})

    if snapshot_raw:
        return _result_from_snapshot(
            cycle_id, cycle_status, appids, per_unit, snapshot_raw, regions
        )

    buckets_by_appid: dict[int, dict[str, int]] = {}
    regions_by_appid: dict[int, dict[str, str]] = {}
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
            _scan(
                buckets_by_appid,
                rows,
                regions,
                started_at,
                window_end,
                regions_by_appid=regions_by_appid,
            )

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
        per_appid=(
            {
                str(a): _snapshot_entry(
                    buckets_by_appid.get(int(a)),
                    regions_by_appid.get(int(a)),
                    regions,
                )
                for a in appids
            }
            if per_appid
            else None
        ),
    )


def _result_from_snapshot(
    cycle_id: int,
    cycle_status: str,
    appids: list[int],
    per_unit: int,
    snapshot_raw: dict,
    regions: set[str],
) -> dict:
    """冻结快照 → 覆盖率结果。分母取 Cycle 期望集（权威），计数取快照桶；
    期望集里缺快照条目的对象按全 unobserved 计（不应发生，防御性兜底）。"""
    snaps = {
        a: _read_snapshot_entry(snapshot_raw.get(str(a)), per_unit, regions)
        for a in appids
    }
    counts = {
        b: sum(snap[b] for snap in snaps.values())
        for b in BUCKETS
        if b != "unobserved"
    }
    counts["unobserved"] = len(appids) * per_unit - sum(counts.values())
    # 快照里 unobserved > 0 的对象即未处理完（其余四桶合计不满期望区数）
    targets_done = sum(1 for snap in snaps.values() if snap["unobserved"] == 0)
    return _result(
        cycle_id,
        cycle_status,
        len(appids) * per_unit,
        counts,
        targets_total=len(appids),
        targets_done=targets_done,
        per_appid={str(a): snaps[a] for a in appids},
    )
