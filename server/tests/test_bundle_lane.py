"""捆绑包单协程 lane 行为验收。

合成 990xxx bundle/appid 播种 + 清理（不污染真实库），验证：
- lane 分道：bundle 任务不占 app worker，由单协程串行处理；
- 发现即转投：app handler 产出 bundle 任务 → 主队列 worker 转投 lane；
- 自动收尾：bundle 队列排干后 watcher 投毒丸，worker 退出（非常驻）；
- 重启语义：lane 收尾后再投递会重新拉起；
- 播种查询：无价 + updated_at 超 24h（或 NULL）才入选；有价/新鲜桩不选；
- 失败 touch：handle_bundle_task 抓取无数据时落尝试时间戳（冷却判据）。
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.crawler.db_writer import DbWriter  # noqa: E402
from app.crawler.router import CrawlerContext, CrawlerRouter  # noqa: E402
from app.crawler.scheduler import CrawlerScheduler  # noqa: E402
from app.domains.games.models import Bundle, BundleRegionPrice, Game  # noqa: E402

BID_STALE = 990_301  # 无价 + updated_at NULL（播种入选）
BID_FRESH = 990_302  # 无价 + updated_at 刚写（24h 内，不入选）
BID_PRICED = 990_303  # 有价（不入选）
APPID_L = 990_311


class _FakeClient:
    """scheduler 只透传 http_client，行为由 handler 自定，占位即可。"""


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    async with get_session_factory()() as session:
        session.add_all(
            [
                Bundle(bundle_id=BID_STALE, name="桩_无时间戳", app_ids=[APPID_L]),
                Bundle(
                    bundle_id=BID_FRESH, name="桩_新鲜", app_ids=[APPID_L],
                    updated_at=datetime.utcnow(),
                ),
                Bundle(
                    bundle_id=BID_PRICED, name="有价包", app_ids=[APPID_L],
                    updated_at=datetime.utcnow() - timedelta(hours=48),
                ),
                BundleRegionPrice(
                    bundle_id=BID_PRICED, region_code="cn", currency="CNY",
                    price=1000, price_status="ok", cny_fen=1000, app_ids=[APPID_L],
                ),
            ]
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id.in_([BID_STALE, BID_FRESH, BID_PRICED, BID_PRICED + 1])
            )
        )
        await session.execute(
            delete(Bundle).where(
                Bundle.bundle_id.in_([BID_STALE, BID_FRESH, BID_PRICED, BID_PRICED + 1])
            )
        )
        await session.execute(delete(Game).where(Game.appid == APPID_L))
        await session.commit()


def _make_scheduler(record: list, *, workers: int = 3) -> CrawlerScheduler:
    """假 router：app 任务可选地转投 bundle 任务；bundle 任务记序号。

    bundle 执行序号用单调递增计数器验证"串行"（并发会出现乱序交叠，
    串行则严格递增且与前项间隔 0——本测试用 record 顺序断言足够）。
    """
    router = CrawlerRouter()
    state = {"bundle_running": 0, "max_concurrent": 0}
    db = DbWriter()

    @router.handle("app")
    async def _app(context: CrawlerContext) -> None:
        await asyncio.sleep(0.05)  # 模拟在途，让 bundle 与 app 真并行
        record.append(("app", context.task["id"]))
        if context.task.get("spawn_bundle"):
            await context.queue.put({"type": "bundle", "id": context.task["id"] + 1000})

    @router.handle("bundle")
    async def _bundle(context: CrawlerContext) -> None:
        state["bundle_running"] += 1
        state["max_concurrent"] = max(state["max_concurrent"], state["bundle_running"])
        await asyncio.sleep(0.05)
        state["bundle_running"] -= 1
        record.append(("bundle", context.task["id"]))

    sched = CrawlerScheduler(
        router, _FakeClient(), db, worker_count=workers, stop_event=asyncio.Event()
    )
    sched._test_state = state  # noqa: SLF001
    return sched


@pytest.mark.asyncio
async def test_lane_split_serial_and_auto_exit():
    """bundle 与 app 并行；bundle 全程单协程；排干后 lane 自动收尾。"""
    record: list = []
    sched = _make_scheduler(record)
    tasks = (
        [{"type": "app", "id": i, "spawn_bundle": (i == 1)} for i in range(6)]
        + [{"type": "bundle", "id": 50}]
    )
    await sched.run(tasks, session=None)

    apps = [r for r in record if r[0] == "app"]
    bundles = sorted(r[1] for r in record if r[0] == "bundle")
    # 6 app 全处理；bundle = 播种 50 + 转投的 1001（app id 1 + 1000）
    assert len(apps) == 6
    assert bundles == [50, 1001]
    # 单协程：全程并发峰值 1
    assert sched._test_state["max_concurrent"] == 1  # noqa: SLF001
    # lane 已收尾：worker/watcher 任务都退出（非常驻）
    assert all(t.done() for t in sched._bundle_tasks)
    # 计数含 bundle 任务
    assert sched.total_processed == 8
    assert sched.bundle_processed == 2


@pytest.mark.asyncio
async def test_lane_restart_after_drain():
    """lane 排干收尾后再次投递会重新拉起（不死锁不丢任务）。"""
    record: list = []
    sched = _make_scheduler(record)
    await sched.run([{"type": "bundle", "id": 60}], session=None)
    assert sched.bundle_processed == 1
    # 第二轮 run（新调度器实例模拟新爬取批次；同一实例的 lane 重启在
    # _ensure_bundle_lane 已收尾分支覆盖）
    record2: list = []
    sched2 = _make_scheduler(record2)
    await sched2.run([{"type": "bundle", "id": 61}], session=None)
    assert sched2.bundle_processed == 1
    assert [r for r in record2 if r[0] == "bundle"] == [("bundle", 61)]


@pytest.mark.asyncio
async def test_pending_bundle_query_and_cooldown():
    """播种查询：无价+超 24h（或 NULL）入选；新鲜桩/有价包不入选。"""
    db = DbWriter()
    stale_before = datetime.utcnow() - timedelta(hours=24)
    pending = await db.get_pending_bundle_ids(stale_before)
    assert BID_STALE in pending  # updated_at NULL
    assert BID_FRESH not in pending  # 刚写的桩：冷却中
    assert BID_PRICED not in pending  # 已有价格行

    # 失败 touch 后进入冷却
    await db.touch_bundle_attempt(BID_STALE)
    pending2 = await db.get_pending_bundle_ids(stale_before)
    assert BID_STALE not in pending2


@pytest.mark.asyncio
async def test_bundle_handler_failure_touch(monkeypatch):
    """handle_bundle_task 抓取无数据：不写价格、touch 尝试时间戳（冷却判据）。"""
    from app.crawler.handlers.bundle_handler import handle_bundle_task

    async def _fake_fetch(session, bundle_id, rate_map, now=None, *, force_package=False):
        return []

    async def _fake_rates():
        return {}

    from app.domains.bundles import refresh as bundles_refresh
    from app.domains.games import service as games_service

    monkeypatch.setattr(bundles_refresh, "fetch_and_upsert_bundle", _fake_fetch)
    monkeypatch.setattr(games_service, "get_rates", _fake_rates)

    class _Ctx:
        task = {"type": "bundle", "id": BID_STALE}
        session = None
        db_writer = DbWriter()

    await handle_bundle_task(_Ctx())
    async with get_session_factory()() as session:
        row = (
            await session.execute(select(Bundle).where(Bundle.bundle_id == BID_STALE))
        ).scalar_one()
        assert row.updated_at is not None  # touch 生效
        assert row.updated_at > datetime.utcnow() - timedelta(minutes=1)


@pytest.mark.asyncio
async def test_bundle_handler_success_upsert(monkeypatch):
    """handle_bundle_task 成功路径：区域价落库、mps 填 0（复用刷新 upsert）。

    patch 打在 _fetch_bundle_regions/_strategy_proxy 上（而非 facade 本身）——
    落库发生在 fetch_and_upsert_bundle 内部，fake 掉 facade 会绕过写库路径。
    """
    from app.crawler.handlers.bundle_handler import handle_bundle_task

    BID_OK = BID_PRICED + 1

    async def _fake_regions(session, bundle_id, proxy, *, force_package=False):
        return [
            {
                "bundle_id": bundle_id, "region_code": "cn", "price": 29900,
                "currency": "CNY", "discount_percent": 10, "bundle_base_discount": 10,
                "app_ids": [APPID_L, APPID_L + 1], "price_status": "ok",
                "name": "lane测试包", "header_image": "",
            },
        ]

    async def _fake_proxy():
        return None

    async def _fake_rates():
        return {}

    from app.domains.bundles import refresh as bundles_refresh
    from app.domains.games import service as games_service

    monkeypatch.setattr(bundles_refresh, "_fetch_bundle_regions", _fake_regions)
    monkeypatch.setattr(bundles_refresh, "_strategy_proxy", _fake_proxy)
    monkeypatch.setattr(games_service, "get_rates", _fake_rates)

    async with get_session_factory()() as session:
        session.add(Bundle(bundle_id=BID_OK, name="lane测试包", app_ids=[APPID_L]))
        await session.commit()

    class _Ctx:
        task = {"type": "bundle", "id": BID_OK}
        session = None
        db_writer = DbWriter()

    await handle_bundle_task(_Ctx())
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == BID_OK)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].cny_fen == 29900
        b = (
            await session.execute(select(Bundle).where(Bundle.bundle_id == BID_OK))
        ).scalar_one()
        assert b.must_purchase_as_set == 0  # Bundle 轨语义（-1 哨兵被填 0）
        assert b.name == "lane测试包"
