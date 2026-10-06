"""运行账本桥语义测试：job 入账 / 异常归因 / 终态采样 / 段位流水 / 受纳运行 / 保留窗。

隔离口径与 test_agent_task_runner 一致（独立 tmp SQLite + 外键开启 + 真实
write_gate）。job 本体全部打桩——本文件证明的是「账本桥语义」。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.agent import service as agent_service
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.runtime import scheduler_bridge
from app.domains.agent.runtime import tasks as task_registry
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.tasks import TaskSpec


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    url = f"sqlite+aiosqlite:///{(tmp_path / 'bridge_test.db').as_posix()}"
    engine = create_async_engine(url, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(agent_service, "get_session_factory", lambda: factory)
    # 桥模块 from-import 了 get_session_factory——按模块属性打桩，
    # 否则 prune 等路径会摸到真实数据目录
    monkeypatch.setattr(scheduler_bridge, "get_session_factory", lambda: factory)
    yield {"factory": factory, "url": url}
    for rid in REGISTRY.active_ids():
        entry = REGISTRY.pop(rid)
        if entry and entry.task and not entry.task.done():
            entry.task.cancel()


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    engine = create_async_engine(db["url"])
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                [AgentSession.__table__, AgentRun.__table__, AgentEvent.__table__],
            )
    finally:
        await engine.dispose()


@pytest.fixture
def stub_kind(monkeypatch):
    def install(observe) -> TaskSpec:
        async def _start() -> dict:
            return {"total": 2}

        spec = TaskSpec(
            kind="stub", label="priceRefresh", start=_start, observe=observe,
        )
        monkeypatch.setitem(task_registry._REGISTRY, "stub", spec)
        return spec

    return install


async def wait_status(run_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    run: dict | None = None
    while time.monotonic() < deadline:
        run = await agent_service.get_run(run_id)
        if run and run["status"] in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for {statuses}, last={run}")


class TestRunAccounted:
    @pytest.mark.asyncio
    async def test_success_run_done_with_meta(self, db):
        async def _job():
            return {"did": "work"}

        result = await scheduler_bridge.run_accounted("some_job", _job, ref={"k": 1})
        assert result == {"did": "work"}

        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "some_job")
        assert run["status"] == "done"
        assert run["runner"] == "job"
        assert run["trigger"] == "scheduled"
        meta = run["meta"]
        assert meta["entry"] == "scheduled" and meta["ref"] == {"k": 1}
        # 挂靠会话幂等建且唯一
        async with db["factory"]() as session:
            sessions = (await session.execute(AgentSession.__table__.select())).fetchall()
        assert [s.id for s in sessions] == [agent_service.SYSTEM_SESSION_ID]

    @pytest.mark.asyncio
    async def test_exception_fails_run_and_reraises(self, db):
        async def _boom():
            raise ValueError("业务配置缺失")

        with pytest.raises(ValueError):
            await scheduler_bridge.run_accounted("boom_job", _boom)

        runs = await agent_service.list_runs(status="failed")
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "boom_job")
        assert run["error_code"] == "job_error"
        events = await agent_service.list_events(run["run_id"])
        failed = [e for e in events if e["event_type"] == "task.failed"]
        assert failed and "业务配置缺失" in failed[0]["payload"]["reason"]

    @pytest.mark.asyncio
    async def test_ledger_failure_does_not_break_job(self, db, monkeypatch):
        """建账失败 = 业务照常执行、返回值原样（fail-soft 边界）。"""
        async def _accounting_boom(*a, **kw):
            raise RuntimeError("账本库不可用")

        monkeypatch.setattr(agent_service, "ensure_system_session", _accounting_boom)

        async def _job():
            return "fine"

        assert await scheduler_bridge.run_accounted("j", _job) == "fine"

    @pytest.mark.asyncio
    async def test_task_kind_samples_domain_outcome(self, db, stub_kind):
        async def _observe(ref):
            return {"active": False, "phase": "idle", "outcome": "failed"}

        stub_kind(_observe)

        async def _job():
            return None

        # fn 正常结束但域账本终态 failed → run 收 failed + domain_failed
        await scheduler_bridge.run_accounted(
            "mapped_job", _job, task_kind="stub", ref={"startedAt": "2026-01-01T00:00:00"},
        )
        runs = await agent_service.list_runs(status="failed")
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "mapped_job")
        assert run["error_code"] == "domain_failed"

    @pytest.mark.asyncio
    async def test_task_kind_unknown_kind_still_done(self, db):
        async def _job():
            return 1

        assert await scheduler_bridge.run_accounted(
            "j2", _job, task_kind="not_registered"
        ) == 1
        runs = await agent_service.list_runs(status="done")
        assert any((r.get("meta") or {}).get("job") == "j2" for r in runs)


class TestNoteStep:
    @pytest.mark.asyncio
    async def test_note_step_inside_run_context(self, db):
        async def _job():
            noted = await scheduler_bridge.note_step("tail", {"skipped": "无欠账"})
            assert noted is True
            return None

        await scheduler_bridge.run_accounted("note_job", _job)
        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "note_job")
        events = await agent_service.list_events(run["run_id"])
        steps = [e for e in events if e["event_type"] == "step.completed"]
        assert steps and steps[0]["payload"] == {"step": "tail", "summary": {"skipped": "无欠账"}}

    @pytest.mark.asyncio
    async def test_note_step_without_context_is_noop(self, db):
        assert await scheduler_bridge.note_step("x", {}) is False


class TestAdoptTaskRun:
    @pytest.mark.asyncio
    async def test_adopted_run_converges_done(self, db, stub_kind):
        async def _observe(ref):
            return {"active": False, "phase": "idle", "outcome": "completed"}

        stub_kind(_observe)

        created = await agent_service.adopt_task_run(
            kind="stub", ref={"startedAt": datetime.now().isoformat(), "count": 2},
        )
        run = await wait_status(created["run_id"], {"done"})
        assert run["runner"] == "task"
        assert run["meta"]["adopted"] is True
        assert run["meta"]["task"] == "stub"


class TestPruneJobRuns:
    @pytest.mark.asyncio
    async def test_prune_removes_only_old_job_runs(self, db):
        factory = db["factory"]
        old_ts = datetime.now() - timedelta(days=40)
        async with factory() as session:
            session.add(AgentSession(id=agent_service.SYSTEM_SESSION_ID,
                                     created_at=old_ts, updated_at=old_ts))
            await session.flush()
            session.add(AgentRun(id="old_job", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="job", status="done", created_at=old_ts))
            session.add(AgentRun(id="old_task", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="task", status="done", created_at=old_ts))
            session.add(AgentRun(id="fresh_job", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="job", status="done", created_at=datetime.now()))
            await session.flush()
            session.add(AgentEvent(run_id="old_job", seq=1, event_type="run.state",
                                   payload={"from": "queued", "to": "running"}))
            await session.commit()

        pruned = await scheduler_bridge.prune_job_runs(days=30)
        assert pruned == 1

        async with factory() as session:
            ids = {r.id for r in (await session.execute(AgentRun.__table__.select())).fetchall()}
        assert ids == {"old_task", "fresh_job"}
        # 事件随行级联删除（外键开启）
        async with factory() as session:
            rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
        assert all(r.run_id != "old_job" for r in rows)
