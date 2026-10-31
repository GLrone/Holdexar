"""Holdexar 统一结构化展示系统（HSUI）数据契约与定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StructuredDensity = Literal["compact", "standard", "expanded", "embedded"]
StructuredStatus = Literal[
    "pending", "loading", "partial", "ready", "empty", "error", "cancelled"
]
StructuredHost = Literal["pilot-drawer", "pilot-page", "view-page", "dashboard"]


@dataclass
class StructuredAction:
    """受控交互动作定义。"""

    id: str
    label: str
    icon: str | None = None
    tone: Literal["default", "primary", "success", "warn", "bad"] = "default"
    disabled: bool = False
    tooltip: str | None = None
    confirm: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "tone": self.tone,
            "disabled": self.disabled,
        }
        if self.icon:
            d["icon"] = self.icon
        if self.tooltip:
            d["tooltip"] = self.tooltip
        if self.confirm:
            d["confirm"] = self.confirm
        return d


@dataclass
class StructuredBlock:
    """统一结构化展示块协议实例。"""

    id: str
    component_id: str
    data: dict[str, Any]
    version: str = "1.0"
    title: str | None = None
    description: str | None = None
    source: str | None = None
    updated_at: str | None = None
    density: StructuredDensity = "standard"
    status: StructuredStatus = "ready"
    error_message: str | None = None
    actions: list[StructuredAction] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": self.id,
            "componentId": self.component_id,
            "version": self.version,
            "status": self.status,
            "density": self.density,
            "data": self.data,
        }
        if self.title:
            res["title"] = self.title
        if self.description:
            res["description"] = self.description
        if self.source:
            res["source"] = self.source
        if self.updated_at:
            res["updatedAt"] = self.updated_at
        if self.error_message:
            res["errorMessage"] = self.error_message
        if self.actions:
            res["actions"] = [a.to_dict() for a in self.actions]
        if self.meta:
            res["meta"] = self.meta
        return res


@dataclass(frozen=True)
class ComponentMeta:
    """组件元数据注册定义。"""

    component_id: str
    name: str
    description: str
    supported_densities: tuple[StructuredDensity, ...] = ("standard", "compact", "expanded")
    supported_hosts: tuple[StructuredHost, ...] = ("pilot-drawer", "pilot-page", "view-page", "dashboard")
    default_title: str | None = None
    schema_type: str | None = None

