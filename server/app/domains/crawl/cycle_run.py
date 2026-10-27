"""一轮价格刷新的完整编排：建轮 → 冻结期望集 → 跑链 → 收尾。

自动轮与手动全队列走同一份编排，仅 `kind` 不同——口径不可能分叉。四段
（planning / running / repairing / finalizing）各自独立可失败，任一段失败
不得让用户少一轮价格：建轮/冻结失败不阻断抓取（cycle_id=None 全段照跑）；
收尾段整体兜异常，防 Cycle 行僵死中间态。未启动目标留给正常补抓。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from app.core.events import bus
from app.core.logging import log_event
from app.domains.crawl import cycle as price_cycle
from app.domains.crawl import events as price_events
from app.domains.crawl import service as crawl_service
from app.domains.crawl import stats as price_stats
from app.domains.proxies import env_check

logger = logging.getLogger(__name__)


def env_abort_reason(skipped: list[str]) -> str | None:
    """本轮是否因本机网络被接管而中止（从段级拒因派生，不另立状态）。"""
    if any(env_check.USER_MESSAGE in s for s in skipped):
        return env_check.USER_MESSAGE
    return None


async def run_price_cycle(
    specs: list[dict], *, kind: str = "scheduled",
    stop_event: asyncio.Event | None = None,
    prepared: tuple[int, list[str] | None, int] | None = None,
) -> bool:
    """执行一轮价格刷新；占用时留给下一正常调度，停止信号贯穿整轮。"""
    chain_stop = stop_event if stop_event is not None else asyncio.Event()
    if (crawl_service._chain_stop is not None and crawl_service._chain_stop is not chain_stop) or (prepared is None and crawl_service.full_queue_running()):
        return False
    crawl_service.register_chain_stop(chain_stop)
    try:
        return await _run_price_cycle(specs, kind=kind, stop_event=chain_stop, prepared=prepared)
    finally:
        crawl_service.unregister_chain_stop(chain_stop)


async def _run_price_cycle(
    specs: list[dict], *, kind: str, stop_event: asyncio.Event,
    prepared: tuple[int, list[str] | None, int] | None = None,
) -> bool:
    """一轮编排本体（建轮 → 冻结 → 跑链 → 收尾），链级停止位由入口登记。"""
    cycle_id: int | None = None
    regions: list[str] | None = None
    expected_units = 0
    try:
        cycle_id = prepared[0] if prepared else await price_cycle.create(kind, "pool", stop_event=stop_event)
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "本轮价格刷新周期创建失败，抓取照常进行（本轮不挂周期）",
            tag="降级",
            level=logging.ERROR,
            exc_info=True,
        )
    if cycle_id is not None:
        crawl_service.register_chain_stop(stop_event, cycle_id=cycle_id)
        try:
            if prepared:
                cycle_id, regions, expected_units = prepared
            else:
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
            specs, cycle_id=cycle_id, skip_reasons=skipped, stop_event=stop_event, trigger=kind
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
        log_event(
            logger,
            "本轮价格链执行异常",
            level=logging.ERROR,
            exc_info=True,
        )
        # 兜底收敛自身再败（同拍两连败）也不外逃：Cycle 行留给重启收尸，
        # 调用方按正常返回值走
        with contextlib.suppress(Exception):
            await settle_cycle(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        with contextlib.suppress(Exception):
            await record_stats(cycle_id)
        return True

    if cycle_id is None:
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
        env_aborted = env_abort_reason(skipped)
        summary = await price_cycle.get(cycle_id)
        deferred = (summary or {}).get("deferredSpecs", [])
        if regions is None:
            terminal = price_cycle.FAILED
            terminal_error = "本轮期望集冻结失败，未按本轮口径记录覆盖"
        else:
            terminal = price_cycle.decide_terminal(
                [j["status"] for j in jobs],
                expected_units=expected_units,
                entered_repairing=entered_repairing,
                deferred=bool(deferred),
            )
            if env_aborted is not None and terminal != price_cycle.COMPLETED:
                # 环境拒因压过通用失败文案：账本上要能看出「本轮是让路停的，
                # 不是爬挂了」，否则使用者会对着失败记录找不存在的故障。
                # 一个段都没起来时按取消记：本轮是主动不跑，不是跑坏了
                terminal_error = env_aborted
                if not jobs:
                    terminal = price_cycle.CANCELLED
            elif not jobs and stop_event is not None and stop_event.is_set():
                # 停在了第一个段之前：用户意图明确，不按「无事可做/没跑起来」记
                terminal = price_cycle.CANCELLED
                terminal_error = "用户已停止"
            elif terminal == price_cycle.FAILED and not jobs:
                detail = "；".join(skipped[:4]) if skipped else "原因未随段留痕（详见服务日志）"
                terminal_error = f"本轮没有任何抓取段启动：{detail}"[:200]
            elif terminal == price_cycle.FAILED:
                terminal_error = f"本轮 {len(jobs)} 个抓取段全部失败，明细见任务列表"
            elif deferred:
                terminal_error = "本轮有未更新的游戏，已保留并将在后续更新中优先补跑"
        if not await price_cycle.advance(cycle_id, price_cycle.FINALIZING):
            return True if stop_event.is_set() else started
        # 收尾段（标记刷新/事件检测/统计）分钟级且无抓取在跑：实时告知前端
        # 切「汇总中」呈现，别让岛停在「正在抓取 100%」像卡死
        bus.publish("price_cycle.finalizing", cycleId=cycle_id)
        # 停止的轮跳过事件检测与通知：未观察到的单元不等于「不可用」，半轮
        # 事实不产生价格事件、不发邮件；终态与统计照常落账
        if stop_event.is_set():
            log_event(
                logger,
                (
                    f"本机网络被接管，周期 {cycle_id} 跳过价格事件检测与通知"
                    if env_aborted is not None
                    else f"用户已停止周期 {cycle_id}，跳过价格事件检测与通知"
                ),
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
        # 兜底收敛自身再败（同拍两连败）也不外逃：Cycle 行留给重启收尸
        with contextlib.suppress(Exception):
            await settle_cycle(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        with contextlib.suppress(Exception):
            await record_stats(cycle_id)
        return True
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
