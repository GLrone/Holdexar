"""Holdexar 结构化展示组件注册中心（后端权威注册表）。"""
from __future__ import annotations

import logging

from app.domains.agent.ui.types import ComponentMeta

logger = logging.getLogger(__name__)


class ServerComponentRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, ComponentMeta] = {}
        self._init_builtin_components()

    def _init_builtin_components(self) -> None:
        # 五大核心通用组件 (P0)
        self.register(
            ComponentMeta(
                component_id="metric-grid",
                name="指标网格",
                description="结构化关键指标卡片，支持数值、单位、状态色彩、相对变化与基准值对比",
                schema_type="MetricGridData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="data-table",
                name="数据表格",
                description="多维数据表格，支持列排序、分页、状态标签、数值右对齐与行操作",
                schema_type="DataTableData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="time-series",
                name="时间序列走势图",
                description="高保真时间序列图，支持离散价格阶梯线、平滑曲线、极值标记与时间窗切换",
                schema_type="TimeSeriesData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="coverage-matrix",
                name="覆盖率矩阵",
                description="二维对象/地区状态矩阵，区分成功、失败原因、未尝试与过期状态",
                schema_type="CoverageMatrixData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="status-board",
                name="状态看板",
                description="系统服务与后台状态总览看板，呈现心跳、就绪态、告警与修复入口",
                schema_type="StatusBoardData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )

        # legacy 业务卡种不经 present_ui 白名单：它们由数据工具的卡片流直通
        # 前端 legacy-* 渲染，进入白名单只会让模型选到降级路径。

    def register(self, meta: ComponentMeta) -> None:
        if meta.component_id in self._registry:
            logger.warning("Component %s already registered. Overwriting.", meta.component_id)
        self._registry[meta.component_id] = meta

    def get(self, component_id: str) -> ComponentMeta | None:
        return self._registry.get(component_id)



COMPONENT_REGISTRY = ServerComponentRegistry()
