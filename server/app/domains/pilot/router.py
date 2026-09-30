"""pilot 域路由：领航员配置与问答（非流式 + SSE 流式）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import config as pilot_config
from . import service
from .schemas import AskRequest, AskResponse, PilotConfigPayload, PilotConfigUpdate

router = APIRouter(prefix="/pilot", tags=["pilot"])


async def _config_payload() -> PilotConfigPayload:
    cfg = await pilot_config.load_config()
    return PilotConfigPayload(
        protocol=cfg["protocol"],
        enabled=cfg["enabled"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        has_api_key=bool(cfg["api_key"]),
        monthly_cap=cfg["monthly_cap"],
        usage_month=await pilot_config.usage_month(),
    )


@router.get("/config")
async def get_config() -> PilotConfigPayload:
    try:
        return await _config_payload()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.put("/config")
async def update_config(payload: PilotConfigUpdate) -> PilotConfigPayload:
    try:
        # 只提交显式给出的字段；缺省字段保持原值（api_key 的清空 = 显式空串）
        update = {k: getattr(payload, k) for k in payload.model_fields_set}
        await pilot_config.save_config(update)
        return await _config_payload()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/ask")
async def ask(payload: AskRequest) -> AskResponse:
    try:
        result = await service.ask(payload.question, payload.appid, payload.session_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AskResponse(**result)


@router.post("/ask/stream")
async def ask_stream(payload: AskRequest):
    """SSE 流式问答：thinking / answer 增量 + facts + done，帧为 data: JSON。"""

    async def gen():
        try:
            async for event in service.ask_stream(payload.question, payload.appid, payload.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001 — 流已开始，HTTP 状态无法再改，帧内报错
            yield f'data: {json.dumps({"type": "error", "reason": "server_error"}, ensure_ascii=False)}\n\n'
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
