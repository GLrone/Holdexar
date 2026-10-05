"""Agent Runtime P1 语义测试：状态机 / 三表账本 / 取消 / 回放 / 收尸。

隔离：tmp 独立 SQLite，monkeypatch session 工厂（database 模块与 agent
service 模块各持引用）；write_gate 用真实全局调度器——并发写用例即
单写者调度的运行时证明。fake 执行器全程无网络无模型。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.agent import service as agent_service
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.router import router as agent_router
from app.domains.agent.runtime import events as ev
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    RUN_AWAITING_APPROVAL,
    RUN_BUDGET_EXHAUSTED,
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    IllegalRunTransition,
    assert_transition,
    is_terminal,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立临时库：替换 session 工厂，write_gate 保持真实全局调度器。"""
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'agent_test.db').as_posix()}", echo=False
    )

    # 与生产同口径开外键校验：关着跑会放过「会话行未落库也能建 run」这类
    # FK 违规（agent 三表的 FK 只在开着校验时才有意义）
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(agent_service, "get_session_factory", lambda: factory)
    yield {"factory": factory, "url": f"sqlite+aiosqlite:///{(tmp_path / 'agent_test.db').as_posix()}"}
    # 遗留执行任务兜底回收（正常用例 runner 已自行退出）
    for rid in REGISTRY.active_ids():
        entry = REGISTRY.pop(rid)
        if entry and entry.task and not entry.task.done():
            entry.task.cancel()


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    """tmp 库只建 agent 三表。"""
    engine = create_async_engine(db["url"])
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                [AgentSession.__table__, AgentRun.__table__, AgentEvent.__table__],
            )
    finally:
        await engine.dispose()


async def wait_status(run_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    run: dict | None = None
    while time.monotonic() < deadline:
        run = await agent_service.get_run(run_id)
        if run and run["status"] in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for {statuses}, last={run}")


async def wait_event(run_id: str, event_type: str, timeout: float = 5.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = await agent_service.list_events(run_id)
        if any(e["event_type"] == event_type for e in events):
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for event {event_type}")


# ── 状态机（纯函数）──────────────────────────────────────────────

from app.domains.agent.runtime.state import TRANSITIONS  # noqa: E402


class TestStateMachine:
    def test_legal_transition_matrix(self):
        for current, targets in TRANSITIONS.items():
            for target in targets:
                assert_transition(current, target)  # 不抛即合法

    def test_terminal_has_no_outgoing(self):
        all_states = (
            RUN_QUEUED, RUN_RUNNING, RUN_AWAITING_APPROVAL,
            RUN_DONE, RUN_FAILED, RUN_CANCELLED, RUN_BUDGET_EXHAUSTED,
        )
        for terminal in (RUN_DONE, RUN_FAILED, RUN_CANCELLED, RUN_BUDGET_EXHAUSTED):
            assert is_terminal(terminal)
            for target in all_states:
                with pytest.raises(IllegalRunTransition):
                    assert_transition(terminal, target)

    def test_illegal_transitions(self):
        for current, target in [
            (RUN_QUEUED, RUN_DONE),
            (RUN_QUEUED, RUN_AWAITING_APPROVAL),
            (RUN_QUEUED, RUN_BUDGET_EXHAUSTED),
            (RUN_RUNNING, RUN_QUEUED),
            (RUN_AWAITING_APPROVAL, RUN_QUEUED),
            (RUN_QUEUED, RUN_QUEUED),
            ("unknown", RUN_RUNNING),
            (RUN_RUNNING, "unknown"),
        ]:
            with pytest.raises(IllegalRunTransition):
                assert_transition(current, target)


# ── 存储与生命周期 ────────────────────────────────────────────────

class TestStore:
    @pytest.mark.asyncio
    async def test_create_run_autocreates_session(self, db):
        created = await agent_service.create_run()
        assert created["status"] == RUN_QUEUED
        run = await agent_service.get_run(created["run_id"])
        assert run["session_id"] == created["session_id"]
        assert run["runner"] == "fake"
        assert run["trigger"] == "manual"
        assert run["started_at"] is None and run["finished_at"] is None

    @pytest.mark.asyncio
    async def test_create_run_with_prompt_lands_turn_event(self, db):
        created = await agent_service.create_run(prompt="帮我看价格")
        events = await agent_service.list_events(created["run_id"])
        assert len(events) == 1
        assert events[0]["event_type"] == ev.EV_TURN_RECEIVED
        assert events[0]["seq"] == 1
        assert events[0]["payload"]["text"] == "帮我看价格"

    @pytest.mark.asyncio
    async def test_create_run_unknown_session_rejected(self, db):
        with pytest.raises(LookupError):
            await agent_service.create_run(session_id="nope")


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_success_lifecycle_full_event_order(self, db):
        created = await agent_service.create_run(prompt="q", meta={"steps": 3})
        rid = created["run_id"]
        await agent_service.start_run(rid)
        run = await wait_status(rid, {RUN_DONE})
        assert run["error_code"] is None
        assert run["started_at"] is not None and run["finished_at"] is not None
        events = await agent_service.list_events(rid)
        types = [e["event_type"] for e in events]
        assert types == (
            [ev.EV_TURN_RECEIVED, ev.EV_RUN_STATE]
            + [ev.EV_STEP_STARTED, ev.EV_STEP_COMPLETED] * 3
            + [ev.EV_RUN_STATE, ev.EV_RUN_FINISHED]
        )
        state_events = [e for e in events if e["event_type"] == ev.EV_RUN_STATE]
        assert state_events[0]["payload"] == {"from": RUN_QUEUED, "to": RUN_RUNNING}
        assert state_events[1]["payload"] == {"from": RUN_RUNNING, "to": RUN_DONE}
        assert events[-1]["payload"] == {"status": RUN_DONE}
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(1, len(seqs) + 1))

    @pytest.mark.asyncio
    async def test_failure_lifecycle(self, db):
        created = await agent_service.create_run(meta={"scenario": "failure", "steps": 3})
        rid = created["run_id"]
        await agent_service.start_run(rid)
        run = await wait_status(rid, {RUN_FAILED})
        assert run["error_code"] == "fake_error"
        events = await agent_service.list_events(rid)
        completed = [e for e in events if e["event_type"] == ev.EV_STEP_COMPLETED]
        started = [e for e in events if e["event_type"] == ev.EV_STEP_STARTED]
        assert len(started) == 1 and not completed
        assert events[-1]["payload"] == {"status": RUN_FAILED, "error_code": "fake_error"}

    @pytest.mark.asyncio
    async def test_awaiting_approval_roundtrip(self, db):
        rid = (await agent_service.create_run())["run_id"]
        await agent_service.apply_transition(rid, RUN_RUNNING)
        await agent_service.apply_transition(rid, RUN_AWAITING_APPROVAL)
        run = await wait_status(rid, {RUN_AWAITING_APPROVAL})
        assert run["status"] == RUN_AWAITING_APPROVAL
        await agent_service.apply_transition(rid, RUN_RUNNING)
        run = await agent_service.get_run(rid)
        assert run["status"] == RUN_RUNNING
        assert run["finished_at"] is None


class TestTransitionGuards:
    @pytest.mark.asyncio
    async def test_illegal_transition_writes_nothing(self, db):
        rid = (await agent_service.create_run())["run_id"]
        with pytest.raises(IllegalRunTransition):
            await agent_service.apply_transition(rid, RUN_DONE)
        run = await agent_service.get_run(rid)
        assert run["status"] == RUN_QUEUED
        assert await agent_service.list_events(rid) == []

    @pytest.mark.asyncio
    async def test_done_run_rejects_restart(self, db):
        rid = (await agent_service.create_run())["run_id"]
        await agent_service.apply_transition(rid, RUN_RUNNING)
        await agent_service.apply_transition(rid, RUN_DONE)
        with pytest.raises(IllegalRunTransition):
            await agent_service.apply_transition(rid, RUN_RUNNING)


# ── 事件账本：seq / 持久化 / 回放 / 边界 ─────────────────────────

class TestEventLedger:
    @pytest.mark.asyncio
    async def test_seq_monotonic_under_concurrency(self, db):
        rid = (await agent_service.create_run())["run_id"]
        seqs = await asyncio.gather(*[
            agent_service.append_event(rid, ev.EV_STEP_STARTED, {"step": n})
            for n in range(10)
        ])
        assert sorted(seqs) == list(range(1, 11))
        events = await agent_service.list_events(rid)
        assert [e["seq"] for e in events] == list(range(1, 11))

    @pytest.mark.asyncio
    async def test_seq_unique_constraint_at_db_level(self, db):
        rid = (await agent_service.create_run())["run_id"]
        await agent_service.append_event(rid, ev.EV_STEP_STARTED, {"step": 1})
        async with db["factory"]() as session:
            session.add(AgentEvent(run_id=rid, seq=1, event_type=ev.EV_STEP_STARTED))
            with pytest.raises(Exception):  # noqa: B017, PT011 — IntegrityError
                await session.commit()

    @pytest.mark.asyncio
    async def test_replay_from_cursor(self, db):
        rid = (await agent_service.create_run())["run_id"]
        for n in range(1, 6):
            await agent_service.append_event(rid, ev.EV_STEP_STARTED, {"step": n})
        page = await agent_service.list_events(rid, after_seq=2, limit=2)
        assert [e["seq"] for e in page] == [3, 4]
        rest = await agent_service.list_events(rid, after_seq=4)
        assert [e["seq"] for e in rest] == [5]
        full = await agent_service.list_events(rid)
        assert [e["seq"] for e in full] == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_payload_boundary(self, db):
        rid = (await agent_service.create_run())["run_id"]
        # 敏感键剔除 + 白名单外键丢弃（端到端）
        seq = await agent_service.append_event(
            rid, ev.EV_STEP_COMPLETED,
            {"step": 1, "api_key": "sk-secret", "cookie": "a=b", "rogue": 1},
        )
        events = await agent_service.list_events(rid)
        payload = next(e["payload"] for e in events if e["seq"] == seq)
        assert payload == {"step": 1}
        dumped = json.dumps([e["payload"] for e in events])
        assert "api_key" not in dumped
        assert "rogue" not in dumped
        # 纯函数层：超限摘要化
        truncated = ev.build_payload(ev.EV_STEP_COMPLETED, {"step": 1, "summary": "x" * 20000})
        assert truncated["truncated"] is True
        assert truncated["bytes"] > 8192

    @pytest.mark.asyncio
    async def test_events_survive_restart(self, db, monkeypatch):
        rid = (await agent_service.create_run(prompt="q"))["run_id"]
        await agent_service.apply_transition(rid, RUN_RUNNING)
        await agent_service.apply_transition(rid, RUN_DONE)
        # 模拟重启：同库文件新引擎 + 重新替换工厂（monkeypatch 还原，防跨用例泄漏）
        import app.core.database as database_module

        engine = create_async_engine(db["url"])
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
        monkeypatch.setattr(agent_service, "get_session_factory", lambda: factory)
        run = await agent_service.get_run(rid)
        assert run["status"] == RUN_DONE
        events = await agent_service.list_events(rid)
        assert [e["event_type"] for e in events] == [
            ev.EV_TURN_RECEIVED, ev.EV_RUN_STATE, ev.EV_RUN_STATE, ev.EV_RUN_FINISHED,
        ]


# ── 取消语义 ─────────────────────────────────────────────────────

class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_queued_run(self, db):
        rid = (await agent_service.create_run())["run_id"]
        result = await agent_service.cancel_run(rid)
        assert result == {"accepted": True, "status": RUN_CANCELLED}
        events = await agent_service.list_events(rid)
        assert [e["event_type"] for e in events] == [
            ev.EV_CANCEL_REQUESTED, ev.EV_RUN_STATE, ev.EV_RUN_FINISHED,
        ]

    @pytest.mark.asyncio
    async def test_cancel_running_stops_steps_and_keeps_completed(self, db):
        rid = (await agent_service.create_run(
            meta={"scenario": "success", "steps": 8, "step_delay": 0.05}))["run_id"]
        await agent_service.start_run(rid)
        await wait_status(rid, {RUN_RUNNING})
        # 等首步完成再取消：同时证明「已完成步保留」与「后续步不再执行」
        await wait_event(rid, ev.EV_STEP_COMPLETED)
        await agent_service.cancel_run(rid)
        run = await wait_status(rid, {RUN_CANCELLED})
        assert run["status"] == RUN_CANCELLED
        events = await agent_service.list_events(rid)
        completed = [e for e in events if e["event_type"] == ev.EV_STEP_COMPLETED]
        started = [e for e in events if e["event_type"] == ev.EV_STEP_STARTED]
        assert 1 <= len(completed) < 8
        assert len(started) == len(completed) + 1  # 取消时的当前步有 started 无 completed

    @pytest.mark.asyncio
    async def test_cancel_idempotent(self, db):
        rid = (await agent_service.create_run(
            meta={"scenario": "success", "steps": 8, "step_delay": 0.05}))["run_id"]
        await agent_service.start_run(rid)
        await wait_status(rid, {RUN_RUNNING})
        first = await agent_service.cancel_run(rid)
        second = await agent_service.cancel_run(rid)
        third = await agent_service.cancel_run(rid)
        assert first["accepted"] is True
        assert second["accepted"] is False
        assert third["accepted"] is False
        await wait_status(rid, {RUN_CANCELLED})
        events = await agent_service.list_events(rid)
        requests = [e for e in events if e["event_type"] == ev.EV_CANCEL_REQUESTED]
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_cancel_terminal_run_is_noop(self, db):
        rid = (await agent_service.create_run())["run_id"]
        await agent_service.apply_transition(rid, RUN_RUNNING)
        await agent_service.apply_transition(rid, RUN_DONE)
        before = await agent_service.list_events(rid)
        result = await agent_service.cancel_run(rid)
        assert result == {"accepted": False, "status": RUN_DONE}
        assert await agent_service.list_events(rid) == before

    @pytest.mark.asyncio
    async def test_cancel_orphan_running_converges_directly(self, db):
        # 无活跃执行者的 running（进程内孤儿形态）：直接收敛
        rid = (await agent_service.create_run())["run_id"]
        await agent_service.apply_transition(rid, RUN_RUNNING)
        result = await agent_service.cancel_run(rid)
        assert result == {"accepted": True, "status": RUN_CANCELLED}


# ── 重启收尸 ─────────────────────────────────────────────────────

class TestReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_orphans(self, db):
        rid_q = (await agent_service.create_run())["run_id"]
        rid_r = (await agent_service.create_run())["run_id"]
        rid_a = (await agent_service.create_run())["run_id"]
        rid_d = (await agent_service.create_run())["run_id"]
        await agent_service.apply_transition(rid_r, RUN_RUNNING)
        await agent_service.apply_transition(rid_a, RUN_RUNNING)
        await agent_service.apply_transition(rid_a, RUN_AWAITING_APPROVAL)
        await agent_service.apply_transition(rid_d, RUN_RUNNING)
        await agent_service.apply_transition(rid_d, RUN_DONE)
        count = await agent_service.reconcile_orphan_runs()
        assert count == 3
        assert (await agent_service.get_run(rid_q))["status"] == RUN_CANCELLED
        assert (await agent_service.get_run(rid_r))["status"] == RUN_CANCELLED
        assert (await agent_service.get_run(rid_a))["status"] == RUN_CANCELLED
        assert (await agent_service.get_run(rid_d))["status"] == RUN_DONE
        state_events = [e for e in await agent_service.list_events(rid_q)
                        if e["event_type"] == ev.EV_RUN_STATE]
        assert state_events[0]["payload"] == {
            "from": RUN_QUEUED, "to": RUN_CANCELLED, "reason": "runtime_restarted",
        }

    @pytest.mark.asyncio
    async def test_reconcile_empty(self, db):
        assert await agent_service.reconcile_orphan_runs() == 0


# ── 写路径（全局单写者调度）与 DDL ───────────────────────────────

class TestWritePathAndDDL:
    @pytest.mark.asyncio
    async def test_concurrent_writes_all_persisted_in_order(self, db):
        async def one():
            created = await agent_service.create_run(prompt="q")
            await agent_service.append_event(created["run_id"], ev.EV_STEP_STARTED, {"step": 1})
            await agent_service.append_event(created["run_id"], ev.EV_STEP_COMPLETED, {"step": 1})
            return created["run_id"]

        rids = await asyncio.gather(*[one() for _ in range(20)])
        assert len(set(rids)) == 20
        for rid in rids:
            events = await agent_service.list_events(rid)
            assert [e["seq"] for e in events] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_ddl_indexes_and_unique(self, db):
        async with db["factory"]() as session:
            sql = (await session.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_events'"
            ))).scalar_one()
            # 表级唯一约束（SQLite 以 autoindex 承载，约束名保留在建表 SQL）
            assert "ux_agent_event_run_seq" in sql and "UNIQUE" in sql
            run_indexes = (await session.execute(text(
                "PRAGMA index_list('agent_runs')"))).fetchall()
            names = {row[1] for row in run_indexes}
            assert {"ix_agent_run_session", "ix_agent_run_status"} <= names
            ev_indexes = (await session.execute(text(
                "PRAGMA index_list('agent_events')"))).fetchall()
            assert any(row[2] for row in ev_indexes)  # (run_id, seq) 唯一约束在位
            fk = (await session.execute(text("PRAGMA foreign_key_list('agent_runs')"))).fetchall()
            assert any(row[2] == "agent_sessions" for row in fk)


# ── API 冒烟（mini app，只挂 agent router）───────────────────────

class TestApi:
    @pytest_asyncio.fixture
    async def client(self):
        app = FastAPI()
        app.include_router(agent_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_api_full_lifecycle(self, client):
        resp = await client.post("/agent/runs", json={"prompt": "hi", "steps": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == RUN_QUEUED
        rid = body["run_id"]
        await wait_status(rid, {RUN_DONE})
        run = (await client.get(f"/agent/runs/{rid}")).json()
        assert run["status"] == RUN_DONE
        events = (await client.get(f"/agent/runs/{rid}/events")).json()
        assert events["run_id"] == rid
        types = [e["event_type"] for e in events["items"]]
        assert ev.EV_RUN_FINISHED in types
        cancel = await client.post(f"/agent/runs/{rid}/cancel", json={})
        assert cancel.json() == {"accepted": False, "status": RUN_DONE}

    @pytest.mark.asyncio
    async def test_api_events_cursor(self, client):
        resp = await client.post("/agent/runs", json={"prompt": "hi", "steps": 2})
        rid = resp.json()["run_id"]
        await wait_status(rid, {RUN_DONE})
        page = (await client.get(
            f"/agent/runs/{rid}/events", params={"after_seq": 1, "limit": 2})).json()
        seqs = [e["seq"] for e in page["items"]]
        assert seqs == [2, 3]

    @pytest.mark.asyncio
    async def test_api_cancel_running(self, client):
        resp = await client.post(
            "/agent/runs", json={"steps": 8, "step_delay": 0.05})
        rid = resp.json()["run_id"]
        await wait_status(rid, {RUN_RUNNING})
        cancel = await client.post(f"/agent/runs/{rid}/cancel", json={"reason": "user"})
        assert cancel.json()["accepted"] is True
        await wait_status(rid, {RUN_CANCELLED})

    @pytest.mark.asyncio
    async def test_api_validation_and_404(self, client):
        assert (await client.post("/agent/runs", json={"scenario": "slow"})).status_code == 422
        assert (await client.get("/agent/runs/nope")).status_code == 404
        assert (await client.get("/agent/runs/nope/events")).status_code == 404
        assert (await client.post("/agent/runs/nope/cancel", json={})).status_code == 404
