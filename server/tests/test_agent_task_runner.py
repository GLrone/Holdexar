"""Price runs are read-only views of domain records, with exact live cancellation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select

from app.domains.agent import service as agent_service
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.router import router as agent_router
from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.crawl import service as crawl_service
from app.domains.crawl.cycle import PriceCycle
from app.domains.crawl.models import CrawlJob


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    yield get_session_factory()
    for rid in REGISTRY.active_ids():
        entry = REGISTRY.pop(rid)
        if entry and entry.task and not entry.task.done():
            entry.task.cancel()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agent_router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def seed(db, *rows):
    async with db() as session:
        session.add_all(rows)
        await session.commit()


async def counts(db):
    async with db() as session:
        return tuple([await session.scalar(select(func.count()).select_from(model))
                      for model in (AgentSession, AgentRun, AgentEvent)])


@pytest.mark.asyncio
async def test_domain_projection_is_read_only_and_deduplicated(db, monkeypatch):
    ts = datetime(2026, 1, 1)
    await seed(db,
        PriceCycle(id=1, kind="scheduled", status="partial", started_at=ts,
                   batches_done=3, batches_expected=4, units_failed=2),
        CrawlJob(id=1, kind="scheduled", status="done", cycle_id=1, started_at=ts),
        CrawlJob(id=2, kind="repair", status="done", started_at=ts + timedelta(seconds=1),
                 stats_json={"processed": 5, "total": 5, "failed": 3, "trigger": "manual"}),
    )
    legacy = await agent_service.create_run(runner="task", meta={"task": "price_refresh"})
    await agent_service.create_run(runner="task", meta={"task": "retired_unknown"})
    legacy_job = await agent_service.create_run(runner="job", meta={"task": "price_repair"})
    await agent_service.append_event(legacy["run_id"], "task.progress", {"done": 1})
    other = await agent_service.create_run(runner="job", meta={"job": "wallet_sync"})
    before = await counts(db)
    rows = await agent_service.list_runs()
    assert {r["run_id"] for r in rows} == {"cycle:1", "crawl:2", other["run_id"]}
    cycle = await agent_service.get_run("cycle:1")
    assert cycle["session_id"] == "runtime" and cycle["runner"] == "task"
    assert cycle["trigger"] == "scheduled" and cycle["status"] == "done"
    assert cycle["meta"]["domainStatus"] == "partial"
    assert cycle["meta"]["progress"] == {"done": 3, "total": 4, "failed": 2}
    job = await agent_service.get_run("crawl:2")
    assert job["status"] == "done" and job["trigger"] == "manual"
    assert job["meta"]["progress"]["failed"] == 3
    assert job["meta"]["task"] == "price_repair"
    assert await agent_service.get_run("crawl:1") is None
    assert await agent_service.list_events("cycle:1") == []
    assert await agent_service.get_run(legacy_job["run_id"]) is not None
    assert len(await agent_service.list_events(legacy["run_id"])) == 1
    assert await counts(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("domain_status,expected", [
    ("planning", "running"), ("repairing", "running"), ("finalizing", "running"),
    ("completed", "done"), ("partial", "done"), ("failed", "failed"),
    ("cancelled", "cancelled"), ("interrupted", "failed"), ("mystery", "failed"),
])
async def test_cycle_status_preserves_domain_verdict(db, domain_status, expected):
    await seed(db, PriceCycle(id=1, kind="manual", status=domain_status))
    row = await agent_service.get_run("cycle:1")
    assert row["status"] == expected
    assert row["meta"]["domainStatus"] == domain_status
    assert [r["run_id"] for r in await agent_service.list_runs(status=expected)] == ["cycle:1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("domain_status,expected", [
    ("pending", "queued"), ("running", "running"), ("done", "done"),
    ("failed", "failed"), ("stopped", "cancelled"), ("interrupted", "failed"),
])
async def test_job_status_and_unknown_source(db, domain_status, expected):
    await seed(db, CrawlJob(id=1, kind="manual", status=domain_status))
    row = await agent_service.get_run("crawl:1")
    assert row["status"] == expected and row["trigger"] == "unknown"
    assert row["meta"].get("task") != "price_repair"
    assert row["meta"]["cancellable"] is False


@pytest.mark.asyncio
async def test_filter_before_limit_and_stable_merged_order(db):
    ts = datetime(2026, 1, 1)
    await seed(db,
        *[PriceCycle(id=i, kind="manual", status="completed", started_at=ts)
          for i in range(1, 15)],
        PriceCycle(id=20, kind="manual", status="running", started_at=ts - timedelta(days=1)),
        CrawlJob(id=1, kind="repair", status="running", started_at=ts),
        CrawlJob(id=2, kind="manual", status="done", started_at=ts + timedelta(days=1)),
    )
    fake = await agent_service.create_run()
    assert [r["run_id"] for r in await agent_service.list_runs(status="running", limit=2)] == ["crawl:1", "cycle:20"]
    assert [r["run_id"] for r in await agent_service.list_runs(status="done", limit=3)] == ["crawl:2", "cycle:9", "cycle:8"]
    assert (await agent_service.list_runs(limit=1))[0]["run_id"] == fake["run_id"]


@pytest.mark.asyncio
async def test_read_queries_are_bounded_and_never_write(db):
    await seed(db, *[PriceCycle(id=i, kind="manual", status="running") for i in range(1, 16)])
    statements = []
    engine = db.kw["bind"].sync_engine
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert len(await agent_service.list_runs(limit=15)) == 15
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) <= 3
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["cycle:0", "cycle:-1", "cycle:01", "crawl:1x", "crawl:999", "cycle:999"])
async def test_missing_or_invalid_exact_domain_id(db, run_id):
    await seed(db, PriceCycle(id=1, kind="manual", status="failed"))
    assert await agent_service.get_run(run_id) is None


@pytest.mark.asyncio
async def test_task_api_retired_and_fake_preserved(db, client):
    async with client:
        catalog = await client.get("/api/v1/agent/tasks")
        assert catalog.json() == [{"kind": "price_refresh", "label": "priceRefresh"},
                                  {"kind": "price_repair", "label": "priceRepair"},
                                  {"kind": "price_missing", "label": "priceMissing"},
                                  {"kind": "dev_pytest", "label": "devPytest"}]
        before = await counts(db)
        for kind in ("price_refresh", "rm_rf"):
            response = await client.post("/api/v1/agent/runs", json={"runner": "task", "task": kind})
            assert response.status_code == 410
        assert await counts(db) == before
        response = await client.post("/api/v1/agent/runs", json={"runner": "fake", "steps": 1})
        assert response.status_code == 200
        for _ in range(100):
            run = await agent_service.get_run(response.json()["run_id"])
            if run["status"] == "done":
                break
            await asyncio.sleep(.01)
        assert run["status"] == "done"


@pytest.mark.asyncio
async def test_domain_events_api_and_missing(db, client):
    await seed(db, PriceCycle(id=1, kind="manual", status="completed"))
    async with client:
        assert (await client.get("/api/v1/agent/runs/cycle:1/events")).json()["items"] == []
        assert (await client.get("/api/v1/agent/runs/cycle:2")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cycle", "crawl"])
async def test_exact_cancel_first_only_no_agent_writes(db, monkeypatch, kind):
    await seed(db, PriceCycle(id=1, kind="manual", status="running"),
               CrawlJob(id=2, kind="repair", status="running"))
    monkeypatch.setattr(crawl_service, "active_cycle_id", lambda: 1, raising=False)
    monkeypatch.setattr(crawl_service, "active_job_id", lambda: 2)
    calls = []
    async def stop(*args, **kwargs):
        calls.append((args, kwargs))
        return len(calls) == 1
    monkeypatch.setattr(crawl_service, "stop_job", stop)
    rid = "cycle:1" if kind == "cycle" else "crawl:2"
    assert (await agent_service.get_run(rid))["meta"]["cancellable"] is True
    before = await counts(db)
    assert await agent_service.cancel_run(rid) == {"accepted": True, "status": "running"}
    assert await agent_service.cancel_run(rid) == {"accepted": False, "status": "running"}
    expected = ((), {"cycle_id": 1, "whole_chain": True}) if kind == "cycle" else ((2,), {"whole_chain": False})
    assert calls == [expected, expected]
    assert await counts(db) == before


@pytest.mark.asyncio
async def test_cancel_mismatch_and_unsupported_return_conflict(db, monkeypatch, client):
    await seed(db, PriceCycle(id=1, kind="manual", status="running"),
               CrawlJob(id=2, kind="repair", status="running"))
    monkeypatch.setattr(crawl_service, "active_cycle_id", lambda: 9, raising=False)
    monkeypatch.setattr(crawl_service, "active_job_id", lambda: 9)
    async def never(*args, **kwargs):
        pytest.fail("must not stop a different live run")
    monkeypatch.setattr(crawl_service, "stop_job", never)
    legacy = await agent_service.create_run(runner="task", meta={"task": "price_refresh"})
    job = await agent_service.create_run(runner="job", meta={"job": "wallet_sync"})
    before = await counts(db)
    async with client:
        for rid in ("cycle:1", "crawl:2", legacy["run_id"], job["run_id"]):
            response = await client.post(f"/api/v1/agent/runs/{rid}/cancel", json={})
            assert response.status_code == 409
    assert await counts(db) == before
    assert (await agent_service.get_run(legacy["run_id"]))["status"] == "queued"


@pytest.mark.asyncio
async def test_terminal_cancel_noop_and_old_runner_never_starts_fake(db):
    await seed(db, PriceCycle(id=1, kind="manual", status="partial"))
    assert await agent_service.cancel_run("cycle:1") == {"accepted": False, "status": "done"}
    for runner in ("task", "job"):
        created = await agent_service.create_run(runner=runner)
        with pytest.raises(ValueError):
            await agent_service.start_run(created["run_id"])
        assert REGISTRY.token_of(created["run_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,source,rid", [("price_refresh", "start_full_queue", "cycle:7"),
                                           ("price_repair", "start_job", "crawl:8")])
async def test_pilot_starts_domain_directly_without_mirror(db, monkeypatch, kind, source, rid):
    from app.domains.agent.tools.builtin import runtime_tools
    calls = []
    async def start(**kwargs):
        calls.append(kwargs)
        return {"cycleId": 7, "total": 3} if source == "start_full_queue" else {"id": 8, "count": 3}
    monkeypatch.setattr(crawl_service, source, start)
    if source == "start_full_queue":
        await seed(db, PriceCycle(id=7, kind="manual", status="running"))
    else:
        await seed(db, CrawlJob(id=8, kind="repair", status="running"))
    before = await counts(db)
    result = await runtime_tools.start_task(kind)
    assert result["kind"] == "rows" and result["rows"][0]["run_id"] == rid
    assert result["rows"][0]["vKey"] == "taskRunning"
    assert calls == ([{}] if source == "start_full_queue" else [{"scope": "appids", "kind": "repair", "missing_cooldown": 0, "trigger": "manual"}])
    assert await counts(db) == before


@pytest.mark.asyncio
async def test_pilot_busy_unknown_and_no_running_remain_explicit(db, monkeypatch):
    from app.domains.agent.tools.builtin import runtime_tools
    async def busy():
        raise RuntimeError("busy")
    monkeypatch.setattr(crawl_service, "start_full_queue", busy)
    assert (await runtime_tools.start_task("price_refresh"))["rows"][0]["vKey"] == "taskBusy"
    assert (await runtime_tools.start_task("rm_rf"))["rows"][0]["vKey"] == "taskUnknown"
    assert (await runtime_tools.cancel_task())["rows"][0]["vKey"] == "taskNoRunning"
    assert await counts(db) == (0, 0, 0)


@pytest.mark.asyncio
async def test_pilot_lists_whitelisted_tasks_and_requests_stop(db, monkeypatch):
    from app.domains.agent.tools.builtin import runtime_tools
    await seed(db, PriceCycle(id=1, kind="manual", status="running", batches_done=1, batches_expected=4),
               CrawlJob(id=2, kind="manual", status="running"))
    monkeypatch.setattr(crawl_service, "active_cycle_id", lambda: 1, raising=False)
    async def stop(**kwargs):
        return True
    monkeypatch.setattr(crawl_service, "stop_job", stop)
    listed = await runtime_tools.list_tasks()
    assert listed["total"] == 1 and listed["rows"][0]["v"] == "1/4"
    result = await runtime_tools.cancel_task(run_id="cycle:1")
    assert result["rows"][0]["vKey"] == "taskStopRequested"
    assert (await agent_service.get_run("cycle:1"))["status"] == "running"
    assert await counts(db) == (0, 0, 0)


@pytest.mark.asyncio
async def test_run_progress_does_not_copy_frozen_targets(db):
    await seed(db, CrawlJob(id=1, kind="repair", status="running", stats_json={
        "total": 100, "processed": 10, "success": 8, "failed": 2,
        "trigger": "manual", "targets": {"appids": list(range(1000))},
        "targetStartedAt": "2026-01-01T00:00:00",
    }))
    row = await agent_service.get_run("crawl:1")
    assert row["meta"]["progress"] == {"done": 10, "total": 100, "success": 8, "failed": 2}
    assert "targets" not in (await agent_service.list_runs())[0]["meta"]["progress"]


@pytest.mark.asyncio
async def test_pilot_missing_accepted_record_is_an_error(db, monkeypatch):
    from app.domains.agent.tools.builtin import runtime_tools
    async def missing():
        return {"cycleId": 123}
    monkeypatch.setattr(crawl_service, "start_full_queue", missing)
    with pytest.raises(LookupError):
        await runtime_tools.start_task("price_refresh")
    assert await counts(db) == (0, 0, 0)


@pytest.mark.asyncio
async def test_cancel_returns_current_domain_status(db, monkeypatch):
    await seed(db, CrawlJob(id=1, kind="repair", status="running"))
    monkeypatch.setattr(crawl_service, "active_job_id", lambda: 1)
    async def stop(*args, **kwargs):
        async with db() as session:
            row = await session.get(CrawlJob, 1)
            row.status = "stopped"
            await session.commit()
        return True
    monkeypatch.setattr(crawl_service, "stop_job", stop)
    assert await agent_service.cancel_run("crawl:1") == {"accepted": True, "status": "cancelled"}


@pytest.mark.asyncio
async def test_single_game_tool_records_manual_source(db, monkeypatch):
    from app.domains.agent.tools.builtin import system
    seen = []
    async def start(**kwargs):
        seen.append(kwargs)
        return {"id": 1}
    monkeypatch.setattr(crawl_service, "start_job", start)
    await system.refresh_game_price(42)
    assert seen == [{"scope": "appids", "appids": [42], "kind": "manual", "trigger": "manual"}]


@pytest.mark.asyncio
async def test_pilot_filters_price_tasks_before_limit(db, monkeypatch):
    from app.domains.agent.tools.builtin import runtime_tools
    ts = datetime(2026, 1, 1)
    await seed(db, AgentSession(id="bulk", created_at=ts, updated_at=ts))
    await seed(db, PriceCycle(id=1, kind="manual", status="running", started_at=ts),
               CrawlJob(id=1, kind="repair", status="running", started_at=ts),
               *[CrawlJob(id=i + 2, kind="manual", status="done", started_at=ts + timedelta(days=1))
                 for i in range(105)],
               *[AgentRun(id=f"bulk{i}", session_id="bulk", runner="job", status="done",
                          created_at=ts + timedelta(days=2), meta={"job": "wallet_sync"})
                 for i in range(105)])
    monkeypatch.setattr(crawl_service, "active_cycle_id", lambda: 1)
    monkeypatch.setattr(crawl_service, "active_job_id", lambda: 1)
    listed = await runtime_tools.list_tasks()
    assert listed["total"] == 2
    assert {row["run_id"] for row in listed["rows"]} == {"cycle:1", "crawl:1"}
    calls = []
    async def stop(**kwargs):
        calls.append(kwargs)
        return True
    monkeypatch.setattr(crawl_service, "stop_job", stop)
    assert (await runtime_tools.cancel_task(kind="price_refresh"))["rows"][0]["vKey"] == "taskStopRequested"
    assert calls == [{"cycle_id": 1, "whole_chain": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["cycle:9223372036854775808", "crawl:" + "9" * 100])
async def test_oversized_domain_id_is_not_found(db, client, run_id):
    assert await agent_service.get_run(run_id) is None
    async with client:
        assert (await client.get(f"/api/v1/agent/runs/{run_id}")).status_code == 404


@pytest.mark.asyncio
async def test_catalog_ingest_tool_records_manual_source(db, monkeypatch):
    from app.domains.agent.tools.builtin import catalog
    seen = []
    async def imported(appids):
        return {"results": [{"appid": appid, "status": "ok"} for appid in appids]}
    async def start(**kwargs):
        seen.append(kwargs)
        return {"id": 1}
    monkeypatch.setattr(crawl_service, "import_appids", imported)
    monkeypatch.setattr(crawl_service, "start_job", start)
    await catalog.ingest_appids([42])
    assert seen == [{"scope": "appids", "appids": [42], "kind": "import", "trigger": "manual"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,params,expected", [
    ("price_repair", {"regions": ["US"], "appids": [12], "cooldown_minutes": 7},
     {"scope": "appids", "kind": "repair", "regions": ["US"], "appids": [12], "missing_cooldown": 7, "trigger": "manual"}),
    ("price_missing", {}, {"scope": "appids", "kind": "missing", "trigger": "manual"}),
])
async def test_parameterized_price_actions_keep_domain_identity_without_mirrors(db, monkeypatch, kind, params, expected):
    from app.domains.agent.tools.builtin import runtime_tools
    calls = []
    async def start(**kwargs):
        calls.append(kwargs)
        return {"id": 29}
    monkeypatch.setattr(crawl_service, "start_job", start)
    await seed(db, CrawlJob(id=29, kind=expected["kind"], status="running"))
    before = await counts(db)
    result = await runtime_tools.start_task(kind, params)
    assert calls == [expected]
    assert result["rows"][0]["run_id"] == "crawl:29"
    assert result["rows"][0]["taskKind"] == kind
    assert await counts(db) == before
