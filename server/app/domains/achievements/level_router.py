"""achievements 等级路由：GET /level 读数（曲线/段位/加成/近期事件）。"""
from __future__ import annotations

from fastapi import APIRouter

from . import level

router = APIRouter(prefix="/achievements", tags=["achievements"])


@router.get("/level")
async def get_level(steamid: str = ""):
    """等级读数；`steamid` 为空取主账号，首调触发基线回填（幂等）。"""
    return await level.level_profile(steamid or None)
