"""元工具：工具表自述与按需解锁。

模型默认只看到**核心切面**（见 projection.CORE_TOOLS）：工具表的完整定义是
每个请求都要重发一遍的固定开销，只把最常用的一批常驻、其余按需解锁。需要
核心之外的能力时，模型先调用 list_tools 拿到清单，本会话的工具表随即展开。

展开是**会话级且单向**的：工具表是请求前缀的一部分（提供方按 system + 工具表
+ 消息的顺序算缓存键），来回切换会整体击穿前缀缓存，把整段历史重新计费一次。
所以只解锁、不回退——本会话第一次展开之后，后续每轮都带着展开后的工具表，
前缀从此稳定。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec

_DESC_CHARS = 56
"""清单里每条工具的说明截断长度。

清单只是给模型「有没有这个能力」的索引，完整定义在解锁后随下一个请求的
工具表一起到场；这里放全文等于把同一段说明付两遍钱。"""


async def _list_tools(args: dict, sid: str | None = None) -> dict:
    from app.domains.agent.tools.projection import CORE_TOOLS, LIST_TOOLS
    from app.domains.agent.tools.registry import REGISTRY

    extra = [
        s for s in REGISTRY.all()
        if s.name not in CORE_TOOLS and s.name != LIST_TOOLS
    ]
    return {
        "kind": "tool_catalog",
        "note": "以下工具已解锁，本会话后续可直接调用，不必再次 list_tools。",
        "tools": [
            {"name": s.name, "group": s.group,
             "desc": " ".join(s.description.split())[:_DESC_CHARS]}
            for s in extra
        ],
        "count": len(extra),
    }


SPECS = [
    ToolSpec(
        name="list_tools", group="read", risk="low",
        description="列出当前工具表之外的其余工具（清单 + 一句话说明）并解锁它们，"
                    "解锁后本会话可直接调用。当你觉得手头工具做不了用户这件事时先调用它"
                    "——同步账单/家庭组/成就/愿望单、代理与通道诊断、日志检索、系统诊断、"
                    "任务调度、汇率、捆绑包、区服开关、批量清理、历史会话列表等都在里面。"
                    "不要在未查看前就回答「做不到」",
        parameters={"type": "object", "properties": {}},
        handler=_list_tools, step_label="listTools", timeout_s=5.0,
    ),
]

# 注：timeout_s 由 builtin/__init__ 统一以 projection.budget_for 覆盖（20s 默认档），
# 此处声明值不作为运行时预算，仅保持声明完整。
