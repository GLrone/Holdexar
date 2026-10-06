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
import uuid

from sqlalchemy import func, select

from app.core.database import (
    WritePriority,
    get_session_factory,
    write_gate,
)
from app.core.logging import log_event
from app.crawler.utils import get_beijing_time_obj
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.runtime import events as agent_events_spec
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    RUN_AWAITING_APPROVAL,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_RUNNING,
    TERMINAL_STATES,
    assert_transition,
    is_terminal,
)

logger = logging.getLogger(__name__)

# 事件回放单页上限：客户端以 after_seq 游标翻页，页大小不改变账本全量可读性
_REPLAY_PAGE_LIMIT = 200

# 重启收尸覆盖的未终态集合：进程内执行者已随进程消失，这些状态的 run
# 不可能再推进
_ORPHAN_STATUSES = (RUN_QUEUED, RUN_RUNNING, RUN_AWAITING_APPROVAL)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now():
    return get_beijing_time_obj().replace(tzinfo=None)


async def _next_seq(session, run_id: str) -> int:
    current = await session.scalar(
        select(func.max(AgentEvent.seq)).where(AgentEvent.run_id == run_id)
    )
    return int(current or 0) + 1


async def create_session(title: str | None = None) -> dict:
    sid = _new_id()
    ts = _now()
    async with write_gate(WritePriority.INTERACTIVE, "agent_session"), get_session_factory()() as session:
        session.add(AgentSession(id=sid, title=title, created_at=ts, updated_at=ts))
        await session.commit()
    return {"session_id": sid, "title": title}


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
    async with write_gate(WritePriority.INTERACTIVE, "agent_run"), get_session_factory()() as session:
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
    async with write_gate(WritePriority.INTERACTIVE, "agent_session"), get_session_factory()() as session:
        if await session.get(AgentSession, SYSTEM_SESSION_ID) is None:
            ts = _now()
            session.add(AgentSession(id=SYSTEM_SESSION_ID, created_at=ts, updated_at=ts))
            await session.commit()
    return SYSTEM_SESSION_ID


async def adopt_task_run(
    *, kind: str, ref: dict | None = None, trigger: str = "manual"
) -> dict:
    """受纳运行：受理已在域内完成（用户入口直接调了域受理口）时建 task 型
    run 并拉起观察执行器（adopted=True，不重复受理）。受理被域拒绝时
    调用方根本走不到这里——HTTP 4xx 即拒绝记录，不为按钮重试造账。"""
    await ensure_system_session()
    created = await create_run(
        session_id=SYSTEM_SESSION_ID,
        runner="task",
        trigger=trigger,
        meta={"task": kind, "adopted": True, "ref": dict(ref or {})},
    )
    await start_run(created["run_id"])
    return created


async def start_run(run_id: str) -> dict:
    """启动执行器（仅 queued 可启动）。令牌先注册再拉起任务，保证
    取消请求从第一刻起就可见。执行器按 `runner` 列分派。"""
    run = await get_run(run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    assert_transition(run["status"], RUN_RUNNING)
    REGISTRY.register(run_id)
    meta = run.get("meta") or {}
    if run["runner"] == "task":
        from app.domains.agent.runtime import task_runner

        task = asyncio.create_task(task_runner.execute_task_run(
            run_id,
            kind=str(meta.get("task") or ""),
            ref=meta.get("ref") if isinstance(meta.get("ref"), dict) else None,
            adopted=bool(meta.get("adopted")),
        ))
    else:
        from app.domains.agent.runtime import fake  # 延迟导入：fake 依赖本模块

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
    async with write_gate(WritePriority.INTERACTIVE, "agent_run"), get_session_factory()() as session:
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
    async with write_gate(WritePriority.INTERACTIVE, "agent_event"), get_session_factory()() as session:
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


async def get_run(run_id: str) -> dict | None:
    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            return None
        return {
            "run_id": run.id,
            "session_id": run.session_id,
            "runner": run.runner,
            "trigger": run.trigger,
            "meta": run.meta,
            "status": run.status,
            "error_code": run.error_code,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }


async def list_runs(*, status: str | None = None, limit: int = 20) -> list[dict]:
    """运行列表（新→旧）。运行中与终态一视同仁，调用方按 status 过滤；
    只读投影，不改变任何状态。"""
    limit = max(1, min(int(limit), 100))
    async with get_session_factory()() as session:
        stmt = select(AgentRun)
        if status:
            stmt = stmt.where(AgentRun.status == status)
        rows = (await session.execute(
            stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit)
        )).scalars().all()
        return [
            {
                "run_id": r.id,
                "session_id": r.session_id,
                "runner": r.runner,
                "trigger": r.trigger,
                "meta": r.meta,
                "status": r.status,
                "error_code": r.error_code,
                "created_at": r.created_at,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in rows
        ]


async def latest_progress(run_id: str) -> dict | None:
    """最近一条进度采样（运行中任务的进度投影；从未采样则 None）。"""
    async with get_session_factory()() as session:
        row = await session.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == run_id,
                AgentEvent.event_type == agent_events_spec.EV_TASK_PROGRESS,
            )
            .order_by(AgentEvent.seq.desc())
            .limit(1)
        )
        return dict(row.payload or {}) if row is not None else None


async def list_events(run_id: str, *, after_seq: int = 0,
                      limit: int = _REPLAY_PAGE_LIMIT) -> list[dict]:
    """按 seq 升序回放；after_seq 游标之后的事件，支持从任意断点续读。"""
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
    """取消请求（幂等）。有活跃执行者 → 置令牌交执行器收敛（令牌置位
    与判定之间无 await，进程内原子）；无执行者（queued / 孤儿）→ 单事务
    内原子收敛：请求事件 + 状态跃迁 + 终态事件一次落账，并发重复请求
    读到终态即返回 accepted=False，不产生第二条请求事件。"""
    run = await get_run(run_id)
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    token = REGISTRY.token_of(run_id)
    if token is not None:
        first = not token.requested
        if first:
            token.request()
            await append_event(run_id, agent_events_spec.EV_CANCEL_REQUESTED,
                               {"reason": reason, "by": "user"})
        fresh = await get_run(run_id)
        return {"accepted": first, "status": fresh["status"] if fresh else run["status"]}
    async with write_gate(WritePriority.INTERACTIVE, "agent_run"), get_session_factory()() as session:
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
    cancelled（reason=runtime_restarted，与用户取消区分）。返回收敛数。"""
    async with get_session_factory()() as session:
        ids = (await session.execute(
            select(AgentRun.id).where(AgentRun.status.in_(_ORPHAN_STATUSES))
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
