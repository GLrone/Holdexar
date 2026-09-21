"""价格刷新周期的生产统计：这一轮实际发生了什么。

统计是 Cycle 的观测结果，写回 `price_cycles` 行本身（不建独立统计表），在
Cycle 收敛时由调用方写一次。

数据来源只有三处，全部是已有事实：

- Cycle 行：冻结期望集、各阶段真实时刻、终态
- 价格结果：`game_current_prices`（经 `coverage.cycle_coverage`）
- 对象新鲜度：`freshness.appid_freshness`

统计口径（每个数字都能追溯到上面三处，不猜）：

- `targetsTotal` / `targetsDone`：期望集对象数 / 「处理完」的对象数——
  全部期望区服都拿到明确终态才算处理完（含 missing / blocked），
  因此「处理完」不等于「成功」，`targetsDone` 满格 + `coverage` 不满可以同时成立
- `unitsExpected` = 对象数 × 地区数；`unitsOk` / `unitsLocked` /
  `unitsFailed`（= missing + blocked）/ `unitsUnobserved` 是五个桶
- `coverage` = ok / expected；`coverageConfirmed` = (ok + locked) / expected
- `staleCount`：对象级——价格记录的最后观察时刻已 ≥ `STALE_HOURS` 的对象数
  （与 Freshness 同口径，不另立一套粒度）
- `durationSeconds` = `finished_at` − `started_at`
- `stageMs`：由 Cycle 行上的真实阶段时刻相减；**没进过的阶段记 0**，不估算

本轮不统计的指标（缺口，不伪造来源）：

- `retryCount` / `repairCount`：5min `price_repair` 的 job `cycle_id` 为 NULL，
  HTTP 重试与批次重推的计数只在进程内，不落库——没有能证明归属的来源，
  也不用时间窗口去推测。
- `errorKinds`：现有错误只有 Job 级自由文本 `crawl_jobs.error`，没有可稳定
  归类的错误体系；不为统计新造一套分类，Job 级错误照旧保留在 job 行上。

统计只描述事实，不驱动任何控制：调度周期、worker 数、重试与 repair 策略
都不读取这里的数字。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.core.database import get_session_factory
from app.domains.crawl import coverage as coverage_mod
from app.domains.crawl import cycle as price_cycle
from app.domains.crawl import freshness as freshness_mod
from app.domains.crawl.cycle import PriceCycle

logger = logging.getLogger(__name__)


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int:
    """两个时刻之间的毫秒数；任一为空返回 0（该阶段没发生过）。"""
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def stage_ms(
    *,
    started_at: datetime | None,
    running_at: datetime | None,
    repairing_at: datetime | None,
    finalizing_at: datetime | None,
    finished_at: datetime | None,
) -> dict:
    """各阶段耗时（毫秒）。阶段边界用 Cycle 行上的真实时刻。

    running 的终点取「下一个进入的阶段」；从 running 直接异常终止时落到
    finished_at——它确实一直在 running 里，直到本轮结束。
    """
    running_end = repairing_at or finalizing_at or finished_at
    return {
        "planning": _elapsed_ms(started_at, running_at),
        "running": _elapsed_ms(running_at, running_end),
        "repairing": _elapsed_ms(repairing_at, finalizing_at),
        "finalizing": _elapsed_ms(finalizing_at, finished_at),
    }


async def cycle_stats(cycle_id: int) -> dict | None:
    """算出本轮统计（不落库）。Cycle 不存在返回 None。"""
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is None:
            return None
        appids = [int(a) for a in ((cycle.expected_json or {}).get("appids") or [])]
        stages = stage_ms(
            started_at=cycle.started_at,
            running_at=cycle.running_at,
            repairing_at=cycle.repairing_at,
            finalizing_at=cycle.finalizing_at,
            finished_at=cycle.finished_at,
        )
        duration = (
            round((cycle.finished_at - cycle.started_at).total_seconds(), 1)
            if cycle.started_at is not None and cycle.finished_at is not None
            else None
        )

    coverage = await coverage_mod.cycle_coverage(cycle_id)
    if coverage is None:
        return None
    fresh = await freshness_mod.appid_freshness(appids)
    return {
        "targetsTotal": coverage["targetsTotal"],
        "targetsDone": coverage["targetsDone"],
        "unitsExpected": coverage["expectedUnits"],
        "unitsOk": coverage["ok"],
        "unitsLocked": coverage["locked"],
        "unitsFailed": coverage["missing"] + coverage["blocked"],
        "unitsUnobserved": coverage["unobserved"],
        "coverage": coverage["coverage"],
        "coverageConfirmed": coverage["coverageConfirmed"],
        "staleCount": sum(1 for f in fresh if f["freshness"] == "stale"),
        "durationSeconds": duration,
        "stageMs": stages,
    }


async def record_stats(cycle_id: int) -> dict | None:
    """算出并写回 Cycle 行；Cycle 不存在返回 None。"""
    stats = await cycle_stats(cycle_id)
    if stats is None:
        return None
    await price_cycle.write_stats(cycle_id, stats)
    return stats
