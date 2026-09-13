"""regions 域路由：区服元数据下发 + 启用配置。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

router = APIRouter(prefix="/regions", tags=["regions"])


class EnabledUpdate(BaseModel):
    # None = 全部启用；否则为 cc 小写代码精确列表
    enabled: list[str] | None = None


class OwnedUpdate(BaseModel):
    # None = 跟随启用集；否则为 cc 小写代码精确列表（空列表 400）
    regions: list[str] | None = None


@router.get("")
async def get_regions() -> dict:
    """区服元数据单一来源：前端删除硬编码副本，从这里下发。"""
    return {
        "regions": await service.list_regions(),
        "ownedRegions": await service.owned_regions(),
    }


@router.put("/enabled")
async def update_enabled(payload: EnabledUpdate) -> dict:
    try:
        await service.set_enabled(payload.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"regions": await service.list_regions()}


@router.put("/owned")
async def update_owned(payload: OwnedUpdate) -> dict:
    try:
        owned = await service.set_owned_regions(payload.regions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ownedRegions": owned}
