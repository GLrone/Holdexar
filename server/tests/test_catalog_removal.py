"""目录移除（假删除）语义：商店列表隐藏 / 已移除视图 / 监控联动 / 恢复。

- remove_games：catalog_removals 落行 + 挂监控排除（catalog_removed）；
  列表默认隐藏、removed=True 只出已移除款；
- restore_games：删行即回到商店；只解除 reason=catalog_removed 的排除，
  用户显式设置的排除语义保留。

合成 99xxxx 探针行（真实库只读前提 + 幂等清理），全程不触网。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.games import service as games_service
from app.domains.games.models import CatalogRemoval, Game, GameCurrentPrice
from app.domains.monitoring import service as monitoring_service

PROBE = [99700301, 99700302]
ABSENT = 99700399  # 目录中不存在的 appid（missing 计数用）


async def _seed() -> None:
    from app.core.database import get_engine

    async with get_engine().begin() as conn:
        await conn.run_sync(
            lambda sc: CatalogRemoval.__table__.create(sc, checkfirst=True)
        )
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(delete(CatalogRemoval).where(CatalogRemoval.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE), GameCurrentPrice.region_code == "CN"
            )
        )
        for i, appid in enumerate(PROBE):
            s.add(Game(appid=appid, name=f"catalog-removal-probe-{i}", min_cny_fen=10000 + i))
            s.add(GameCurrentPrice(
                appid=appid, region_code="CN", price_status="ok",
                currency="CNY", price=10000 + i, original_price=10000 + i,
                discount_percent=0, cny_fen=10000 + i,
            ))
        await s.commit()


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(delete(CatalogRemoval).where(CatalogRemoval.appid.in_(PROBE)))
        await s.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(PROBE), GameCurrentPrice.region_code == "CN"
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_remove_hides_from_list_and_removed_view_shows():
    """移除后默认列表隐藏、total 同步收缩；removed=True 只出已移除款。"""
    await _seed()
    try:
        res = await games_service.remove_games(PROBE)
        assert res == {"removed": 2, "missing": 0}

        default = await games_service.list_games(limit=100, q="catalog-removal-probe")
        assert {it["appid"] for it in default["items"]} == set()
        assert default["total"] == 0

        removed_view = await games_service.list_games(
            limit=100, q="catalog-removal-probe", removed=True
        )
        assert {it["appid"] for it in removed_view["items"]} == set(PROBE)
        assert removed_view["total"] == 2
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_remove_sets_exclusion_and_restore_clears():
    """移除挂 catalog_removed 排除；恢复删行 + 解除该排除，列表随之恢复。"""
    await _seed()
    try:
        await games_service.remove_games(PROBE)
        assert await monitoring_service.is_excluded("game", PROBE[0]) is True
        assert (
            await monitoring_service.exclusion_reason("game", PROBE[0])
            == games_service.REMOVAL_EXCLUSION_REASON
        )

        res = await games_service.restore_games(PROBE)
        assert res == {"restored": 2, "missing": 0}
        assert await monitoring_service.is_excluded("game", PROBE[0]) is False

        default = await games_service.list_games(limit=100, q="catalog-removal-probe")
        assert {it["appid"] for it in default["items"]} == set(PROBE)
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_restore_preserves_user_exclusion():
    """移除后用户又显式排除（改写 reason）：恢复只回商店，不洗掉该排除。"""
    await _seed()
    try:
        await games_service.remove_games([PROBE[0]])
        await monitoring_service.set_exclusion("game", PROBE[0], True, "user_choice")

        res = await games_service.restore_games([PROBE[0]])
        assert res == {"restored": 1, "missing": 0}
        assert await monitoring_service.is_excluded("game", PROBE[0]) is True
        assert await monitoring_service.exclusion_reason("game", PROBE[0]) == "user_choice"

        default = await games_service.list_games(limit=100, q="catalog-removal-probe")
        assert PROBE[0] in {it["appid"] for it in default["items"]}
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_remove_idempotent_and_counts_missing():
    """重复移除幂等（保留首次）；目录外 appid 计入 missing 不落行。"""
    await _seed()
    try:
        first = await games_service.remove_games([PROBE[0], ABSENT])
        assert first == {"removed": 1, "missing": 1}

        again = await games_service.remove_games([PROBE[0], PROBE[1]])
        assert again == {"removed": 2, "missing": 0}

        async with get_session_factory()() as s:
            rows = (
                (await s.execute(select(CatalogRemoval.appid).where(
                    CatalogRemoval.appid.in_(PROBE))))
                .scalars().all()
            )
            assert set(rows) == set(PROBE)
    finally:
        await _cleanup()
