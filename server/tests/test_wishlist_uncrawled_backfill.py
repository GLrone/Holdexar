"""首爬漏爬回补层单元测试（mock，不触网）。

背景（v0.1.0 实测事故语义）：绑定后 _post_bind_fetch 会触发 wishlist_sync
首爬，但三种现实路径会让首爬「入库了却没爬」：
1. 任务被进程重启中断（crawl_jobs 留 failed=进程重启中断，价格零写入）；
2. 代理闸门拦截（无可用代理时 from_scheduler 任务静默跳过）；
3. 任务占用漏爬后 15min 内再次撞上占用。

回补层（scheduler._uncrawled_active_appids + 回补段）按「active=1 且
games 无行」判定真欠账，每轮账户同步收尾补一批（配额帽 400）：
- 已有终态痕迹（games 行 / locked/blocked/missing 价格行）不进回补；
- 回补跑完仍无 games 行的条目落 missing 尝试痕迹（region_code=''），
  转入补抓账本通道，不在回补层无限重扫；
- 空区行不生成补抓任务（country_code='' 的请求必 400）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.core.database as database_module
import app.core.scheduler as sched_mod
import app.crawler.db_writer as db_writer_mod
import app.domains.crawl.service as crawl_service_mod
import app.domains.wishlist.service as wishlist_service_mod
from app.core.database import Base
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.wishlist.models import TrackedAccount, WishlistItem

PRIMARY = "76561198000000001"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (
        database_module,
        crawl_service_mod,
        db_writer_mod,
        wishlist_service_mod,
    ):
        monkeypatch.setattr(mod, "get_session_factory", lambda: factory)

    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    sched_mod._WISH_UNCRAWLED_BATCH = 400


async def _seed_wishlist_row(db, appid: int, *, active=True):
    async with db() as session:
        if await session.get(TrackedAccount, PRIMARY) is None:
            session.add(TrackedAccount(steamid=PRIMARY, kinds_json={"wishlist": True, "owned": True}))
        session.add(WishlistItem(steamid=PRIMARY, appid=appid, active=active, wishlisted=True))
        await session.commit()


# ── 未爬判定 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uncrawled_picks_rows_without_games(db):
    """首爬从未抵达写入阶段（games 无行）→ 进回补。"""
    # 400（无任何行）+ 401（有 locked 价格状态行但无 games 行——首爬尝试过）
    await _seed_wishlist_row(db, 400)
    await _seed_wishlist_row(db, 401)
    async with db() as session:
        session.add(GameCurrentPrice(
            appid=401, region_code="CN", price_status="locked", fail_count=0,
            updated_at=datetime.now(),
        ))
        await session.commit()

    appids = await sched_mod._uncrawled_active_appids()

    assert appids == [400], "有终态痕迹的条目不得进回补，只有零痕迹的才进"


@pytest.mark.asyncio
async def test_uncrawled_excludes_removed_and_inactive(db):
    """下架行 / 停用行不进回补。"""
    await _seed_wishlist_row(db, 500)
    await _seed_wishlist_row(db, 501, active=False)
    async with db() as session:
        session.add(Game(
            appid=502, name="已下架", created_at=datetime.now(), updated_at=datetime.now(),
            removed_at=datetime.now(),
        ))
        session.add(WishlistItem(steamid=PRIMARY, appid=502, active=True, wishlisted=True))
        await session.commit()

    appids = await sched_mod._uncrawled_active_appids()

    assert appids == [500]


@pytest.mark.asyncio
async def test_uncrawled_batch_cap(db, monkeypatch):
    """配额帽：超出 _WISH_UNCRAWLED_BATCH 的部分下一轮再补。"""
    monkeypatch.setattr(sched_mod, "_WISH_UNCRAWLED_BATCH", 3)
    for a in range(600, 606):
        await _seed_wishlist_row(db, a)

    appids = await sched_mod._uncrawled_active_appids()

    assert appids == [600, 601, 602]


# ── 回补后痕迹落账 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stamp_missing_after_backfill(db):
    """回补跑完仍无 games 行 → 落 region_code='' 的 missing 痕迹；
    有 games 行（回补成功）的不落。"""
    async with db() as session:
        session.add(Game(appid=700, name="回补成功", created_at=datetime.now(), updated_at=datetime.now()))
        await session.commit()

    wrote = await sched_mod._stamp_uncrawled_missing([700, 701, 702])

    assert wrote == 2
    async with db() as session:
        rows = (
            await session.execute(
                __import__("sqlalchemy").select(GameCurrentPrice).where(
                    GameCurrentPrice.appid.in_([701, 702])
                )
            )
        ).scalars().all()
    assert {r.price_status for r in rows} == {"missing"}
    assert all(r.region_code == "" for r in rows)
    # 已进回补判定 → 痕迹行让下轮不再选中
    appids = await sched_mod._uncrawled_active_appids()
    assert 701 not in appids and 702 not in appids


# ── 空区行不生成补抓任务 ─────────────────────────────────


@pytest.mark.asyncio
async def test_empty_region_missing_row_skipped_by_retry_lane(db):
    """region_code='' 的痕迹行不进 missing 补抓任务（country_code='' 必 400）。"""
    from app.crawler.db_writer import DbWriter

    now = datetime(2020, 1, 1)  # 冷却窗外
    async with db() as session:
        session.add(GameCurrentPrice(
            appid=800, region_code="", price_status="missing", fail_count=1,
            updated_at=now,
        ))
        session.add(GameCurrentPrice(
            appid=801, region_code="CN", price_status="missing", fail_count=1,
            updated_at=now,
        ))
        await session.commit()

    tasks = await DbWriter().generate_missing_tasks(cooldown_minutes=0)

    assert [t["region"] for t in tasks] == ["cn"]
    assert [t["appids"] for t in tasks] == [[801]]
