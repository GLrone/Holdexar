"""rates 域路由：汇率查询 / 刷新 / 历史。
"""
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


@router.get("/history")
async def history(currency: str = "USD", limit: int = 0, range: str | None = None):
    """日线历史。range 六档：1mo/6mo/1y/5y/10y/all。

    默认全部（16 年档案全量）；limit>0 时再取尾段 limit 天。
    """
    try:
        return await service.rate_history(currency, limit, range)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/history/gaps")
async def history_gaps():
    """本地缺口扫描（零网络）：缺行日 ∪ carried 日，按窗口聚类。"""
    from . import history as rates_history

    return await rates_history.scan_history_gaps()


@router.post("/history/repair")
async def history_repair(dry_run: bool = False, max_windows: int | None = None):
    """历史缺口修复：本地扫描 → timeframe 批量拉取 → 只写缺失日/carried 日。

    dry_run=true 只出计划（不触网、不写库）；Provider 不可用 / 额度不足时
    返回 status=no_key / quota_exhausted / provider_error 等状态，不抛错。
    """
    from . import history as rates_history

    return await rates_history.repair_history_gaps(dry_run=dry_run, max_windows=max_windows)
