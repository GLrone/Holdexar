"""工具执行器：策略判定 → 超时包裹 → 失败归一化。

铁律：工具失败 / 超时 = 归一化 ToolResult 回灌模型，绝不终结 run。
失败 / 超时的结果形状（kind=failed/timeout）与旧 service 侧行为逐字对齐；
入参语义校验沿用各 handler 既有口径，强类型校验随 P5 审批面引入。"""
from __future__ import annotations

import asyncio
import logging

from app.core.logging import log_event
from app.domains.agent.tools import policy
from app.domains.agent.tools.base import Status, ToolResult, ToolSpec, derive_status

logger = logging.getLogger(__name__)


async def execute(spec: ToolSpec, arguments: dict, *, guarded: bool = False,
                  sid: str | None = None) -> ToolResult:
    if policy.decide(spec, guarded=guarded) == "deny":
        return ToolResult(status="denied", data=dict(policy.DENY_RESULT))
    try:
        data = await asyncio.wait_for(spec.handler(arguments or {}, sid=sid), timeout=spec.timeout_s)
    except asyncio.TimeoutError:
        return ToolResult(status="timeout",
                          data={"kind": "timeout", "tool": spec.name, "budget_s": int(spec.timeout_s)},
                          error_code="timeout")
    except Exception as e:  # noqa: BLE001 — 归一化铁律：异常 = 结果
        log_event(
            logger,
            f"领航员工具「{spec.name}」执行失败，已将失败结果回灌模型",
            level=logging.ERROR,
            exc_info=True,
            detail={"工具": spec.name, "原因": type(e).__name__},
        )
        return ToolResult(status="error",
                          data={"kind": "failed", "tool": spec.name, "note": type(e).__name__},
                          error_code="tool_error")
    status: Status = derive_status(data)
    return ToolResult(status=status, data=data)
