"""导航工具：target 白名单 → 站内路径（与 web 路由表同名对齐），支持页面板块与游戏详情。"""
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

# 页面板块白名单：target → 页签键集合（与前端页签键逐字对齐，不在表内的板块一律拒绝）
NAV_SECTIONS: dict[str, tuple[str, ...]] = {
    "gamelib": ("owned", "insights", "family", "play"),
    "bills": ("ledger", "topup", "cdk"),
    "family": ("contrib", "heat", "buy", "wish"),
}


async def _navigate(args: dict, sid: str | None = None) -> dict:
    target = str(args.get("target") or "")
    if target == "game":
        try:
            appid = int(args.get("appid") or 0)
        except (TypeError, ValueError):
            appid = 0
        if appid <= 0:
            return {"kind": "navigate", "target": "", "path": ""}
        return {"kind": "navigate", "target": "game", "path": f"/game/{appid}", "appid": appid}
    path = NAV_TARGETS.get(target)
    if not path:
        return {"kind": "navigate", "target": "", "path": ""}
    out = {"kind": "navigate", "target": target, "path": path}
    section = str(args.get("section") or "")
    if section and section in NAV_SECTIONS.get(target, ()):
        out["section"] = section
    return out


_SPECS_DESC = (
    "跳转到用户想查看的模块页面。target 取值（用户说法 → target）："
    "仪表盘=dashboard、游戏商店=library、游戏库=gamelib、我的关注=follows、"
    "捆绑包=bundles、价格提醒=alerts、活动日历=events、成就=achievements、"
    "家庭=family、账单=bills、汇率=rates、工具箱=toolbox、任务=crawl、"
    "网络=proxies、自动抓取=fetch、日志=logs、设置=settings、"
    "某款游戏的详情页=target 传 game 且给 appid。"
    "gamelib/bills/family 三个页面支持 section 定位到具体板块"
    "（游戏库：owned 我的游戏/insights 游玩洞察/family 家庭库/play 游玩动态；"
    "账单：ledger 消费总览/topup 充值/cdk 激活码；家庭：contrib 成员贡献/"
    "heat 热度/buy 合购分析/wish 愿望单）。"
    "仅当用户表达想查看/打开某模块时调用"
)

SPECS = [
    ToolSpec(
        name="navigate", group="navigate", risk="low",
        description=_SPECS_DESC,
        parameters={"type": "object", "properties": {
            "target": {"type": "string", "description": "模块标识，取上方取值列表之一；游戏详情页传 game"},
            "section": {"type": "string", "description": "可选板块键，仅 gamelib/bills/family 支持上方列出的取值"},
            "appid": {"type": "integer", "description": "仅 target=game 时必填：游戏 AppID"},
        }, "required": ["target"]},
        handler=_navigate, step_label="navigate",
    ),
]
