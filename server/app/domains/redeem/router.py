"""redeem 域路由：CDK 批量激活 + 免费产品领取。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import service

router = APIRouter(prefix="/redeem", tags=["redeem"])


class ActivateRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=100)


class FreeClaimRequest(BaseModel):
    subids: list[int] = Field(min_length=1, max_length=100)


@router.post("/keys")
async def activate_keys(req: ActivateRequest):
    """批量激活 CDK（串行，逐码真实调用 Steam 激活端点）。"""
    try:
        return await service.activate_batch([k.strip() for k in req.keys if k.strip()])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"激活请求失败: {e}")


@router.post("/free")
async def claim_free(req: FreeClaimRequest):
    """批量领取免费产品（SubID 通道，串行真实调用）。"""
    try:
        return await service.add_free_licenses(req.subids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"领取请求失败: {e}")


@router.get("/quota")
async def quota():
    """激活前提状态（Cookie 可用性）——前端激活前的引导提示。"""
    return await service.quota_status()
