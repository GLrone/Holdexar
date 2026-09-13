"""捆绑包域路由：浏览视图列表 + 补齐计算详情 + 全量刷新。

注意：`/{bundle_id}` 动态段在文件内唯一；`/refresh` 固定段在前，无排序冲突。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import refresh, service

router = APIRouter(prefix="/bundles", tags=["bundles"])


@router.get("")
async def list_bundles():
    """全量捆绑包（差价降序）。数据源 bundles/bundle_region_prices 小表直读。"""
    return {"bundles": await service.list_bundles()}


@router.post("/refresh")
async def refresh_bundles():
    """全量刷新：逐区真实抓取（双轨 Bundle→Package，代理优先）→ upsert。"""
    return await refresh.refresh_bundles()


@router.post("/import")
async def import_bundle(payload: dict):
    """导入捆绑包/Sub：粘贴 Steam 商店或 SteamDB 链接（或裸 ID）识别入库。"""
    text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
    try:
        return await refresh.import_bundle(str(text))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{bundle_id}")
async def bundle_detail(bundle_id: int):
    """单包详情：列表字段 + 包内游戏各区现价（计算器求和用）。"""
    detail = await service.get_bundle_detail(bundle_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    return detail
