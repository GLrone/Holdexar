"""导航工具：target 白名单 → 站内路径（与 web 路由表同名对齐）。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec

# 导航目标白名单：target 键 → 站内路径（agent 导航工具的目标集，
# 与 web 路由表同名对齐；不在表内的目标一律拒绝执行）
NAV_TARGETS = {
    "dashboard": "/dashboard",
    "library": "/library",
    "gamelib": "/gamelib",
    "follows": "/pool",
    "bundles": "/bundles",
    "alerts": "/alerts",
    "events": "/events",
    "achievements": "/achievements",
    "family": "/family",
    "bills": "/bills",
    "rates": "/rates",
    "toolbox": "/toolbox",
    "crawl": "/crawl",
    "proxies": "/proxies",
    "fetch": "/fetch",
    "logs": "/logs",
    "settings": "/settings",
}


async def _navigate(args: dict, sid: str | None = None) -> dict:
    target = str(args.get("target") or "")
    path = NAV_TARGETS.get(target)
    if not path:
        return {"kind": "navigate", "target": "", "path": ""}
    return {"kind": "navigate", "target": target, "path": path}


SPECS = [
    ToolSpec(
        name="navigate", group="navigate", risk="low",
        description="跳转到用户想查看的模块页面。target 取值（用户说法 → target）："
                    "仪表盘=dashboard、找游戏=library、游戏库=gamelib、我的关注=follows、"
                    "捆绑包=bundles、价格提醒=alerts、活动日历=events、成就=achievements、"
                    "家庭=family、账单=bills、汇率=rates、工具箱=toolbox、任务=crawl、"
                    "网络=proxies、自动抓取=fetch、日志=logs、设置=settings。"
                    "仅当用户表达想查看/打开某模块时调用",
        parameters={"type": "object", "properties": {
            "target": {"type": "string", "description": "模块标识，取上方取值列表之一"},
        }, "required": ["target"]},
        handler=_navigate, step_label="navigate",
    ),
]
