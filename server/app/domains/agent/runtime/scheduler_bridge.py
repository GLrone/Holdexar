"""运行账本桥：APScheduler job / 手动入口 / CLI 的调度事实统一入账。

口径（阶段四设计稿，域账本语义零变化）：
- 一次 job 调用 = agent_runs 一行（runner='job'），开始 / 结束 / 异常经
  service 层状态机落 run.state / run.finished 事件；
- 业务事实不进这里：job 体照常写域账本；桥只提供段位流水（note_step）
  与终态采样（登记过 task kind 的 job 按域终态映射收口）；
- 全链 fail-soft：账本写失败只记日志、绝不拖垮业务执行；最坏情形是
  run 停在 running，由启动收尸（reconcile_orphan_runs）收敛；
- job 体可通过 note_step 向当前 run 追加「为什么没跑」类段位流水，
  上下文经 ContextVar 传递（asyncio 任务创建时复制，不跨任务泄漏）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta

from app.core.database import WritePriority, get_session_factory, write_gate
from app.core.logging import log_event
from app.domains.agent import service as agent_service
from app.domains.agent.runtime import events as agent_events_spec
from app.domains.agent.runtime import tasks as task_registry
from app.domains.agent.runtime.state import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    map_domain_status,
)

logger = logging.getLogger(__name__)

_current_run: ContextVar[str | None] = ContextVar("agent_current_run", default=None)

# job 型运行账保留窗口：调度流水只服务「最近跑没跑、为什么没跑」，
# 过窗级联删除（agent_events 外键 CASCADE）；task/fake run（领航会话
# 回放依据）不受本清理影响
JOB_RUN_RETENTION_DAYS = 30


def current_run_id() -> str | None:
    return _current_run.get()


async def note_step(step: str, summary: dict | None = None) -> bool:
    """向当前 run 追加一段运行流水；无 run 上下文或落账失败都是 no-op。"""
    run_id = _current_run.get()
    if run_id is None:
        return False
    try:
        await agent_service.append_event(
            run_id,
            agent_events_spec.EV_STEP_COMPLETED,
            {"step": step, "summary": dict(summary or {})},
        )
        return True
    except Exception:  # noqa: BLE001 —— 流水失败不影响 job 体
        log_event(
            logger,
            f"记录运行流水「{step}」失败，本段流水已丢弃",
            level=logging.ERROR,
            exc_info=True,
            detail={"流水段": step},
        )
        return False


async def run_accounted(
    job_id: str,
    fn,
    *,
    trigger: str = "scheduled",
    task_kind: str | None = None,
    ref: dict | None = None,
):
    """一次 job 调用的账本生命周期；返回 fn() 的结果（异常原样抛出）。

    task_kind 给定时（job 对应任务登记表条目），结束后采样一次域账本，
    按 state.map_domain_status 把域终态投影成 run 终态；采样不到业务
    结论按 done 收敛——run 不冒充业务裁判。"""
    run_id: str | None = None
    try:
        await agent_service.ensure_system_session()
        created = await agent_service.create_run(
            session_id=agent_service.SYSTEM_SESSION_ID,
            runner="job",
            trigger=trigger,
            meta={
                "job": job_id,
                "entry": trigger,
                "task": task_kind,
                "ref": dict(ref or {}),
            },
        )
        run_id = created["run_id"]
    except Exception:  # noqa: BLE001 —— 账本不可用不拖垮业务
        log_event(
            logger,
            f"定时任务「{job_id}」建立运行账本失败，业务照常执行",
            level=logging.ERROR,
            exc_info=True,
            detail={"任务": job_id},
        )

    token = _current_run.set(run_id)
    try:
        if run_id is None:
            return await fn()
        with contextlib.suppress(Exception):
            await agent_service.apply_transition(run_id, RUN_RUNNING)
        try:
            result = await fn()
        except asyncio.CancelledError:
            await _settle(run_id, RUN_CANCELLED, reason="runtime_stopped")
            raise
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                f"定时任务「{job_id}」执行出错，运行账本按失败收敛",
                level=logging.ERROR,
                exc_info=True,
                detail={"任务": job_id},
            )
            await _settle_event(
                run_id, agent_events_spec.EV_TASK_FAILED,
                {"reason": str(e)[:200], "error_code": "job_error"},
            )
            await _settle(run_id, RUN_FAILED, error_code="job_error")
            raise
        status = await _sample_outcome(run_id, task_kind, ref)
        await _settle(
            run_id, status, error_code="domain_failed" if status == RUN_FAILED else None
        )
        return result
    finally:
        _current_run.reset(token)


async def _settle(run_id: str, status: str, *, reason: str | None = None,
                  error_code: str | None = None) -> None:
    with contextlib.suppress(Exception):
        await agent_service.apply_transition(
            run_id, status, reason=reason, error_code=error_code
        )


async def _settle_event(run_id: str, event_type: str, payload: dict) -> None:
    with contextlib.suppress(Exception):
        await agent_service.append_event(run_id, event_type, payload)


async def _sample_outcome(run_id: str, task_kind: str | None, ref: dict | None) -> str:
    if not task_kind:
        return RUN_DONE
    spec = task_registry.get(task_kind)
    if spec is None:
        return RUN_DONE
    try:
        obs = await spec.observe(dict(ref or {}))
    except Exception:  # noqa: BLE001 —— 采样失败按 done 收敛，细节在域账本
        log_event(
            logger,
            f"任务「{task_kind}」终态采样失败，本次运行按已完成收敛",
            level=logging.ERROR,
            exc_info=True,
            detail={"任务": task_kind},
        )
        return RUN_DONE
    outcome = (obs or {}).get("outcome")
    if outcome is not None:
        await _settle_event(
            run_id, agent_events_spec.EV_TASK_PROGRESS,
            {"phase": "idle", "note": str(outcome)},
        )
    return map_domain_status(outcome) or RUN_DONE


async def prune_job_runs(days: int = JOB_RUN_RETENTION_DAYS) -> int:
    """清理窗口外的 job 型运行账（行删级联事件）；task/fake run 不动。"""
    from sqlalchemy import delete

    from app.domains.agent.models import AgentRun

    cutoff = datetime.now() - timedelta(days=days)
    async with write_gate(WritePriority.BACKGROUND, "agent_prune"), get_session_factory()() as session:
        result = await session.execute(
            delete(AgentRun).where(
                AgentRun.runner == "job", AgentRun.created_at < cutoff
            )
        )
        await session.commit()
        return int(result.rowcount or 0)
