"""monitoring 域路由：监控状态查询与用户侧的排除 / 停止 / 重新监控。

排除与停止都不删除任何数据：Catalog 行、价格历史、愿望单来源全部保留，
只改 Monitoring 层的状态。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import service
from .models import TARGET_TYPES

router = APIRouter(tags=["monitoring"])


class TargetRequest(BaseModel):
    target_type: str = Field(default="game", pattern="^(game|bundle)$")
    target_id: int


class ExcludeRequest(TargetRequest):
    reason: str = ""


class TrackRequest(TargetRequest):
    source: str = "manual"


@router.get("/monitoring/stats")
async def stats():
    return await service.stats()


@router.get("/monitoring/state")
async def state(target_type: str = "game", target_id: int = 0):
    if target_type not in TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"未知 target_type: {target_type}")
    return {
        "targetType": target_type,
        "targetId": target_id,
        "state": await service.state_of(target_type, target_id),
        "sources": await service.sources_of(target_type, target_id),
        "excluded": await service.is_excluded(target_type, target_id),
    }


@router.post("/monitoring/exclude")
async def exclude(req: ExcludeRequest):
    """排除监控：无论来源如何都不再进入 crawl；Catalog 与价格历史保留。"""
    state = await service.set_exclusion(req.target_type, req.target_id, True, req.reason)
    return {"ok": True, "state": state}


@router.post("/monitoring/include")
async def include(req: TargetRequest):
    """解除排除：若仍有有效来源则自动回到 active。"""
    state = await service.set_exclusion(req.target_type, req.target_id, False)
    return {"ok": True, "state": state}


@router.post("/monitoring/stop")
async def stop(req: TargetRequest):
    """停止监控：摘掉全部来源 → released（对象仍在 Catalog）。"""
    state = await service.stop(req.target_type, req.target_id)
    return {"ok": True, "state": state}


@router.post("/monitoring/track")
async def track(req: TrackRequest):
    """重新监控：清排除 + 挂来源 → active。"""
    state = await service.track(req.target_type, req.target_id, req.source)
    return {"ok": True, "state": state}
