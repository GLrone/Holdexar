"""Agent Runtime 路由：最小运行面（创建 / 查询 / 事件回放 / 取消）。

不接真实模型：创建即启动 fake 执行器，验证 Runtime 骨架的生命周期、
账本与取消语义。错误口径：NotFound → 404；LookupError/非法迁移 →
400/409 由端点翻译；服务端异常 → 502（与既有域一致）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service
from .runtime.state import IllegalRunTransition
from .schemas import (
    AgentCancelRequest,
    AgentCancelResult,
    AgentEventItem,
    AgentEventPage,
    AgentRunCreate,
    AgentRunCreated,
    AgentRunView,
)

router = APIRouter(prefix="/agent", tags=["agent"])

_FAKE_SCENARIOS = ("success", "failure")


@router.post("/runs")
async def create_run(payload: AgentRunCreate) -> AgentRunCreated:
    if payload.scenario not in _FAKE_SCENARIOS:
        raise HTTPException(status_code=422, detail="unknown scenario")
    try:
        created = await service.create_run(
            session_id=payload.session_id,
            prompt=payload.prompt,
            trigger=payload.trigger,
            meta={
                "scenario": payload.scenario,
                "steps": payload.steps,
                "step_delay": payload.step_delay,
            },
        )
        await service.start_run(created["run_id"])
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except IllegalRunTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AgentRunCreated(**created)


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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AgentCancelResult(**result)
