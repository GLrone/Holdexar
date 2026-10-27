"""监控生命周期测试：Catalog / Tracking / Crawl Queue 三层分离。

覆盖验收清单：
1. 家族愿望单 → Monitoring（第一优先级保持）
2. 多来源共存：愿望单删掉后仍有 manual → 继续监控
3. 最后一个来源消失 → released
4. 排除：来源仍在也不进 Monitoring / 不进 crawl
5. 解除排除 → 有来源即自动恢复
6. 纯 Catalog（games 有行、无来源）→ 不进 Monitoring
7. Bundle 走同一套生命周期（active / excluded / released / 重新激活）
8. 多账户同 AppID 只产生一个 Monitoring Target
9. crawl scope：pool 只取监控层，catalog 取目录层减去监控层
10. 永久免费是业务状态：脱池后是 released，不是 excluded
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.core.database import Base
from app.domains.crawl import service as crawl_service
from app.domains.games import preset as preset_mod
from app.domains.games.models import Bundle, Game
from app.domains.monitoring import service as monitoring
from app.domains.monitoring.models import (
    MonitorExclusion,
    MonitorSource,
    MonitorTarget,
)
from app.domains.wishlist import service as wishlist_service
from app.domains.wishlist.models import TrackedAccount, WishlistItem

PRIMARY = "76561198000000001"
SECOND = "76561198000000002"


@pytest.fixture
def db(monkeypatch):
    from app.core.database import get_session_factory


    async def _no_start(**kwargs):
        return {"id": 1, "scope": kwargs.get("scope"), "count": 0}

    monkeypatch.setattr(crawl_service, "start_job", _no_start)

    return get_session_factory()
async def _account(db):
    async with db() as session:
        if await session.get(TrackedAccount, PRIMARY) is None:
            session.add(TrackedAccount(steamid=PRIMARY, label="主号"))
            await session.commit()


async def _wl(db, appid, steamid=PRIMARY, *, active=True, **flags):
    async with db() as session:
        row = await session.get(WishlistItem, (steamid, appid))
        if row is None:
            row = WishlistItem(
                steamid=steamid, appid=appid, added_at=datetime.now(), active=active, **flags
            )
            session.add(row)
        else:
            row.active = active
            for k, v in flags.items():
                setattr(row, k, v)
        await session.commit()


async def _game(db, appid, **kw):
    async with db() as session:
        session.add(Game(appid=appid, name=f"g{appid}", updated_at=datetime.now(), **kw))
        await session.commit()


# ── 1. 家族愿望单 → Monitoring ────────────────────────────────


@pytest.mark.asyncio
async def test_family_wishlist_enters_monitoring_with_first_priority(db):
    await _wl(db, 700, wishlisted=True)
    states = await monitoring.sync_game_sources([700])

    assert states[700] == "active"
    assert await monitoring.sources_of("game", 700) == ["family_wishlist"]
    assert await monitoring.active_ids("game") == [700]

    async with db() as session:
        row = (
            await session.execute(
                select(MonitorTarget).where(MonitorTarget.target_id == 700)
            )
        ).scalar_one()
    assert row.priority == 95  # 家族愿望单：第一优先级组


@pytest.mark.asyncio
async def test_pool_scope_orders_family_wishlist_before_second_priority(db):
    await _game(db, 700)
    await _game(db, 800)
    await _wl(db, 700, wishlisted=True)
    await _wl(db, 800, manual_pool=True)
    await monitoring.sync_game_sources([700, 800])
    # 手动加入 = 用户显式来源（不再由 wishlist_items 的 manual_pool 派生）
    await monitoring.ensure_source("game", 800, "manual")

    assert await crawl_service.resolve_scope_appids("pool", None) == [(700, ""), (800, "")]


# ── 2. 多来源共存 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wishlist_removal_keeps_monitoring_when_manual_source_exists(db):
    await _wl(db, 700, wishlisted=True, manual_pool=True)
    await monitoring.sync_game_sources([700])
    await monitoring.ensure_source("game", 700, "manual")
    assert await monitoring.sources_of("game", 700) == ["family_wishlist", "manual"]

    # 从愿望单删除：wishlisted 标清零（同步反向核对的既有行为）；
    # 账号对账只收敛派生来源，用户显式来源不被洗掉 → 仍在监控
    await _wl(db, 700, wishlisted=False, manual_pool=True)
    assert (await monitoring.sync_game_sources([700]))[700] == "active"
    assert await monitoring.sources_of("game", 700) == ["manual"]
    assert 700 in await monitoring.active_ids("game")


# ── 3. 最后一个来源消失 → released ────────────────────────────


@pytest.mark.asyncio
async def test_last_source_gone_releases_target(db):
    await _game(db, 700)
    await _wl(db, 700, wishlisted=True)
    await monitoring.sync_game_sources([700])
    assert await monitoring.state_of("game", 700) == "active"

    await _wl(db, 700, wishlisted=True, active=False)
    assert (await monitoring.sync_game_sources([700]))[700] == "released"
    assert await monitoring.active_ids("game") == []
    # Catalog 仍在
    async with db() as session:
        assert await session.get(Game, 700) is not None


# ── 4 / 5. 排除与解除 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_excluded_blocks_monitoring_and_crawl(db):
    await _wl(db, 700, wishlisted=True)
    await monitoring.sync_game_sources([700])

    assert await monitoring.set_exclusion("game", 700, True, "不想要了") == "excluded"
    # 来源与 Catalog 原样保留
    assert await monitoring.sources_of("game", 700) == ["family_wishlist"]
    assert await monitoring.state_of("game", 700) == "excluded"
    assert await monitoring.active_ids("game") == []
    assert await crawl_service.resolve_scope_appids("pool", None) == []

    # 来源重现也不绕过排除门
    await monitoring.attach_source("game", 700, "manual")
    assert await monitoring.state_of("game", 700) == "excluded"


@pytest.mark.asyncio
async def test_unexclude_restores_monitoring_when_source_exists(db):
    await _wl(db, 700, wishlisted=True)
    await monitoring.sync_game_sources([700])
    await monitoring.set_exclusion("game", 700, True)

    assert await monitoring.set_exclusion("game", 700, False) == "active"
    assert 700 in await monitoring.active_ids("game")
    # 排除行保留为历史（active 翻 0，不删除）
    async with db() as session:
        rows = (
            await session.execute(
                select(MonitorExclusion).where(MonitorExclusion.target_id == 700)
            )
        ).scalars().all()
    assert len(rows) == 1 and rows[0].active is False and rows[0].cleared_at is not None


@pytest.mark.asyncio
async def test_unexclude_without_source_stays_released(db):
    await _game(db, 700)
    await monitoring.set_exclusion("game", 700, True)
    assert await monitoring.set_exclusion("game", 700, False) == "released"


@pytest.mark.asyncio
async def test_remove_pool_items_writes_exclusion(db):
    await _account(db)
    await _wl(db, 700, manual_pool=True)
    await wishlist_service.add_pool_items([700], auto_crawl=False)
    await monitoring.sync_game_sources([700])

    out = await wishlist_service.remove_pool_items([700])
    assert out["removed"] == 1
    assert await monitoring.is_excluded("game", 700) is True
    assert await monitoring.state_of("game", 700) == "excluded"

    # 重新加入 = 解除排除
    await wishlist_service.add_pool_items([700], auto_crawl=False)
    assert await monitoring.state_of("game", 700) == "active"


# ── 6. 纯 Catalog 不进 Monitoring ─────────────────────────────


@pytest.mark.asyncio
async def test_catalog_only_game_is_not_monitored(db):
    await _game(db, 900)
    assert await monitoring.state_of("game", 900) is None
    assert await monitoring.active_ids("game") == []
    assert await crawl_service.resolve_scope_appids("pool", None) == []


# ── 7. Bundle 同一套生命周期 ──────────────────────────────────


@pytest.mark.asyncio
async def test_bundle_full_lifecycle(db):
    async with db() as session:
        session.add(Bundle(bundle_id=21478, name="测试包"))
        await session.commit()

    assert await monitoring.state_of("bundle", 21478) is None
    # 无监控记录 → 不在 blocked 里（库内既有包照刷）
    assert await monitoring.blocked_ids("bundle") == set()

    assert await monitoring.track("bundle", 21478, "import") == "active"
    assert await monitoring.sources_of("bundle", 21478) == ["import"]
    assert await monitoring.active_ids("bundle") == [21478]

    assert await monitoring.set_exclusion("bundle", 21478, True) == "excluded"
    assert await monitoring.blocked_ids("bundle") == {21478}
    assert await monitoring.active_ids("bundle") == []

    assert await monitoring.set_exclusion("bundle", 21478, False) == "active"
    assert await monitoring.stop("bundle", 21478) == "released"
    assert await monitoring.blocked_ids("bundle") == {21478}

    assert await monitoring.track("bundle", 21478, "import") == "active"
    assert await monitoring.blocked_ids("bundle") == set()
    # 主档与来源行都还在（排除不是删除）
    async with db() as session:
        rows = (
            await session.execute(
                select(MonitorSource).where(MonitorSource.target_type == "bundle")
            )
        ).scalars().all()
    assert [r.source for r in rows] == ["import"]


# ── 8. 多账户同 AppID 去重 ────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_account_same_appid_yields_single_target(db):
    await _account(db)
    async with db() as session:
        session.add(TrackedAccount(steamid=SECOND, label="副号"))
        await session.commit()
    await _wl(db, 700, PRIMARY, wishlisted=True)
    await _wl(db, 700, SECOND, wishlisted=True)
    await _wl(db, 700, SECOND, manual=True)

    await monitoring.sync_game_sources([700])
    # 关注是用户显式来源（与 Steam 账户来源平行），由关注动作直接挂上
    await monitoring.ensure_source("game", 700, "favorite")

    async with db() as session:
        targets = (
            await session.execute(
                select(MonitorTarget).where(MonitorTarget.target_type == "game")
            )
        ).scalars().all()
        sources = (
            await session.execute(
                select(MonitorSource).where(MonitorSource.target_type == "game")
            )
        ).scalars().all()
    assert len(targets) == 1
    assert {s.source for s in sources} == {"family_wishlist", "favorite"}
    assert targets[0].priority == 100  # 任一账户关注 → 关注档


# ── 9. crawl scope 分层 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_pool_and_catalog_scopes_are_disjoint(db):
    await _game(db, 700)  # 监控中
    await _game(db, 800)  # 纯 Catalog
    await _wl(db, 700, wishlisted=True)
    await monitoring.sync_game_sources([700])

    pool = [a for a, _ in await crawl_service.resolve_scope_appids("pool", None)]
    catalog = [a for a, _ in await crawl_service.resolve_scope_appids("catalog", None)]
    assert pool == [700]
    assert catalog == [800]


@pytest.mark.asyncio
async def test_catalog_scope_skips_removed_and_free_games(db):
    await _game(db, 800)
    await _game(db, 801, removed_at=datetime.now())
    await _game(db, 802, free_kind="f2p")

    catalog = [a for a, _ in await crawl_service.resolve_scope_appids("catalog", None)]
    assert catalog == [800]


# ── 10. 永久免费是业务状态，不是用户排除 ──────────────────────


@pytest.mark.asyncio
async def test_free_game_release_is_not_exclusion(db):
    await _account(db)
    await _game(db, 700, free_kind="f2p")
    await _wl(db, 700, manual_pool=True)
    await monitoring.sync_game_sources([700])
    await monitoring.ensure_source("game", 700, "manual")
    assert await monitoring.state_of("game", 700) == "active"

    released = await wishlist_service.release_free_games([700])
    assert released == 1
    assert await monitoring.state_of("game", 700) == "released"
    assert await monitoring.is_excluded("game", 700) is False


# ── 11. 解绑账户回收派生来源（账本连续性）─────────────────────


@pytest.mark.asyncio
async def test_remove_account_releases_derived_sources(db):
    """解绑：派生来源摘除，无其他来源的对象退出监控与池列表。"""
    await _account(db)
    await _game(db, 700)
    await _game(db, 701)
    await _wl(db, 700, wishlisted=True, owned=True)
    await _wl(db, 701, wishlisted=True)
    await monitoring.sync_game_sources([700, 701])
    assert await monitoring.state_of("game", 700) == "active"

    receipt = await wishlist_service.remove_account(PRIMARY)

    assert receipt["itemsRemoved"] == 2
    assert receipt["poolReleased"] == 2 and receipt["reconciled"] is True
    # 派生来源全部摘除
    assert await monitoring.sources_of("game", 700) == []
    assert await monitoring.sources_of("game", 701) == []
    # 失去全部来源 → released，退出 crawl 与池列表
    assert await monitoring.state_of("game", 700) == "released"
    assert await monitoring.active_ids("game") == []
    assert await monitoring.crawl_order("game") == []
    items = {it["appid"]: it for it in await wishlist_service.list_items()}
    assert 700 not in items and 701 not in items
    # 账户与来源数据已删
    async with db() as session:
        assert await session.get(TrackedAccount, PRIMARY) is None
        rows = (
            await session.execute(
                select(WishlistItem).where(WishlistItem.steamid == PRIMARY)
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_remove_account_keeps_explicit_and_other_account_sources(db):
    """解绑：manual 等非派生来源保留；其余账户仍持有的派生来源保留。"""
    await _account(db)
    async with db() as session:
        session.add(TrackedAccount(steamid=SECOND, label="副号"))
        await session.commit()
    await _game(db, 700)
    await _game(db, 701)
    await _game(db, 702)
    # 700：仅 manual（用户显式来源）；701：主号愿望单 + manual；702：双账户愿望单
    await _wl(db, 700, PRIMARY, manual_pool=True)
    await _wl(db, 701, PRIMARY, wishlisted=True, manual_pool=True)
    await _wl(db, 702, PRIMARY, wishlisted=True)
    await _wl(db, 702, SECOND, wishlisted=True)
    await monitoring.sync_game_sources([700, 701, 702])
    await monitoring.ensure_source("game", 700, "manual")
    await monitoring.ensure_source("game", 701, "manual")

    receipt = await wishlist_service.remove_account(PRIMARY)

    assert receipt["itemsRemoved"] == 3
    assert receipt["poolReleased"] == 0
    # 700：manual 来源原样保留，仍在监控与池列表
    assert await monitoring.sources_of("game", 700) == ["manual"]
    assert await monitoring.state_of("game", 700) == "active"
    # 701：family_wishlist 摘除、manual 保留 → 仍 active
    assert await monitoring.sources_of("game", 701) == ["manual"]
    assert await monitoring.state_of("game", 701) == "active"
    # 702：副号仍持有 → family_wishlist 保留
    assert await monitoring.sources_of("game", 702) == ["family_wishlist"]
    assert await monitoring.state_of("game", 702) == "active"
    assert sorted(await monitoring.active_ids("game")) == [700, 701, 702]
    items = {it["appid"]: it for it in await wishlist_service.list_items()}
    assert set(items) == {700, 701, 702}
    assert items[700]["manualPool"] is True and items[700]["wishlisted"] is False
    assert items[701]["manualPool"] is True and items[701]["wishlisted"] is False
    assert items[702]["wishlisted"] is True


@pytest.mark.asyncio
async def test_remove_account_recycles_historical_orphans(db):
    """历史孤儿（解绑遗留、无对应账户数据的派生来源）被解绑流程回收。"""
    await _account(db)
    await _game(db, 700)
    await _wl(db, 700, wishlisted=True)
    await monitoring.sync_game_sources([700])
    # 伪造历史孤儿：600 曾被已解绑账户派生（无 wishlist_items 行、无账户），
    # 601 只有 owned 孤儿来源
    now = datetime.now()
    async with db() as session:
        for appid, src in ((600, "family_wishlist"), (601, "owned")):
            session.add(
                MonitorSource(
                    target_type="game", target_id=appid, source=src,
                    priority=95 if src == "family_wishlist" else 40,
                    active=True, created_at=now, updated_at=now,
                )
            )
            session.add(
                MonitorTarget(
                    target_type="game", target_id=appid,
                    created_at=now, updated_at=now,
                )
            )
        await session.commit()

    receipt = await wishlist_service.remove_account(PRIMARY)

    assert receipt["poolReleased"] == 3  # 700 + 两个孤儿
    assert await monitoring.state_of("game", 600) == "released"
    assert await monitoring.state_of("game", 601) == "released"
    assert await monitoring.active_ids("game") == []
    items = {it["appid"]: it for it in await wishlist_service.list_items()}
    assert 600 not in items and 601 not in items
    # 再跑全量对账：无新回收（幂等）
    assert await monitoring.sync_all_game_sources() == 0
