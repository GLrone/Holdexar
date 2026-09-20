"""捆绑包域路由：浏览视图列表 + 补齐计算详情 + 全量刷新。

注意：`/{bundle_id}` 动态段在文件内唯一；`/refresh` 固定段在前，无排序冲突。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from . import refresh, service

router = APIRouter(prefix="/bundles", tags=["bundles"])


@router.get("")
async def list_bundles(sort: str = Query("diff")):
    """全量捆绑包列表。

    sort=diff（默认，差价降序）| smart（智能评分降序——games 商店同款四因子：
    差价省钱 + 成员游戏质量 + 限时促销 + 评测规模，服务端预计算列）。
    未知排序值回落 diff。返回服务端**预序列化**的 JSON（见
    service.list_bundles_json）：15k 条聚合结果的重复编码是每次请求 2s 级
    开销，编码结果随缓存指纹复用（按排序分槽）。
    """
    return Response(
        content=await service.list_bundles_json(sort),
        media_type="application/json",
    )


@router.post("/refresh")
async def refresh_bundles():
    """全量刷新：监控区整表每区一发（南亚 pk/bd 双发，代理优先）→ upsert。"""
    try:
        return await refresh.refresh_bundles()
    except ValueError as e:
        # 未启用任何区服等配置错误 → 400（前端静默失败即可，不落 500）
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/import")
async def import_bundle(payload: dict):
    """导入捆绑包/Sub：粘贴 Steam 商店或 SteamDB 链接（或裸 ID）识别入库。

    抓取区 = 监控启用区（南亚 pk/bd 双发）——与全量刷新同一口径。
    """
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
