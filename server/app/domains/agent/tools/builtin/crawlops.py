"""采集运行面只读工具：占用 / 新鲜度 / 轮次 / 价格事件 / 任务停止。

全部为 crawl 域既有投影的只读消费（同 /crawl/* 路由同一事实源）；
crawl_stop 与 cancel_task 的差别：它停的是当前爬取任务本身（含工具
直接发起、未入运行账本的 job），语义与任务页停止按钮一致。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared


async def _crawl_active(args: dict, sid: str | None = None) -> dict:
    """当前活动任务、占用与限流排队（与 /crawl/active 同一投影）。"""
    from app.domains.crawl import service as crawl_service

    snap = await crawl_service.active_snapshot()
    rows = [
        {"k": "抓取进行中", "v": "是" if snap.get("busy") else "否",
         "tone": "ok" if snap.get("busy") else "warn"},
        {"k": "活动任务", "v": str(snap.get("activeJobId") or "无")},
    ]
    if snap.get("maintenance"):
        info = snap.get("maintenanceInfo") or {}
        rows.append({"k": "池体检进行中", "v": str(info.get("kind") or ""),
                     "tone": "warn"})
    throttled = int(snap.get("throttled") or 0)
    if throttled:
        rows.append({"k": "限流排队", "v": str(throttled), "tone": "warn"})
    return _shared.rows_result("crawlActive", rows)


async def _price_freshness(args: dict, sid: str | None = None) -> dict:
    """对象级价格新鲜度 + 全库时钟（对象一次最多 200 个）。"""
    from app.domains.crawl import freshness as freshness_service

    raw = args.get("appids") or []
    appids = list(dict.fromkeys(int(a) for a in raw if int(a) > 0))[:200]
    if not appids:
        return {"kind": "empty", "note": "no_appids"}
    items = await freshness_service.appid_freshness(appids)
    latest = await freshness_service.latest_price_observation()
    rows = [
        {"k": str(it.get("appid")), "v": str(it.get("freshness") or "no_data"),
         "at": it.get("observedAt")}
        for it in items
    ]
    out = _shared.rows_result("freshness", rows, total=len(rows))
    out["lastSuccessAt"] = latest.isoformat() if latest else None
    return out


async def _recent_cycles(args: dict, sid: str | None = None) -> dict:
    """最近价格刷新轮次（新→旧，默认 5 条）。"""
    from app.domains.crawl import cycle as cycle_service

    limit = max(1, min(20, int(args.get("limit") or 5)))
    cycles = await cycle_service.list_cycles(limit)
    rows = [
        {
            "k": f"#{c.get('id')}",
            "v": str(c.get("status") or ""),
            "v2": f"{c.get('batchesDone')}/{c.get('batchesExpected')}",
            "at": c.get("finishedAt") or c.get("startedAt"),
        }
        for c in cycles
    ]
    return _shared.rows_result("cycles", rows, total=len(rows))


async def _price_events(args: dict, sid: str | None = None) -> dict:
    """最近价格事实事件（新低 / 促销免费 / 下架等，新→旧）。"""
    from app.domains.crawl import events as events_service

    event_type = str(args.get("event_type") or "") or None
    if event_type is not None and event_type not in events_service.EVENT_TYPES:
        return {"kind": "empty", "note": "bad_event_type"}
    appid = args.get("appid")
    cycle_id = args.get("cycle_id")
    limit = max(1, min(100, int(args.get("limit") or 20)))
    events = await events_service.list_events(
        cycle_id=int(cycle_id) if cycle_id else None,
        appid=int(appid) if appid else None,
        event_type=event_type, limit=limit,
    )
    rows = [
        {"k": f"{e.get('type')}·{e.get('appid')}", "v": str(e.get("region") or ""),
         "at": e.get("at") or e.get("detectedAt")}
        for e in events
    ]
    return _shared.rows_result("priceEvents", rows, total=len(rows))


async def _crawl_stop(args: dict, sid: str | None = None) -> dict:
    """停止当前爬取任务（含工具直接发起、未入运行账本的 job）。"""
    from app.domains.crawl import service as crawl_service

    ok = await crawl_service.stop_job()
    return _shared.rows_result("crawlStop", [
        {"k": "停止请求", "v": "已受理" if ok else "当前无任务", "tone": "ok" if ok else "warn"},
    ])


SPECS = (
    ToolSpec(
        name="crawl_active", group="read", risk="low",
        description="查看当前是否有抓取任务在跑、代理池体检是否进行中、"
                    "有多少请求在限流排队。「价格怎么不更新」「任务卡了吗」的第一入口",
        parameters={"type": "object", "properties": {}},
        handler=_crawl_active, step_label="crawlActive",
    ),
    ToolSpec(
        name="price_freshness", group="read", risk="low",
        description="查游戏价格数据有多新：每个对象最后观察时刻与档位"
                    "（fresh/lagging/stale），并附全库最近成功观察时刻。"
                    "「这游戏价格是多久前的」用它",
        parameters={"type": "object", "properties": {
            "appids": {"type": "array", "items": {"type": "integer"},
                       "description": "游戏 appid 列表（一次最多 200 个）"},
        }, "required": ["appids"]},
        handler=_price_freshness, step_label="freshness",
    ),
    ToolSpec(
        name="recent_cycles", group="read", risk="low",
        description="最近几轮价格刷新的状态（完成/部分/失败）与批次进度。"
                    "「上一轮刷得怎么样」用它",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 5，最多 20"},
        }},
        handler=_recent_cycles, step_label="cycles",
    ),
    ToolSpec(
        name="price_events", group="read", risk="low",
        description="最近的价格事实事件（新低 / 平史低 / 促销免费 / 下架等，新→旧）。"
                    "可按事件类型 / 游戏 / 轮次过滤",
        parameters={"type": "object", "properties": {
            "event_type": {"type": "string", "description": "事件类型过滤（如 new_low）"},
            "appid": {"type": "integer", "description": "按游戏过滤"},
            "cycle_id": {"type": "integer", "description": "按刷新轮次过滤"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 100"},
        }},
        handler=_price_events, step_label="priceEvents",
    ),
    ToolSpec(
        name="crawl_stop", group="read", risk="high",
        description="停止当前正在跑的抓取任务。用户说「别刷了」「停下来」且"
                    "cancel_task 找不到登记任务时用它",
        parameters={"type": "object", "properties": {}},
        handler=_crawl_stop, step_label="crawlStop",
    ),
)
