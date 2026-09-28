"""启动新鲜度补偿测试：池过期判定与首轮提前排程。

判定口径见 app/core/scheduler.py 的「启动新鲜度补偿」块：
- 占比 = 池内 active 游戏对象中 24h 内有价格观察的比例（与地区无关）；
- 空池不补偿；占比达门槛不提前；网格点已近在眼前不提前；
- 自动价格链总开关关闭时整个补偿停转。
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


async def _seed_target(appid: int, *, state: str = "active",
                       price_age: timedelta | None) -> None:
    """落一个监控对象；price_age 非空时带一条对应龄的价格观察行。"""
    from app.core.database import get_session_factory
    from app.domains.games.models import GameCurrentPrice
    from app.domains.monitoring.models import MonitorTarget

    async with get_session_factory()() as session:
        session.add(MonitorTarget(
            target_type="game", target_id=appid, state=state, priority=60,
        ))
        if price_age is not None:
            session.add(GameCurrentPrice(
                appid=appid, region_code="CN", currency="CNY",
                price=1999, original_price=1999, discount_percent=0,
                sub_id=0, price_status="ok", cny_fen=1999,
                updated_at=datetime.now() - price_age,
                fail_count=0, discount_end_ts=0,
            ))
        await session.commit()


def _patch_job(monkeypatch, next_run_time: datetime) -> dict:
    """给调度器挂一个只记录 modify 调用的假 price_refresh job。"""
    job = type("Job", (), {"next_run_time": next_run_time})()
    modified: dict = {}
    monkeypatch.setattr(scheduler_mod.scheduler, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        scheduler_mod.scheduler,
        "modify_job",
        lambda job_id, next_run_time: modified.update(
            id=job_id, next_run_time=next_run_time,
        ),
    )
    return modified


@pytest.mark.asyncio
async def test_empty_pool_returns_none():
    assert await scheduler_mod._pool_fresh_ratio() is None


@pytest.mark.asyncio
async def test_ratio_counts_only_active_pool_with_recent_observation():
    await _seed_target(101, price_age=timedelta(hours=1))
    await _seed_target(102, price_age=timedelta(days=3))
    # released 状态不属池内对象，新鲜与否都不计入
    await _seed_target(103, state="released", price_age=timedelta(hours=1))
    total, share = await scheduler_mod._pool_fresh_ratio()
    assert (total, round(share, 4)) == (2, 0.5)


@pytest.mark.asyncio
async def test_kick_pulls_first_run_earlier_when_stale(monkeypatch):
    await _seed_target(201, price_age=timedelta(days=3))

    async def _enabled():
        return True

    monkeypatch.setattr(scheduler_mod, "price_auto_enabled", _enabled)
    late = datetime.now().astimezone() + timedelta(hours=3)
    modified = _patch_job(monkeypatch, late)

    await scheduler_mod._kick_stale_price_refresh()
    assert modified["id"] == "price_refresh"
    assert modified["next_run_time"] < late
    assert modified["next_run_time"] <= (
        datetime.now().astimezone() + timedelta(seconds=130)
    )


@pytest.mark.asyncio
async def test_kick_skips_when_pool_fresh(monkeypatch):
    await _seed_target(301, price_age=timedelta(hours=1))

    async def _enabled():
        return True

    monkeypatch.setattr(scheduler_mod, "price_auto_enabled", _enabled)
    modified = _patch_job(
        monkeypatch, datetime.now().astimezone() + timedelta(hours=3)
    )

    await scheduler_mod._kick_stale_price_refresh()
    assert modified == {}


@pytest.mark.asyncio
async def test_kick_skips_when_switch_off(monkeypatch):
    await _seed_target(401, price_age=timedelta(days=3))

    async def _disabled():
        return False

    monkeypatch.setattr(scheduler_mod, "price_auto_enabled", _disabled)
    modified = _patch_job(
        monkeypatch, datetime.now().astimezone() + timedelta(hours=3)
    )

    await scheduler_mod._kick_stale_price_refresh()
    assert modified == {}


@pytest.mark.asyncio
async def test_kick_skips_when_grid_point_already_near(monkeypatch):
    await _seed_target(501, price_age=timedelta(days=3))

    async def _enabled():
        return True

    monkeypatch.setattr(scheduler_mod, "price_auto_enabled", _enabled)
    # 现有网格点比提前时刻更近：正常轮即刻就是补偿，不改排程
    soon = datetime.now().astimezone() + timedelta(seconds=30)
    modified = _patch_job(monkeypatch, soon)

    await scheduler_mod._kick_stale_price_refresh()
    assert modified == {}
