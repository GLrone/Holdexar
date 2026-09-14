"""wishlist 域路由：账户绑定 / 同步 / 监控池管理（条目增删）/ 关注（星标）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import follows, service

router = APIRouter(tags=["wishlist"])


class AccountAdd(BaseModel):
    steamid: str  # SteamID64 / 好友码 / 个人资料 URL / 自定义 URL
    label: str = ""
    kinds: dict | None = None


class AccountSyncResult(BaseModel):
    steamid: str
    wishlistCount: int
    ownedCount: int
    added: int
    active: int
    newAppids: list[int]
    crawlTriggered: bool | None = None


class AccountKinds(BaseModel):
    wishlist: bool | None = None
    owned: bool | None = None


@router.get("/accounts")
async def accounts():
    return await service.list_accounts()


@router.post("/accounts")
async def add_account(req: AccountAdd):
    try:
        return await service.add_account(req.steamid, req.label, req.kinds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/accounts/{steamid}/kinds")
async def update_account_kinds(steamid: str, req: AccountKinds):
    """账户同步类型（任务页·已购游戏抓取·账户设置的已购开关落点）。"""
    try:
        patch = {k: v for k, v in req.model_dump().items() if v is not None}
        return await service.update_kinds(steamid, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/accounts/{steamid}")
async def remove_account(steamid: str):
    removed = await service.remove_account(steamid)
    if not removed:
        raise HTTPException(status_code=404, detail="账户不存在")
    return {"removed": True}


@router.post("/accounts/{steamid}/sync")
async def sync_account(steamid: str, autoCrawl: bool = True):
    try:
        return await service.sync_account(steamid, auto_crawl=autoCrawl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/wishlist")
async def wishlist(steamid: str | None = None):
    """监控条目列表（按 appid 聚合，含来源标记：愿望单/关注/已购/手动入池）。"""
    return await service.list_items(steamid)


@router.get("/wishlist/appids")
async def wishlist_appids():
    """监控池 appid 轻量全集（dashboard 展厅入选判定用，不带名称/封面）。"""
    return await service.list_appids()


@router.get("/owned-library")
async def owned_library():
    """游戏库页数据源：全部追踪账户的已购游戏矩阵（账户档案 + 去重游戏 + 拥有者）。"""
    return await service.owned_library()


@router.get("/ownership")
async def ownership(appids: str):
    """批量查询游戏归属（游戏卡左上角徽章）。appids=逗号分隔，单次上限 200。"""
    ids: list[int] = []
    for raw in appids.split(","):
        raw = raw.strip()
        if raw.isdigit():
            ids.append(int(raw))
    if not ids:
        raise HTTPException(status_code=400, detail="appids 不能为空")
    return await service.ownership(ids[:200])


# ── 监控池管理（监控条目的添加 / 移除；批量操作）──


class PoolItemsRequest(BaseModel):
    """监控条目批量操作请求（单批上限 500，前端超出时分批调用）。"""

    appids: list[int] = Field(min_length=1, max_length=500)


@router.post("/pool/items")
async def add_pool_items(req: PoolItemsRequest):
    """批量添加监控条目（池页添加 / 任务页导入共用）。

    无行新建 manual_pool 条目（普通监控条目，不进愿望单/关注名单）；
    已有行复活（含愿望单/已购/关注行）。返回逐条分类明细。
    """
    try:
        return await service.add_pool_items(req.appids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pool/items/remove")
async def remove_pool_items(req: PoolItemsRequest):
    """批量移除监控条目（脱池 + excluded 挡同步复活 + 清星标）。"""
    return await service.remove_pool_items(req.appids)


# ── 关注列表（游戏卡星标；manual 条目语义，见 follows.py 模块注释）──


@router.get("/follows")
async def followed_appids():
    """当前关注的 appid 清单（升序）。"""
    return {"appids": await follows.followed_appids()}


@router.put("/follows/{appid}")
async def follow_game(appid: int):
    """关注：入追踪池 + 打 manual 标（爬取队列最优先，同步免疫）。"""
    try:
        return await follows.follow(appid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/follows/{appid}")
async def unfollow_game(appid: int):
    """取消关注：只清 manual 标（真愿望单追踪保留，纯手动条目下次同步出池）。"""
    return await follows.unfollow(appid)
