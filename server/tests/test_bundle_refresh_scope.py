"""捆绑包刷新候选集测试：默认只刷监控层在册的包。

- 候选集 = monitoring target_type=bundle 的 active 对象（favorite / import
  都是显式用户意图）；无监控记录的包（含发现桩）不进刷新；
- 无关注包时整轮跳过（ok 空结果，不发请求）。

tmp 库隔离，网络层（区域抓取 / 汇率 / appid 联动）全部打桩。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.core.database import init_db
from app.domains.bundles import refresh as bundles_refresh
from app.domains.games.models import Bundle, BundleRegionPrice
from app.domains.monitoring import service as monitoring_service
from app.domains.monitoring.models import MonitorExclusion, MonitorSource, MonitorTarget

BID_A = 99710401  # 星标关注
BID_B = 99710402  # 手动导入
BID_C = 99710403  # 无监控记录（发现桩形态）
APPIDS = [99710411, 99710412]


@pytest_asyncio.fixture(autouse=True)
async def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    database_module.get_settings.cache_clear()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()
    await init_db()
    yield
    database_module.get_settings.cache_clear()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()


async def _seed_bundles() -> None:
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(delete(Bundle).where(Bundle.bundle_id.in_([BID_A, BID_B, BID_C])))
        await session.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id.in_([BID_A, BID_B, BID_C])
            )
        )
        for i, bid in enumerate([BID_A, BID_B, BID_C]):
            session.add(Bundle(bundle_id=bid, name=f"bundle-scope-probe-{i}", app_ids=APPIDS))
        await session.commit()


async def _cleanup() -> None:
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(delete(Bundle).where(Bundle.bundle_id.in_([BID_A, BID_B, BID_C])))
        await session.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id.in_([BID_A, BID_B, BID_C])
            )
        )
        for table in (MonitorTarget, MonitorSource, MonitorExclusion):
            await session.execute(
                delete(table).where(
                    table.target_type == "bundle",
                    table.target_id.in_([BID_A, BID_B, BID_C]),
                )
            )
        await session.commit()


def _fake_region_rows(bid: int) -> list[dict]:
    return [
        {
            "bundle_id": bid,
            "region_code": "cn",
            "price": 10000,
            "original_price": 10000,
            "currency": "CNY",
            "discount_percent": 0,
            "bundle_base_discount": 0,
            "discount_end_ts": None,
            "app_ids": list(APPIDS),
            "price_status": "ok",
            "name": f"bundle-scope-probe-{bid}",
            "header_image": "",
            "mps": 0,
            "item_kind": 0,
            "kind": "bundleid",
        }
    ]


@pytest.mark.asyncio
async def test_refresh_only_covers_watched_bundles(monkeypatch):
    """候选集 = 监控层在册（关注/导入）；无记录的包不进刷新。"""
    await _seed_bundles()
    try:
        await monitoring_service.track("bundle", BID_A, "favorite")
        await monitoring_service.track("bundle", BID_B, "import")

        fetched: list[list[tuple[int, object]]] = []

        async def _fake_fetch(session, want, ccs=None):
            fetched.append([(int(bid), kind) for bid, kind in want])
            return {int(bid): _fake_region_rows(int(bid)) for bid, _kind in want}

        async def _fake_ccs() -> list[str]:
            return ["cn"]

        async def _fake_rates():
            return {"CNY": 1.0}

        async def _fake_enqueue() -> tuple[int, int]:
            return 0, 0

        async def _fake_sort_cache(_ids):
            return None

        monkeypatch.setattr(bundles_refresh, "_fetch_regions_batched", _fake_fetch)
        monkeypatch.setattr(bundles_refresh, "_bundle_fetch_ccs", _fake_ccs)
        monkeypatch.setattr(bundles_refresh, "get_rates", _fake_rates)
        monkeypatch.setattr(bundles_refresh, "_enqueue_new_bundle_apps", _fake_enqueue)
        monkeypatch.setattr(bundles_refresh, "refresh_bundle_sort_cache", _fake_sort_cache)

        result = await bundles_refresh.refresh_bundles()

        assert len(fetched) == 1
        want_ids = {bid for bid, _kind in fetched[0]}
        assert want_ids == {BID_A, BID_B}
        assert result["ok"] is True
        assert result["total"] == 2
        assert result["updated"] == 2
        assert BID_C not in want_ids
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_refresh_skips_round_without_watched_bundles(monkeypatch):
    """无关注包：整轮跳过，不发任何请求。"""
    await _seed_bundles()
    try:
        async def _boom(*args, **kwargs):
            raise AssertionError("无关注包时不应发起抓取")

        async def _fake_rates():
            return {"CNY": 1.0}

        monkeypatch.setattr(bundles_refresh, "_fetch_regions_batched", _boom)
        monkeypatch.setattr(bundles_refresh, "_bundle_fetch_ccs", _boom)
        monkeypatch.setattr(bundles_refresh, "get_rates", _fake_rates)
        monkeypatch.setattr(bundles_refresh, "_enqueue_new_bundle_apps", _boom)

        result = await bundles_refresh.refresh_bundles()

        assert result == {
            "ok": True, "updated": 0, "total": 0, "regionPrices": 0, "failed": [],
        }
    finally:
        await _cleanup()
