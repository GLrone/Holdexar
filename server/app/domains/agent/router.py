"""Agent run queries, historical replay and exact cancellation."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import memory as agent_memory
from . import service
from .runtime.state import IllegalRunTransition
from .schemas import (
    AgentCancelRequest,
    AgentCancelResult,
    AgentEventItem,
    AgentEventPage,
    AgentMemoryItem,
    AgentMemoryListOut,
    AgentMemoryUpsert,
    AgentRunCreate,
    AgentRunCreated,
    AgentRunListOut,
    AgentRunView,
    AgentTaskItem,
)

router = APIRouter(prefix="/agent", tags=["agent"])

_FAKE_SCENARIOS = ("success", "failure")


@router.get("/tasks")
async def list_tasks() -> list[AgentTaskItem]:
    """两种价格动作的固定名称。"""
    return [
        AgentTaskItem(kind=kind, label=label) for kind, label in service.TASK_LABELS.items()
    ]


@router.post("/runs")
async def create_run(payload: AgentRunCreate) -> AgentRunCreated:
    if payload.runner == "task":
        raise HTTPException(status_code=410, detail="start price work through the crawl domain")
    else:
        if payload.scenario not in _FAKE_SCENARIOS:
            raise HTTPException(status_code=422, detail="unknown scenario")
        meta = {
            "scenario": payload.scenario,
            "steps": payload.steps,
            "step_delay": payload.step_delay,
        }
    try:
        created = await service.create_run(
            session_id=payload.session_id,
            prompt=payload.prompt,
            trigger=payload.trigger,
            runner=payload.runner,
            meta=meta,
        )
        await service.start_run(created["run_id"])
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except IllegalRunTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AgentRunCreated(**created)


@router.get("/runs")
async def list_runs(status: str | None = None, limit: int = 20) -> AgentRunListOut:
    rows = await service.list_runs(status=status, limit=limit)
    return AgentRunListOut(items=[AgentRunView(**r) for r in rows])


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> AgentRunView:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return AgentRunView(**run)


@router.get("/runs/{run_id}/events")
async def list_events(run_id: str, after_seq: int = 0, limit: int = 200) -> AgentEventPage:
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    events = await service.list_events(run_id, after_seq=after_seq, limit=limit)
    return AgentEventPage(
        run_id=run_id,
        items=[AgentEventItem(**e) for e in events],
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, payload: AgentCancelRequest) -> AgentCancelResult:
    try:
        result = await service.cancel_run(run_id, reason=payload.reason)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except service.RunCancellationUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AgentCancelResult(**result)


@router.get("/memories")
async def list_memories(category: str | None = None) -> AgentMemoryListOut:
    """查询长期记忆列表。"""
    items = await agent_memory.list_memories(category=category)
    return AgentMemoryListOut(items=[AgentMemoryItem(**item) for item in items])


@router.post("/memories")
async def upsert_memory(payload: AgentMemoryUpsert) -> AgentMemoryItem:
    """写入或更新长期记忆。"""
    item = await agent_memory.upsert_memory(
        category=payload.category,
        key=payload.key,
        value=payload.value,
        confidence=payload.confidence,
        source=payload.source,
    )
    return AgentMemoryItem(**item)


@router.delete("/memories/{category}/{key}")
async def delete_memory(category: str, key: str) -> dict:
    """删除单条长期记忆。"""
    ok = await agent_memory.delete_memory(category, key)
    if not ok:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"ok": True}


@router.delete("/memories")
async def clear_memories(category: str | None = None) -> dict:
    """清空长期记忆。"""
    count = await agent_memory.clear_memories(category=category)
    return {"ok": True, "count": count}
