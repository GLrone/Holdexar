"""关注列表（游戏卡星标）服务。

关注 = **用户显式监控来源** `monitor_sources(source="favorite")`，不依赖
Steam 账户：星标与愿望单/已购是两个平行的来源，任一存在即维持监控资格。

口径：favorite 来源就是关注事实本身；Steam 侧名单（愿望单/已购）走
`family_wishlist` / `owned` 来源，由账户同步对账维护，两者互不覆盖
（monitoring 的账号对账只收敛 DERIVED_SOURCES）。

unfollow 只摘 favorite 来源，不动 Steam 账户来源数据：真愿望单条目的追踪
照常保留；与监控池页「移除」的差异——移除是脱池（摘用户来源 + 排除标，
挡账号同步复活），unfollow 不脱池。
"""
from __future__ import annotations

from sqlalchemy import update

from app.core.database import get_session_factory
from .models import WishlistItem


async def followed_appids() -> list[int]:
    """当前关注的 appid（favorite 来源为激活态，升序去重）。"""
    from app.domains.monitoring import service as monitoring_service

    return await monitoring_service.ids_with_source("game", "favorite")


async def follow(appid: int) -> dict:
    """关注一款游戏：挂 favorite 来源（不需要绑定 Steam 账户）。"""
    from app.domains.monitoring import service as monitoring_service

    target = int(appid)
    # 关注 = 用户显式要求监控：解除「别再爬它」后挂来源
    await monitoring_service.set_exclusion("game", target, False, "followed")
    await monitoring_service.ensure_source("game", target, "favorite")
    return {"appid": target, "followed": True}


async def unfollow(appid: int) -> dict:
    """取消关注：只摘 favorite 来源，Steam 账户来源（愿望单/已购）不受影响。"""
    from app.domains.monitoring import service as monitoring_service

    target = int(appid)
    await monitoring_service.detach_source("game", target, "favorite")
    # 历史行上的星标标一并清掉（旧模型遗留：manual 标已不再作为关注真相源）
    async with get_session_factory()() as session:
        await session.execute(
            update(WishlistItem)
            .where(WishlistItem.appid == target, WishlistItem.manual.is_(True))
            .values(manual=False)
        )
        await session.commit()
    return {"appid": target, "followed": False}
