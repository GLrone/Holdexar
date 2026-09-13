"""crawl 域路由：任务运行 / 停止 / 记录 + SSE 事件流。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.events import bus
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
