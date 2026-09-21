"""crawl 域单元测试：wishlist_only / owned scope 拆分选择（tmp sqlite）。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.crawl import service as crawl_service
from app.domains.wishlist.models import TrackedAccount, WishlistItem


PRIMARY = "76561198000000001"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module
    import app.domains.monitoring.service as monitoring_service
    import app.domains.wishlist.service as wishlist_service

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(monitoring_service, "get_session_factory", lambda: factory)
    # import_appids 会走 wishlist 域入池 + 主账户解析：一并打桩到测试库
    monkeypatch.setattr(wishlist_service, "get_session_factory", lambda: factory)

    import app.domains.account.service as account_service

    async def fake_primary():
        return PRIMARY

    monkeypatch.setattr(account_service, "get_primary_steam_id", fake_primary)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.monitoring.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed(db, appid: int, *, owned: bool, active: bool = True):
    async with db() as session:
        session.add(WishlistItem(
            steamid=PRIMARY, appid=appid, active=active, owned=owned,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_scope_split_selection(db):
    """wishlist=全部活跃（含已购）；wishlist_only=非已购；owned=已购；inactive 不进任何 scope。"""
    await _seed(db, 570, owned=False)   # 纯愿望单
    await _seed(db, 620, owned=True)     # 已购
    await _seed(db, 730, owned=True, active=False)  # 已购但已停用

    wl = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist", None)]
    only = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist_only", None)]
    owned = [a for a, _ in await crawl_service._resolve_scope_appids("owned", None)]

    assert wl == [570, 620]       # 历史合并路径：含已购、排除 inactive
    assert only == [570]          # 拆分：非已购
    assert owned == [620]         # 拆分：已购


@pytest.mark.asyncio
async def test_scope_owned_empty_list_semantics(db):
    """无已购条目时 owned scope 返回空（上层 ValueError 跳过该 job）。"""
    await _seed(db, 570, owned=False)
    owned = await crawl_service._resolve_scope_appids("owned", None)
    assert owned == []
    only = await crawl_service._resolve_scope_appids("wishlist_only", None)
    assert [a for a, _ in only] == [570]


# ── 队列优先级：手动关注（manual）最优先 ──
# pairs 序 = 入队序 = worker 消费序（FIFO），排头即先爬；
# 三档：manual 关注 > hot（打折/史低）> 其余按 appid 稳定序。


async def _seed_manual(db, appid: int, *, owned=False, manual=False):
    async with db() as session:
        session.add(WishlistItem(
            steamid=PRIMARY, appid=appid, active=True, owned=owned, manual=manual,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_manual_priority_over_hot_and_rest(db):
    """混排三档：关注（manual）排头，hot（打折）居中，其余 appid 稳定序殿后。"""
    from app.domains.games.models import Game, GameCurrentPrice

    now = __import__("app.crawler.utils", fromlist=["get_beijing_time_obj"]).get_beijing_time_obj().replace(tzinfo=None)
    async with db() as session:
        # 三个非关注池游戏：400 打折（hot）、300 史低（hot）、200 普通
        session.add_all([
            Game(appid=400, name="打折", created_at=now, updated_at=now),
            Game(appid=300, name="史低", hl_flag=1, created_at=now, updated_at=now),
            Game(appid=200, name="普通", created_at=now, updated_at=now),
        ])
        session.add(GameCurrentPrice(
            appid=400, region_code="CN", currency="CNY", price=1000,
            discount_percent=50, price_status="ok", updated_at=now,
        ))
        await session.commit()
    await _seed_manual(db, 400, owned=False, manual=False)   # hot 非 manual
    await _seed_manual(db, 300, owned=False, manual=False)   # hot 非 manual
    await _seed_manual(db, 200, owned=False, manual=False)   # 普通
    await _seed_manual(db, 100, owned=False, manual=True)    # 关注，非 hot
    await _seed_manual(db, 500, owned=True, manual=True)     # 关注已购

    ordered = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist", None)]
    assert ordered == [100, 500, 300, 400, 200], (
        "期望 [manual 关注（100,500 按 appid）] → [hot（300,400）] → [普通 200]，"
        f"实际 {ordered}"
    )


@pytest.mark.asyncio
async def test_all_manual_sorted_stably(db):
    """全关注池：三档塌缩为一档，按 appid 稳定序（不报错、无空段）。"""
    for aid in (930, 210, 640):
        await _seed_manual(db, aid, manual=True)
    ordered = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist", None)]
    assert ordered == [210, 640, 930]


@pytest.mark.asyncio
async def test_manual_from_any_account_counts(db):
    """多账户 distinct 行：任一账户标 manual 即按关注计（跨账户关注并集）。"""
    async with db() as session:
        session.add_all([
            WishlistItem(steamid=PRIMARY, appid=700, active=True, owned=False, manual=False),
            WishlistItem(steamid="76561198000000002", appid=700, active=True, owned=False, manual=True),
            WishlistItem(steamid=PRIMARY, appid=800, active=True, owned=False, manual=False),
        ])
        await session.commit()
    ordered = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist", None)]
    assert ordered == [700, 800]


# ── import_appids：批量导入只分类、绝不写 wishlist_items ──────────────────


@pytest.mark.asyncio
async def test_import_appids_classification(db):
    """导入分类：无行/挂名行 → ok（待首爬）；有元数据 → own（已在库）；非 int → fail。"""
    from datetime import datetime

    from app.domains.games.models import Game

    async with db() as session:
        session.add(Game(appid=620, name="crawled", updated_at=datetime.now()))
        session.add(Game(appid=570, name="name-only"))  # 挂名行（updated_at NULL）
        await session.commit()

    out = await crawl_service.import_appids([620, 570, 999, "abc", 0])
    assert out["ok"] == 2 and out["own"] == 1 and out["fail"] == 2
    by = {r["appid"]: r["status"] for r in out["results"]}
    assert by[620] == "own"       # 已爬过 → 已在库
    assert by[570] == "ok"        # 挂名行 → 待首爬
    assert by[999] == "ok"        # 库内无行 → 待首爬
    assert by["abc"] == "fail"    # 非法 appid
    assert by[0] == "fail"        # 非正数 appid


@pytest.mark.asyncio
async def test_import_appids_writes_pool(db):
    """导入 = 入池：合法 appid 落 manual_pool 条目（监控条目必爬）+ 分类口径不变。"""
    from sqlalchemy import select

    from app.domains.wishlist.models import WishlistItem

    out = await crawl_service.import_appids([620, 570, 730])
    assert out["poolAdded"] == 3
    async with db() as session:
        rows = (await session.execute(select(WishlistItem))).scalars().all()
    assert {int(r.appid) for r in rows} == {620, 570, 730}
    assert all(r.active and r.manual_pool and not r.manual for r in rows)


@pytest.mark.asyncio
async def test_pool_scope_orders_monitoring_first(db):
    """pool=监控层：只有进入 Monitoring 的对象（关注 > 愿望单 > 已购序）；
    其余 games 行归 catalog 层，不在 pool 里。"""
    from datetime import datetime

    from app.domains.games.models import Game
    from app.domains.monitoring import service as monitoring_service

    async with db() as session:
        session.add_all(
            [
                Game(appid=800, name="目录甲", updated_at=datetime.now()),
                Game(appid=801, name="目录乙", updated_at=datetime.now()),
                Game(appid=802, name="已下架", updated_at=datetime.now(),
                     removed_at=datetime.now()),
            ]
        )
        await session.commit()
    await _seed(db, 700, owned=False, active=True)   # manual 关注另行补
    await _seed(db, 701, owned=True, active=True)    # 已购
    await _seed(db, 702, owned=True, active=False)   # inactive → 无有效来源
    async with db() as session:
        row = await session.get(WishlistItem, (PRIMARY, 700))
        row.manual = True
        await session.commit()
    await monitoring_service.sync_game_sources([700, 701, 702])

    pool = [a for a, _ in await crawl_service._resolve_scope_appids("pool", None)]
    catalog = [a for a, _ in await crawl_service._resolve_scope_appids("catalog", None)]
    assert pool == [700, 701]        # 关注最先，已购次之
    assert catalog == [800, 801]     # 纯目录行（下架行剔除）
