"""关注列表（游戏卡星标）语义：follows API + games 列表关注置顶。

关注 = wishlist_items.manual 条目（星标是唯一入口，导入不产生关注）：
- follow：无行在主账号下新建 manual 条目；真愿望单行打 manual 标（追踪保留）
- unfollow：只清 manual 标——真愿望单条目照常追踪，纯手动条目交由下次
  账户同步的反向核对自然出池
- 关注口径：任一账户的 manual 行都算（对齐 crawl/service 的 manual_ids）
- 列表排序：关注置顶（default / 地区模式共用 IN 布尔前缀，top100 为
  Python 重排首位键）

合成 99xxxx 探针行（幂等清理），全程不触网。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.wishlist import follows
from app.domains.wishlist.models import WishlistItem

PROBE = [99700301, 99700302, 99700303]
PRIMARY = "76561199000000001"
OTHER = "76561199000000002"


async def _seed_games() -> None:
    """三款探针游戏：CN 价 + US 更低价（cheaper 模式可见），元数据齐全。"""
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE),
                GameCurrentPrice.region_code.in_(["CN", "US"]),
            )
        )
        await s.execute(delete(WishlistItem).where(WishlistItem.appid.in_(PROBE)))
        for i, appid in enumerate(PROBE):
            s.add(Game(appid=appid, name=f"follow-probe-{i}", min_cny_fen=8000 + i))
            s.add(GameCurrentPrice(
                appid=appid, region_code="CN", price_status="ok",
                currency="CNY", price=10000 + i, original_price=10000 + i,
                discount_percent=0, cny_fen=10000 + i,
            ))
            s.add(GameCurrentPrice(
                appid=appid, region_code="US", price_status="ok",
                currency="USD", price=1100 + i, original_price=1100 + i,
                discount_percent=0, cny_fen=8000 + i,
            ))
        await s.commit()


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE),
                GameCurrentPrice.region_code.in_(["CN", "US"]),
            )
        )
        await s.execute(delete(WishlistItem).where(WishlistItem.appid.in_(PROBE)))
        await s.commit()


def _stub_primary(monkeypatch, primary: str) -> None:
    """主账户解析双通道都桩掉（账号表 + 设置键），不让测试读生产配置。"""
    import app.domains.account.service as account_service
    from app.domains.settings import service as settings_service

    async def fake_table_primary():
        return primary

    async def fake_get(key, default=None):
        return primary if key == "account.steam_id" else default

    monkeypatch.setattr(account_service, "get_primary_steam_id", fake_table_primary)
    monkeypatch.setattr(settings_service, "get_value", fake_get)


async def _rows(appid: int) -> list[WishlistItem]:
    async with get_session_factory()() as s:
        return (
            (
                await s.execute(
                    select(WishlistItem).where(WishlistItem.appid == appid)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_follow_creates_manual_row_under_primary(monkeypatch):
    """无行关注：主账号下新建 manual 条目，出现在关注清单里。"""
    _stub_primary(monkeypatch, PRIMARY)
    appid = PROBE[0]
    await _cleanup()
    try:
        res = await follows.follow(appid)
        assert res == {"appid": appid, "followed": True}
        assert appid in await follows.followed_appids()
        rows = await _rows(appid)
        assert len(rows) == 1
        assert rows[0].steamid == PRIMARY
        assert rows[0].manual is True and rows[0].active is True
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_follow_requires_binding_for_new_entry(monkeypatch):
    """全新条目但无主账号：拒绝（与导入/已购追踪同一条身份要求），且不落行。"""
    _stub_primary(monkeypatch, "")
    await _cleanup()
    with pytest.raises(ValueError):
        await follows.follow(PROBE[0])
    assert await _rows(PROBE[0]) == []


@pytest.mark.asyncio
async def test_unfollow_keeps_wishlist_row(monkeypatch):
    """真愿望单条目：加星 → manual=True；取消 → 只清标，行照常追踪。"""
    _stub_primary(monkeypatch, PRIMARY)
    appid = PROBE[1]
    await _cleanup()
    async with get_session_factory()() as s:
        s.add(WishlistItem(steamid=PRIMARY, appid=appid, active=True, manual=False))
        await s.commit()
    try:
        await follows.follow(appid)
        rows = await _rows(appid)
        assert len(rows) == 1  # 打标不是新建
        assert rows[0].manual is True and rows[0].active is True

        await follows.unfollow(appid)
        rows = await _rows(appid)
        assert len(rows) == 1
        assert rows[0].manual is False and rows[0].active is True  # 追踪保留
        assert appid not in await follows.followed_appids()
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_follow_counts_any_account(monkeypatch):
    """关注口径 = 任一账户的 manual 行（对齐爬取池 manual_ids）。"""
    _stub_primary(monkeypatch, OTHER)
    appid = PROBE[2]
    await _cleanup()
    try:
        await follows.follow(appid)  # OTHER 账户下的手动条目
        assert appid in await follows.followed_appids()
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_list_games_followed_first(monkeypatch):
    """列表关注置顶：default 与地区（cheaper）排序里关注款都排最前。"""
    _stub_primary(monkeypatch, PRIMARY)
    await _seed_games()
    try:
        # 三款探针元数据同档，普通排序按 appid 降序收敛为 [303, 302, 301]。
        # 关注 301（本来垫底）：置顶只能来自关注前缀，不可能是排序巧合。
        await follows.follow(PROBE[0])
        res = await games_service.list_games(q="follow-probe", limit=100)
        ids = [it["appid"] for it in res["items"]]
        assert set(ids) == set(PROBE)
        assert ids[0] == PROBE[0]

        res = await games_service.list_games(
            q="follow-probe", region="US", filter_mode="cheaper", limit=100
        )
        ids = [it["appid"] for it in res["items"]]
        assert set(ids) == set(PROBE)
        assert ids[0] == PROBE[0]

        await follows.unfollow(PROBE[0])
        res = await games_service.list_games(q="follow-probe", limit=100)
        ids = [it["appid"] for it in res["items"]]
        assert ids == [PROBE[2], PROBE[1], PROBE[0]]  # 取消后回到普通排序
    finally:
        await _cleanup()
