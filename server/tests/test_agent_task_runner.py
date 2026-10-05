"""任务执行器语义测试：登记表 / 受理 / 采样 / 取消 / 接管 / 运行面 API。

隔离口径与 test_agent_runtime 一致（独立 tmp SQLite + 真实 write_gate）。
任务本体全部打桩——本文件证明的是「调度语义」，不触网、不动价格链。
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.agent import service as agent_service
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.router import router as agent_router
from app.domains.agent.runtime import events as ev
from app.domains.agent.runtime import task_runner, tasks as task_registry
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
)
from app.domains.agent.runtime.tasks import TaskSpec


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    url = f"sqlite+aiosqlite:///{(tmp_path / 'task_test.db').as_posix()}"
    engine = create_async_engine(url, echo=False)

    # 与生产同口径开外键校验：agent 三表的 FK 只在开着校验时才有意义，
    # 关着跑等于把「会话行没落库也能建 run」这类缺陷放过
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(agent_service, "get_session_factory", lambda: factory)
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
def fast_runner(monkeypatch):
    """把采样节拍调到毫秒级：用例断言的是语义，不是真实等待。"""
    monkeypatch.setattr(task_runner, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(task_runner, "START_GRACE", 0.3)


@pytest.fixture
def stub_kind(monkeypatch):
    """注册一个可观察的桩任务，返回它的调用记录。"""
    log: dict = {"start": 0, "stop": 0, "observations": 0}

    def install(*, start=None, observe=None, stop=None) -> TaskSpec:
        async def _start() -> dict:
            log["start"] += 1
            if start is not None:
                return await start()
            return {"total": 3}

        async def _observe(ref: dict) -> dict:
            log["observations"] += 1
            return await observe(ref)

        async def _stop() -> bool:
            log["stop"] += 1
            return True

        spec = TaskSpec(
            kind="stub", label="priceRefresh", start=_start, observe=_observe,
            stop=_stop if stop is None else stop,
        )
        monkeypatch.setitem(task_registry._REGISTRY, "stub", spec)
        return spec

    install.log = log
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


async def events_of(run_id: str) -> list[dict]:
    return await agent_service.list_events(run_id)


async def launch(kind: str = "stub", **meta) -> str:
    created = await agent_service.create_run(
        runner="task", meta={"task": kind, **meta},
    )
    await agent_service.start_run(created["run_id"])
    return created["run_id"]


class TestRegistry:
    def test_builtin_kinds_are_static_whitelist(self):
        assert task_registry.TASK_KINDS == ("price_refresh", "price_repair")
        assert task_registry.label_of("price_refresh") == "priceRefresh"
        assert task_registry.label_of("price_repair") == "priceRepair"

    def test_unknown_kind_is_not_registered(self):
        assert task_registry.get("delete_everything") is None
        # 未登记 kind 的标签回落到 kind 本身，不抛异常
        assert task_registry.label_of("delete_everything") == "delete_everything"

    def test_specs_have_start_and_observe(self):
        for spec in task_registry.all_specs():
            assert callable(spec.start) and callable(spec.observe)
            assert spec.kind and spec.label


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_progress_sampled_and_terminal_done(self, db, fast_runner, stub_kind):
        state = {"n": 0}

        async def observe(ref):
            state["n"] += 1
            if state["n"] >= 3:
                return {"active": False, "phase": "idle", "done": 3, "total": 3}
            return {"active": True, "phase": "running", "done": state["n"], "total": 3}

        stub_kind(observe=observe)
        run_id = await launch()
        run = await wait_status(run_id, {RUN_DONE})
        assert run["error_code"] is None
        events = await events_of(run_id)
        kinds = [e["event_type"] for e in events]
        assert kinds[0] == ev.EV_RUN_STATE and kinds[-1] == ev.EV_RUN_FINISHED
        assert ev.EV_TASK_STARTED in kinds
        progress = [e for e in events if e["event_type"] == ev.EV_TASK_PROGRESS]
        # 采样去重：进度变化才上账（不同 done 值 → 多条；重复值不重复记）
        assert len(progress) >= 2
        assert progress[-1]["payload"]["phase"] == "idle"
        assert stub_kind.log["start"] == 1

    @pytest.mark.asyncio
    async def test_business_rejection_is_failed_not_retried(self, db, fast_runner, stub_kind):
        async def boom():
            raise RuntimeError("已有爬取任务在运行")

        async def observe(ref):  # pragma: no cover - 拒绝路径不该进入观察
            return {"active": False, "phase": "idle"}

        stub_kind(start=boom, observe=observe)
        run_id = await launch()
        run = await wait_status(run_id, {RUN_FAILED})
        assert run["error_code"] == "rejected"
        events = await events_of(run_id)
        failed = [e for e in events if e["event_type"] == ev.EV_TASK_FAILED]
        assert failed and "已有爬取任务在运行" in failed[0]["payload"]["reason"]
        # 拒绝即终态：没有再观察、没有任务启动事件
        assert stub_kind.log["observations"] == 0
        assert stub_kind.log["start"] == 1

    @pytest.mark.asyncio
    async def test_unknown_task_kind_fails(self, db, fast_runner):
        run_id = await launch("no_such_task")
        run = await wait_status(run_id, {RUN_FAILED})
        assert run["error_code"] == "unknown_task"

    @pytest.mark.asyncio
    async def test_cancel_requests_domain_stop_and_converges(
        self, db, fast_runner, stub_kind
    ):
        async def observe(ref):
            return {"active": True, "phase": "running", "done": 1, "total": 9}

        stub_kind(observe=observe)
        run_id = await launch()
        await wait_status(run_id, {RUN_RUNNING})
        res = await agent_service.cancel_run(run_id, reason="user")
        assert res["accepted"] is True
        run = await wait_status(run_id, {RUN_CANCELLED})
        assert run["status"] == RUN_CANCELLED
        assert stub_kind.log["stop"] == 1
        events = await events_of(run_id)
        assert ev.EV_CANCEL_REQUESTED in [e["event_type"] for e in events]

    @pytest.mark.asyncio
    async def test_adopted_run_does_not_restart_domain_task(
        self, db, fast_runner, stub_kind
    ):
        state = {"n": 0}

        async def observe(ref):
            state["n"] += 1
            return {"active": state["n"] < 2, "phase": "running" if state["n"] < 2 else "idle"}

        stub_kind(start=observe, observe=observe)
        run_id = await launch(adopted=True, ref={"total": 5})
        await wait_status(run_id, {RUN_DONE})
        # 受理已在调用方完成 → 执行器不得重复发起
        assert stub_kind.log["start"] == 0

    @pytest.mark.asyncio
    async def test_observe_failure_is_fail_soft(self, db, fast_runner, stub_kind):
        state = {"n": 0}

        async def observe(ref):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("库抖了一下")
            return {"active": state["n"] < 4, "phase": "running" if state["n"] < 4 else "idle"}

        stub_kind(observe=observe)
        run_id = await launch()
        run = await wait_status(run_id, {RUN_DONE})
        assert run["status"] == RUN_DONE


class TestAgentApi:
    @pytest.mark.asyncio
    async def test_task_catalog_endpoint(self, db):
        app = FastAPI()
        app.include_router(agent_router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/agent/tasks")
        assert r.status_code == 200
        kinds = [i["kind"] for i in r.json()]
        assert kinds == ["price_refresh", "price_repair"]

    @pytest.mark.asyncio
    async def test_create_run_rejects_unknown_task_kind(self, db):
        app = FastAPI()
        app.include_router(agent_router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            bad = await c.post("/api/v1/agent/runs",
                               json={"runner": "task", "task": "rm_rf"})
            assert bad.status_code == 422
            ok = await c.post("/api/v1/agent/runs", json={"runner": "fake", "steps": 1})
            assert ok.status_code == 200
            listed = await c.get("/api/v1/agent/runs")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert any(i["runner"] == "fake" for i in items)

    @pytest.mark.asyncio
    async def test_list_runs_filters_by_status(self, db):
        created = await agent_service.create_run(runner="fake", meta={"steps": 1})
        rows = await agent_service.list_runs(limit=5)
        assert any(r["run_id"] == created["run_id"] for r in rows)
        assert await agent_service.list_runs(status="done", limit=5) == []


class TestPilotDispatch:
    """pilot 第三层权限：调度只经登记表，不获得写数据能力。"""

    @pytest.mark.asyncio
    async def test_start_task_admits_then_creates_run(
        self, db, fast_runner, stub_kind
    ):
        from app.domains.pilot import tools as pilot_tools

        state = {"n": 0}

        async def observe(ref):
            state["n"] += 1
            return {"active": state["n"] < 2, "phase": "running" if state["n"] < 2 else "idle",
                    "done": state["n"], "total": 3}

        async def _ok():
            return {"total": 3}

        stub_kind(start=_ok, observe=observe)
        result = await pilot_tools.execute_tool("start_task", {"kind": "stub"})
        assert result["kind"] == "rows" and result["titleKey"] == "tasks"
        row = result["rows"][0]
        assert row["kindKey"] == "pilot.task.priceRefresh"
        assert row["vKey"] in ("taskRunning", "taskDone")
        assert stub_kind.log["start"] == 1  # 受理在本次调用内完成
        runs = [r for r in await agent_service.list_runs(limit=5) if r["runner"] == "task"]
        assert len(runs) == 1 and runs[0]["meta"]["adopted"] is True

    @pytest.mark.asyncio
    async def test_start_task_busy_does_not_create_run(self, db, fast_runner, stub_kind):
        from app.domains.pilot import tools as pilot_tools

        async def busy():
            raise RuntimeError("已有爬取任务在运行")

        async def observe(ref):  # pragma: no cover
            return {"active": False, "phase": "idle"}

        stub_kind(start=busy, observe=observe)
        result = await pilot_tools.execute_tool("start_task", {"kind": "stub"})
        row = result["rows"][0]
        assert row["vKey"] == "taskBusy" and row["tone"] == "warn"
        assert await agent_service.list_runs(limit=5) == []

    @pytest.mark.asyncio
    async def test_start_task_unknown_kind_is_rejected(self, db):
        from app.domains.pilot import tools as pilot_tools

        result = await pilot_tools.execute_tool("start_task", {"kind": "rm_rf"})
        assert result["rows"][0]["vKey"] == "taskUnknown"

    @pytest.mark.asyncio
    async def test_list_and_cancel_task(self, db, fast_runner, stub_kind):
        from app.domains.pilot import tools as pilot_tools

        async def observe(ref):
            return {"active": True, "phase": "running", "done": 1, "total": 4}

        async def _ok2():
            return {"total": 4}

        stub_kind(start=_ok2, observe=observe)
        await pilot_tools.execute_tool("start_task", {"kind": "stub"})
        run = (await agent_service.list_runs(limit=5))[0]
        await wait_status(run["run_id"], {RUN_RUNNING})  # 执行器已接管再查投影
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:  # 等首次进度采样落账
            if await agent_service.latest_progress(run["run_id"]):
                break
            await asyncio.sleep(0.01)
        listed = await pilot_tools.execute_tool("list_tasks", {})
        assert listed["titleKey"] == "tasks"
        assert listed["total"] == 1
        assert listed["rows"][0]["vKey"] == "taskRunning"
        assert listed["rows"][0]["v"] == "1/4"
        cancelled = await pilot_tools.execute_tool("cancel_task", {"kind": "stub"})
        assert cancelled["rows"][0]["vKey"] == "taskCancelled"
        final = await wait_status(run["run_id"], {RUN_CANCELLED})
        assert final["status"] == RUN_CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_without_running_task(self, db):
        from app.domains.pilot import tools as pilot_tools

        result = await pilot_tools.execute_tool("cancel_task", {})
        assert result["rows"][0]["vKey"] == "taskNoRunning"
