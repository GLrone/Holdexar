"""一轮价格刷新的完整编排：建轮 → 冻结期望集 → 跑链 → 收尾。

自动轮与手动全队列走同一份编排，仅 `kind` 不同——口径不可能分叉。四段
（planning / running / repairing / finalizing）各自独立可失败，任一段失败
不得让用户少一轮价格：建轮/冻结失败不阻断抓取（cycle_id=None 全段照跑）；
收尾段整体兜异常，防 Cycle 行僵死中间态。让路语义属调度器（调用方处理），
编排对「一个段都没跑起来」按 `decide_terminal` 如实判 failed。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.events import bus
from app.core.logging import log_event
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


async def run_price_cycle(
    specs: list[dict], *, kind: str = "scheduled",
    stop_event: asyncio.Event | None = None,
) -> bool:
    """跑完一轮价格刷新，返回**本轮是否值得让调用方让路重试**。

    返回 False 仅当本轮一个任务都没起来且属于「被占、稍后跑得动」；链异常与
    建轮失败返回 True（重试只会重复建轮、二次广播同一终态）。`kind` 仅作
    归属标签落 `price_cycles.kind`（scheduled=定时网格 / manual=用户手动）。
    `stop_event` 缺省时自建并登记为链级停止位（stop 端点经 service 置位即停
    整轮）；传入时直接用作链令牌。"""
    chain_stop = stop_event if stop_event is not None else asyncio.Event()
    crawl_service.register_chain_stop(chain_stop)
    try:
        return await _run_price_cycle(specs, kind=kind, stop_event=chain_stop)
    finally:
        crawl_service.unregister_chain_stop(chain_stop)


async def _run_price_cycle(
    specs: list[dict], *, kind: str, stop_event: asyncio.Event
) -> bool:
    """一轮编排本体（建轮 → 冻结 → 跑链 → 收尾），链级停止位由入口登记。"""
    cycle_id: int | None = None
    regions: list[str] | None = None
    expected_units = 0
    try:
        cycle_id = await price_cycle.create(kind, "pool")
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "本轮价格刷新周期创建失败，抓取照常进行（本轮不挂周期）",
            tag="降级",
            level=logging.ERROR,
            exc_info=True,
        )
    if cycle_id is not None:
        try:
            regions, expected_units = await price_cycle.freeze_expected(cycle_id, specs)
        except Exception:  # noqa: BLE001
            # 冻结失败不影响抓取：本轮照跑，终态按「无区可爬 / 无期望集」收 failed
            log_event(
                logger,
                "本轮期望集冻结失败，抓取照常进行（本轮终态记失败）",
                tag="降级",
                level=logging.ERROR,
                exc_info=True,
            )
            regions = None

    entered_repairing = False
    started = False
    skipped: list[str] = []
    try:
        await price_cycle.advance(cycle_id, price_cycle.RUNNING)
        results = await crawl_service.run_sequential(
            specs, cycle_id=cycle_id, skip_reasons=skipped, stop_event=stop_event
        )
        started = bool(results)
        if not results and specs:
            log_event(
                logger,
                "本轮没有任何抓取段启动",
                detail={"跳过原因": "；".join(skipped) if skipped else "无跳过留痕"},
            )
        else:
            log_event(
                logger,
                f"本轮链已跑完，共启动 {len(results)} 个爬取任务",
                detail={"任务": "/".join(str(r["id"]) for r in results)},
            )
        # 本轮结束时仍有待补欠账 → 本轮进入 repairing。补抓由既有 5min
        # repair 通道承担（它扫全库账本，与本轮期望集没有确定性关联，job
        # 暂不挂 Cycle），Cycle 只把「本轮确实遗留了未覆盖单元」记下来。
        # 用户已停整轮时不进 repairing——停止语义是「到此为止」，不是欠账信号。
        if (
            cycle_id is not None
            and not stop_event.is_set()
            and await crawl_service.has_pending_missing()
        ):
            entered_repairing = await price_cycle.advance(
                cycle_id, price_cycle.REPAIRING
            )
    except Exception as e:  # noqa: BLE001
        # 链异常 = 本轮确实跑过（跑崩了），不是「被占用」。返回 True 让调用方
        # 不触发让路重试：重试一个崩掉的链只会再崩一遍，并把同一轮的失败
        # 广播两次（终态推进第二次会被 advance 拒绝，但事件已发出去）。
        log_event(
            logger,
            "本轮价格链执行异常",
            level=logging.ERROR,
            exc_info=True,
        )
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
            if not jobs and stop_event is not None and stop_event.is_set():
                # 停在了第一个段之前：用户意图明确，不按「无事可做/没跑起来」记
                terminal = price_cycle.CANCELLED
                terminal_error = "用户已停止"
            elif terminal == price_cycle.FAILED and not jobs:
                detail = "；".join(skipped[:4]) if skipped else "原因未随段留痕（详见服务日志）"
                terminal_error = f"本轮没有任何抓取段启动：{detail}"[:200]
            elif terminal == price_cycle.FAILED:
                terminal_error = f"本轮 {len(jobs)} 个抓取段全部失败，明细见任务列表"
        if not await price_cycle.advance(cycle_id, price_cycle.FINALIZING):
            return True if stop_event.is_set() else started
        # 停止的轮跳过事件检测与通知：未观察到的单元不等于「不可用」，半轮
        # 事实不产生价格事件、不发邮件；终态与统计照常落账
        if stop_event.is_set():
            log_event(
                logger,
                f"用户已停止周期 {cycle_id}，跳过价格事件检测与通知",
                tag="跳过",
                detail={"周期": cycle_id},
            )
        else:
            # 事件检测排在本轮最终有效结果之上（finalizing 内、终态之前）：job 自己
            # 产生事件会让「暂时失败→随后补抓成功」的单元先报不可用再报恢复
            await detect_events(cycle_id)
        await settle_cycle(cycle_id, terminal, error=terminal_error)
        log_event(
            logger,
            f"价格刷新周期 {cycle_id} 已收敛：抓取任务 {len(jobs)} 个、期望 {expected_units} 单元",
            detail={
                "周期": cycle_id,
                "类型": kind,
                "终态": terminal,
                "任务数": len(jobs),
                "期望单元": expected_units,
            },
        )
        # 通知是 Cycle 收敛后的下游副作用：失败只影响候选状态，不影响终态与统计
        if not stop_event.is_set():
            await notify_events(cycle_id)
        await record_stats(cycle_id)
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            f"价格刷新周期 {cycle_id} 收尾异常，兜底收敛为失败",
            tag="降级",
            level=logging.ERROR,
            exc_info=True,
            detail={"周期": cycle_id},
        )
        await settle_cycle(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        await record_stats(cycle_id)
        # 本轮抓取已经跑完（收尾崩的是记账，不是抓取）——同异常路径，
        # 返回 True 让调用方不重试，重试不会补上抓取只会重复广播
        return True
    # 停止的轮返回 True：调用方（调度器让路重试）不得把「用户喊停」当成
    # 「被占、稍后重跑」再拉起一轮
    return True if stop_event.is_set() else started


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
            log_event(
                logger,
                f"价格刷新周期 {cycle_id} 检测到 {len(written)} 条价格变化事件",
                detail={
                    "周期": cycle_id,
                    "事件数": len(written),
                    "类型": "/".join(sorted({e["event_type"] for e in written})),
                },
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            f"价格刷新周期 {cycle_id} 的价格变化事件检测失败（不影响本轮结果）",
            level=logging.ERROR,
            exc_info=True,
            detail={"周期": cycle_id},
        )


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
        log_event(
            logger,
            f"价格刷新周期 {cycle_id} 的通知投递失败（不影响本轮结果）",
            level=logging.ERROR,
            exc_info=True,
            detail={"周期": cycle_id},
        )


async def record_stats(cycle_id: int | None) -> None:
    """Cycle 收敛后留下本轮生产统计（观测结果，不参与任何控制）。

    统计失败只记日志：本轮抓取结果已经落库，统计缺失不该影响它。
    """
    if cycle_id is None:
        return
    try:
        recorded = await price_stats.record_stats(cycle_id)
        if recorded is not None:
            log_event(
                logger,
                f"价格刷新周期 {cycle_id} 生产统计已落账：对象覆盖 {recorded['targetsDone']}/{recorded['targetsTotal']}",
                detail={
                    "周期": cycle_id,
                    "完成对象": recorded["targetsDone"],
                    "对象总数": recorded["targetsTotal"],
                    "期望单元": recorded["unitsExpected"],
                    "正常": recorded["unitsOk"],
                    "不可售": recorded["unitsLocked"],
                    "失败": recorded["unitsFailed"],
                    "未观察": recorded["unitsUnobserved"],
                    "覆盖率": recorded["coverage"],
                    "确认覆盖": recorded["coverageConfirmed"],
                    "已过期待刷新": recorded["staleCount"],
                    "耗时": f"{recorded['durationSeconds']}秒",
                },
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            f"价格刷新周期 {cycle_id} 的统计落库失败（不影响本轮抓取结果）",
            level=logging.ERROR,
            exc_info=True,
            detail={"周期": cycle_id},
        )
