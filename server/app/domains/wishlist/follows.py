"""关注列表（游戏卡星标）服务。

关注 = 追踪池 wishlist_items 的 manual 条目（星标是关注的唯一入口；
任务页的批量导入/收藏导入不产生关注——导入的 游戏只爬取入库作监控数据，
与愿望单、关注列表都无关）。关注即入池，爬取队列最优先。

口径（对齐 crawl/service 的 manual_ids）：manual 按任一账户计，appid 去重——
池子的存续以行为准，主账号换绑后旧账户的手动行仍是池内事实。

unfollow 只清 manual 标记、不动行本身：真愿望单条目的追踪照常保留
（下次同步不会洗掉）；纯手动条目交由下次账户同步的反向核对自然出池。
"""
from __future__ import annotations

from sqlalchemy import select, update

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from .models import WishlistItem


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


async def _resolve_pool_steamid() -> str:
    """新关注条目的落行身份：主账号优先，回退设置页手填。"""
    from app.domains.account import service as account_service

    primary = await account_service.get_primary_steam_id()
    if primary:
        return primary
    from app.domains.settings.service import get_value

    return ((await get_value("account.steam_id", "")) or "").strip()


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
            # 已有行（真愿望单/已停用的旧手动行）：复活 + 打标，一行不漏；
            # 无需新建 → 不要求绑定（池内事实先行，身份只在落新行时才需要）
            for row in rows:
                row.active = True
                row.manual = True
        else:
            steamid = await _resolve_pool_steamid()
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
    return {"appid": int(appid), "followed": False}
