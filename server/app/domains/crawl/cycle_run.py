"""一轮价格刷新的完整编排：建轮 → 冻结期望集 → 跑链 → 收尾。

自动轮与手动全队列走同一份编排，仅 `kind` 不同——口径不可能分叉。四段
（planning / running / repairing / finalizing）各自独立可失败，任一段失败
不得让用户少一轮价格：建轮/冻结失败不阻断抓取（cycle_id=None 全段照跑）；
收尾段整体兜异常，防 Cycle 行僵死中间态。让路语义属调度器（调用方处理），
编排对「一个段都没跑起来」按 `decide_terminal` 如实判 failed。
"""
from __future__ import annotations

import logging

from app.core.events import bus
from app.core.orchestration import (
    KIND_PRICE_CYCLE_FINISHED,
    LEVEL_ERROR,
    LEVEL_INFO,
    record,
)
from app.domains.crawl import cycle as price_cycle
from app.domains.crawl import events as price_events
from app.domains.crawl import service as crawl_service
from app.domains.crawl import stats as price_stats

logger = logging.getLogger(__name__)


async def run_price_cycle(specs: list[dict], *, kind: str = "scheduled") -> bool:
    """跑完一轮价格刷新，返回**本轮是否值得让调用方让路重试**。

    返回 False 仅当本轮一个任务都没起来且属于「被占、稍后跑得动」；链异常与
    建轮失败返回 True（重试只会重复建轮、二次广播同一终态）。`kind` 仅作
    归属标签落 `price_cycles.kind`（scheduled=定时网格 / manual=用户手动）。
    """
    cycle_id: int | None = None
    regions: list[str] | None = None
    expected_units = 0
    try:
        cycle_id = await price_cycle.create(kind, "pool")
    except Exception:  # noqa: BLE001
        logger.exception("[周期] 本轮 Cycle 创建失败：抓取照常进行（本轮不挂 Cycle）")
    if cycle_id is not None:
        try:
            regions, expected_units = await price_cycle.freeze_expected(cycle_id, specs)
        except Exception:  # noqa: BLE001
            # 冻结失败不影响抓取：本轮照跑，终态按「无区可爬 / 无期望集」收 failed
            logger.exception("[周期] 本轮期望集冻结失败：抓取照常进行（本轮记 failed）")
            regions = None

    entered_repairing = False
    started = False
    skipped: list[str] = []
    try:
        await price_cycle.advance(cycle_id, price_cycle.RUNNING)
        results = await crawl_service.run_sequential(
            specs, cycle_id=cycle_id, skip_reasons=skipped
        )
        started = bool(results)
        if not results and specs:
            logger.info("[周期] 本轮无任务启动：%s", skipped or "无跳过留痕")
        else:
            logger.info("[周期] 本轮链完成：%s", [r["id"] for r in results])
        # 本轮结束时仍有待补欠账 → 本轮进入 repairing。补抓由既有 5min
        # repair 通道承担（它扫全库账本，与本轮期望集没有确定性关联，job
        # 暂不挂 Cycle），Cycle 只把「本轮确实遗留了未覆盖单元」记下来。
        if cycle_id is not None and await crawl_service.has_pending_missing():
            entered_repairing = await price_cycle.advance(
                cycle_id, price_cycle.REPAIRING
            )
    except Exception as e:  # noqa: BLE001
        # 链异常 = 本轮确实跑过（跑崩了），不是「被占用」。返回 True 让调用方
        # 不触发让路重试：重试一个崩掉的链只会再崩一遍，并把同一轮的失败
        # 广播两次（终态推进第二次会被 advance 拒绝，但事件已发出去）。
        logger.exception("[周期] 本轮价格链异常")
        await settle_cycle(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        await record_stats(cycle_id)
        return True

    if cycle_id is None:
        # 建轮失败：抓取照常跑完了（本轮没有归属可查）。返回 True 让调用方
        # 不重试——重试解决的是「被别的任务挤占」，与建轮失败无关。
        return True
    # 收尾段（job 汇总 → finalizing → 事件 → 终态 → 通知 → 统计）必须兜异常：
    # 这些库操作撞上写锁竞争时若异常外逃，Cycle 行会永久停在中间态——直到
    # 下次进程重启才被孤儿清理收尸，期间前端「这轮跑到哪了」永远无解。
    try:
        jobs = await price_cycle.jobs_of(cycle_id)
        # 终态为 failed 时必须随行写明拒因（error 字段与日志同源）：
        # 「整轮零启动」「全段失败」「冻结失败」各有真实原因可查，
        # 账本只记 failed 不记原因等于让使用者面对不可信记录。
        terminal_error: str | None = None
        if regions is None:
            terminal = price_cycle.FAILED
            terminal_error = "本轮期望集冻结失败，未按本轮口径记录覆盖"
        else:
            terminal = price_cycle.decide_terminal(
                [j["status"] for j in jobs],
                expected_units=expected_units,
                entered_repairing=entered_repairing,
            )
            if terminal == price_cycle.FAILED and not jobs:
                detail = "；".join(skipped[:4]) if skipped else "原因未随段留痕（详见服务日志）"
                terminal_error = f"本轮没有任何抓取段启动：{detail}"[:200]
            elif terminal == price_cycle.FAILED:
                terminal_error = f"本轮 {len(jobs)} 个抓取段全部失败，明细见任务列表"
        if not await price_cycle.advance(cycle_id, price_cycle.FINALIZING):
            return started
        # 事件检测排在本轮最终有效结果之上（finalizing 内、终态之前）：job 自己产生
        # 事件会让「暂时失败→随后补抓成功」的单元先报不可用再报恢复
        await detect_events(cycle_id)
        await settle_cycle(cycle_id, terminal, error=terminal_error)
        logger.info(
            "[周期] 价格刷新 Cycle %d（%s）→ %s（job %d 个，期望 %d 单元）",
            cycle_id, kind, terminal, len(jobs), expected_units,
        )
        # 通知是 Cycle 收敛后的下游副作用：失败只影响候选状态，不影响终态与统计
        await notify_events(cycle_id)
        await record_stats(cycle_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 收尾异常：兜底收敛为 failed", cycle_id)
        await settle_cycle(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        await record_stats(cycle_id)
        # 本轮抓取已经跑完（收尾崩的是记账，不是抓取）——同异常路径，
        # 返回 True 让调用方不重试，重试不会补上抓取只会重复广播
        return True
    return started


async def settle_cycle(
    cycle_id: int | None, terminal: str, error: str | None = None
) -> bool:
    """把 Cycle 收敛到终态，并广播 `price_cycle.completed`；返回是否真的收敛。

    先落库终态再发事件：客户端收到事件后立刻重拉，读到的就是新数据。只广播
    跃迁成功的那一次——非法/重复跃迁没有产生新结果，不该让前端白刷一遍列表。
    """
    if not await price_cycle.advance(cycle_id, terminal, error=error):
        return False
    bus.publish("price_cycle.completed", cycleId=cycle_id, status=terminal)
    # 事实留痕（独立会话，fail-soft）：进程重启后仍能回答「上一轮价格刷新何时、
    # 以何种终态收敛、失败原因是什么」；SSE 广播只覆盖在线时刻。
    if cycle_id is not None:
        await record(
            KIND_PRICE_CYCLE_FINISHED,
            error or f"价格刷新轮 #{cycle_id} 收敛：{terminal}",
            level=LEVEL_ERROR if error else LEVEL_INFO,
            payload={"cycle_id": cycle_id, "status": terminal},
        )
    return True


async def detect_events(cycle_id: int | None) -> None:
    """Cycle finalizing：把本轮观察相对历史的变化写成 price_events。

    检测失败只记日志：本轮抓取结果与统计已经落库，事件缺失不该影响它们。
    """
    if cycle_id is None:
        return
    try:
        written = await price_events.detect(cycle_id)
        if written:
            logger.info(
                "[周期] Cycle %d 价格事件 %d 条：%s",
                cycle_id, len(written),
                sorted({e["event_type"] for e in written}),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 价格事件检测失败（不影响本轮结果）", cycle_id)


async def notify_events(cycle_id: int | None) -> None:
    """Cycle 收敛后把本轮事件交给通知层（策略 → 候选 → 聚合摘要 → 邮件）。

    失败只记日志：通知挂了不能让 Cycle 变 failed，更不能让用户少一轮价格。
    """
    if cycle_id is None:
        return
    try:
        from app.domains.notifications import service as notification_service

        await notification_service.dispatch(cycle_id)
    except Exception:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 通知投递失败（不影响本轮结果）", cycle_id)


async def record_stats(cycle_id: int | None) -> None:
    """Cycle 收敛后留下本轮生产统计（观测结果，不参与任何控制）。

    统计失败只记日志：本轮抓取结果已经落库，统计缺失不该影响它。
    """
    if cycle_id is None:
        return
    try:
        recorded = await price_stats.record_stats(cycle_id)
        if recorded is not None:
            logger.info(
                "[周期] Cycle %d 统计：对象 %s/%s，单元 %s（ok %s / locked %s / 失败 %s / "
                "未观察 %s），覆盖 %s（确认 %s），stale %s，耗时 %ss",
                cycle_id,
                recorded["targetsDone"], recorded["targetsTotal"],
                recorded["unitsExpected"], recorded["unitsOk"],
                recorded["unitsLocked"], recorded["unitsFailed"],
                recorded["unitsUnobserved"], recorded["coverage"],
                recorded["coverageConfirmed"], recorded["staleCount"],
                recorded["durationSeconds"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 统计落库失败（不影响本轮抓取结果）", cycle_id)
