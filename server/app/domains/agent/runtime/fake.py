"""fake 执行器：Runtime 骨架生命周期的验证替身，不接真实模型。

生命周期：queued → running → (step.started / step.completed)×N → done；
failure 场景在第 1 步完成后以 fake_error 收敛 failed；取消由令牌在
步边界与工作延时中生效，收敛 cancelled（已完成步的事件保留原样）。

执行器职责边界：只经 service 层跃迁状态与落事件，不触碰状态行；
run 状态已被他方收敛（孤儿收尸、终态后取消）时跃迁抛
IllegalRunTransition，按「本轮执行作废」静默退出。
"""
from __future__ import annotations

import logging

from app.core.logging import log_event
from app.domains.agent import service as agent_service
from app.domains.agent.runtime import events as agent_events_spec
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    IllegalRunTransition,
)

logger = logging.getLogger(__name__)


async def execute_fake_run(
    run_id: str,
    *,
    scenario: str = "success",
    steps: int = 3,
    step_delay: float = 0.0,
) -> None:
    token = REGISTRY.token_of(run_id)
    if token is None:  # 未注册即执行：无取消通道，建本地令牌兜底
        from app.domains.agent.runtime.cancellation import CancelToken

        token = CancelToken()
    try:
        await agent_service.apply_transition(run_id, RUN_RUNNING)
        for n in range(1, max(1, steps) + 1):
            if token.requested:
                break
            await agent_service.append_event(
                run_id, agent_events_spec.EV_STEP_STARTED, {"step": n})
            if scenario == "failure" and n == 1:
                await agent_service.apply_transition(
                    run_id, RUN_FAILED, error_code="fake_error")
                return
            if step_delay > 0:
                if await token.wait(step_delay):
                    break  # 延时中被取消：停止继续执行
            await agent_service.append_event(
                run_id, agent_events_spec.EV_STEP_COMPLETED,
                {"step": n, "summary": f"fake step {n}"})
        if token.requested:
            await agent_service.apply_transition(run_id, RUN_CANCELLED, reason="user")
        else:
            await agent_service.apply_transition(run_id, RUN_DONE)
    except IllegalRunTransition:
        # 状态已被他方收敛（收尸/取消竞态）：本轮执行作废，不与账本对抗
        log_event(
            logger,
            f"模拟运行 {run_id} 的状态已被他方收敛，本轮执行作废",
            tag="忽略",
            detail={"运行": run_id},
        )
    finally:
        REGISTRY.pop(run_id)
