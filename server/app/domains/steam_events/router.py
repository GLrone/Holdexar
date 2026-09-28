"""steam_events 域路由：官方活动日历查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/steam-events", tags=["steam_events"])


@router.get("")
async def steam_events():
    """活动全量 + 进行中（live）+ 最近未来（next）+ 新鲜度。"""
    try:
        return await service.list_events()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/sync")
async def sync():
    """手动立即同步（校验门不过返回 502，旧数据保留）。"""
    try:
        return await service.sync()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
