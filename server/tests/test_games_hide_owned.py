"""games 列表 hide_owned 筛选语义（游戏库「隐藏已拥有」服务端过滤）。

口径与游戏卡归属徽章一致：主账户 owned 行排除（未配置主账户时任一追踪
账户 owned 行排除），非主账户 owned（家庭共享徽章款）与无人拥有款保留。

合成 99xxxx 探针行（真实库只读前提 + 幂等清理），全程不触网。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.wishlist.models import WishlistItem

PROBE = [99700101, 99700102, 99700103]
PRIMARY = "76561199000000001"
OTHER = "76561199000000002"


async def _seed() -> None:
    async with get_session_factory()() as s:
        for table, keys in (
            (Game, {"appid": PROBE}),
            (WishlistItem, {"steamid": [PRIMARY, OTHER]}),
        ):
            col = getattr(table, list(keys)[0])
            await s.execute(delete(table).where(col.in_(list(keys.values())[0])))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE), GameCurrentPrice.region_code == "CN"
            )
        )
        for i, appid in enumerate(PROBE):
            s.add(Game(appid=appid, name=f"hide-owned-probe-{i}", min_cny_fen=10000 + i))
            s.add(GameCurrentPrice(
                appid=appid, region_code="CN", price_status="ok",
                currency="CNY", price=10000 + i, original_price=10000 + i,
                discount_percent=0, cny_fen=10000 + i,
            ))
        # 101 = 主账户拥有；102 = 其他追踪账户拥有（家庭共享徽章款）；103 = 无人拥有
        s.add(WishlistItem(steamid=PRIMARY, appid=PROBE[0], owned=True, active=True))
        s.add(WishlistItem(steamid=OTHER, appid=PROBE[1], owned=True, active=True))
        await s.commit()


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE), GameCurrentPrice.region_code == "CN"
            )
        )
        await s.execute(
            delete(WishlistItem).where(WishlistItem.steamid.in_([PRIMARY, OTHER]))
        )
        await s.commit()


def _stub_primary(monkeypatch, primary: str) -> None:
    """主账户解析双通道都桩掉（账号表 + 设置键），不让测试读生产配置。"""
    import app.domains.account.service as account_service
    from app.domains.settings import service as settings_service

    async def fake_table_primary():
        return primary

    async def fake_get(key, default=None):
        return PRIMARY if key == "account.steam_id" else default

    monkeypatch.setattr(account_service, "get_primary_steam_id", fake_table_primary)
    monkeypatch.setattr(settings_service, "get_value", fake_get)


@pytest.mark.asyncio
async def test_hide_owned_excludes_primary_only(monkeypatch):
    """配置主账户：主账户 owned 款隐藏；家庭共享款与无人拥有款保留。"""
    _stub_primary(monkeypatch, PRIMARY)
    await _seed()
    try:
        res = await games_service.list_games(hide_owned=True, limit=100, q="hide-owned-probe")
        ids = {it["appid"] for it in res["items"]}
        assert PROBE[0] not in ids  # 主账户拥有 → 隐藏
        assert PROBE[1] in ids      # 非主账户拥有（家庭共享徽章）→ 保留
        assert PROBE[2] in ids      # 无人拥有 → 保留

        # 不开筛选：三款全在（基线，确认过滤来自 hide_owned 而非种子问题）
        res = await games_service.list_games(limit=100, q="hide-owned-probe")
        assert {it["appid"] for it in res["items"]} == set(PROBE)
    finally:
        await _cleanup()
