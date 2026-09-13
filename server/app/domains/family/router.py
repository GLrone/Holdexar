"""family 域路由：输入解析 / 家庭组同步 / 状态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

router = APIRouter(prefix="/family", tags=["family"])


class BindRequest(BaseModel):
    input: str  # 好友码 / SteamID64 / 资料 URL / 自定义 URL


@router.get("/resolve")
async def resolve(input: str):
    """好友码/SteamID64/URL → SteamID64（添加成员输入框的实时预览）。"""
    try:
        return await service.resolve_steamid(input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/bind")
async def bind(req: BindRequest):
    """解析输入 → 存为主账号 SteamID64（家庭组跟随 Cookie 持有者，此处只校验身份）。"""
    try:
        resolved = await service.resolve_steamid(req.input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from app.domains.settings.service import set_value

    await set_value("account.steam_id", resolved["steamid"])
    return {**resolved, "message": "已保存为我的 SteamID64"}


@router.post("/sync")
async def sync():
    """发现家庭组：拉成员昵称/头像 → 自动补齐 tracked_accounts → 快照落库。"""
    try:
        return await service.sync_family_group()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭组同步失败: {e}")


@router.get("/status")
async def status():
    return await service.get_status()


class MemberRegionsPayload(BaseModel):
    regions: dict[str, str]  # {steamid: region_code}


@router.put("/member-regions")
async def save_member_regions(req: MemberRegionsPayload):
    """保存家庭页成员地区选择（手动切换持久化）。"""
    saved = await service.save_member_regions(req.regions)
    return {"ok": True, "memberRegions": saved}


@router.get("/library")
async def library():
    """家庭共享库全量（共享清单 ∪ 成员已购 + 游玩聚合 + 本地元数据/CN 价）。"""
    try:
        return await service.cached_family_library()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭库拉取失败: {e}")


@router.post("/library/refresh")
async def library_refresh():
    """强制刷新家庭库快照（绕过缓存重拉）。"""
    service.invalidate_library_cache()
    try:
        return await service.cached_family_library()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭库拉取失败: {e}")


@router.get("/wishlist")
async def wishlist():
    """家庭成员愿望单聚合（本地 wishlist_items × games join，无需 Cookie）。"""
    try:
        return await service.family_wishlist()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭愿望单聚合失败: {e}")
