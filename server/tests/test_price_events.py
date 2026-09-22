"""价格事实事件测试（P5）。

测的是**变化**而不是状态：当前值等于上一有效观察时不产生任何事件；状态类
事件没有上一状态（首次观察）时不产生。未观察（`unobserved`）永远不产生事件。

隔离：tmp 库 + get_session_factory 打桩（cycle / events 等逐模块打桩），
不触生产库、不出网。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import events as events_mod
from app.domains.crawl.cycle import PriceCycle
from app.domains.crawl.events import PriceEvent
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

APP = 800001
BEFORE = 5.0  # 窗口前的快照（小时前）
INSIDE = 0.5  # 本轮窗口内的快照（小时前）
NEXT = 0.05  # 「下一轮」窗口内的观察时刻（3 分钟前，晚于上一轮快照）


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (cycle_mod, events_mod):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _cycle(db, appids, regions=("cn",), *, started_minutes_ago: int = 60) -> int:
    """建一个已收敛的 Cycle，窗口 = [started_minutes_ago, 现在]。

    同一个测试里造「下一轮」时用更小的 started_minutes_ago，让上一轮写入的
    快照落到新窗口之外——真实轮次不会重叠。
    """
    cid = await cycle_mod.create("scheduled", "pool")
    async with db() as session:
        cycle = await session.get(PriceCycle, cid)
        cycle.expected_json = {"appids": list(appids), "regions": list(regions)}
        cycle.status = cycle_mod.COMPLETED
        cycle.started_at = datetime.now() - timedelta(minutes=started_minutes_ago)
        cycle.finished_at = datetime.now()
        await session.commit()
    return cid


async def _next_cycle(db, appids, regions=("cn",)) -> int:
    """下一轮：窗口只覆盖最近 10 分钟，且不写入新快照（价格未变）。"""
    return await _cycle(db, appids, regions, started_minutes_ago=10)


async def _game(db, appid, *, removed_at: datetime | None = None) -> None:
    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=appid, name=f"g{appid}", created_at=now,
                         updated_at=now, removed_at=removed_at))
        await session.commit()


async def _cur(db, appid, region, *, status="ok", price=None, hours_ago=INSIDE):
    """写当前行（同单元重复调用即覆盖——当前表每个单元只有一行）。"""
    async with db() as session:
        row = await session.get(GameCurrentPrice, (appid, region.upper()))
        if row is not None:
            await session.delete(row)
            await session.commit()
    async with db() as session:
        session.add(GameCurrentPrice(
            appid=appid, region_code=region.upper(), currency="CNY",
            price=price, sub_id=1 if price is not None else None,
            price_status=status, fail_count=0, cny_fen=price,
            updated_at=datetime.now() - timedelta(hours=hours_ago),
        ))
        await session.commit()


async def _hist(db, appid, region, *, price, original=None, hours_ago=BEFORE,
                sub_id=1):
    async with db() as session:
        session.add(GamePriceHistory(
            appid=appid, region_code=region.upper(), currency="CNY",
            price=price, original_price=price if original is None else original,
            discount_percent=0, sub_id=sub_id, is_gold=False, version_suffix=None,
            is_bundle=False, price_status="ok", cny_fen=price,
            snapshot_at=datetime.now() - timedelta(hours=hours_ago),
        ))
        await session.commit()


async def _prior_event(db, appid, region, status, event_type) -> None:
    """播一条更早的状态事件：状态类判定的「上一状态」来源。"""
    async with db() as session:
        session.add(PriceEvent(
            cycle_id=1, appid=appid, region_code=region.upper(),
            event_type=event_type, current_json={"status": status},
            occurred_at=datetime.now() - timedelta(hours=3),
        ))
        await session.commit()


def _types(events: list[dict]) -> list[str]:
    return sorted(e["event_type"] for e in events)


# ── 价格涨跌 ──


@pytest.mark.asyncio
async def test_price_drop(db):
    """199 → 99 → PRICE_DROP（更早还有更低价，故不构成新史低）。"""
    cid = await _cycle(db, [APP])
    await _game(db, APP)
    await _hist(db, APP, "cn", price=19900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=5000, hours_ago=10.0)
    await _hist(db, APP, "cn", price=9900, original=19900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=9900)

    events = await events_mod.detect(cid)
    assert _types(events) == [events_mod.PRICE_DROP]
    assert events[0]["previous_json"] == {"price": 19900, "cnyFen": 19900}
    assert events[0]["current_json"] == {"price": 9900, "cnyFen": 9900}
    assert events[0]["region_code"] == "CN"


@pytest.mark.asyncio
async def test_price_increase(db):
    """99 → 199 → PRICE_INCREASE。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=5000, hours_ago=10.0)
    await _hist(db, APP, "cn", price=19900, original=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=19900)

    assert _types(await events_mod.detect(cid)) == [events_mod.PRICE_INCREASE]


@pytest.mark.asyncio
async def test_price_unchanged_produces_nothing(db):
    """价格没变 → 没有事件（差量门禁下也不会有新快照）。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    await _cur(db, APP, "cn", price=9900)

    assert await events_mod.detect(cid) == []


@pytest.mark.asyncio
async def test_first_observation_produces_no_price_event(db):
    """首次观察（没有上一有效价）→ 不产生涨跌事件。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=9900)

    assert _types(await events_mod.detect(cid)) == []


@pytest.mark.asyncio
async def test_price_compared_within_same_sub(db):
    """同一区多个在售包（sub_id 不同）不互相比较：只跟当前标准版的同 sub 快照比。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=19900, hours_ago=5.0, sub_id=1)
    await _hist(db, APP, "cn", price=4200, hours_ago=5.0, sub_id=2)  # 另一个包，更便宜
    await _hist(db, APP, "cn", price=9900, original=19900, hours_ago=INSIDE, sub_id=1)
    await _cur(db, APP, "cn", price=9900)                            # 标准版 sub=1

    events = await events_mod.detect(cid)
    assert events_mod.PRICE_DROP in _types(events)
    drop = next(e for e in events if e["event_type"] == events_mod.PRICE_DROP)
    assert drop["previous_json"]["price"] == 19900, "基线必须是同一个 sub 的价格"


@pytest.mark.asyncio
async def test_zero_price_not_treated_as_drop(db):
    """0 价（促销态）不参与涨跌判定——它属于 FREE_PROMO 的语义。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    await _cur(db, APP, "cn", price=0)

    assert events_mod.PRICE_DROP not in _types(await events_mod.detect(cid))


# ── 历史低价 / 永降 ──


@pytest.mark.asyncio
async def test_new_historical_low_once(db):
    """本轮写下更低的快照 → NEW_HISTORICAL_LOW；下一轮无新快照不再产生。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=19900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=9900)
    assert events_mod.NEW_HISTORICAL_LOW in _types(await events_mod.detect(cid))

    # 下一轮：价格没变 → 没有新快照 → 不重复
    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", price=9900, hours_ago=NEXT)
    assert events_mod.NEW_HISTORICAL_LOW not in _types(await events_mod.detect(cid2))


@pytest.mark.asyncio
async def test_historical_low_match_on_return(db):
    """从更高价回落到史低 → HISTORICAL_LOW_MATCH（不是新史低）。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=10.0)   # 史低
    await _hist(db, APP, "cn", price=19900, hours_ago=5.0)   # 之后涨回
    await _hist(db, APP, "cn", price=9900, hours_ago=INSIDE)  # 本轮跌回史低
    await _cur(db, APP, "cn", price=9900)

    types = _types(await events_mod.detect(cid))
    assert events_mod.HISTORICAL_LOW_MATCH in types
    assert events_mod.NEW_HISTORICAL_LOW not in types


@pytest.mark.asyncio
async def test_permanent_price_change_only_on_change(db):
    """原价变化才产生 PERMANENT_PRICE_CHANGE；价格不变本身不构成事件。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=19900, original=19900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=19900, original=29900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=19900)

    events = await events_mod.detect(cid)
    assert _types(events) == [events_mod.PERMANENT_PRICE_CHANGE]
    assert events[0]["previous_json"] == {"originalPrice": 19900}
    assert events[0]["current_json"] == {"originalPrice": 29900}

    # 下一轮原价未变 → 不重复
    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", price=19900, hours_ago=NEXT)
    assert events_mod.PERMANENT_PRICE_CHANGE not in _types(await events_mod.detect(cid2))


# ── 状态跃迁 ──


@pytest.mark.asyncio
async def test_region_locked_and_unlocked(db):
    """ok → locked → REGION_LOCKED；locked → ok → REGION_UNLOCKED。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)  # 窗口前有有效价 ⇒ 当时 ok
    await _cur(db, APP, "cn", status="locked")
    events = await events_mod.detect(cid)
    assert _types(events) == [events_mod.REGION_LOCKED]
    assert events[0]["previous_json"] == {"status": "ok"}
    assert events[0]["current_json"]["status"] == "locked"

    # 解除锁区：上一状态由状态事件提供
    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", status="ok", price=9900, hours_ago=NEXT)
    assert _types(await events_mod.detect(cid2)) == [events_mod.REGION_UNLOCKED]


@pytest.mark.asyncio
async def test_locked_again_does_not_repeat(db):
    """已经是 locked 再观察一次 → 没有新的跃迁，不重复产生。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    await _cur(db, APP, "cn", status="locked")
    await events_mod.detect(cid)

    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", status="locked", hours_ago=NEXT)
    assert await events_mod.detect(cid2) == []


@pytest.mark.asyncio
async def test_price_unavailable_and_restored(db):
    """ok → missing → PRICE_UNAVAILABLE；missing → ok → PRICE_RESTORED。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    await _cur(db, APP, "cn", status="missing")
    assert _types(await events_mod.detect(cid)) == [events_mod.PRICE_UNAVAILABLE]

    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", status="ok", price=9900, hours_ago=NEXT)
    assert _types(await events_mod.detect(cid2)) == [events_mod.PRICE_RESTORED]


@pytest.mark.asyncio
async def test_locked_to_missing_is_unavailable(db):
    """locked → blocked 也算「明确不可用」（ok/locked → missing/blocked）。"""
    cid = await _cycle(db, [APP])
    await _prior_event(db, APP, "cn", "locked", events_mod.REGION_LOCKED)
    await _cur(db, APP, "cn", status="blocked")
    assert _types(await events_mod.detect(cid)) == [events_mod.PRICE_UNAVAILABLE]


@pytest.mark.asyncio
async def test_new_unit_locked_produces_no_event(db):
    """首次观察就是 locked（无历史、无上一状态）→ 不声称发生跃迁。"""
    cid = await _cycle(db, [APP])
    await _cur(db, APP, "cn", status="locked")
    assert await events_mod.detect(cid) == []


# ── unobserved ──


@pytest.mark.asyncio
async def test_unobserved_produces_nothing(db):
    """本轮没抓到（窗口内无结果）→ 不产生 PRICE_UNAVAILABLE 等任何事件。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)  # 上一轮有价
    await _cur(db, APP, "cn", status="missing", hours_ago=5.0)  # 越出本轮窗口

    assert await events_mod.detect(cid) == []


@pytest.mark.asyncio
async def test_no_current_row_is_unobserved(db):
    """完全没有当前行 → 同样不产生事件。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, hours_ago=5.0)
    assert await events_mod.detect(cid) == []


@pytest.mark.asyncio
async def test_rows_outside_expected_regions_ignored(db):
    """期望集外的区服结果不参与本轮事件。"""
    cid = await _cycle(db, [APP], regions=("cn",))
    await _hist(db, APP, "ru", price=19900, hours_ago=5.0)
    await _hist(db, APP, "ru", price=9900, hours_ago=INSIDE)
    await _cur(db, APP, "ru", price=9900)

    assert await events_mod.detect(cid) == []


# ── 促销免费 ──


@pytest.mark.asyncio
async def test_free_promo_once(db):
    """普通 → 促销（0 价 + 有原价）→ FREE_PROMO；促销持续不重复。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=9900, original=9900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=0, original=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=0)

    events = await events_mod.detect(cid)
    assert _types(events) == [events_mod.FREE_PROMO]
    assert events[0]["region_code"] is None, "促销是游戏级对象（free_kind 在 games 上）"

    # 促销持续：没有新快照 → 不重复
    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", price=0, hours_ago=NEXT)
    assert await events_mod.detect(cid2) == []


@pytest.mark.asyncio
async def test_first_seen_promo_is_not_free_promo(db):
    """首次观察就在促销（没有「之前」可比）→ 不声称发生了进入促销。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=0, original=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=0)

    assert events_mod.FREE_PROMO not in _types(await events_mod.detect(cid))


# ── 下架 ──


@pytest.mark.asyncio
async def test_removed_once(db):
    """NULL → removed_at（落在本轮窗口）→ REMOVED；持续态不重复。"""
    cid = await _cycle(db, [APP])
    await _game(db, APP, removed_at=datetime.now() - timedelta(minutes=30))
    await _cur(db, APP, "cn", status="missing")

    events = await events_mod.detect(cid)
    assert _types(events) == [events_mod.REMOVED]
    assert events[0]["previous_json"] == {"removedAt": None}
    assert events[0]["region_code"] is None

    # 下一轮：removed_at 不在新窗口内 → 不重复
    cid2 = await _next_cycle(db, [APP])
    await _cur(db, APP, "cn", status="missing", hours_ago=NEXT)
    assert await events_mod.detect(cid2) == []


@pytest.mark.asyncio
async def test_removed_before_window_is_not_this_cycle(db):
    """窗口之前就已下架 → 不属于本轮事件。"""
    cid = await _cycle(db, [APP])
    await _game(db, APP, removed_at=datetime.now() - timedelta(days=3))
    await _cur(db, APP, "cn", status="missing")

    assert await events_mod.detect(cid) == []


# ── 幂等与查询 ──


@pytest.mark.asyncio
async def test_detect_is_idempotent(db):
    """重复执行同一轮检测：事件数量不增加。"""
    cid = await _cycle(db, [APP])
    await _hist(db, APP, "cn", price=19900, hours_ago=5.0)
    await _hist(db, APP, "cn", price=5000, hours_ago=10.0)
    await _hist(db, APP, "cn", price=9900, hours_ago=INSIDE)
    await _cur(db, APP, "cn", price=9900)

    first = await events_mod.detect(cid)
    assert first == 1 or first
    assert await events_mod.detect(cid) == []
    assert len(await events_mod.list_events(cycle_id=cid)) == len(first)


@pytest.mark.asyncio
async def test_empty_expected_set_produces_nothing(db):
    cid = await _cycle(db, [])
    assert await events_mod.detect(cid) == []


@pytest.mark.asyncio
async def test_unknown_cycle_produces_nothing(db):
    assert await events_mod.detect(99999) == []


@pytest.mark.asyncio
async def test_list_events_filters(db):
    cid = await _cycle(db, [APP], regions=("cn", "ru"))
    for region in ("cn", "ru"):
        await _hist(db, APP, region, price=19900, hours_ago=5.0)
        await _hist(db, APP, region, price=5000, hours_ago=10.0)
        await _hist(db, APP, region, price=9900, original=19900, hours_ago=INSIDE)
        await _cur(db, APP, region, price=9900)
    await events_mod.detect(cid)

    assert len(await events_mod.list_events(cycle_id=cid)) == 2
    assert len(await events_mod.list_events(appid=APP)) == 2
    assert len(await events_mod.list_events(event_type=events_mod.PRICE_DROP)) == 2
    assert await events_mod.list_events(event_type=events_mod.REMOVED) == []
    assert await events_mod.list_events(cycle_id=99999) == []


@pytest.mark.asyncio
async def test_list_orders_by_occurrence_not_insertion(db):
    """展示序按事实发生时刻：写库序（id）与发生时刻可以不同。

    cn 先写库但发生得更晚，ru 后写库但发生得更早——按 id 排会得到 RU 在前，
    按 occurred_at 排必须 CN 在前。
    """
    cid = await _cycle(db, [APP], regions=("cn", "ru"))
    await _hist(db, APP, "cn", price=19900, hours_ago=BEFORE)
    await _hist(db, APP, "cn", price=5000, hours_ago=BEFORE + 1)
    await _hist(db, APP, "cn", price=9900, original=19900, hours_ago=0.2)
    await _cur(db, APP, "cn", price=9900, hours_ago=0.2)
    await _hist(db, APP, "ru", price=19900, hours_ago=BEFORE)
    await _hist(db, APP, "ru", price=5000, hours_ago=BEFORE + 1)
    await _hist(db, APP, "ru", price=9900, original=19900, hours_ago=0.5)
    await _cur(db, APP, "ru", price=9900, hours_ago=0.5)
    await events_mod.detect(cid)

    rows = await events_mod.list_events(cycle_id=cid)
    assert [row["region"] for row in rows] == ["CN", "RU"]
