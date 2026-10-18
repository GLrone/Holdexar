"""Holdexar 统一结构化展示系统（HSUI）后端服务与契约模块。"""
from app.domains.agent.ui.types import StructuredBlock, StructuredAction, ComponentMeta
from app.domains.agent.ui.registry import COMPONENT_REGISTRY

__all__ = ["StructuredBlock", "StructuredAction", "ComponentMeta", "COMPONENT_REGISTRY"]
