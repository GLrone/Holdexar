"""pilot 域路由：领航员配置与问答（非流式 + SSE 流式）。"""
from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.logging import log_event

logger = logging.getLogger(__name__)

from . import attachments as pilot_attachments
from . import config as pilot_config
from . import llm as pilot_llm
from . import service
from .schemas import (AskRequest, AskResponse, DetectRequest, DetectResponse,
                      PilotAttachmentCreate, PilotAttachmentListOut, PilotAttachmentOut,
                      PilotCompactMarker, PilotConfigPayload, PilotConfigUpdate,
                      PilotForkOut, PilotForkRequest,
                      PilotFlywheelExportOut, PilotFlywheelStats,
                      PilotProviderListOut, PilotProviderOut, PilotProviderPayload,
                      PilotRewindOut, PilotRewindRequest,
                      PilotSessionFlagOut, PilotSessionFlagUpdate,
                      PilotSessionListItem, PilotSessionStats,
                      PilotSessionListOut, PilotSessionOut, PilotSessionTitleUpdate,
                      PilotTurnFeedbackOut, PilotTurnFeedbackRequest,
                      PilotSessionTurn, PilotProposalConfirm, PilotProposalOut, PilotProposalState,
                      ToolRunRequest, ToolRunResponse,
                      TestRequest, TestResponse)

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
        models_disabled=cfg["models_disabled"],
        has_api_key=bool(cfg["api_key"]),
        monthly_cap=cfg["monthly_cap"],
        context_window=cfg.get("context_window"),
        tool_content_limit=cfg.get("tool_content_limit") or 3000,
        context_window_source=cfg.get("context_window_source", "none"),
        context_window_auto=cfg.get("context_window_auto"),
        usage_inp=usage["inp"],
        usage_out=usage["out"],
        usage_calls=usage["calls"],
        usage_total=usage["total"],
        active=cfg["active"],
        providers=[PilotProviderOut(**p) for p in cfg["providers"]],
    )


@router.get("/providers")
async def list_providers() -> PilotProviderListOut:
    """供应商清单（has_key 布尔，不含密钥原文）。"""
    try:
        return PilotProviderListOut(items=[PilotProviderOut(**p) for p in await pilot_config.load_providers()])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/providers")
async def create_provider(payload: PilotProviderPayload) -> PilotProviderOut:
    """新增供应商；fields_set 区分缺省与显式空值（api_key 同 config 语义）。"""
    try:
        update = {k: getattr(payload, k) for k in payload.model_fields_set}
        created = await pilot_config.create_provider(update)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotProviderOut(**created)


@router.put("/providers/{pid}")
async def update_provider(pid: str, payload: PilotProviderPayload) -> PilotProviderOut:
    try:
        update = {k: getattr(payload, k) for k in payload.model_fields_set}
        updated = await pilot_config.update_provider(pid, update)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotProviderOut(**updated)


@router.delete("/providers/{pid}")
async def delete_provider(pid: str) -> dict:
    try:
        await pilot_config.delete_provider(pid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True}


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

    密钥缺省时：带 provider_id 只用该供应商已存密钥（可为空=匿名探测，
    不得跨家回落活跃供应商）；无 provider_id 才回落活跃配置密钥。"""
    api_key = payload.api_key
    if not api_key and payload.provider_id:
        api_key = await pilot_config.provider_key(payload.provider_id)
    elif not api_key:
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
            await pilot_config.add_usage(usage[0], usage[1])
        return TestResponse(ok=True, latency_ms=latency, model=model, reply="".join(reply_parts))
    except pilot_llm.PilotLlmError as e:
        latency = int((_time.monotonic() - started) * 1000)
        detail = str(e)[:200]
        lowered = detail.lower()
        if "401" in detail or "403" in detail or "unauthorized" in lowered:
            reason = "key_invalid"
        elif "connect" in lowered or "timed out" in lowered or "timeout" in lowered or "unreachable" in lowered:
            reason = "unreachable"
        else:
            reason = "server_error"
        return TestResponse(ok=False, latency_ms=latency, model=model, reason=reason, detail=detail)


@router.get("/sessions")
async def list_sessions(limit: int = 50, q: str = "") -> PilotSessionListOut:
    """最近会话投影（mtime 倒序，置顶在前）：标题/轮数/更新时间 + 是否生成中
    （运行登记），供历史面板与会话列。q 给定时按关键词过滤（标题或问答正文）。"""
    try:
        # 折叠/检索入线程：账本文件扫描不占事件循环，流式回合不被卡顿
        items = await asyncio.to_thread(
            service.get_store().list_recent, max(1, min(limit, 200)), q or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotSessionListOut(items=[
        PilotSessionListItem(**{**i, "running": service.session_running(i["sid"])})
        for i in items
    ])


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> PilotSessionOut:
    """会话账本读取：历史轮还原（重启 / 重开抽屉后续接对话）。

    无此会话或 id 形态非法 → 404（前端按空白新会话处理）。"""
    try:
        state = await service.get_store().load(session_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")
    turns: list = []
    feedback_by_tid = {
        str(record.get("tid")): str(record.get("label"))
        for record in service.get_store().raw_records(session_id)
        if record.get("t") == "feedback"
        and record.get("label") in ("accepted", "rejected")
        and record.get("tid")
    }
    for t in state.turns:
        try:
            turns.append(PilotSessionTurn(
                q=t.get("q") or "", resp=t.get("resp") or {}, ts=t.get("ts"),
                tid=t.get("tid"), feedback=feedback_by_tid.get(str(t.get("tid") or ""))))
        except Exception:  # noqa: BLE001 — 单条坏记录跳过，不拖垮整段历史
            log_event(
                logger,
                f"会话 {session_id} 有一条历史记录无法还原，已跳过",
                tag="跳过",
                level=logging.WARNING,
                detail={"会话": session_id},
            )
            continue
    return PilotSessionOut(
        session_id=session_id, turns=turns, updated_at=state.updated_at,
        turn_total=len(state.turns), summary_through=state.summary_through,
        stats=PilotSessionStats(**service.session_stats(state)),
        markers=[PilotCompactMarker(**m) for m in state.markers],
        title=service.get_store().title_of(state),
        proposals=[PilotProposalState(pid=pid,
                                      state=state.proposal_state_of(pid),
                                      done=info.get("done"),
                                      failedCount=info.get("failedCount"))
                   for pid, info in state.proposals.items()],
        pinned=state.pinned,
        archived=state.archived,
        forked_from=state.forked_from,
        inflight_q=service.inflight_question(session_id),
    )


@router.put("/sessions/{session_id}/title")
async def rename_session(session_id: str, payload: PilotSessionTitleUpdate) -> dict:
    """会话改名：追加一条 title 记录（账本后者胜）。无此会话 → 404。"""
    try:
        store = service.get_store()
        state = await store.load(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session not found")
        ok = await store.set_title(session_id, payload.text)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=502, detail="rename_failed")
    return {"ok": True}


@router.put("/sessions/{session_id}/pin")
async def pin_session(session_id: str, payload: PilotSessionFlagUpdate) -> PilotSessionFlagOut:
    """置顶开/关（账本追加 pin 记录）。机器码响应：no_session / write_failed。"""
    try:
        result = await service.pin_session(session_id, payload.on)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotSessionFlagOut(**result)


@router.put("/sessions/{session_id}/archive")
async def archive_session(session_id: str, payload: PilotSessionFlagUpdate) -> PilotSessionFlagOut:
    """归档开/关（账本追加 archive 记录）。机器码响应：no_session / write_failed。"""
    try:
        result = await service.archive_session(session_id, payload.on)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotSessionFlagOut(**result)


@router.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, payload: PilotForkRequest) -> PilotForkOut:
    """话题分叉：从源会话前 keep_turns 轮派生新会话（源账本零改动）。

    生成中拒绝。机器码响应：no_session / busy / write_failed；成功返回新会话 id。"""
    try:
        result = await service.fork_session(session_id, payload.keep_turns)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotForkOut(**result)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """删除会话文件（账本生命周期动作，与 TTL 清扫同机制）。无此会话 → 404。

    附件是会话的外部资源（见 attachments 模块），随会话一并清理——留着的
    附件没有引用它的对话，只会在数据目录里无主堆积。"""
    try:
        result = await service.get_store().delete_with_reason(session_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result == "no_session":
        raise HTTPException(status_code=404, detail="session not found")
    if result != "deleted":
        raise HTTPException(status_code=502, detail=result)
    pilot_attachments.purge(session_id)
    return {"ok": True}


@router.post("/attachments")
async def create_attachment(payload: PilotAttachmentCreate) -> PilotAttachmentOut:
    """长文本落盘为附件（提问字段只带引用 id）。

    前端粘贴超阈值时先调它——消息体里从此不放全文；服务端也会对超长提问
    做同样的兜底，两条路同一套存储。落盘失败（sid 非法 / 内容超上限）→ 422。"""
    ref = pilot_attachments.save(payload.session_id, payload.text, payload.name)
    if ref is None:
        raise HTTPException(status_code=422, detail="attachment_rejected")
    return PilotAttachmentOut(**ref)


@router.get("/attachments")
async def list_attachments(session_id: str) -> PilotAttachmentListOut:
    """本会话附件清单（引用形式，不含全文）。"""
    return PilotAttachmentListOut(
        items=[PilotAttachmentOut(**ref) for ref in pilot_attachments.list_of(session_id)])


@router.get("/attachments/{aid}")
async def read_attachment(aid: str, session_id: str, start_line: int = 1,
                          end_line: int | None = None) -> dict:
    """按行号区间读附件（前端展开附件时用；与模型侧读取同一实现与同一预算）。"""
    got = pilot_attachments.read_range(session_id, aid, start_line=start_line, end_line=end_line)
    if got is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return got


@router.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str) -> dict:
    """手动归档：把早期轮次蒸馏进要点存档（与自动压缩同一实现）。

    机器码响应（不抛 5xx）：llm_off / cap_reached / nothing_to_distill /
    compact_failed——前端翻用户语言。无此会话 → 404。"""
    try:
        state = await service.get_store().load(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session not found")
        cfg = await pilot_config.load_config()
        if not pilot_config.llm_ready(cfg):
            return {"ok": False, "reason": "llm_off"}
        if pilot_config.over_cap(await pilot_config.usage_month(), cfg["monthly_cap"]):
            return {"ok": False, "reason": "cap_reached"}
        return await service.compact_session_now(state, cfg)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/sessions/{session_id}/rewind")
async def rewind_session(session_id: str, payload: PilotRewindRequest) -> PilotRewindOut:
    """编辑重问：账本保留前 keep_turns 轮，其后轮次作废（软截断，历史行不动）。

    生成中拒绝。机器码响应：no_session / busy / write_failed。"""
    try:
        result = await service.rewind_session(session_id, payload.keep_turns)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotRewindOut(**result)


@router.put("/sessions/{session_id}/turns/{turn_id}/feedback")
async def rate_turn(session_id: str, turn_id: str,
                    payload: PilotTurnFeedbackRequest) -> PilotTurnFeedbackOut:
    """将用户明确的满意/不满意评价追加到对应会话账本。"""
    result = await service.rate_turn(session_id, turn_id, payload.feedback)
    reason = result.get("reason")
    if reason in ("no_session", "turn_gone"):
        raise HTTPException(status_code=404, detail=reason)
    if reason == "bad_request":
        raise HTTPException(status_code=422, detail=reason)
    if reason:
        raise HTTPException(status_code=502, detail=reason)
    return PilotTurnFeedbackOut(**result)


@router.get("/flywheel")
async def flywheel_stats() -> PilotFlywheelStats:
    """决策飞轮画像：决策日志 × 会话账本反馈的只读投影（意图 × 反馈标签）。

    读取侧现算，不落独立账本；无会话态问答计入 unlabeled。"""
    try:
        built = service.build_flywheel()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotFlywheelStats(**built["stats"])


@router.post("/flywheel/export")
async def flywheel_export() -> PilotFlywheelExportOut:
    """导出可训数据集（覆盖写派生投影，随时可重算）；返回计数与画像。"""
    try:
        result = service.export_flywheel()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    stats = result.get("stats")
    return PilotFlywheelExportOut(
        ok=bool(result["ok"]), count=int(result.get("count") or 0),
        preference_count=int(result.get("preference_count") or 0),
        path=result.get("path"), reason=result.get("reason"),
        preference_path=result.get("preference_path"),
        stats=PilotFlywheelStats(**stats) if stats else None,
    )


@router.post("/proposal/confirm")
async def confirm_proposal(payload: PilotProposalConfirm) -> PilotProposalOut:
    """批量提议确认：approve=true 逐项执行，false 作废。

    机器码响应（不抛 5xx）：no_session / proposal_gone（已确认、已取消或被
    更新的提议覆盖）——前端翻用户语言。"""
    try:
        result = await service.confirm_proposal(payload.session_id, payload.pid,
                                                payload.approve)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PilotProposalOut(**result)


@router.post("/tool")
async def run_tool(payload: ToolRunRequest) -> ToolRunResponse:
    """快捷动作直达：+ 菜单点选 → 确定性工具执行（不经模型）。"""
    try:
        result = await service.run_tool(
            payload.name, label=payload.label or payload.name, session_id=payload.session_id
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return ToolRunResponse(step=result["step"], cards=result["cards"])


@router.post("/ask")
async def ask(payload: AskRequest) -> AskResponse:
    try:
        result = await service.ask(payload.question, payload.appid, payload.session_id,
                                   payload.reference_sid,
                                   payload.resume.model_dump() if payload.resume else None,
                                   payload.attachments,
                                   payload.view_context,
                                   payload.element_refs,
                                   payload.quotes)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AskResponse(**result)


@router.post("/ask/stream")
async def ask_stream(payload: AskRequest):
    """SSE 流式问答：thinking / answer 增量 + facts + done，帧为 data: JSON。"""

    async def gen():
        # 运行登记挂在此层（而非 service 内部）：客户端断开时 StreamingResponse
        # 的 finally 必达，登记不会滞留；service.ask_stream 本体保持零感知
        sid = payload.session_id
        if sid:
            service.mark_session_running(sid)
            # 与本层运行登记同生命周期：刷新后据此补一屏进行中轮（含只挂附件的提交）
            service.set_inflight_question(
                sid, (payload.question or "").strip() or ("附件" if payload.attachments else ""))
        try:
            try:
                async for event in service.ask_stream(payload.question, payload.appid,
                                                      payload.session_id, payload.reference_sid,
                                                      payload.resume.model_dump() if payload.resume else None,
                                                      payload.attachments,
                                                      payload.view_context,
                                                      payload.element_refs,
                                                      payload.quotes):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except pilot_llm.PilotLlmError as e:
                yield f'data: {json.dumps({"type": "error", "reason": "llm_failed", "diagnostic": e.to_diagnostic()}, ensure_ascii=False)}\n\n'
            except Exception as e:  # noqa: BLE001 — 流已开始，HTTP 状态无法再改，帧内报错
                diagnostic_id = uuid4().hex[:12]
                log_event(
                    logger,
                    "领航员本轮问答未能完成",
                    tag="失败",
                    level=logging.ERROR,
                    exc_info=True,
                    detail={"诊断编号": diagnostic_id, "会话": sid, "错误类型": type(e).__name__},
                )
                diagnostic = {"id": diagnostic_id, "code": "server_error", "message": ""}
                yield f'data: {json.dumps({"type": "error", "reason": "server_error", "diagnostic": diagnostic}, ensure_ascii=False)}\n\n'
            yield "data: [DONE]\n\n"
        finally:
            if sid:
                service.clear_session_running(sid)
                service.clear_inflight_question(sid)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
