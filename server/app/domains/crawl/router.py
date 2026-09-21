"""crawl 域路由：任务运行 / 停止 / 记录 + SSE 事件流。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.events import bus
from . import coverage as coverage_service
from . import cycle as cycle_service
from . import events as events_service
from . import freshness as freshness_service
from . import service

router = APIRouter(tags=["crawl"])


class CrawlRunRequest(BaseModel):
    scope: str = "appids"  # appids | wishlist | wishlist_only | owned | pool（全池，愿望单优先序排前）
    appids: list[int] | None = None
    regions: list[str] | None = None
    kind: str = "manual"  # manual | missing | backfill（定时层专用 kind 亦可显式触发）


@router.post("/crawl/run")
async def run(req: CrawlRunRequest):
    # 已购抓取未显式指定地区时，套用「已购游戏抓取地区」配置（None=跟随监控地区）
    if req.scope == "owned" and req.regions is None:
        from app.domains.regions.service import owned_regions

        req.regions = await owned_regions()
    try:
        return await service.start_job(
            scope=req.scope,
            appids=req.appids,
            regions=req.regions,
            kind=req.kind,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/crawl/stop")
async def stop(jobId: int | None = None):
    stopped = await service.stop_job(jobId)
    return {"stopped": stopped}


class ImportRequest(BaseModel):
    """批量导入监控单批上限 100（前端超出分批调）。"""

    appids: list[int] = Field(min_length=1, max_length=100)


@router.post("/crawl/import")
async def import_apps(req: ImportRequest):
    """批量导入监控：只分类（ok 新导入 / own 已在库 / fail 无效），不落库表、
    不启动任务——爬取由前端对新导入触发。绝不写 wishlist_items：追踪池只
    收真实 Steam 同步条目与星标关注，导入的游戏仅作 games 库监控数据。"""
    return await service.import_appids(req.appids)


@router.get("/crawl/jobs")
async def jobs(limit: int = Query(20, ge=1, le=100)):
    return await service.list_jobs(limit)


@router.get("/crawl/active")
async def active():
    job_id = service.active_job_id()
    return {"activeJobId": job_id}


@router.get("/crawl/cycles")
async def cycles(limit: int = Query(10, ge=1, le=50)):
    """最近若干轮价格刷新（新→旧），每行带本轮归属的 job。

    回答「这一轮是哪个 Cycle / 现在什么状态 / 包含哪些 Job / 是否进过
    repair / 什么时候结束 / 什么终态」；`cycle_id` 为 NULL 的历史任务与
    暂不归属的修复轮不在任何 Cycle 的 jobs 里。
    """
    return await cycle_service.list_cycles(limit)


# 新鲜度查询一次最多问多少个对象：避免一条请求把全池拉进来
FRESHNESS_MAX_APPIDS = 200


@router.get("/crawl/cycles/{cycle_id}/coverage")
async def cycle_coverage(cycle_id: int):
    """本轮覆盖率：分母来自 Cycle 冻结的期望集（不重查当前监控池）。

    `coverage` = 拿到可购买价格的比例；`coverageConfirmed` = Steam 明确给了
    答复（含锁区）的比例。两者是 Cycle 的观测结果，不是独立生命周期。
    """
    result = await coverage_service.cycle_coverage(cycle_id)
    if result is None:
        raise HTTPException(status_code=404, detail="周期不存在")
    return result


@router.get("/crawl/freshness")
async def freshness(appid: list[int] = Query(default=[])):
    """价格数据新鲜度：每个对象的最后观察时刻与档位（fresh / lagging / stale）。

    观察时间来源是 `game_current_prices.updated_at`；没有任何价格记录的对象
    两者为 null。与覆盖率是两个维度，不合并成综合评分。
    """
    appids = list(dict.fromkeys(int(a) for a in appid))
    if not appids:
        raise HTTPException(status_code=400, detail="缺少 appid 参数")
    if len(appids) > FRESHNESS_MAX_APPIDS:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多查询 {FRESHNESS_MAX_APPIDS} 个 appid",
        )
    return {"items": await freshness_service.appid_freshness(appids)}


@router.get("/crawl/price-events")
async def price_events(
    cycle_id: int | None = None,
    appid: int | None = None,
    event_type: str | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    """最近的价格事实事件（新→旧）。

    事实记录，只读：没有确认、删除或状态流转。`region` 为 null 表示该事件由
    游戏级对象表达（促销免费 / 下架），不属于单一区域。
    """
    if event_type is not None and event_type not in events_service.EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"未知事件类型: {event_type}")
    return await events_service.list_events(
        cycle_id=cycle_id, appid=appid, event_type=event_type, limit=limit
    )


@router.get("/events/stream")
async def events_stream(request: Request):
    """SSE：crawl.progress / job.started / job.status 等事件实时推送。"""
    queue = bus.subscribe()

    async def generator():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield event.as_sse()
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
