"""games 列表「关注和愿望单优先」排序语义（高级筛选面板勾选项）。

口径：关注（monitoring favorite 来源）恒置顶；wishlistPriority 开启后，
愿望单成员（wishlist_items.wishlisted 行，含家庭愿望单派生源）在关注之后
叠加置顶前缀，压过常规排序。未开启时排序与不开关完全一致。

合成 99xxxxxx 探针行（真实库只读前提 + 幂等清理），全程不触网。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.games.router import router as games_router
from app.domains.monitoring.models import TARGET_GAME, MonitorSource
from app.domains.wishlist.models import WishlistItem

PROBE = [99700201, 99700202, 99700203, 99700204]
PRIMARY = "76561199000000001"
OTHER = "76561199000000002"
Q = "wishlist-prio-probe"


async def _seed() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE), GameCurrentPrice.region_code == "CN"
            )
        )
        await s.execute(
            delete(WishlistItem).where(
                WishlistItem.steamid.in_([PRIMARY, OTHER]),
                WishlistItem.appid.in_(PROBE),
            )
        )
        await s.execute(
            delete(MonitorSource).where(
                MonitorSource.target_type == TARGET_GAME,
                MonitorSource.target_id.in_(PROBE),
                MonitorSource.source == "favorite",
            )
        )
        # positive_rate 递增决定常规排序（complex_sort 末位）的确定性：
        # A(关注) < B(愿望单) < C(无标记) < D(家庭愿望单)
        for i, appid in enumerate(PROBE):
            s.add(Game(appid=appid, name=f"{Q}-{i}", positive_rate=1000 * (i + 1)))
            s.add(GameCurrentPrice(
                appid=appid, region_code="CN", price_status="ok",
                currency="CNY", price=10000 + i, original_price=10000 + i,
                discount_percent=0, cny_fen=10000 + i,
            ))
        # A = 关注（游戏卡星标）；B = 主账户愿望单；D = 其他追踪账户愿望单（家庭愿望单）
        s.add(MonitorSource(
            target_type=TARGET_GAME, target_id=PROBE[0], source="favorite", active=True,
        ))
        s.add(WishlistItem(steamid=PRIMARY, appid=PROBE[1], wishlisted=True, active=True))
        s.add(WishlistItem(steamid=OTHER, appid=PROBE[3], wishlisted=True, active=True))
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
            delete(WishlistItem).where(
                WishlistItem.steamid.in_([PRIMARY, OTHER]),
                WishlistItem.appid.in_(PROBE),
            )
        )
        await s.execute(
            delete(MonitorSource).where(
                MonitorSource.target_type == TARGET_GAME,
                MonitorSource.target_id.in_(PROBE),
                MonitorSource.source == "favorite",
            )
        )
        await s.commit()


def _ids(res: dict) -> list[int]:
    return [it["appid"] for it in res["items"]]


@pytest.mark.asyncio
async def test_wishlist_priority_reorders():
    """开启后愿望单成员越过常规排序靠前；关闭时保持关注置顶 + 常规排序。"""
    await _seed()
    try:
        kw = {"q": Q, "limit": 100}
        # 关闭：关注 A 置顶，其余按 complex_sort 的 positive_rate 降序 D > C > B
        assert _ids(await games_service.list_games(**kw)) == [PROBE[0], PROBE[3], PROBE[2], PROBE[1]]
        # 开启：愿望单成员 {B, D} 叠加置顶（组内仍按 positive_rate 降序 D > B），C 落后
        assert _ids(await games_service.list_games(**kw, wishlist_priority=True)) == [
            PROBE[0], PROBE[3], PROBE[1], PROBE[2],
        ]
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_wishlist_priority_top100_branch(monkeypatch):
    """top100 分支：开启后愿望单成员压过榜序，关闭时保持纯榜序。"""
    from app.domains.games import boards as boards_mod

    board = [PROBE[2], PROBE[1]]  # 榜序 C > B

    async def fake_board(key: str) -> list[int]:
        return board

    monkeypatch.setattr(boards_mod, "get_board", fake_board)
    await _seed()
    try:
        kw = {"q": Q, "limit": 100}
        # 榜集 IN 过滤：只有榜内 C/B 出现；关闭时纯榜序
        assert _ids(await games_service.list_games(sort="top100", **kw)) == [PROBE[2], PROBE[1]]
        # 开启：B（愿望单）压过榜序靠前
        assert _ids(await games_service.list_games(sort="top100", **kw, wishlist_priority=True)) == [
            PROBE[1], PROBE[2],
        ]
    finally:
        await _cleanup()


def test_router_passes_wishlist_priority(monkeypatch):
    """HTTP 层：wishlistPriority 查询参数透传到 service（缺省 False）。"""
    captured: dict = {}

    async def _stub(**kw):
        captured.update(kw)
        return {"items": [], "total": 0, "hasMore": False, "nextCursor": None}

    monkeypatch.setattr(games_service, "list_games", _stub)
    app = FastAPI()
    app.include_router(games_router, prefix="/api/v1")
    client = TestClient(app)

    r = client.get("/api/v1/games", params={"wishlistPriority": "true", "q": "x"})
    assert r.status_code == 200, r.text
    assert captured["wishlist_priority"] is True

    client.get("/api/v1/games")
    assert captured["wishlist_priority"] is False
