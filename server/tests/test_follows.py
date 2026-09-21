"""关注列表（游戏卡星标）语义：follows API + games 列表关注置顶。

关注 = 用户显式监控来源 `monitor_sources(source="favorite")`，**不依赖 Steam
账户**（与愿望单/已购来源平行）：
- follow：挂 favorite 来源（无账户也生效）；不写 wishlist_items——那是 Steam
  账户来源数据，不是本地用户身份模型
- unfollow：只摘 favorite 来源，Steam 账户来源照常追踪
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
from app.domains.monitoring import service as monitoring
from app.domains.monitoring.models import MonitorExclusion, MonitorSource, MonitorTarget
from app.domains.wishlist import follows
from app.domains.wishlist.models import WishlistItem

PROBE = [99700301, 99700302, 99700303]
PRIMARY = "76561199000000001"
OTHER = "76561199000000002"


async def _clean_monitoring(session) -> None:
    """探针的监控行一并清理：follow/unfollow 会经 monitoring 域落来源。

    复用调用方的会话——另开会话会在既有写事务之外争 SQLite 锁。
    """
    for model in (MonitorSource, MonitorExclusion, MonitorTarget):
        await session.execute(
            delete(model).where(
                model.target_type == "game", model.target_id.in_(PROBE)
            )
        )


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
        await _clean_monitoring(s)
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
        await _clean_monitoring(s)
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
async def test_follow_attaches_favorite_source_without_account(monkeypatch):
    """无账户关注：只挂 favorite 来源，不伪造 Steam 账户行。"""
    _stub_primary(monkeypatch, "")
    appid = PROBE[0]
    await _cleanup()
    try:
        res = await follows.follow(appid)
        assert res == {"appid": appid, "followed": True}
        assert appid in await follows.followed_appids()
        assert await _rows(appid) == [], "关注不得写 wishlist_items"
        assert await monitoring.sources_of("game", appid) == ["favorite"]
        assert await monitoring.state_of("game", appid) == "active"
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_follow_is_account_independent(monkeypatch):
    """关注与账户无关：换主账号后关注清单不变。"""
    appid = PROBE[2]
    await _cleanup()
    try:
        _stub_primary(monkeypatch, PRIMARY)
        await follows.follow(appid)
        assert appid in await follows.followed_appids()
        _stub_primary(monkeypatch, OTHER)
        assert appid in await follows.followed_appids()
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_unfollow_only_detaches_favorite(monkeypatch):
    """真愿望单条目：关注挂 favorite；取消只摘 favorite，账户来源照常追踪。"""
    _stub_primary(monkeypatch, PRIMARY)
    appid = PROBE[1]
    await _cleanup()
    async with get_session_factory()() as s:
        s.add(WishlistItem(steamid=PRIMARY, appid=appid, active=True, wishlisted=True))
        await s.commit()
    try:
        await monitoring.sync_game_sources([appid])  # 账号派生来源
        await follows.follow(appid)
        assert set(await monitoring.sources_of("game", appid)) == {
            "family_wishlist",
            "favorite",
        }

        await follows.unfollow(appid)
        assert await monitoring.sources_of("game", appid) == ["family_wishlist"]
        assert await monitoring.state_of("game", appid) == "active"  # 追踪保留
        assert appid not in await follows.followed_appids()
        rows = await _rows(appid)
        assert len(rows) == 1 and rows[0].active is True
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
