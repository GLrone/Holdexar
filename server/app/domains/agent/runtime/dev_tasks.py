"""源码环境的开发选测，执行状态沿用真实 Agent 运行记录。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.domains.agent.runtime.cancellation import REGISTRY
from app.domains.agent.runtime.state import (
    IllegalRunTransition, RUN_CANCELLED, RUN_DONE, RUN_FAILED, RUN_RUNNING,
)

TEST_SELECTS = {
    "smoke": ("tests/test_startup_chain.py",),
    "agent": ("tests/test_agent_tools.py", "tests/test_agent_runtime.py", "tests/test_agent_task_runner.py"),
    "mcp": ("tests/test_mcp_surface.py",),
    "crawl": ("tests/test_crawl_task_plan.py", "tests/test_crawl_plan_recovery.py"),
    "db": ("tests/test_schema_migrations.py", "tests/test_core_write_scheduler.py"),
}


def command(selection: str) -> tuple[Path, list[str]]:
    root = Path(__file__).resolve().parents[4]
    paths = TEST_SELECTS.get(selection)
    if paths is None:
        raise ValueError(f"未知测试选集：{selection}")
    if not all((root / path).is_file() for path in paths):
        raise ValueError("当前环境没有完整的源码测试选集")
    return root, [sys.executable, "-m", "pytest", *paths, "-q", "--no-header", "-p", "no:cacheprovider", "-x"]


async def execute_run(run_id: str, *, selection: str) -> None:
    from app.domains.agent import service
    from app.domains.agent.runtime import events

    token = REGISTRY.token_of(run_id)
    if token is None:
        raise RuntimeError("开发运行未登记取消句柄")
    proc = None
    waiters = []
    try:
        root, args = command(selection)
        await service.apply_transition(run_id, RUN_RUNNING)
        if token.requested:
            await service.apply_transition(run_id, RUN_CANCELLED)
            return
        with tempfile.TemporaryFile() as output:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            await service.append_event(run_id, events.EV_TASK_STARTED,
                                       {"kind": "dev_pytest", "label": "devPytest", "total": len(TEST_SELECTS[selection])})
            finished = asyncio.create_task(proc.wait())
            cancelled = asyncio.create_task(token.wait())
            waiters = [finished, cancelled]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if token.requested and proc.returncode is None:
                proc.terminate()
            code = await finished
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 4000))
            tail = output.read().decode("utf-8", errors="replace")
            await service.append_event(run_id, events.EV_STEP_COMPLETED, {"step": 1, "summary": tail})
            status = RUN_CANCELLED if token.requested else RUN_DONE if code == 0 else RUN_FAILED
            await service.apply_transition(run_id, status, error_code="pytest_failed" if status == RUN_FAILED else None)
    except IllegalRunTransition:
        pass
    except asyncio.CancelledError:
        await service.apply_transition(run_id, RUN_CANCELLED)
        raise
    except Exception as exc:
        await service.append_event(run_id, events.EV_TASK_FAILED,
                                   {"error_code": "dev_execution_failed", "reason": str(exc)[:200]})
        await service.apply_transition(run_id, RUN_FAILED, error_code="dev_execution_failed")
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            await proc.wait()
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)
        REGISTRY.pop(run_id)
