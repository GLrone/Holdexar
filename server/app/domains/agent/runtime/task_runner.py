"""任务执行器：把域内长任务接进运行账本（受理 → 采样 → 收敛）。

与 fake 执行器的分工：fake 验证 Runtime 骨架；本执行器承载真实长任务。
职责边界与 fake 一致——只经 service 层跃迁状态与落事件，不触碰状态行。

三条纪律：
1. **受理即返回**：`spec.start()` 只把任务交给域内既有编排，本执行器不等待
   业务完成体，只按采样间隔观察域账本投影；
2. **进度是观测，不是账本**：观测失败只记日志、不杀任务（fail-soft），
   业务事实仍以域账本为准，运行账本记的是「这一次调度跑成什么样」；
3. **取消是协作式的**：置令牌后由本执行器在采样边界退出，并调用域内既有
   停止口——不 cancel 域内链任务（那会把域账本停在中间态）。
"""
from __future__ import annotations

import logging
import time

from app.core.logging import log_event
from app.domains.agent import service as agent_service
from app.domains.agent.runtime import events as agent_events_spec
from app.domains.agent.runtime import tasks as task_registry
from app.domains.agent.runtime.cancellation import REGISTRY, CancelToken
from app.domains.agent.runtime.state import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    IllegalRunTransition,
    map_domain_status,
)

logger = logging.getLogger(__name__)

# 采样间隔：既是进度上账的节拍，也是取消请求的最坏延迟
POLL_INTERVAL = 5.0
# 受理后域侧迟迟观测不到活动的最长等待：超时按「未观测到活动」收敛 done
# （宁可如实记成 done + note=unobserved，也不把进程内句柄的瞬时态当失败）
START_GRACE = 120.0


def _payload(obs: dict) -> dict:
    return {
        "phase": obs.get("phase"),
        "done": obs.get("done"),
        "total": obs.get("total"),
        "note": obs.get("note"),
    }


async def execute_task_run(
    run_id: str,
    *,
    kind: str,
    ref: dict | None = None,
    adopted: bool = False,
    poll_interval: float | None = None,
    start_grace: float | None = None,
) -> None:
    """执行一次任务调度；终态由本函数收敛（域侧异常不外逃）。

    `adopted=True` 表示受理已在调用方完成（调度面同步校验过域内占用闸，
    拿到确切的受理结果才建 run）——本执行器不再重复受理，只接管观察与
    取消；此时域侧活动已被确认，故不等待首次活动采样。
    `poll_interval` / `start_grace` 缺省取模块常量（测试可整体调快）。"""
    interval = POLL_INTERVAL if poll_interval is None else poll_interval
    grace = START_GRACE if start_grace is None else start_grace
    token = REGISTRY.token_of(run_id) or CancelToken()
    spec = task_registry.get(kind)
    try:
        if spec is None:
            await agent_service.apply_transition(
                run_id, RUN_FAILED, error_code="unknown_task"
            )
            return
        await agent_service.apply_transition(run_id, RUN_RUNNING)
        if adopted:
            accepted = dict(ref or {})
        else:
            try:
                accepted = await spec.start()
            except (RuntimeError, ValueError) as e:
                # 域侧业务拒绝（占用 / 无可抓对象）：如实记原因，不重试
                await agent_service.append_event(
                    run_id, agent_events_spec.EV_TASK_FAILED,
                    {"reason": str(e)[:200], "error_code": "rejected"},
                )
                await agent_service.apply_transition(
                    run_id, RUN_FAILED, error_code="rejected"
                )
                return
            except Exception:  # noqa: BLE001 —— 受理异常不外逃，账本记 failed
                log_event(
                    logger,
                    f"任务「{kind}」受理失败，本次执行按失败收敛",
                    level=logging.ERROR,
                    exc_info=True,
                    detail={"任务": kind},
                )
                await agent_service.apply_transition(
                    run_id, RUN_FAILED, error_code="start_error"
                )
                return

        ctx = {**(ref or {}), **(accepted or {})}
        await agent_service.append_event(
            run_id, agent_events_spec.EV_TASK_STARTED,
            {"kind": kind, "label": spec.label, "total": ctx.get("total")},
        )

        seen_active = adopted
        last_sig: tuple | None = None
        last_obs: dict | None = None
        t0 = time.monotonic()
        while True:
            try:
                obs = await spec.observe(ctx)
            except Exception:  # noqa: BLE001 —— 观测失败不杀任务（fail-soft）
                log_event(
                    logger,
                    f"任务「{kind}」进度观测失败，已跳过本次观测继续执行",
                    level=logging.ERROR,
                    exc_info=True,
                    detail={"任务": kind},
                )
                obs = None
            if obs:
                last_obs = obs
                if obs.get("active"):
                    seen_active = True
                sig = (obs.get("phase"), obs.get("done"), obs.get("total"), obs.get("note"))
                if sig != last_sig:
                    last_sig = sig
                    await agent_service.append_event(
                        run_id, agent_events_spec.EV_TASK_PROGRESS, _payload(obs)
                    )
                idle = obs.get("phase") == "idle"
                elapsed = time.monotonic() - t0
                if idle and (seen_active or elapsed > grace):
                    break
                if not seen_active and elapsed > grace:
                    await agent_service.append_event(
                        run_id, agent_events_spec.EV_TASK_PROGRESS,
                        {"phase": "idle", "note": "unobserved"},
                    )
                    break
            if await token.wait(interval):
                break

        if token.requested:
            if spec.stop is not None:
                try:
                    await spec.stop()
                except Exception:  # noqa: BLE001 —— 停止失败不改变「已请求取消」
                    log_event(
                        logger,
                        f"任务「{kind}」停止请求失败，已请求的取消不受影响",
                        level=logging.ERROR,
                        exc_info=True,
                        detail={"任务": kind},
                    )
            await agent_service.apply_transition(run_id, RUN_CANCELLED, reason="user")
        else:
            # 终态按域账本投影：观察到域终态才下业务结论；没观察到活动
            # 一律 done + unobserved（进程内句柄瞬时态不冒充业务失败）
            outcome = (last_obs or {}).get("outcome") if seen_active else None
            run_state = map_domain_status(outcome) or RUN_DONE
            if run_state == RUN_FAILED:
                await agent_service.append_event(
                    run_id, agent_events_spec.EV_TASK_FAILED,
                    {"reason": f"domain_status={outcome}", "error_code": "domain_failed"},
                )
                await agent_service.apply_transition(
                    run_id, RUN_FAILED, error_code="domain_failed"
                )
            elif run_state == RUN_CANCELLED:
                await agent_service.apply_transition(run_id, RUN_CANCELLED, reason="domain")
            else:
                await agent_service.apply_transition(run_id, RUN_DONE)
    except IllegalRunTransition:
        # 状态已被他方收敛（孤儿收尸 / 终态后取消）：本轮执行作废
        log_event(
            logger,
            f"运行 {run_id} 的状态已被他方收敛，本轮执行作废",
            tag="忽略",
            detail={"运行": run_id},
        )
    finally:
        REGISTRY.pop(run_id)
