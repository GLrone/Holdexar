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

# job_id → 在飞 run_id：job 跳拍事件（上一轮还没跑完/到点错失）由调度事件
# 监听器按 job_id 投给在飞 run（监听器跑在调度器分发上下文，拿不到 ContextVar）
_RUNNING_JOB_RUNS: dict[str, str] = {}

# job 型运行账保留窗口：调度流水只服务「最近跑没跑、为什么没跑」，
# 过窗级联删除（agent_events 外键 CASCADE）；task/fake run（领航会话
# 回放依据）不受本清理影响
JOB_RUN_RETENTION_DAYS = 30
# 分块删除（每块一个事务过写闸）：一次长 DELETE 会长时间持写锁，与
# 遥测保留模块同一硬要求；删不完留给下一轮
_PRUNE_CHUNK = 5000
_PRUNE_MAX_CHUNKS = 20


def current_run_id() -> str | None:
    return _current_run.get()


async def _append_step(run_id: str, step: str, summary: dict | None) -> bool:
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


async def note_step(step: str, summary: dict | None = None) -> bool:
    """向当前 run 追加一段运行流水；无 run 上下文或落账失败都是 no-op。"""
    run_id = _current_run.get()
    if run_id is None:
        return False
    return await _append_step(run_id, step, summary)


async def note_job_skipped(job_id: str, step: str, summary: dict | None = None) -> bool:
    """向 job 当前在飞的 run 记让路流水；无在飞 run 返回 False。"""
    run_id = _RUNNING_JOB_RUNS.get(job_id)
    if run_id is None:
        return False
    return await _append_step(run_id, step, summary)


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
    if task_kind is not None and not (ref or {}).get("startedAt"):
        # 桥接采样只认本 run 建账后启动的 job/cycle：不补锚点会把历史
        # 或手动轮的业务终态错收进本次 run（受理路径同源构造）
        from app.crawler.utils import get_beijing_time_obj

        ref = {
            **dict(ref or {}),
            "startedAt": get_beijing_time_obj().replace(tzinfo=None).isoformat(),
        }
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
        _RUNNING_JOB_RUNS[job_id] = run_id
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
        if run_id is not None and _RUNNING_JOB_RUNS.get(job_id) == run_id:
            _RUNNING_JOB_RUNS.pop(job_id, None)
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
    """清理窗口外的 job 型运行账（行删级联事件）；task/fake run 不动。

    分块删除：每块一个事务过写闸，块间写者位空出；单轮块上限内删不完
    留给下一轮（每日 04:36 一拍）。"""
    from sqlalchemy import text

    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    factory = get_session_factory()
    for _ in range(_PRUNE_MAX_CHUNKS):
        async with write_gate(WritePriority.BACKGROUND, "agent_prune"), factory() as session:
            result = await session.execute(
                text(
                    "DELETE FROM agent_runs WHERE id IN ("
                    "SELECT id FROM agent_runs"
                    " WHERE runner = 'job' AND created_at < :cutoff"
                    " ORDER BY id LIMIT :chunk)"
                ),
                {"cutoff": cutoff, "chunk": _PRUNE_CHUNK},
            )
            await session.commit()
        affected = int(result.rowcount or 0)
        deleted += affected
        if affected < _PRUNE_CHUNK:
            return deleted
    return deleted
