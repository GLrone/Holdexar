"""dataview 工具（游戏目录/价格/历史/捆绑包/汇率/区服）真实 handler 验收。

直接 executor.execute 调真实 handler（隔离库）：空对象 → empty 不抛异常；
regions_config / rates_history / 播种后的 ok 路径各验一例。"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import get_session_factory, init_db
from app.domains.agent.runtime import executor
from app.domains.agent.tools.builtin import dataview

APPID = 991_001  # 合成 appid，避开真实库
MISSING_APPID = 991_099
MISSING_BUNDLE = 991_099
STANDARD_SUB = 991_00101

_SPECS = {s.name: s for s in dataview.SPECS}


def _run(name: str, args: dict):
    return asyncio.run(executor.execute(_SPECS[name], args))


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    yield


def test_specs_all_read_low_with_pinned_labels():
    assert [s.name for s in dataview.SPECS] == [
        "game_detail", "game_price_history", "game_versions",
        "linked_bundles", "bundle_detail", "rates_history", "regions_config",
    ]
    for spec in dataview.SPECS:
        assert spec.group == "read" and spec.risk == "low", spec.name


@pytest.mark.parametrize("name", [
    "game_detail", "game_price_history", "game_versions", "linked_bundles",
])
def test_missing_game_returns_empty(name):
    result = _run(name, {"appid": MISSING_APPID})
    assert result.status == "empty"
    assert result.data["kind"] == "empty"


def test_non_positive_appid_returns_empty():
    assert _run("game_detail", {"appid": 0}).status == "empty"


def test_missing_bundle_returns_empty():
    result = _run("bundle_detail", {"bundle_id": MISSING_BUNDLE})
    assert result.status == "empty"
    assert result.data["note"] == "no_bundle"


def test_regions_config_ok_contains_cn():
    result = _run("regions_config", {})
    assert result.status == "ok"
    regions = result.data["regions"]
    cn = next(r for r in regions if r["code"] == "cn")
    assert cn["enabled"] is True
    assert result.data["enabledCount"] >= 1
    assert set(cn) == {"code", "name", "currency", "enabled"}


def test_rates_history_empty_db_shape():
    result = _run("rates_history", {})
    assert result.status in ("ok", "empty")
    if result.status == "ok":
        assert result.data["currency"] == "USD"
        assert result.data["points"]


@pytest_asyncio.fixture
async def _seeded_game():
    from app.domains.games.models import Game, GamePriceHistory

    def _h(price: int, at: datetime) -> GamePriceHistory:
        return GamePriceHistory(
            appid=APPID, region_code="CN", currency="CNY",
            price=price, original_price=price, discount_percent=0,
            sub_id=STANDARD_SUB, is_gold=False, version_suffix=None,
            price_status="ok", cny_fen=price, snapshot_at=at,
        )

    now = datetime.now()
    async with get_session_factory()() as session:
        session.add(Game(appid=APPID, name="验收专用游戏", review_count=100))
        session.add_all([
            _h(10000, now - timedelta(days=100)),
            _h(8000, now - timedelta(days=50)),
            _h(9000, now - timedelta(days=10)),
        ])
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(delete(GamePriceHistory).where(GamePriceHistory.appid == APPID))
        await session.execute(delete(Game).where(Game.appid == APPID))
        await session.commit()


@pytest.mark.usefixtures("_seeded_game")
def test_seeded_detail_history_versions_ok():
    detail = _run("game_detail", {"appid": APPID})
    assert detail.status == "ok"
    assert detail.data["name"] == "验收专用游戏"
    assert detail.data["tracked"] is False
    assert "priceMatrix" not in detail.data  # 大对象已瘦身

    history = _run("game_price_history", {"appid": APPID})
    assert history.status == "ok"
    assert history.data["lowest"]["cnyFen"] == 8000
    assert [p[1] for p in history.data["points"]] == [10000, 8000, 9000]

    versions = _run("game_versions", {"appid": APPID})
    assert versions.status == "ok"
    cn = versions.data["versions"][0]["regions"]["CN"]
    assert cn["cnyFen"] == 9000 and cn["discount"] == 0
