"""rates 域路由：汇率查询 / 刷新 / 历史。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/rates", tags=["rates"])


@router.post("/refresh")
async def refresh():
    try:
        return await service.refresh_rates()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/history/repair")
async def history_repair(dry_run: bool = False, max_windows: int | None = None):
    """历史缺口修复：本地扫描 → timeframe 批量拉取 → 只写缺失日/carried 日。

    dry_run=true 只出计划（不触网、不写库）；Provider 不可用 / 额度不足时
    返回 status=no_key / quota_exhausted / provider_error 等状态，不抛错。
    """
    from . import history as rates_history

    return await rates_history.repair_history_gaps(dry_run=dry_run, max_windows=max_windows)
