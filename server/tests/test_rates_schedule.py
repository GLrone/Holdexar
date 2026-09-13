"""rates 域调度兜底测试：refresh_if_stale 的过期判定与幂等行为。

背景（汇率刷新事故）：fx_refresh 原为 interval 24h 从启动起算，
本地服务频繁重启（单日 10+ 次）导致 24h 永远到不了点，"每日自动抓取"
承诺落空。修复 = 每日 cron 03:00 + 启动时快照龄检查（>12h 即补刷）。

隔离：对齐 test_account_wallet.py —— monkeypatch 临时库 session factory，
不触真实网络（refresh_rates 打桩，只测判定逻辑）。
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.rates import service as rates_service
from app.domains.rates.models import FxRate
from app.crawler.utils import get_beijing_time_obj


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(rates_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_snapshot(db, age_hours: float) -> None:
    """种一条指定龄的 USD 快照。"""
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with db() as session:
        session.add(
            FxRate(
                currency_code="USD",
                rate_to_cny=6.71,
                fetched_at=now - timedelta(hours=age_hours),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_if_stale_skips_fresh_snapshot(db, monkeypatch):
    """快照 2h 前更新过 → 不触发刷新（正常日内的重复启动不重复抓）。"""
    await _seed_snapshot(db, age_hours=2)
    called = []
    monkeypatch.setattr(rates_service, "refresh_rates", lambda: called.append(1))
    assert await rates_service.refresh_if_stale() is False
    assert called == []


@pytest.mark.asyncio
async def test_refresh_if_stale_triggers_on_stale_snapshot(db, monkeypatch):
    """快照 20h 前更新（超过 12h 阈值）→ 触发补刷新。"""
    await _seed_snapshot(db, age_hours=20)

    async def _fake_refresh():
        called.append("refreshed")
        return {"source": "test", "count": 39, "fetchedAt": "now"}

    called = []
    monkeypatch.setattr(rates_service, "refresh_rates", _fake_refresh)
    assert await rates_service.refresh_if_stale() is True
    assert called == ["refreshed"]


@pytest.mark.asyncio
async def test_refresh_if_stale_empty_db_returns_false(db, monkeypatch):
    """空库（无快照行）→ 不视为过期（首刷走正常调度/手动）。"""
    called = []
    monkeypatch.setattr(rates_service, "refresh_rates", lambda: called.append(1))
    assert await rates_service.refresh_if_stale() is False
    assert called == []


@pytest.mark.asyncio
async def test_refresh_if_stale_swallows_refresh_failure(db, monkeypatch):
    """补刷新抛异常（双源全败）→ 吞掉不炸启动链，返回 False。"""
    await _seed_snapshot(db, age_hours=30)

    async def _boom():
        raise RuntimeError("所有汇率源均失败")

    monkeypatch.setattr(rates_service, "refresh_rates", _boom)
    assert await rates_service.refresh_if_stale() is False
