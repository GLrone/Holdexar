"""目录层随价格更新（catalog_refresh）测试。

- specs 组成随 KV `crawl.catalog_refresh` 变化（常驻：欠账 + 监控层；
  目录层与特惠榜尾段仅开关打开时带上；默认随策略——代理形态开、
  直连形态关，显式设置过的值优先）；
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
    from app.domains.crawl.service import resolve_scope_appids

    pairs = await resolve_scope_appids("specials", None)
    assert pairs == [(999, ""), (888, "")]


@pytest.mark.asyncio
async def test_specials_scope_empty_when_boards_switch_off(monkeypatch):
    await _seed_games()
    from app.domains.settings import service as settings_service

    await settings_service.set_value("fetch.boards", False)
    from app.domains.crawl.service import resolve_scope_appids

    assert await resolve_scope_appids("specials", None) == []


@pytest.mark.asyncio
async def test_discounts_scope_keeps_catalog_games(monkeypatch):
    """折扣队列段：榜单全量作爬取对象——不减目录集（直连下目录段默认关，
    榜内在库游戏由本段刷新价格）；只减监控层与业务排除。"""
    await _seed_games()

    async def _fake_board(key: str) -> list[int]:
        return [200, 300, 400, 500, 999]

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    from app.domains.crawl.service import resolve_scope_appids

    pairs = await resolve_scope_appids("discounts", None)
    # 200 目录层保留（与 specials 差集段的关键差异）/ 300 监控层剔除 /
    # 400 已下架、500 免费（业务状态剔除）/ 999 新面孔保留
    assert pairs == [(200, ""), (999, "")]


@pytest.mark.asyncio
async def test_discounts_scope_empty_when_boards_switch_off(monkeypatch):
    await _seed_games()
    from app.domains.settings import service as settings_service

    await settings_service.set_value("fetch.boards", False)
    from app.domains.crawl.service import resolve_scope_appids

    assert await resolve_scope_appids("discounts", None) == []


@pytest.mark.asyncio
async def test_price_refresh_specs_gate_catalog_segment():
    from app.domains.settings import service as settings_service

    tail = {"scope": "specials", "kind": "specials_backfill"}
    base = [{"kind": "missing"}, {"scope": "pool"}]

    # 代理形态（显式选择）：默认全量——目录层 + 特惠榜差值段随默认开关打开
    await settings_service.set_value("proxy.strategy", "proxy_first")
    assert await scheduler_mod._price_refresh_specs() == (
        base + [{"scope": "catalog"}, tail]
    )

    await settings_service.set_value("crawl.catalog_refresh", True)
    assert await scheduler_mod._price_refresh_specs() == (
        base + [{"scope": "catalog"}, tail]
    )

    # 只盯特定游戏：目录层与特惠榜差值段一并停
    await settings_service.set_value("crawl.catalog_refresh", False)
    assert await scheduler_mod._price_refresh_specs() == base


@pytest.mark.asyncio
async def test_direct_only_defaults_catalog_segments_off():
    """直连形态：目录/特惠榜段默认不随轮（单出口预算留给监控层），改带
    折扣队列段（search specials 全量作爬取对象）；显式开过目录层则两种
    形态都尊重（折扣段与目录段并存）。"""
    from app.domains.settings import service as settings_service

    tail = {"scope": "specials", "kind": "specials_backfill"}
    discounts = {"scope": "discounts", "kind": "discounts"}
    base = [{"kind": "missing"}, {"scope": "pool"}]

    await settings_service.set_value("proxy.strategy", "direct_only")
    assert await scheduler_mod._price_refresh_specs() == base + [discounts]

    # 显式打开 = 用户明确要目录层，直连形态同样执行（折扣段与目录段并存）
    await settings_service.set_value("crawl.catalog_refresh", True)
    assert await scheduler_mod._price_refresh_specs() == (
        base + [discounts, {"scope": "catalog"}, tail]
    )

    # 代理形态未显式设置时仍默认开（无折扣段——目录层覆盖大盘子）
    await settings_service.set_value("proxy.strategy", "proxy_first")
    await settings_service.delete_value("crawl.catalog_refresh")
    assert await scheduler_mod._price_refresh_specs() == (
        base + [{"scope": "catalog"}, tail]
    )


@pytest.mark.asyncio
async def test_followed_games_ride_pool_regardless_of_discount_board(monkeypatch):
    """星标关注始终随轮：pool 是两形态常驻段，不看折扣榜——不在折扣列表的
    关注游戏照常进队列（直连下自动更新范围 = 关注层 + 折扣面的边界保证）。"""
    await _seed_games()  # 300 = 监控层在册（favorite=第一优先级组，星级关注同语义）
    from app.domains.crawl.service import default_queue_specs, resolve_scope_appids

    # 榜单故意不含 300：关注对象不依赖折扣榜
    async def _fake_board(key: str) -> list[int]:
        return [999]

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)

    specs = await default_queue_specs()
    assert {"scope": "pool"} in specs  # 直连/代理两形态常驻

    pairs = await resolve_scope_appids("pool", None)
    assert (300, "") in pairs  # 在册对象照爬，与榜单无关


@pytest.mark.asyncio
async def test_fetch_settings_read_shows_effective_direct_default():
    """设置读口与队列组成同一有效默认：直连形态未设置时开关如实显示关。"""
    from app.domains.settings import service as settings_service
    from app.domains.settings.router import get_fetch_settings

    await settings_service.set_value("proxy.strategy", "direct_only")
    assert (await get_fetch_settings()).catalog_refresh is False

    await settings_service.set_value("crawl.catalog_refresh", True)
    assert (await get_fetch_settings()).catalog_refresh is True


@pytest.mark.asyncio
async def test_manual_full_queue_shares_one_composition(monkeypatch):
    """任务页「全部」档与自动价格轮同源：组成逐段一致，仅无显式 kind 的
    scope 段记 manual（missing / specials_backfill 是通道/段身份标签）；
    受理响应带各段预解析款数（missing 段计 0/1 欠账存在性）。"""
    from app.domains.crawl import service as crawl_service
    from app.domains.settings import service as settings_service

    # 代理形态（显式选择）下的全量组成：missing+pool+catalog+specials
    await settings_service.set_value("proxy.strategy", "proxy_first")
    await _seed_games()

    async def _fake_board(key: str) -> list[int]:
        return []

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)

    scheduled = await scheduler_mod._price_refresh_specs()
    composed = await crawl_service.default_queue_specs()
    assert composed == scheduled

    captured: list[list[dict]] = []

    async def _fake_run_sequential(specs, **kwargs):
        captured.append(specs)
        return []

    import app.crawler.occupancy as occupancy_mod

    monkeypatch.setattr(crawl_service, "run_sequential", _fake_run_sequential)
    monkeypatch.setattr(occupancy_mod, "crawler_busy", lambda: False)

    ack = await crawl_service.start_full_queue()
    assert ack["queued"] is True and ack["scope"] == "all"
    assert ack["segments"] == [
        {"name": "missing", "count": 0},
        {"name": "pool", "count": 1},
        {"name": "catalog", "count": 2},
        {"name": "specials", "count": 0},
    ]
    assert ack["total"] == 3
    assert captured == []

    # 链在跑时重入被挡（防段间隙双开链）
    with pytest.raises(RuntimeError):
        await crawl_service.start_full_queue()

    await crawl_service._queue_chain_task  # 等后台链收尾，防泄漏到别的用例
    assert captured == [[
        {"kind": "missing"},
        {"scope": "pool", "kind": "manual"},
        {"scope": "catalog", "kind": "manual"},
        {"scope": "specials", "kind": "specials_backfill"},
    ]]


@pytest.mark.asyncio
async def test_manual_full_queue_empty_rejected(monkeypatch):
    """关注与游戏库全空：受理前直接拒绝（用户语言 400），不留死胡同。"""
    from app.domains.crawl import service as crawl_service

    async def _fake_board(key: str) -> list[int]:
        return []

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    import app.crawler.occupancy as occupancy_mod

    monkeypatch.setattr(occupancy_mod, "crawler_busy", lambda: False)
    with pytest.raises(ValueError):
        await crawl_service.start_full_queue()


@pytest.mark.asyncio
async def test_manual_full_queue_blocked_when_busy(monkeypatch):
    """单任务模型：爬虫占用（含 bundles 链尾直调）时全队列启动被挡。"""
    from app.domains.crawl import service as crawl_service

    import app.crawler.occupancy as occupancy_mod

    monkeypatch.setattr(occupancy_mod, "crawler_busy", lambda: True)
    with pytest.raises(RuntimeError):
        await crawl_service.start_full_queue()


@pytest.mark.asyncio
async def test_catalog_scope_excludes_non_crawl_states():
    """catalog 减集 = monitor_targets 全部在册对象：released / excluded
    按状态定义不进任何 crawl 段——games 行保留，但目录维护轮不再重爬。"""
    await _seed_games()
    from app.core.database import get_session_factory
    from app.domains.crawl.service import resolve_scope_appids

    async with get_session_factory()() as session:
        session.add(Game(appid=600, name="Game600", type="game"))
        session.add_all([
            MonitorTarget(target_type="game", target_id=100, state="released"),
            MonitorTarget(target_type="game", target_id=200, state="excluded"),
        ])
        await session.commit()

    catalog = [a for a, _ in await resolve_scope_appids("catalog", None)]
    assert catalog == [600]
    # pool 段口径不变：仍然只有 active（released/excluded 不在其列）
    pool = [a for a, _ in await resolve_scope_appids("pool", None)]
    assert pool == [300]


@pytest.mark.asyncio
async def test_plan_and_resolve_share_one_exit():
    """冻结口（plan_scope_appids）与执行口（start_job）必须同一解析：
    各 scope 的 id 序列逐位一致；显式 appids 原样保序直通。"""
    await _seed_games()
    from app.domains.crawl.service import plan_scope_appids, resolve_scope_appids

    for scope in ("wishlist", "wishlist_only", "owned", "pool", "catalog", "specials"):
        planned = await plan_scope_appids(scope, None)
        resolved = [a for a, _ in await resolve_scope_appids(scope, None)]
        assert planned == resolved, scope
    assert await plan_scope_appids("appids", [42, 7]) == [42, 7]
    assert [a for a, _ in await resolve_scope_appids("appids", [42, 7])] == [42, 7]
