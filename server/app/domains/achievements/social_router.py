"""achievements 社交路由：批注墙 / 多账号对比 / 最近解锁动态流。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import social

router = APIRouter(prefix="/achievements", tags=["achievements"])


@router.get("/notes")
async def notes_list(steamid: str = ""):
    """锚定成就的批注墙（最新更新在前）。"""
    return await social.list_notes(steamid or None)


class NotePost(BaseModel):
    steamid: str = ""
    appid: int
    imageName: str
    content: str
    mood: str | None = None


@router.post("/notes")
async def notes_save(payload: NotePost):
    """保存批注（首评发 XP；空白/超长/心情越界 400）。"""
    try:
        return await social.save_note(
            payload.steamid or None, payload.appid, payload.imageName, payload.content, payload.mood
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/notes/{appid}/{image_name}")
async def notes_remove(appid: int, image_name: str, steamid: str = ""):
    """删除批注（不存在静默；已发 XP 不回冲）。"""
    return {"removed": await social.remove_note(steamid or None, appid, image_name)}


@router.get("/compare")
async def compare(steamids: str = ""):
    """多账号对比（等级/白金/时长/挑战/稀有度；缺省取可切换账号前 4，上限 6）。"""
    return await social.compare(steamids or None)


@router.get("/feed")
async def feed(steamid: str = "", limit: int = 30):
    """最近解锁动态流（解锁时刻倒序，≤100 条）。"""
    return await social.recent_unlocks(steamid or None, limit)
