"""ToolSpec 契约与 ToolResult 归一。

一处声明携带模型视图（description/parameters）、时间线词条（step_label）、
风险面（group/risk）与卡片映射；加一个新工具 = 一份声明 + 一个词条键。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

Group = Literal["read", "write", "navigate", "admin"]
Risk = Literal["low", "medium", "high"]
Status = Literal["ok", "empty", "denied", "error", "timeout"]


@dataclass(frozen=True)
class ToolResult:
    status: Status
    data: dict
    error_code: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: Group
    risk: Risk
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[dict]]
    step_label: str
    timeout_s: float = 20.0
    idempotent: bool = False
    requires_approval: bool | None = None   # None=按 group/risk 推导（审批 P5 生效）
    card: Callable[[dict], dict | None] | None = None   # 缺省走 kind 驱动通用投影


def derive_status(data: dict) -> Status:
    """handler 结果 dict → 状态（denied / empty / failed→error / timeout / ok）。"""
    kind = data.get("kind")
    if kind == "denied":
        return "denied"
    if kind == "empty":
        return "empty"
    if kind == "failed":
        return "error"
    if kind == "timeout":
        return "timeout"
    return "ok"
