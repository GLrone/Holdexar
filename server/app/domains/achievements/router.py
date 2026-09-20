"""achievements 域路由：汇总 KPI / 游戏列表 / 单游戏明细 / 同步触发与状态 /
游戏生涯（称号与热力图的原始度量、评语墙）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import career, service

router = APIRouter(prefix="/achievements", tags=["achievements"])


@router.get("/summary")
async def summary(steamid: str = ""):
    """奖杯 KPI（白金/有成就游戏/成就总数/已获得）+ 解锁趋势 + 同步状态。

    `steamid` 为空取主账号；指定即读该账号（数据按 steamid 隔离）。
    """
    return await service.get_summary(steamid or None)


@router.get("/accounts")
async def accounts():
    """可切换账号清单（已绑账号 + 家庭成员；只回展示字段，不含凭证）。"""
    return {"accounts": await service.available_accounts()}


@router.get("/games")
async def games(filter: str = "trophy", sort: str = "playtime", q: str = "", steamid: str = ""):
    """成就视角的游戏列表（含库外；filter: trophy/platinum/progress/external/all）。"""
    return await service.list_games(filter_=filter, sort_=sort, q=q, target=steamid or None)


@router.get("/games/{appid}")
async def game_detail(appid: int, steamid: str = ""):
    """单游戏成就明细（图标/名称/描述/全球占比/解锁态）。"""
    detail = await service.get_game_detail(appid, steamid or None)
    if detail is None:
        raise HTTPException(status_code=404, detail="该游戏不在成就快照中，请先同步")
    return detail


@router.post("/sync")
async def start_sync(steamid: str = ""):
    """发起后台全量同步（进行中返回 409）；`steamid` 指定目标账号。"""
    try:
        return await service.start_sync(steamid or None)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"成就同步启动失败: {e}")


@router.get("/sync")
async def sync_status():
    """同步进度快照（轮询端点）。"""
    return await service.sync_status()


# ─── 游戏生涯（称号 / 热力图 / 偏好画像 / 纪录 / 评语墙）──────────


@router.get("/career")
async def game_career(steamid: str = ""):
    """生涯原始度量：这里只吐数值，称号阈值与文案在前端词典。"""
    return await career.get_career(steamid or None)


class WallPost(BaseModel):
    text: str


@router.get("/career/wall")
async def wall_list():
    """评语墙手写便利贴（最新在后）。"""
    return {"comments": await career.list_wall()}


@router.post("/career/wall")
async def wall_add(payload: WallPost):
    """追加一条便利贴（空白/超长返回 400）。"""
    try:
        return {"comments": await career.add_wall(payload.text)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/career/wall/{comment_id}")
async def wall_remove(comment_id: str):
    """删除一条便利贴（不存在时静默，返回最新列表）。"""
    return {"comments": await career.remove_wall(comment_id)}
