"""Agent V2 P3 工具层：ToolSpec 一处声明 + 注册表 + 策略 + 执行器。"""
from app.domains.agent.tools.base import ToolResult, ToolSpec
from app.domains.agent.tools.registry import REGISTRY, ToolRegistry

__all__ = ["REGISTRY", "ToolRegistry", "ToolResult", "ToolSpec"]
