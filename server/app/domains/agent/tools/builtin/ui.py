"""结构化 UI 展示工具：允许 Agent 主动选择组件并构建数据呈现。"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.domains.agent.tools.base import Group, Risk, ToolSpec
from app.domains.agent.ui.registry import COMPONENT_REGISTRY
from app.domains.agent.ui.types import StructuredBlock

logger = logging.getLogger(__name__)


async def present_ui(
    component_id: str,
    data: dict[str, Any],
    title: str = "",
    density: str = "standard",
) -> dict:
    """Agent 主动选用结构化 UI 组件展示多维事实数据。

    安全约束：
    1. component_id 必须在 COMPONENT_REGISTRY 注册白名单内；
    2. 禁止包含外部可执行脚本或任意越权操作；
    3. 产生受控的 StructuredBlock 实例。
    """
    meta = COMPONENT_REGISTRY.get(component_id)
    if not meta:
        logger.warning("Agent 尝试调用未注册的 UI 组件: %s", component_id)
        return {
            "kind": "denied",
            "reason": f"未知的 UI 组件类型 '{component_id}'，只允许使用注册中心白名单内的组件。",
        }

    # 规范化展示密度
    valid_density = density if density in ("compact", "standard", "expanded") else "standard"
    instance_id = f"ui_{component_id}_{uuid.uuid4().hex[:8]}"

    block = StructuredBlock(
        id=instance_id,
        component_id=component_id,
        title=title or meta.name,
        density=valid_density,  # type: ignore[arg-type]
        status="ready",
        data=data or {},
    )

    res = block.to_dict()
    res["kind"] = component_id  # 保持与卡片流分发兼容
    return res


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="present_ui",
        group="read",
        risk="low",
        handler=present_ui,
        description="自主选择标准 UI 组件呈现结构化分析结果（支持 metric-grid, data-table, time-series, coverage-matrix, status-board 等）。当用户明确需要图表、矩阵或多维排查对比时使用。",
        step_label="presentUI",
        parameters={
            "type": "object",
            "properties": {
                "component_id": {
                    "type": "string",
                    "description": "注册组件唯一标识（如 'metric-grid', 'data-table', 'time-series', 'coverage-matrix', 'status-board'）",
                },
                "data": {
                    "type": "object",
                    "description": "组件所需的结构化数据载荷",
                },
                "title": {
                    "type": "string",
                    "description": "卡片标题（可选）",
                },
                "density": {
                    "type": "string",
                    "enum": ["compact", "standard", "expanded"],
                    "description": "展示密度，默认为 standard",
                },
            },
            "required": ["component_id", "data"],
        },
    ),
)
