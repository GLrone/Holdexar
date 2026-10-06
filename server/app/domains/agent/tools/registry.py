"""工具注册表：ToolSpec 登记（重名即错）、分组过滤与模型视图投影。"""
from __future__ import annotations

from app.domains.agent.tools.base import Group, Risk, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"重复注册工具: {spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def views(self) -> list[dict]:
        """模型可见的 function 视图（openai function 信封形状）。"""
        out = []
        for s in self._specs.values():
            params = dict(s.parameters)
            props = dict(params.get("properties") or {})
            if "emit_card" not in props:
                props["emit_card"] = {
                    "type": "boolean",
                    "description": "是否在用户界面渲染可视化卡片（默认 true；若仅为内部核验/中间查询请传 false）",
                }
                params["properties"] = props
            out.append({
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": params,
                },
            })
        return out

    def meta(self) -> dict[str, dict]:
        """时间线 label 表（旧 TOOL_META 形状：name → {"label": 词条片段}）。"""
        return {name: {"label": s.step_label} for name, s in self._specs.items()}

    def names_of_group(self, group: Group) -> frozenset[str]:
        return frozenset(n for n, s in self._specs.items() if s.group == group)

    def risk_of(self, name: str) -> Risk | None:
        s = self._specs.get(name)
        return s.risk if s else None


REGISTRY = ToolRegistry()
