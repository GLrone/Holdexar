"""价格刷新周期的覆盖率（Coverage）：本轮该刷多少 × 实际确认了多少。

分母**只**来自 Cycle 在 planning 冻结的期望集（`price_cycles.expected_json`），
不重查当前监控池——否则监控池增删、区服配置变化会让同一轮的覆盖率分母漂移。

最小单元 = `(appid, region_code)`，与 `game_current_prices` 主键同形。每个期望
单元落进五个桶之一：`ok` / `locked` / `missing` / `blocked` / `unobserved`。
前四个沿用 `price_status` 的现状取值（不另造一套价格状态），第五个表示本轮
窗口内没有任何结果。

两个口径并存，不合并成一个数：

    coverage           = ok / expected              真正拿到可购买价格的比例
    coverage_confirmed = (ok + locked) / expected   Steam 明确给了答复的比例

`locked` 既不是成功价格也不是缺失，单独计量——把锁区塞进普通 coverage 会把
「Steam 说这个区不卖」误报成「拿到了价格」。

本轮结果口径：只认 updated_at 落在 **`[cycle.started_at, cycle.finished_at]`**
（未收敛的取当前时刻为上界）的行。上界让本轮结果可复现——下一轮刷新会把同一行
推到窗口外，历史 Cycle 的覆盖率不会随之后的刷新变大。

这是 **cycle-window observation**，不是严格的 observation provenance——价格行
没有 `cycle_id`，窗口内并行的 repair / 手动抓取同样会刷新 `updated_at`，无法
区分来源。该限制是当前口径的一部分，不引入第二套生命周期去掩盖它。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.crawl.cycle import PriceCycle
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


def _result(
    cycle_id: int,
    status: str | None,
    expected_units: int,
    counts: dict[str, int],
    *,
    targets_total: int = 0,
    targets_done: int = 0,
) -> dict:
    """计数 → 覆盖率结果。期望集为空时两个比值为 null（0/0 无意义）。"""
    ok = counts["ok"]
    confirmed = ok + counts["locked"]
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
        "coverage": round(ok / expected_units, 4) if expected_units else None,
        "coverageConfirmed": (
            round(confirmed / expected_units, 4) if expected_units else None
        ),
    }


async def cycle_coverage(cycle_id: int) -> dict | None:
    """本轮覆盖率；Cycle 不存在返回 None。"""
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

    appids = [int(a) for a in (expected.get("appids") or [])]
    # 区服码大小写不敏感：期望集来自 regions 域（小写 code），
    # game_current_prices.region_code 落库是大写，直接比会整批漏配
    regions = {str(r).strip().lower() for r in (expected.get("regions") or [])}
    expected_units = len(appids) * len(regions)
    if expected_units == 0:
        return _result(cycle_id, cycle_status, 0, {b: 0 for b in BUCKETS})

    counts = {b: 0 for b in BUCKETS}
    # 每个对象本轮拿到结果的区服集合（判「这个对象处理完了吗」）
    hits: dict[int, set[str]] = {}
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
            for appid, region_code, price_status, updated_at in rows:
                # 只认本轮窗口内的更新：窗口外的行等于本轮没结果
                region = str(region_code).strip().lower()
                if region not in regions or updated_at is None:
                    continue
                if started_at is not None and updated_at < started_at:
                    continue
                if updated_at > window_end:
                    continue
                counts[_bucket_of(price_status)] += 1
                hits.setdefault(int(appid), set()).add(region)

    counts["unobserved"] = expected_units - sum(counts.values())
    targets_done = sum(1 for a in appids if len(hits.get(a, ())) == len(regions))
    return _result(
        cycle_id,
        cycle_status,
        expected_units,
        counts,
        targets_total=len(appids),
        targets_done=targets_done,
    )
