"""目录层随价格更新（catalog_refresh）测试。

- specs 组成随 KV `crawl.catalog_refresh` 变化（常驻：欠账 + 监控层 +
  特惠榜尾段；目录层仅开关打开时带上）；
- specials 尾段去重：特惠榜里与 pool/catalog 重合、下架、免费的对象
  全部剔除，只剩榜单独有差集；「Steam 榜单」源关闭时该段为空。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.core import scheduler as scheduler_mod
from app.core.database import init_db
from app.domains.games import boards as boards_mod
from app.domains.games.models import Game
from app.domains.monitoring.models import MonitorTarget


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


async def _seed_games() -> None:
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        session.add_all([
            Game(appid=100, name="Game100", type="game"),
            Game(appid=200, name="Game200", type="game"),
            Game(appid=400, name="Game400", type="game",
                 removed_at=datetime.now() - timedelta(hours=48)),
            Game(appid=500, name="Game500", type="game", free_kind="f2p"),
        ])
        session.add(MonitorTarget(
            target_type="game", target_id=300, state="active", priority=60,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_specials_scope_keeps_board_only_diff(monkeypatch):
    await _seed_games()
    # 榜单：200 在目录层（重合剔除）/ 300 在监控层（重合剔除）/
    # 400 已下架（过宽限期）、500 免费（业务状态剔除）/
    # 999 榜上出现两次（榜内去重保一）/ 888 榜单独有（保留）
    # → 尾段只剩 999、888
    async def _fake_board(key: str) -> list[int]:
        return [200, 300, 400, 500, 999, 888, 999]

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    from app.domains.crawl.service import _resolve_scope_appids

    pairs = await _resolve_scope_appids("specials", None)
    assert pairs == [(999, ""), (888, "")]


@pytest.mark.asyncio
async def test_specials_scope_empty_when_boards_switch_off(monkeypatch):
    await _seed_games()
    from app.domains.settings import service as settings_service

    await settings_service.set_value("fetch.boards", False)
    from app.domains.crawl.service import _resolve_scope_appids

    assert await _resolve_scope_appids("specials", None) == []


@pytest.mark.asyncio
async def test_price_refresh_specs_gate_catalog_segment():
    from app.domains.settings import service as settings_service

    tail = {"scope": "specials", "kind": "specials_backfill"}
    base = [{"kind": "missing"}, {"scope": "pool"}]

    assert await scheduler_mod._price_refresh_specs() == base + [tail]

    await settings_service.set_value("crawl.catalog_refresh", True)
    assert await scheduler_mod._price_refresh_specs() == (
        base + [{"scope": "catalog"}, tail]
    )

    await settings_service.set_value("crawl.catalog_refresh", False)
    assert await scheduler_mod._price_refresh_specs() == base + [tail]
