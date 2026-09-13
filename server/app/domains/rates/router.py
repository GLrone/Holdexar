"""rates 域路由：汇率查询 / 刷新 / 历史。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/rates", tags=["rates"])


@router.get("")
async def rates():
    return await service.list_rates()


@router.post("/refresh")
async def refresh():
    try:
        return await service.refresh_rates()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/backfill")
async def backfill(dry_run: bool = False):
    """历史缺口补齐（幂等，只补零历史行的交易日）。dry_run=true 只算不写。"""
    return await service.backfill_history(dry_run=dry_run)


@router.get("/history")
async def history(currency: str = "USD", limit: int = 0, range: str | None = None):
    """日线历史。range 对齐原版 fx-trend 六档：1mo/6mo/1y/5y/10y/all。

    默认全部（16 年档案全量）；limit>0 时再取尾段 limit 天。
    """
    try:
        return await service.rate_history(currency, limit, range)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
