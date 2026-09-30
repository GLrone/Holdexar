"""pilot 域路由：领航员配置与问答（非流式 + SSE 流式）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import config as pilot_config
from . import llm as pilot_llm
from . import service
from .schemas import (AskRequest, AskResponse, DetectRequest, DetectResponse,
                      PilotConfigPayload, PilotConfigUpdate, TestRequest,
                      TestResponse)

router = APIRouter(prefix="/pilot", tags=["pilot"])


async def _config_payload() -> PilotConfigPayload:
    cfg = await pilot_config.load_config()
    usage = await pilot_config.usage_month()
    return PilotConfigPayload(
        protocol=cfg["protocol"],
        enabled=cfg["enabled"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        models=cfg["models"],
        has_api_key=bool(cfg["api_key"]),
        monthly_cap=cfg["monthly_cap"],
        usage_inp=usage["inp"],
        usage_out=usage["out"],
        usage_calls=usage["calls"],
        usage_total=usage["total"],
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


@router.post("/detect")
async def detect(payload: DetectRequest) -> DetectResponse:
    """网址 + 密钥 → 协议 / 服务商 / 可用模型清单（智能识别）。

    密钥缺省时使用已保存的密钥（识别已存配置）。"""
    api_key = payload.api_key
    if not api_key:
        cfg = await pilot_config.load_config()
        api_key = cfg.get("api_key") or ""
    try:
        result = await pilot_llm.detect_provider(payload.base_url, api_key, payload.protocol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return DetectResponse(**result)


@router.post("/test")
async def test_connection(payload: TestRequest) -> TestResponse:
    """连通性测试：按提交配置（缺省回落已存配置）发一次最小真实补全。"""
    import time as _time

    cfg = await pilot_config.load_config()
    protocol = payload.protocol or cfg["protocol"]
    base_url = payload.base_url if payload.base_url is not None else cfg["base_url"]
    model = payload.model if payload.model is not None else cfg["model"]
    api_key = payload.api_key
    if api_key is None:
        api_key = cfg.get("api_key") or ""
    if not base_url or not model:
        return TestResponse(ok=False, latency_ms=0, model=model, reason="missing_fields")
    messages = [
        {"role": "system", "content": "连通性测试：收到任何内容都只回复 pong。"},
        {"role": "user", "content": "ping"},
    ]
    started = _time.monotonic()
    try:
        reply_parts: list[str] = []
        usage = (0, 0)
        async for kind, delta in pilot_llm.chat_stream(
            protocol=protocol,
            base_url=pilot_llm.effective_base_url(protocol, base_url),
            api_key=api_key,
            model=model,
            messages=messages,
            max_tokens=16,
        ):
            if kind == "answer":
                reply_parts.append(delta)
            elif kind == "usage":
                usage = delta
        latency = int((_time.monotonic() - started) * 1000)
        if usage != (0, 0):
            await pilot_config.add_usage(usage[0] + usage[1])
        return TestResponse(ok=True, latency_ms=latency, model=model, reply="".join(reply_parts))
    except pilot_llm.PilotLlmError as e:
        latency = int((_time.monotonic() - started) * 1000)
        detail = str(e)[:200]
        lowered = detail.lower()
        if "401" in detail or "403" in detail or "unauthorized" in lowered:
            reason = "key_invalid"
        elif "connect" in lowered or "timed out" in lowered or "unreachable" in lowered:
            reason = "unreachable"
        else:
            reason = "server_error"
        return TestResponse(ok=False, latency_ms=latency, model=model, reason=reason, detail=detail)


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
