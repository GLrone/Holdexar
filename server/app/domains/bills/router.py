"""bills 域路由：Steam 账单自动同步（Cookie 在线拉取）/ 查询 / CDK 计价。

说明：账单由后端常驻任务在 Cookie 绑定后自动拉取
（手动导出 → JSON 上传的旧链路已退役，POST /bills/imports 不再提供）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import service

router = APIRouter(prefix="/bills", tags=["bills"])


class CdkPriceRequest(BaseModel):
    # 前端发 camelCase（manualFen）；不设别名时 Pydantic 静默丢值——实付录入从未生效过
    manual_fen: int | None = Field(
        default=None, alias="manualFen", description="手动实付（分）；null 清除"
    )
    model_config = {"populate_by_name": True}


@router.post("/sync")
async def sync_bills() -> dict:
    """手动触发账单同步（Cookie 绑定与定时任务之外的前端入口）。"""
    result = await service.sync_bills(force=True)
    if result.get("status") == "no_cookie":
        raise HTTPException(409, "尚未绑定 Steam Cookie，无法同步账单")
    if not result.get("ok"):
        raise HTTPException(502, f"账单同步失败：{result.get('error', '未知错误')}")
    return result


@router.get("/sync")
async def sync_status() -> dict:
    """最近一次账单同步状态（running / 结果 / 错误 / 时间）。"""
    return await service.get_sync_snapshot()


@router.get("/imports")
async def list_imports() -> dict:
    return {"imports": await service.list_imports()}


@router.delete("/imports/{import_id}")
async def delete_import(import_id: int) -> dict:
    return await service.delete_import(import_id)


@router.get("/imports/{import_id}/overview")
async def overview(import_id: int) -> dict:
    try:
        return await service.overview(import_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/imports/{import_id}/game-txs")
async def game_txs(
    import_id: int,
    year: str | None = None,
    month: str | None = None,
    txType: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    try:
        return await service.game_txs(import_id, year, month, txType, search, limit, offset)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/imports/{import_id}/topup-txs")
async def topup_txs(import_id: int, limit: int = 200, offset: int = 0) -> dict:
    try:
        return await service.topup_txs(import_id, limit, offset)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/imports/{import_id}/cdk")
async def cdk_games(
    import_id: int, acq: str | None = None, search: str | None = None, limit: int = 500, offset: int = 0
) -> dict:
    try:
        return await service.cdk_games(import_id, acq, search, limit, offset)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/cdk/{cdk_id}/price")
async def set_cdk_price(cdk_id: int, req: CdkPriceRequest) -> dict:
    try:
        return await service.set_cdk_price(cdk_id, req.manual_fen)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
