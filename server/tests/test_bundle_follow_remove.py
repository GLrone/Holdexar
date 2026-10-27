"""捆绑包关注 / 移除语义：monitoring bundle 目标的用户面动作。

- remove_bundle：挂 bundle_removed 排除 → blocked_ids 命中、列表隐藏、
  removed=True 只出被移除包；星标一并清掉；
- restore_bundle：只解除本链路所挂的排除，用户显式设置的排除保留；
- follow_bundle / unfollow_bundle：favorite 来源增删，列表关注置顶。

合成 99xxxx 探针行（真实库只读前提 + 幂等清理），全程不触网。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory
from app.domains.bundles import service as bundles_service
from app.domains.games.models import Bundle, BundleRegionPrice
from app.domains.monitoring import service as monitoring_service
from app.domains.monitoring.models import MonitorExclusion, MonitorSource, MonitorTarget

BID_A = 99700401
BID_B = 99700402
BID_C = 99700403
APPIDS = [99700411, 99700412]


@pytest_asyncio.fixture(autouse=True)
async def isolated_database(tmp_path, monkeypatch):
    from app.core import database
    from app.core.config import get_settings

    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    database.get_engine.cache_clear()
    get_session_factory.cache_clear()
    await database.init_db()
    yield
    await database.get_engine().dispose()


async def _seed() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Bundle).where(Bundle.bundle_id.in_([BID_A, BID_B, BID_C])))
        await s.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id.in_([BID_A, BID_B, BID_C])
            )
        )
        for i, bid in enumerate([BID_A, BID_B, BID_C]):
            s.add(Bundle(bundle_id=bid, name=f"bundle-mark-probe-{i}", app_ids=APPIDS))
            s.add(BundleRegionPrice(
                bundle_id=bid, region_code="CN", currency="CNY",
                price=10000 + i, original_price=10000 + i, discount_percent=0,
                price_status="ok", cny_fen=10000 + i, app_ids=APPIDS,
            ))
        await s.commit()


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Bundle).where(Bundle.bundle_id.in_([BID_A, BID_B, BID_C])))
        await s.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id.in_([BID_A, BID_B, BID_C])
            )
        )
        for table in (MonitorTarget, MonitorSource, MonitorExclusion):
            await s.execute(
                delete(table).where(
                    table.target_type == "bundle",
                    table.target_id.in_([BID_A, BID_B, BID_C]),
                )
            )
        await s.commit()


def _ids(items: dict) -> set[int]:
    return {i["bundleId"] for i in items["bundles"]}


@pytest.mark.asyncio
async def test_remove_hides_from_list_and_removed_view_shows():
    """移除后默认列表隐藏、removed 视图独占；刷新候选集同步排除。"""
    await _seed()
    try:
        res = await bundles_service.remove_bundle(BID_A)
        assert res == {"removed": True, "bundleId": BID_A}
        assert BID_A in await monitoring_service.blocked_ids("bundle")

        default = _ids(await bundles_service.list_bundles("diff"))
        assert BID_A not in default
        assert {BID_B, BID_C} <= default

        removed_view = _ids(await bundles_service.list_bundles("diff", removed=True))
        assert BID_A in removed_view
        assert BID_B not in removed_view
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_remove_clears_star_and_restore_brings_back():
    """移除清星标；恢复删排除后回到列表，星标不自动重挂。"""
    await _seed()
    try:
        await bundles_service.follow_bundle(BID_A)
        await bundles_service.remove_bundle(BID_A)
        assert BID_A not in await bundles_service.followed_bundle_ids()

        res = await bundles_service.restore_bundle(BID_A)
        assert res == {"restored": True, "bundleId": BID_A}
        assert BID_A not in await monitoring_service.blocked_ids("bundle")
        assert BID_A in _ids(await bundles_service.list_bundles("diff"))
        assert BID_A not in await bundles_service.followed_bundle_ids()
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_restore_preserves_user_exclusion():
    """移除后用户又显式排除（改写 reason）：恢复不动它，包仍隐藏。"""
    await _seed()
    try:
        await bundles_service.remove_bundle(BID_A)
        await monitoring_service.set_exclusion("bundle", BID_A, True, "user_choice")

        res = await bundles_service.restore_bundle(BID_A)
        assert res == {"restored": False, "bundleId": BID_A}
        assert await monitoring_service.exclusion_reason("bundle", BID_A) == "user_choice"
        assert BID_A in await monitoring_service.blocked_ids("bundle")
        assert BID_A not in _ids(await bundles_service.list_bundles("diff"))
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_follow_pins_to_top_and_unfollow_releases():
    """关注置顶（组内保持原序）；取消关注回到原位。"""
    await _seed()
    try:
        base = [i["bundleId"] for i in (await bundles_service.list_bundles("diff"))["bundles"]]
        assert base.index(BID_A) < base.index(BID_B)

        await bundles_service.follow_bundle(BID_B)
        pinned = [i["bundleId"] for i in (await bundles_service.list_bundles("diff"))["bundles"]]
        assert pinned[0] == BID_B
        # 组内相对序保持：A 与 C 的先后不变
        assert pinned.index(BID_A) < pinned.index(BID_C)
        assert BID_B in await bundles_service.followed_bundle_ids()

        await bundles_service.unfollow_bundle(BID_B)
        released = [i["bundleId"] for i in (await bundles_service.list_bundles("diff"))["bundles"]]
        assert released == base
    finally:
        await _cleanup()
