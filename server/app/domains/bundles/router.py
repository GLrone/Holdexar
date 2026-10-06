"""捆绑包域路由：浏览视图列表 + 补齐计算详情 + 全量刷新 + 关注/移除。

注意：`/{bundle_id}` 动态段之前必须排全部固定段（/refresh、/import、
/follows），否则固定路径会被动态段吞掉。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from . import refresh, service

router = APIRouter(prefix="/bundles", tags=["bundles"])


@router.get("")
async def list_bundles(request: Request, sort: str = Query("diff"), removed: bool = False):
    """全量捆绑包列表。

    sort=diff（默认，差价降序）| smart（智能评分降序——games 商店同款四因子：
    差价省钱 + 成员游戏质量 + 限时促销 + 评测规模，服务端预计算列）。
    未知排序值回落 diff。返回服务端**预序列化**的 JSON（见
    service.list_bundles_json）：15k 条聚合结果的重复编码是每次请求 2s 级
    开销，编码结果随缓存指纹复用（按排序分槽）。

    客户端声明 gzip 时下发预压缩变体（service.list_bundles_gzip_json）并显式
    带 Content-Encoding：传输压缩中间件见响应已有编码即原样透传，不再收齐
    整包重压一次。

    removed=true 只出已移除/已排除的包（恢复视图，普通 dict 序列化——
    恢复集小，不走 15k 全量的预序列化缓存槽）。
    """
    if removed:
        return {"bundles": await service.list_bundles(sort, removed=True)}
    if "gzip" in (request.headers.get("accept-encoding") or ""):
        return Response(
            content=await service.list_bundles_gzip_json(sort),
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(
        content=await service.list_bundles_json(sort),
        media_type="application/json",
    )


@router.get("/follows")
async def followed_bundle_ids():
    """当前关注的包 id 清单（升序；星标状态一次性整表拉取）。"""
    return {"bundleIds": await service.followed_bundle_ids()}


@router.put("/{bundle_id}/follow")
async def follow_bundle(bundle_id: int):
    """关注一个包：挂 favorite 来源（列表置顶）。"""
    return await service.follow_bundle(bundle_id)


@router.delete("/{bundle_id}/follow")
async def unfollow_bundle(bundle_id: int):
    """取消关注：只摘 favorite 来源。"""
    return await service.unfollow_bundle(bundle_id)


@router.post("/{bundle_id}/remove")
async def remove_bundle(bundle_id: int):
    """移除捆绑包：列表隐藏 + 退出刷新；「已移除」视图可恢复。"""
    try:
        return await service.remove_bundle(bundle_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{bundle_id}/restore")
async def restore_bundle(bundle_id: int):
    """恢复被移除的包（只解除移除链路所挂的排除）。"""
    return await service.restore_bundle(bundle_id)


@router.post("/refresh")
async def refresh_bundles():
    """全量刷新：监控区整表每区一发（南亚 pk/bd 双发，代理优先）→ upsert。

    与价格轮链尾同口径入运行账（独立 run：手动按钮无调度桥上下文）。"""
    from app.domains.agent.runtime import scheduler_bridge

    try:
        return await scheduler_bridge.run_accounted(
            "bundle_refresh", refresh.refresh_bundles, trigger="manual",
            ref={"entry": "manual_refresh_button"},
        )
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
