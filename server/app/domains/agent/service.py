"""Agent Runtime 服务层：账本写入、状态跃迁、取消收敛、事件回放。

写路径约定（与全域一致）：全部写事务经 `write_gate()` 进入全局单写者
调度，本模块不持有任何独立写锁 / 写槽 / 提交机制。状态跃迁唯一入口是
`apply_transition`——校验合法迁移、更新 run 状态行、写入对应事件，
三者在同一写事务内完成；`AgentRun.status` 不得在本模块之外赋值。

事件 seq 在写事务内按 max(seq)+1 分配，(run_id, seq) 唯一约束兜底；
回放按 seq 升序 + after_seq 游标续读，不依赖「最近 N 条」窗口。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
import uuid

from sqlalchemy import String, and_, cast, func, literal, not_, or_, select

from app.core.database import (
    WritePriority,
    get_session_factory,
    write_session,
)
from app.core.logging import log_event
from app.crawler.utils import get_beijing_time_obj
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.runtime import events as agent_events_spec
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    RUN_AWAITING_APPROVAL,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    TERMINAL_STATES,
    assert_transition,
    is_terminal,
    DOMAIN_TERMINAL_TO_RUN,
)

logger = logging.getLogger(__name__)

TASK_LABELS = {"price_refresh": "priceRefresh", "price_repair": "priceRepair",
               "price_missing": "priceMissing", "dev_pytest": "devPytest"}
PRICE_TASKS = ("price_refresh", "price_repair", "price_missing")
_DOMAIN_STATUSES = {
    **DOMAIN_TERMINAL_TO_RUN,
    "pending": RUN_QUEUED,
    "planning": RUN_RUNNING,
    "running": RUN_RUNNING,
    "repairing": RUN_RUNNING,
    "finalizing": RUN_RUNNING,
    "interrupted": RUN_FAILED,
}


class RunCancellationUnavailable(ValueError):
    """No live execution handle belongs to the requested run."""

# 事件回放单页上限：客户端以 after_seq 游标翻页，页大小不改变账本全量可读性
_REPLAY_PAGE_LIMIT = 200

# 重启收尸覆盖的未终态集合：进程内执行者已随进程消失，这些状态的 run
# 不可能再推进
_ORPHAN_STATUSES = (RUN_QUEUED, RUN_RUNNING, RUN_AWAITING_APPROVAL)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now():
    return get_beijing_time_obj().replace(tzinfo=None)


# 进程启动时刻（模块导入即进程启动）：收尸只认上一进程遗留的 run——
# 后台收拾链可能晚于首个用户请求跑完，开门后新建的 run 是活 run 不得误收敛
_PROCESS_STARTED_AT = _now()


async def _next_seq(session, run_id: str) -> int:
    current = await session.scalar(
        select(func.max(AgentEvent.seq)).where(AgentEvent.run_id == run_id)
    )
    return int(current or 0) + 1


async def create_run(
    *,
    session_id: str | None = None,
    prompt: str | None = None,
    trigger: str = "manual",
    runner: str = "fake",
    meta: dict | None = None,
) -> dict:
    """建 run（queued）。session_id 缺省时自动建会话；prompt 有值时
    同事务落 turn.received 事件（消息历史的派生源）。"""
    rid = _new_id()
    ts = _now()
    async with write_session(WritePriority.INTERACTIVE, "agent_run") as session:
        if session_id is None:
            sid = _new_id()
            session.add(AgentSession(id=sid, created_at=ts, updated_at=ts))
            # 先落会话行再建 run：生产库开着 PRAGMA foreign_keys=ON，同批插入的
            # 语句顺序不受保证，不显式 flush 会撞 agent_runs.session_id 外键
            await session.flush()
        else:
            existing = await session.get(AgentSession, session_id)
            if existing is None:
                raise LookupError(f"session not found: {session_id}")
            sid = session_id
            existing.updated_at = ts
        session.add(AgentRun(
            id=rid, session_id=sid, runner=runner, trigger=trigger,
            meta=meta, status=RUN_QUEUED, created_at=ts,
        ))
        if prompt:
            seq = await _next_seq(session, rid)
            session.add(AgentEvent(
                run_id=rid, seq=seq, event_type=agent_events_spec.EV_TURN_RECEIVED,
                payload=agent_events_spec.build_payload(
                    agent_events_spec.EV_TURN_RECEIVED, {"text": prompt}),
                created_at=ts,
            ))
        await session.commit()
    return {"run_id": rid, "session_id": sid, "status": RUN_QUEUED}


# 非交互 run 的统一挂靠会话：调度桥 / CLI / 手动受理的运行账本都锚在
# 这一行（agent_runs.session_id 非空外键）；领航台会话列表走 pilot
# SessionStore（JSONL 文件），不受此行影响
SYSTEM_SESSION_ID = "runtime"


async def ensure_system_session() -> str:
    """挂靠会话幂等建（首次一次写，之后零开销）。"""
    async with write_session(WritePriority.INTERACTIVE, "agent_session") as session:
        if await session.get(AgentSession, SYSTEM_SESSION_ID) is None:
            ts = _now()
            session.add(AgentSession(id=SYSTEM_SESSION_ID, created_at=ts, updated_at=ts))
            await session.commit()
    return SYSTEM_SESSION_ID


async def start_run(run_id: str) -> dict:
    """启动真实开发任务或模拟运行；价格执行留在原领域入口。"""
    run = await get_run(run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    if run["runner"] == "task" and (run.get("meta") or {}).get("task") == "dev_pytest":
        from app.domains.agent.runtime import dev_tasks

        selection = str((run.get("meta") or {}).get("select") or "")
        dev_tasks.command(selection)
        assert_transition(run["status"], RUN_RUNNING)
        REGISTRY.register(run_id)
        task = asyncio.create_task(dev_tasks.execute_run(run_id, selection=selection))
        REGISTRY.attach_task(run_id, task)
        return {"run_id": run_id, "started": True}
    if run["runner"] != "fake":
        raise ValueError("this runner has no executor here")
    assert_transition(run["status"], RUN_RUNNING)
    REGISTRY.register(run_id)
    meta = run.get("meta") or {}
    from app.domains.agent.runtime import fake

    task = asyncio.create_task(fake.execute_fake_run(
        run_id,
        scenario=str(meta.get("scenario") or "success"),
        steps=int(meta.get("steps") or 3),
        step_delay=float(meta.get("step_delay") or 0.0),
    ))
    REGISTRY.attach_task(run_id, task)
    return {"run_id": run_id, "started": True}


async def apply_transition(
    run_id: str, target: str, *, reason: str | None = None,
    error_code: str | None = None,
) -> dict:
    """状态跃迁唯一入口：校验 → 状态行 → run.state 事件 → 终态补
    run.finished 事件，全部在单一写事务内。非法迁移抛
    IllegalRunTransition 且不产生任何写入。"""
    async with write_session(WritePriority.INTERACTIVE, "agent_run") as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        current = run.status
        assert_transition(current, target)
        ts = _now()
        run.status = target
        if target == RUN_RUNNING and run.started_at is None:
            run.started_at = ts
        if target in TERMINAL_STATES:
            run.finished_at = ts
            if error_code:
                run.error_code = error_code
        seq = await _next_seq(session, run_id)
        session.add(AgentEvent(
            run_id=run_id, seq=seq, event_type=agent_events_spec.EV_RUN_STATE,
            payload=agent_events_spec.build_payload(
                agent_events_spec.EV_RUN_STATE,
                {"from": current, "to": target, "reason": reason}),
            created_at=ts,
        ))
        if target in TERMINAL_STATES:
            seq2 = await _next_seq(session, run_id)
            session.add(AgentEvent(
                run_id=run_id, seq=seq2, event_type=agent_events_spec.EV_RUN_FINISHED,
                payload=agent_events_spec.build_payload(
                    agent_events_spec.EV_RUN_FINISHED,
                    {"status": target, "error_code": error_code}),
                created_at=ts,
            ))
        await session.commit()
        return {"run_id": run_id, "from": current, "status": target}


async def append_event(run_id: str, event_type: str, payload: dict | None = None) -> int:
    """追加事件（账本流水）。payload 经入账边界后落库，返回分配的 seq。"""
    async with write_session(WritePriority.INTERACTIVE, "agent_event") as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        seq = await _next_seq(session, run_id)
        session.add(AgentEvent(
            run_id=run_id, seq=seq, event_type=event_type,
            payload=agent_events_spec.build_payload(event_type, payload),
            created_at=_now(),
        ))
        await session.commit()
        return seq


def _domain_key(run_id: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(cycle|crawl):([1-9][0-9]{0,18})", run_id)
    return (match[1], int(match[2])) if match and int(match[2]) <= 2**63 - 1 else None


def _live_domain_ids() -> tuple[int | None, int | None]:
    from app.domains.crawl import service as crawl_service

    return crawl_service.active_cycle_id(), crawl_service.active_job_id()


def _agent_view(row: AgentRun) -> dict:
    token = REGISTRY.token_of(row.id)
    meta = {**(row.meta or {}), "cancellable": not is_terminal(row.status) and (
        token is not None or row.runner == "fake"
    )}
    return {
        "run_id": row.id, "session_id": row.session_id, "runner": row.runner,
        "trigger": row.trigger, "meta": meta, "status": row.status,
        "error_code": row.error_code, "created_at": row.created_at,
        "started_at": row.started_at, "finished_at": row.finished_at,
    }


def _domain_view(row, source: str, live: tuple[int | None, int | None]) -> dict:
    status = _DOMAIN_STATUSES.get(row.status, RUN_FAILED)
    if source == "cycle":
        task = "price_refresh"
        trigger = row.kind
        progress = {"done": row.batches_done, "total": row.batches_expected,
                    "failed": row.units_failed}
        active_id = live[0]
    else:
        task = {"repair": "price_repair", "missing": "price_missing"}.get(row.kind)
        stats = row.stats_json or {}
        trigger = stats.get("trigger") or "unknown"
        progress = {"done": stats.get("processed"), "total": stats.get("total"), **{
            key: stats[key] for key in ("success", "failed", "skipped_no_discount", "discount_ended")
            if key in stats
        }}
        active_id = live[1]
    meta = {"domainStatus": row.status, "progress": progress,
            "cancellable": not is_terminal(status) and active_id == row.id}
    if task:
        meta["task"] = task
    else:
        meta["job"] = row.kind
    return {
        "run_id": f"{source}:{row.id}", "session_id": SYSTEM_SESSION_ID,
        "runner": "task", "trigger": trigger, "meta": meta, "status": status,
        "error_code": row.error, "created_at": row.started_at,
        "started_at": row.started_at, "finished_at": row.finished_at,
    }


async def get_run(run_id: str) -> dict | None:
    key = _domain_key(run_id)
    if key is not None:
        from app.domains.crawl.cycle import PriceCycle
        from app.domains.crawl.models import CrawlJob

        source, domain_id = key
        async with get_session_factory()() as session:
            row = await session.get(PriceCycle if source == "cycle" else CrawlJob, domain_id)
            if row is None or (source == "crawl" and row.cycle_id is not None):
                return None
            return _domain_view(row, source, _live_domain_ids())
    if run_id.startswith(("cycle:", "crawl:")):
        return None
    async with get_session_factory()() as session:
        row = await session.get(AgentRun, run_id)
        return _agent_view(row) if row is not None else None


async def list_runs(
    *, status: str | None = None, limit: int = 20, price_tasks_only: bool = False,
) -> list[dict]:
    """Merge source-filtered read-only views, excluding historical price mirrors."""
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.crawl.models import CrawlJob

    limit = max(1, min(int(limit), 100))
    live = _live_domain_ids()
    async with get_session_factory()() as session:
        result = []
        stmt = select(AgentRun).where(
            or_(AgentRun.runner != "task", func.coalesce(AgentRun.meta["task"].as_string(), "") == "dev_pytest"),
            not_(and_(AgentRun.runner == "job",
                func.coalesce(AgentRun.meta["task"].as_string(), "").in_(PRICE_TASKS))),
        )
        if price_tasks_only:
            stmt = stmt.where(AgentRun.runner == "task", AgentRun.meta["task"].as_string() == "dev_pytest")
        if status:
            stmt = stmt.where(AgentRun.status == status)
        rows = (await session.scalars(stmt.order_by(
            AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit))).all()
        result.extend(_agent_view(row) for row in rows)
        for source, model in (("cycle", PriceCycle), ("crawl", CrawlJob)):
            stmt = select(model)
            if source == "crawl":
                stmt = stmt.where(model.cycle_id.is_(None))
                if price_tasks_only:
                    stmt = stmt.where(model.kind.in_(("repair", "missing")))
            if status:
                matches = [value for value, mapped in _DOMAIN_STATUSES.items() if mapped == status]
                condition = model.status.in_(matches)
                if status == RUN_FAILED:
                    condition = condition | model.status.not_in(tuple(_DOMAIN_STATUSES))
                stmt = stmt.where(condition)
            run_id = literal(source + ":") + cast(model.id, String)
            rows = (await session.scalars(stmt.order_by(
                model.started_at.desc(), run_id.desc()).limit(limit))).all()
            result.extend(_domain_view(row, source, live) for row in rows)
    result.sort(key=lambda row: (row["created_at"] or datetime.min, row["run_id"]), reverse=True)
    return result[:limit]


async def list_events(run_id: str, *, after_seq: int = 0,
                      limit: int = _REPLAY_PAGE_LIMIT) -> list[dict]:
    """按 seq 升序回放；after_seq 游标之后的事件，支持从任意断点续读。"""
    if _domain_key(run_id) is not None:
        return []
    limit = max(1, min(int(limit), _REPLAY_PAGE_LIMIT))
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id, AgentEvent.seq > int(after_seq))
            .order_by(AgentEvent.seq.asc())
            .limit(limit)
        )).scalars().all()
        return [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in rows
        ]


async def cancel_run(run_id: str, *, reason: str = "user") -> dict:
    """Request the exact domain stop handle or the existing fake cancellation path."""
    run = await get_run(run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    if is_terminal(run["status"]):
        return {"accepted": False, "status": run["status"]}
    key = _domain_key(run_id)
    if key is not None:
        from app.domains.crawl import service as crawl_service

        source, domain_id = key
        live_cycle, live_job = _live_domain_ids()
        if domain_id != (live_cycle if source == "cycle" else live_job):
            raise RunCancellationUnavailable("run has no matching live execution")
        if source == "cycle":
            accepted = await crawl_service.stop_job(cycle_id=domain_id, whole_chain=True)
        else:
            accepted = await crawl_service.stop_job(domain_id, whole_chain=False)
        fresh = await get_run(run_id)
        if fresh is None:
            raise LookupError(f"run not found: {run_id}")
        return {"accepted": accepted, "status": fresh["status"]}
    token = REGISTRY.token_of(run_id)
    if token is None and run["runner"] != "fake":
        raise RunCancellationUnavailable("run has no live stop handle")
    if token is not None:
        first = not token.requested
        if first:
            token.request()
            await append_event(run_id, agent_events_spec.EV_CANCEL_REQUESTED,
                               {"reason": reason, "by": "user"})
        fresh = await get_run(run_id)
        return {"accepted": first, "status": fresh["status"] if fresh else run["status"]}
    async with write_session(WritePriority.INTERACTIVE, "agent_run") as session:
        row = await session.get(AgentRun, run_id)
        if row is None:
            raise LookupError(f"run not found: {run_id}")
        if is_terminal(row.status):
            return {"accepted": False, "status": row.status}
        current = row.status
        assert_transition(current, RUN_CANCELLED)
        ts = _now()
        payloads = (
            (agent_events_spec.EV_CANCEL_REQUESTED,
             {"reason": reason, "by": "user"}),
            (agent_events_spec.EV_RUN_STATE,
             {"from": current, "to": RUN_CANCELLED, "reason": reason}),
            (agent_events_spec.EV_RUN_FINISHED,
             {"status": RUN_CANCELLED, "error_code": None}),
        )
        for event_type, payload in payloads:
            seq = await _next_seq(session, run_id)
            session.add(AgentEvent(
                run_id=run_id, seq=seq, event_type=event_type,
                payload=agent_events_spec.build_payload(event_type, payload),
                created_at=ts,
            ))
        row.status = RUN_CANCELLED
        row.finished_at = ts
        await session.commit()
        return {"accepted": True, "status": RUN_CANCELLED}


async def reconcile_orphan_runs() -> int:
    """启动链收尸：进程重启后未终态 run 的执行者已不存在，一律收敛
    cancelled（reason=runtime_restarted，与用户取消区分）。只收 created_at
    早于本进程启动时刻的行（开门到收尸之间新建的是活 run）。返回收敛数。"""
    async with get_session_factory()() as session:
        ids = (await session.execute(
            select(AgentRun.id).where(
                AgentRun.status.in_(_ORPHAN_STATUSES),
                AgentRun.created_at < _PROCESS_STARTED_AT,
            )
        )).scalars().all()
    count = 0
    for rid in ids:
        try:
            await apply_transition(rid, RUN_CANCELLED, reason="runtime_restarted")
            count += 1
        except LookupError:  # 并发窗口内已被收敛，跳过
            continue
    if count:
        log_event(
            logger,
            f"启动收尸：{count} 条重启前遗留的未完成运行已收敛为已取消",
            detail={"收敛条数": count},
        )
    return count
