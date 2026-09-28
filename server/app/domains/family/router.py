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
async def sync(steam_id: str | None = None):
    """家庭组发现（多账号）：默认遍历全部绑定账号，steam_id 指定时只同步该账号。

    每账号独立成败（Cookie 失效只记该条），成员自动补齐 tracked_accounts，
    快照按账号落行。
    """
    try:
        return await service.sync_family_group(steam_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭组同步失败: {e}")


@router.get("/status")
async def status():
    return await service.get_status()


class MemberRegionsPayload(BaseModel):
    regions: dict[str, str]  # {steamid: region_code}；空值键 = 清除该成员覆盖


@router.put("/member-regions")
async def save_member_regions(req: MemberRegionsPayload):
    """合并保存成员地区选择（前端只上报被改动的成员）。"""
    saved = await service.save_member_regions(req.regions)
    return {"ok": True, "memberRegions": saved}


@router.get("/library")
async def library(steam_id: str | None = None):
    """家庭共享库全量（共享清单 ∪ 成员已购 + 游玩聚合 + 本地元数据/CN 价）。

    steam_id 缺省 = 主账号；多账号下每组各拉各的。
    """
    try:
        return await service.cached_family_library(steam_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭库拉取失败: {e}")


@router.post("/library/refresh")
async def library_refresh(steam_id: str | None = None):
    """强制刷新家庭库快照（绕过缓存重拉）。"""
    service.invalidate_library_cache(steam_id)
    try:
        return await service.cached_family_library(steam_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭库拉取失败: {e}")


@router.get("/wishlist")
async def wishlist(steam_id: str | None = None):
    """家庭成员愿望单聚合（本地 wishlist_items × games join，无需 Cookie）。"""
    try:
        return await service.family_wishlist(steam_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"家庭愿望单聚合失败: {e}")
