"""挑战中心路由：目录读数（GET 即幂等重算）与奖励领取。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import challenges

router = APIRouter(prefix="/achievements", tags=["achievements"])


@router.get("/challenges")
async def challenges_list(steamid: str = ""):
    """挑战目录全量读数；tier/category/target/reward 恒出代码常量，GET 触发一次幂等重算。"""
    return await challenges.challenges_overview(steamid or None)


@router.post("/challenges/{challenge_id}/claim")
async def claim_challenge(challenge_id: str, steamid: str = ""):
    """领取挑战奖励；未知 id 404，未完成或已领取 409。"""
    if challenge_id not in challenges.CATALOG_BY_ID:
        raise HTTPException(status_code=404, detail="未知挑战")
    try:
        return await challenges.claim(steamid or None, challenge_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
