"""关注列表（游戏卡星标）服务。

关注 = 监控池 wishlist_items 的 manual 条目（星标是关注的唯一入口，
导入/添加通道不产生关注——那是手动入池 manual_pool，与关注列表无关）。
关注即入池，与愿望单同属爬取队列第一优先级。

口径（对齐 crawl/service 的 manual_ids）：manual 按任一账户计，appid 去重——
池子的存续以行为准，主账号换绑后旧账户的手动行仍是池内事实。

unfollow 只清 manual 标记、不动行本身：真愿望单条目的追踪照常保留
（下次同步不会洗掉）；纯手动条目交由下次账户同步的反向核对自然出池。
与监控池页「移除」的差异：移除是脱池（active=0 + excluded 挡同步复活），
unfollow 不脱池。
"""
from __future__ import annotations

from sqlalchemy import select, update

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from .models import WishlistItem
from .service import _sync_monitoring, resolve_pool_steamid


async def followed_appids() -> list[int]:
    """当前关注的 appid（manual + active，任一账户，去重升序）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(WishlistItem.appid)
                .where(WishlistItem.manual.is_(True), WishlistItem.active.is_(True))
                .distinct()
                .order_by(WishlistItem.appid)
            )
        ).all()
    return [int(r.appid) for r in rows]


def _naive_now():
    return get_beijing_time_obj().replace(tzinfo=None)


async def follow(appid: int) -> dict:
    """关注一款游戏：已有行复活 + 打 manual 标；无行则在主账号下新建手动条目。"""
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(WishlistItem).where(WishlistItem.appid == int(appid))
                )
            )
            .scalars()
            .all()
        )
        if rows:
            # 已有行（真愿望单/已停用的旧手动行/池页移除过的行）：复活 + 打标，
            # 一行不漏；同时清 excluded——关注 = 用户显式要求入池；
            # 无需新建 → 不要求绑定（池内事实先行，身份只在落新行时才需要）
            for row in rows:
                row.active = True
                row.manual = True
                row.excluded = False
        else:
            steamid = await resolve_pool_steamid()
            if not steamid:
                raise ValueError("尚未绑定 SteamID64（请先在「我」页绑定账户）")
            session.add(
                WishlistItem(
                    steamid=steamid,
                    appid=int(appid),
                    added_at=_naive_now(),
                    active=True,
                    owned=False,
                    manual=True,
                )
            )
        await session.commit()
    # 关注 = 用户显式要求监控：解除排除后按现状重算来源（挂上 favorite）
    await _sync_monitoring([int(appid)], exclusion=False)
    return {"appid": int(appid), "followed": True}


async def unfollow(appid: int) -> dict:
    """取消关注：清 manual 标记（行保留——真愿望单追踪不受影响，纯手动条目下次同步出池）。"""
    async with get_session_factory()() as session:
        await session.execute(
            update(WishlistItem)
            .where(WishlistItem.appid == int(appid), WishlistItem.manual.is_(True))
            .values(manual=False)
        )
        await session.commit()
    await _sync_monitoring([int(appid)])
    return {"appid": int(appid), "followed": False}
