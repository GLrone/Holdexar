"""告警触发后行为（repeat_mode / repeat_hours）。

此前评估无任何去重：条件持续成立则每次评估都触发、规则永不收敛——
同一告警一天重复触发多次。本文件验证三种策略：
once=触发即收敛（默认）；cooldown=冷却窗内不重复；always=持续提醒。

隔离：tmp 库 + get_session_factory 逐模块打桩；send_mail 用桩记录调用。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.alerts import service as alerts_service
from app.domains.alerts.models import AlertEvent, PriceAlert
from app.domains.games.models import Game, GameCurrentPrice

APP = 990101
NOW = datetime(2026, 10, 2, 12, 0, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(alerts_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(alerts_service, "get_beijing_time_obj", lambda: NOW)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def mail(monkeypatch):
    calls: list[str] = []

    async def _region_name(r):
        return f"区:{r}"

    async def _send(subject, html):
        calls.append(subject)
        return True

    monkeypatch.setattr(alerts_service, "region_display_name", _region_name)
    monkeypatch.setattr(alerts_service.notify, "send_mail", _send)
    return calls


async def _seed(db, *, repeat_mode="once", repeat_hours=24, last_triggered=None):
    async with db() as session:
        session.add(Game(appid=APP, name="测试游戏", type="game"))
        session.add(
            GameCurrentPrice(
                appid=APP, region_code="RU", price=20900,
                original_price=30000, discount_percent=30, sub_id=0,
                price_status="ok", cny_fen=1682,
                updated_at=NOW - timedelta(minutes=5),
            )
        )
        session.add(
            PriceAlert(
                appid=APP, region="RU", target_type="price",
                target_value=2400.0, active=True, repeat_mode=repeat_mode,
                repeat_hours=repeat_hours, created_at=NOW - timedelta(days=1),
                last_triggered_at=last_triggered,
            )
        )
        await session.commit()


async def _alert(db) -> PriceAlert:
    async with db() as session:
        return (await session.execute(select(PriceAlert))).scalars().first()


async def _events(db) -> list[AlertEvent]:
    async with db() as session:
        return list(
            (await session.execute(select(AlertEvent).order_by(AlertEvent.id))).scalars().all()
        )


@pytest.mark.asyncio
async def test_once_converges_after_trigger(db, mail):
    await _seed(db, repeat_mode="once")
    triggered = await alerts_service.check_appids([APP])
    assert len(triggered) == 1 and len(mail) == 1
    alert = await _alert(db)
    assert alert.active is False
    # 已收敛：再评估不触发、不发信
    again = await alerts_service.check_appids([APP])
    assert again == [] and len(mail) == 1
    assert len(await _events(db)) == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_inside_window(db, mail):
    await _seed(db, repeat_mode="cooldown", repeat_hours=24,
                last_triggered=NOW - timedelta(hours=1))
    triggered = await alerts_service.check_appids([APP])
    assert triggered == [] and len(mail) == 0


@pytest.mark.asyncio
async def test_cooldown_fires_after_window(db, mail):
    await _seed(db, repeat_mode="cooldown", repeat_hours=24,
                last_triggered=NOW - timedelta(hours=25))
    triggered = await alerts_service.check_appids([APP])
    assert len(triggered) == 1
    alert = await _alert(db)
    assert alert.active is True  # 冷却模式不收敛


@pytest.mark.asyncio
async def test_always_keeps_refiring(db, mail):
    await _seed(db, repeat_mode="always", last_triggered=NOW - timedelta(hours=1))
    first = await alerts_service.check_appids([APP])
    second = await alerts_service.check_appids([APP])
    assert len(first) == 1 and len(second) == 1
    alert = await _alert(db)
    assert alert.active is True


@pytest.mark.asyncio
async def test_default_repeat_mode_is_once(db, mail):
    # 不显式给 repeat_mode：模型默认 once（迁移列默认值同口径）
    await _seed(db)
    alert = await _alert(db)
    assert alert.repeat_mode == "once"
