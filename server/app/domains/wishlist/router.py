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
    # 导入文件来源（池页「导入文件」传文件名，仅添加端点消费）：非空时把
    # appid 登记进预设池清单（随资产种子分发的出厂游戏集，见 games/preset.py）
    source: str | None = Field(default=None, max_length=80)


@router.post("/pool/items")
async def add_pool_items(req: PoolItemsRequest):
    """批量加入关注（池页添加 / 导入文件共用）。

    走 monitoring 层挂 manual 来源并解除排除——不要求绑定 Steam 账户，
    也不写 wishlist_items（那是 Steam 账户来源数据）。返回逐条分类明细。
    """
    try:
        return await service.add_pool_items(req.appids, source=req.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pool/items/remove")
async def remove_pool_items(req: PoolItemsRequest):
    """批量移出关注（摘用户来源 + 排除标挡账号同步复活 + 清星标）。"""
    return await service.remove_pool_items(req.appids)


# ── 关注列表（游戏卡星标；favorite 来源语义，见 follows.py 模块注释）──


@router.get("/follows")
async def followed_appids():
    """当前关注的 appid 清单（升序）。"""
    return {"appids": await follows.followed_appids()}


@router.put("/follows/{appid}")
async def follow_game(appid: int):
    """关注：挂 favorite 来源（不需要绑定 Steam 账户）。"""
    try:
        return await follows.follow(appid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/follows/{appid}")
async def unfollow_game(appid: int):
    """取消关注：只摘 favorite 来源（Steam 账户来源照常追踪）。"""
    return await follows.unfollow(appid)
