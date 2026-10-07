"""链级停止与占用前移测试。

覆盖：stop 整轮语义（链级停止位贯通 run_sequential / run_price_cycle）、
whole_chain=False 旧语义、停轮跳过 repairing 与事件/通知、占用前移的
预取/失败释放/收尾释放、has_pending_missing 的 EXISTS 直查口径。
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import scheduler as sched_mod
from app.core.database import Base
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import cycle_run
from app.domains.crawl import events as _events_mod  # noqa: F401  price_events 建表
from app.domains.crawl import service as crawl_service
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
# pool / monitoring 表注册进 Base.metadata
from app.domains.monitoring import models as _monitoring_models  # noqa: F401
from app.crawler.occupancy import CrawlerBusyError, begin_crawl, current_holder, end_crawl


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module
    import app.crawler.db_writer as dw

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cycle_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(dw, "get_session_factory", lambda: factory)
    import app.domains.monitoring.service as monitoring_service
    import app.core.orchestration as orchestration_mod
    import app.domains.crawl.events as crawl_events
    import app.domains.crawl.stats as crawl_stats
    import app.domains.crawl.coverage as crawl_coverage
    import app.domains.notifications.service as notification_service

    monkeypatch.setattr(monitoring_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(orchestration_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_events, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_stats, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_coverage, "get_session_factory", lambda: factory)
    monkeypatch.setattr(notification_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    crawl_service._active = None
    crawl_service.unregister_chain_stop(crawl_service._chain_stop)
    sched_mod._price_cycle_busy = False
    end_crawl()


# ── occupancy：同 tag 幂等重入 ──


def test_occupancy_same_tag_reentrant():
    begin_crawl("t")
    begin_crawl("t")  # 同 tag 重入不抛
    assert current_holder() == "t"
    end_crawl()
    assert current_holder() is None


def test_occupancy_other_tag_raises():
    begin_crawl("a")
    with pytest.raises(CrawlerBusyError):
        begin_crawl("b")
    end_crawl()


# ── 链级停止：run_sequential 段间退出 ──


def _fake_start_job_factory(started):
    async def _fake_start_job(**kw):
        started.append(kw.get("scope") or kw.get("kind"))
        ev = asyncio.Event()

        async def _segment():
            # 模拟一段在跑的 job：stop_event 置位即结束；未被停止也定时自然
            # 收敛（真实链路里 run_crawl 总会返回）
            try:
                await asyncio.wait_for(ev.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(_segment())
        crawl_service._active = crawl_service.JobHandle(
            id=len(started), task=task, stop_event=ev
        )
        return {"id": len(started)}

    return _fake_start_job


@pytest.mark.asyncio
async def test_stop_whole_chain_skips_remaining_segments(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(crawl_service, "start_job", _fake_start_job_factory(started))
    chain = asyncio.Event()
    crawl_service.register_chain_stop(chain)
    specs = [{"kind": "missing"}, {"scope": "pool"}, {"scope": "catalog"}]
    runner = asyncio.create_task(crawl_service.run_sequential(specs, stop_event=chain))
    await asyncio.sleep(0.05)
    assert await crawl_service.stop_job(whole_chain=True) is True
    results = await runner
    assert len(started) == 1 and len(results) == 1
    assert chain.is_set() and crawl_service.chain_stop_requested() is True
    crawl_service.unregister_chain_stop(chain)


@pytest.mark.asyncio
async def test_stop_current_segment_only_keeps_chain(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(crawl_service, "start_job", _fake_start_job_factory(started))
    chain = asyncio.Event()
    crawl_service.register_chain_stop(chain)
    specs = [{"kind": "missing"}, {"scope": "pool"}]
    runner = asyncio.create_task(crawl_service.run_sequential(specs, stop_event=chain))
    await asyncio.sleep(0.05)
    assert await crawl_service.stop_job(whole_chain=False) is True
    results = await runner
    assert len(started) == 2 and len(results) == 2
    assert not chain.is_set()  # 只停当前段：链级停止位不置位
    crawl_service.unregister_chain_stop(chain)


# ── run_price_cycle：停轮返回 True、终态 cancelled、不进 repairing、不发事件 ──


@pytest.mark.asyncio
async def test_stopped_cycle_returns_true_skips_repairing_and_events(
    db, monkeypatch
):
    async def _regions(regions=None):
        return ["CN"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async with db() as session:
        session.add(Game(appid=770001, name="T", created_at=_now_naive(),
                         updated_at=_now_naive()))
        session.add(GameCurrentPrice(
            appid=770001, region_code="CN", currency="CNY", price=None,
            original_price=None, discount_percent=0, sub_id=None,
            price_status="missing", fail_count=1,
            updated_at=datetime.now() - timedelta(hours=2),
        ))
        await session.commit()

    calls = {"detect": 0, "notify": 0}

    async def _no_detect(cycle_id):
        calls["detect"] += 1

    async def _no_notify(cycle_id):
        calls["notify"] += 1

    monkeypatch.setattr(cycle_run, "detect_events", _no_detect)
    monkeypatch.setattr(cycle_run, "notify_events", _no_notify)

    async def _stopped_mid_chain(specs, **kw):
        async with db() as session:
            session.add(CrawlJob(
                kind="scheduled", status="stopped", cycle_id=kw.get("cycle_id"),
                started_at=datetime.now(), finished_at=datetime.now(),
            ))
            await session.commit()
        # 第一段收尾时用户按停止：整轮语义
        await crawl_service.stop_job()
        return [{"id": 1}]

    monkeypatch.setattr(crawl_service, "run_sequential", _stopped_mid_chain)

    result = await cycle_run.run_price_cycle([{"scope": "pool"}])
    assert result is True  # 停轮不得触发调用方让路重试
    assert crawl_service.chain_stop_requested() is False  # 收尾后注销
    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.CANCELLED  # 全停 → 非 failed
    assert rows[0]["enteredRepairing"] is False  # 停轮不进 repairing
    assert calls == {"detect": 0, "notify": 0}  # 停轮不发事件不投递通知


# ── has_pending_missing：EXISTS 直查 ──


def _now_naive() -> datetime:
    """时间戳列统一北京时 naive（与生产的冷却判定同源）：本机时区若是
    UTC（云端 runner），datetime.now() 会与判定口径错位整 8 小时。"""
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)


def _missing_row(appid, region="CN", age_hours=2.0):
    return GameCurrentPrice(
        appid=appid, region_code=region, currency="CNY", price=None,
        original_price=None, discount_percent=0, sub_id=None,
        price_status="missing", fail_count=1,
        updated_at=_now_naive() - timedelta(hours=age_hours),
    )


@pytest.mark.asyncio
async def test_has_pending_missing_true_on_old_debt(db):
    async with db() as session:
        session.add(_missing_row(770002))
        await session.commit()
    assert await crawl_service.has_pending_missing(cooldown_minutes=60) is True


@pytest.mark.asyncio
async def test_has_pending_missing_false_when_empty_or_fresh(db):
    async with db() as session:
        session.add(_missing_row(770003, region=""))  # 空区痕迹不算待补
        await session.commit()
    assert await crawl_service.has_pending_missing(cooldown_minutes=60) is False

    async with db() as session:
        session.add(_missing_row(770004, age_hours=0.01))  # 冷却内不算
        await session.commit()
    assert await crawl_service.has_pending_missing(cooldown_minutes=60) is False


@pytest.mark.asyncio
async def test_has_pending_missing_no_write_side_effect(db, monkeypatch):
    """EXISTS 直查不产生写事务：欠账存在时函数本身不得改库。"""

    async with db() as session:
        session.add(_missing_row(770005))
        await session.commit()

    assert await crawl_service.has_pending_missing(cooldown_minutes=60) is True
    assert await crawl_service.has_pending_missing(cooldown_minutes=60) is True


# ── 占用前移：失败释放 / 成功收尾释放 ──


@pytest.mark.asyncio
async def test_start_job_failure_releases_occupancy(db, monkeypatch):
    async def _empty_scope(scope, appids):
        return []

    monkeypatch.setattr(crawl_service, "resolve_scope_appids", _empty_scope)

    with pytest.raises(ValueError):
        await crawl_service.start_job(scope="appids", appids=[])
    assert current_holder() is None  # 预取的占用已随失败路径释放


@pytest.mark.asyncio
async def test_start_job_success_releases_at_execute_end(db, monkeypatch):
    from app.crawler.runner import CrawlRunConfig

    async def _regions(regions=None):
        return ["CN"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async def _value(key, default=None):
        # proxy.strategy=direct_only：走直连分支，绕开 lane plan（本测试断言的是
        # 占用生命周期，不是运行时可用性）
        return "direct_only" if key == "proxy.strategy" else default

    import app.domains.settings.service as settings_service

    monkeypatch.setattr(settings_service, "get_value", _value)

    import app.domains.proxies.service as proxies_service

    async def _no_system_proxy():
        return None  # 系统代理解析读真实注册表，桩掉保持确定性

    monkeypatch.setattr(proxies_service, "resolve_system_proxy_url", _no_system_proxy)

    async def _workers():
        return 4

    monkeypatch.setattr(crawl_service, "_resolve_worker_count", _workers)

    async def _run_crawl(pairs, *, config, stop_event=None, pre_tasks=None,
                         crawl_job_id=None):
        assert config.occupancy_tag == crawl_service.JOB_OCCUPANCY_TAG
        assert current_holder() == crawl_service.JOB_OCCUPANCY_TAG
        return {"total": 1, "processed": 1, "success": 1, "failed": 0}

    monkeypatch.setattr(crawl_service, "run_crawl", _run_crawl)

    for attr in ("check_appids", "check_new_lows"):
        monkeypatch.setattr(crawl_service.alerts_service, attr, _noop)
    for attr in ("refresh_hl_flags", "refresh_pp_flags", "refresh_sort_cache"):
        monkeypatch.setattr(crawl_service.games_service, attr, _noop)
    monkeypatch.setattr(crawl_service.games_series, "has_unassigned", _noop)
    monkeypatch.setattr(crawl_service.games_series, "overrides_changed", _noop)
    import app.domains.wishlist.service as wishlist_service

    monkeypatch.setattr(wishlist_service, "release_free_games", _noop)

    result = await crawl_service.start_job(scope="appids", appids=[770006])
    assert current_holder() == crawl_service.JOB_OCCUPANCY_TAG  # 执行期间持占用
    handle = crawl_service._active
    await handle.task
    assert current_holder() is None  # 收尾释放（不依赖 run_crawl 桩）
    assert crawl_service._active is None
    assert result["count"] == 1


async def _noop(*a, **kw):
    return 0
