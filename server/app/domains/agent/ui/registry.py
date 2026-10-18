"""Holdexar 结构化展示组件注册中心（后端权威注册表）。"""
from __future__ import annotations

import logging
from typing import Any

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
                agent_description="适用于呈现核心统计数据、监控覆盖率总览、代理健康评分、抓取吞吐指标或个人成就账单概况。",
                schema_type="MetricGridData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="data-table",
                name="数据表格",
                description="多维数据表格，支持列排序、分页、状态标签、数值右对齐与行操作",
                agent_description="适用于游戏清单、代理节点明细、抓取任务列表、地区比价清单等多行结构化数据。",
                schema_type="DataTableData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="time-series",
                name="时间序列走势图",
                description="高保真时间序列图，支持离散价格阶梯线、平滑曲线、极值标记与时间窗切换",
                agent_description="适用于游戏历史价格变化趋势、网络延迟波动、系统吞吐变化等带时间戳的数据。",
                schema_type="TimeSeriesData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="coverage-matrix",
                name="覆盖率矩阵",
                description="二维对象/地区状态矩阵，区分成功、失败原因、未尝试与过期状态",
                agent_description="适用于展现多款游戏在多个地区的采集覆盖情况、节点在不同目标域名的测试状态等。",
                schema_type="CoverageMatrixData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )
        self.register(
            ComponentMeta(
                component_id="status-board",
                name="状态看板",
                description="系统服务与后台状态总览看板，呈现心跳、就绪态、告警与修复入口",
                agent_description="适用于呈现代理池健康状态、调度器运行态、写闸队列、更新器与后台任务就绪情况。",
                schema_type="StatusBoardData",
                supported_densities=("compact", "standard", "expanded"),
            )
        )

        # 既有业务卡种（作为 legacy 契约挂牌注册）
        self.register(
            ComponentMeta(
                component_id="legacy-price",
                name="游戏价格卡",
                description="单款游戏的当前价格、史低比对与年份区间",
                agent_description="展示单款游戏在国区与其他主要地区的现价与史低信息。",
                schema_type="PilotPriceFacts",
            )
        )
        self.register(
            ComponentMeta(
                component_id="legacy-games",
                name="游戏卡片流",
                description="游戏图文卡片列表，包含封面、评分与价格",
                agent_description="展示搜索、推荐或榜单查询出的游戏简短图文列表。",
                schema_type="PilotGameFacts",
            )
        )
        self.register(
            ComponentMeta(
                component_id="legacy-compare",
                name="游戏对比卡",
                description="多款游戏横向比对价格与评价指标",
                agent_description="对比 2-4 款游戏的当前价格、史低与好评率。",
                schema_type="PilotCompareFacts",
            )
        )
        self.register(
            ComponentMeta(
                component_id="legacy-regions",
                name="多区比价表",
                description="单款游戏在全球各区服的折合人民币价格排序",
                agent_description="展示全球各主要地区的售价与最低购买建议。",
                schema_type="PilotRegionsFacts",
            )
        )
        self.register(
            ComponentMeta(
                component_id="legacy-stepper",
                name="步骤工作流",
                description="连续工作流或排查步骤执行状态",
                agent_description="展示多阶段清理、迁移或体检的逐步执行进度。",
                schema_type="PilotStepperFacts",
            )
        )
        self.register(
            ComponentMeta(
                component_id="legacy-proposal",
                name="操作确认面板",
                description="涉及批量增删或敏感配置变更的双向确认面板",
                agent_description="需要用户授权批准时展示的操作建议面板。",
                schema_type="PilotProposalFacts",
            )
        )

    def register(self, meta: ComponentMeta) -> None:
        if meta.component_id in self._registry:
            logger.warning("Component %s already registered. Overwriting.", meta.component_id)
        self._registry[meta.component_id] = meta

    def get(self, component_id: str) -> ComponentMeta | None:
        return self._registry.get(component_id)

    def all(self) -> list[ComponentMeta]:
        return list(self._registry.values())

    def agent_catalog(self) -> list[dict[str, Any]]:
        """供 Agent 提示词或工具发现读取的轻量级组件目录。"""
        # 优先暴露五大通用核心组件及 legacy 专有卡片
        return [meta.to_agent_summary() for meta in self._registry.values()]


COMPONENT_REGISTRY = ServerComponentRegistry()
