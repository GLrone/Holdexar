"""开发验收面与 Agent 自观测工具：dev_pytest 任务契约 + 运行账本读取。"""
import asyncio

import pytest

from app.domains.agent.runtime import executor
from app.domains.agent.runtime import dev_tasks
from app.domains.agent import service as agent_service
from app.domains.agent.tools.builtin import runtime_tools
from app.domains.agent.tools.registry import REGISTRY

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_dev_pytest_runs_selection_to_completion():
    result = await runtime_tools.start_task("dev_pytest", {"select": "mcp"})
    run_id = result["rows"][0]["run_id"]
    for _ in range(240):
        run = await agent_service.get_run(run_id)
        if run["status"] in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    events = await agent_service.list_events(run_id)
    output = "\n".join(str(e.get("payload", {}).get("summary", "")) for e in events)
    assert run["status"] == "done", output[-500:]
    assert "passed" in output
    assert any(r["run_id"] == run_id for r in await agent_service.list_runs())


async def test_dev_pytest_rejects_unknown_select():
    with pytest.raises(ValueError):
        dev_tasks.command("__nope__")
    result = await runtime_tools.start_task("dev_pytest", {"select": "__nope__"})
    assert result["rows"][0]["vKey"] == "taskBusy"
    assert not await agent_service.list_runs()


@pytest.mark.parametrize("selection", tuple(dev_tasks.TEST_SELECTS))
async def test_dev_pytest_selections_only_name_existing_tests(selection):
    root, args = dev_tasks.command(selection)
    assert root.is_dir()
    assert all((root / name).is_file() for name in dev_tasks.TEST_SELECTS[selection])
    assert "pytest" in args


async def test_cancel_dev_pytest_stops_the_actual_process(monkeypatch):
    import sys

    started = asyncio.Event()
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def slow(*args, **kwargs):
        proc = await real_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(dev_tasks.asyncio, "create_subprocess_exec", slow)
    result = await runtime_tools.start_task("dev_pytest", {"select": "mcp"})
    run_id = result["rows"][0]["run_id"]
    await asyncio.wait_for(started.wait(), 5)
    assert (await agent_service.cancel_run(run_id))["accepted"]
    for _ in range(100):
        run = await agent_service.get_run(run_id)
        if run["status"] == "cancelled":
            break
        await asyncio.sleep(0.02)
    assert run["status"] == "cancelled"
    assert processes[0].returncode is not None


async def test_agent_runs_list_and_events_empty_ledger():
    runs = await executor.execute(REGISTRY.get("agent_runs_list"), {})
    assert runs.status in ("ok", "empty")
    events = await executor.execute(
        REGISTRY.get("agent_run_events"), {"run_id": "nonexistent"})
    assert events.status == "empty"

    noargs = await executor.execute(REGISTRY.get("agent_run_events"), {})
    assert noargs.status == "empty"


async def test_agent_run_listing_keeps_identity_for_event_lookup():
    created = await agent_service.create_run(runner="fake")
    await agent_service.append_event(created["run_id"], "step.completed", {"step": 1, "summary": "observed"})
    runs = await executor.execute(REGISTRY.get("agent_runs_list"), {})
    row = runs.data["rows"][0]
    assert row["run_id"] == created["run_id"] and row["at"] is not None
    events = await executor.execute(REGISTRY.get("agent_run_events"), {"run_id": row["run_id"]})
    assert events.data["runId"] == created["run_id"]
    assert any(item["k"] == "step.completed" for item in events.data["rows"])
