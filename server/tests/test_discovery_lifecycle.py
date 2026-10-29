"""新游戏入库 + 移除监控测试。

议题一：popularcomingsoon 新源 + COMING_SOON 暂缓语义
- comingsoon 板参数对齐页面（filter=popularcomingsoon + os=win）；
- 反哺差集：uncrawled_only（在库不碰，含 COMING_SOON 暂缓行不重复反哺）；
- app_handler coming_soon 无包 → mark_coming_soon（type=COMING_SOON 脱池，
  不再走 mark_non_game_type 的 GAME 落库）。

议题二：下架监控（标记写入与排除层已退役，保留复活清标与反哺复活）
- 清标路径 clear_removed_mark：带历史标记的行清两列并归零计数；
- backfill_specs 复活语义：removed 行重新上榜 → 反哺重爬；
（连续 404 记击 → removed_at 落值与 36h 宽限排除层全仓无生产写入方，已删除。）
（_is_all_gone 属旧 appdetails 接口专属信号矩阵，browse 批量层整批失败不落
  逐 appid 状态，该守卫已结构性不需要，随旧接口废弃。）
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db
from app.crawler.db_writer import DbWriter
from app.domains.games import boards as boards_mod
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.wishlist.models import WishlistItem


@pytest_asyncio.fixture(autouse=True)
async def _setup():
    boards_mod._reset_cache_for_tests()
    await init_db()
    yield
    boards_mod._reset_cache_for_tests()


_PROBE_IDS = [990101, 990102, 990103, 990104, 990105]


async def _cleanup_probes():
    """幂等清理：测试播种行 + 价格行（多套件共跑防 UNIQUE 冲突/残留）。"""
    from sqlalchemy import delete

    async with get_session_factory()() as session:
        await session.execute(delete(Game).where(Game.appid.in_(_PROBE_IDS)))
        await session.execute(
            delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(_PROBE_IDS))
        )
        await session.execute(
            delete(WishlistItem).where(WishlistItem.appid.in_(_PROBE_IDS))
        )
        # 落池会经 monitoring 域登记来源，探针的监控行一并清理
        from app.domains.monitoring.models import (
            MonitorExclusion,
            MonitorSource,
            MonitorTarget,
        )

        for model in (MonitorSource, MonitorExclusion, MonitorTarget):
            await session.execute(
                delete(model).where(
                    model.target_type == "game", model.target_id.in_(_PROBE_IDS)
                )
            )
        await session.commit()


def _steam_page(appids):
    return {
        "items": [
            {"logo": f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{a}/logo.png"}
            for a in appids
        ]
    }


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, proxy=None):
        self.calls.append({"params": params})
        item = self.responses.pop(0) if self.responses else {"items": []}
        return _FakeResponse(item) if isinstance(item, dict) else item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _mock_fetch(monkeypatch, responses):
    session = _FakeSession(responses)

    class _SessionFactory:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    async def _fake_proxy():
        return None

    monkeypatch.setattr(boards_mod.aiohttp, "ClientSession", _SessionFactory)
    monkeypatch.setattr(boards_mod, "_strategy_proxy", _fake_proxy)
    return session


# ── 议题一：comingsoon 板 ────────────────────────────────────


@pytest.mark.asyncio
async def test_comingsoon_board_params(monkeypatch):
    """comingsoon：参数对齐商店页（filter=popularcomingsoon + os=win）。"""
    await _cleanup_probes()
    session = _mock_fetch(monkeypatch, [_steam_page(_PROBE_IDS[:2]), _steam_page([])])

    appids = await boards_mod.fetch_board("comingsoon")

    assert appids == _PROBE_IDS[:2]
    p = session.calls[0]["params"]
    assert p["filter"] == "popularcomingsoon"
    assert p["os"] == "win"


@pytest.mark.asyncio
async def test_comingsoon_backfill_skips_library_rows(monkeypatch):
    """comingsoon 反哺差集（uncrawled_only）：无行才补；COMING_SOON 暂缓行
    与已爬行一律不碰（转正走重探通道，防每日榜单重复反哺）。"""
    from sqlalchemy import delete

    await _cleanup_probes()
    try:
        _mock_fetch(monkeypatch, [_steam_page(_PROBE_IDS[:3])])
        async with get_session_factory()() as session:
            # 990102 = COMING_SOON 暂缓行（updated_at 有值 + type 标记）
            session.add(Game(appid=990102, name="AppID_990102", type="COMING_SOON",
                             updated_at=datetime.now()))
            # 990103 = 已爬行
            session.add(Game(appid=990103, name="crawled", type="GAME",
                             updated_at=datetime.now()))
            await session.commit()

        specs = await boards_mod.backfill_specs("comingsoon")

        appids = specs[0]["appids"] if specs else []
        assert appids == [990101]                       # 仅库内无行
        assert specs[0]["kind"] == "comingsoon_backfill"
    finally:
        await _cleanup_probes()


@pytest.mark.asyncio
async def test_mark_coming_soon_defers():
    """mark_coming_soon：暂缓行 type=COMING_SOON + updated_at 落值（脱回补池），
    名字保持挂名值；已有行不覆盖真实元数据。"""
    from sqlalchemy import delete

    await _cleanup_probes()
    db = DbWriter()
    try:
        await db.mark_coming_soon(990101)
        async with get_session_factory()() as session:
            row = await session.get(Game, 990101)
            assert row.type == "COMING_SOON"
            assert row.updated_at is not None
            assert row.name == "AppID_990101"
            assert row.removed_at is None      # 暂缓语义不携带下架标记
    finally:
        await _cleanup_probes()


# ── 议题二：下架监控 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_removed_mark_clear_cycle():
    """复活清标：带历史标记的行经 clear_removed_mark 清两列并归零计数。

    标记写入通道（连续 404 记击）已退役；标记行由测试直接构造，
    钉住清标路径对带标记行的行为（与 upsert 成功路径同一入口）。
    """
    from sqlalchemy import delete

    await _cleanup_probes()
    db = DbWriter()
    try:
        async with get_session_factory()() as session:
            session.add(Game(appid=990105, name="victim", type="GAME",
                             updated_at=datetime.now(),
                             removed_at=datetime.now(), removed_strikes=2))
            await session.commit()

        await db.clear_removed_mark(990105)
        async with get_session_factory()() as session:
            row = await session.get(Game, 990105)
            assert row.removed_at is None
            assert (row.removed_strikes or 0) == 0
    finally:
        await _cleanup_probes()


@pytest.mark.asyncio
async def test_backfill_resurrection_semantics(monkeypatch):
    """复活语义：removed_at 非空的在库游戏重新上榜 → 所有板统一反哺重爬。"""
    from sqlalchemy import delete

    await _cleanup_probes()
    try:
        _mock_fetch(monkeypatch, [_steam_page(_PROBE_IDS[:3])])
        async with get_session_factory()() as session:
            # 990103 = 已爬 + 判定下架 → specials（uncrawled_only）也必须补
            session.add(Game(appid=990103, name="removed", type="GAME",
                             updated_at=datetime.now(),
                             removed_at=datetime.now() - timedelta(hours=48)))
            await session.commit()

        specs = await boards_mod.backfill_specs("specials")

        appids = specs[0]["appids"] if specs else []
        assert 990103 in appids      # removed 复活反例：uncrawled_only 也放行
    finally:
        await _cleanup_probes()
