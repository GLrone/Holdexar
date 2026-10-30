from datetime import datetime, timedelta

import pytest

from app.core.database import get_session_factory
from app.domains.games.models import GameCurrentPrice, GamePriceHistory


async def _history(session, at, *, appid=930001, region="US", sub=1,
                   currency="USD", price=1000, source="steam", **extra):
    session.add(GamePriceHistory(
        appid=appid, region_code=region, sub_id=sub, currency=currency,
        price=price, original_price=2000, source=source, snapshot_at=at,
        price_status="ok", **extra,
    ))


@pytest.mark.asyncio
async def test_history_context_keeps_identity_and_reliable_sources_separate():
    from app.domains.games.price_comparison import history_context

    before = datetime(2026, 10, 30, 12)
    async with get_session_factory()() as session:
        await _history(session, before - timedelta(days=5), price=1100)
        await _history(session, before - timedelta(days=2), price=900, source="community")
        await _history(session, before - timedelta(days=1), price=100, currency="EUR")
        await _history(session, before - timedelta(days=1), price=100, sub=2)
        await _history(session, before - timedelta(days=1), price=100, region="CN")
        await _history(session, before - timedelta(days=1), price=101, source="other")
        await _history(session, before - timedelta(days=1), price=102, product_unknown=True)
        await _history(session, before - timedelta(days=1), price=104, is_gold=True)
        await _history(session, before - timedelta(days=1), price=105, version_suffix="Deluxe")
        await _history(session, before - timedelta(days=1), price=103, is_bundle=True)
        await _history(session, before - timedelta(days=1), price=0)
        await _history(session, before - timedelta(days=1), price=-10)
        await _history(session, before, price=500)
        await session.commit()
        result = await history_context(session, [
            {"appid": 930001, "region": "us", "subId": 1, "currency": "usd"},
            {"appid": 930002, "region": "US", "subId": 1, "currency": "USD"},
        ], before=before)
    ctx = result[(930001, "US", 1, "USD")]
    assert (ctx["previousPrice"], ctx["baselineLow"], ctx["points"]) == (900, 900, 2)
    assert ctx["firstAt"] == (before - timedelta(days=5)).isoformat()
    assert ctx["lastAt"] == (before - timedelta(days=2)).isoformat()
    assert ctx["complete"] is False
    assert result[(930002, "US", 1, "USD")]["baselineLow"] is None


@pytest.mark.asyncio
async def test_history_context_uses_each_observation_window_and_excludes_itself():
    from app.domains.games.price_comparison import history_context

    before = datetime(2026, 10, 30, 12)
    async with get_session_factory()() as session:
        for appid in (930001, 930002):
            await _history(session, before - timedelta(days=91), appid=appid, price=100)
            await _history(session, before - timedelta(days=90), appid=appid, price=200)
            await _history(session, before - timedelta(days=1), appid=appid, price=300)
            await _history(session, before, appid=appid, price=50)
        await session.commit()
        result = await history_context(session, [
            {"appid": 930001, "region": "US", "subId": 1, "currency": "USD"},
            {"appid": 930002, "region": "US", "subId": 1, "currency": "USD",
             "observedAt": before - timedelta(days=1)},
        ], before=before, window_days=90)
    assert result[(930001, "US", 1, "USD")]["baselineLow"] == 200
    assert result[(930001, "US", 1, "USD")]["points"] == 2
    assert result[(930002, "US", 1, "USD")]["baselineLow"] == 100
    assert result[(930002, "US", 1, "USD")]["previousPrice"] == 200


@pytest.mark.asyncio
async def test_community_without_steam_identity_cannot_establish_baseline():
    from app.domains.games.price_comparison import history_context

    before = datetime(2026, 10, 30, 12)
    async with get_session_factory()() as session:
        await _history(session, before - timedelta(days=1), source="community")
        await session.commit()
        result = await history_context(session, [
            {"appid": 930001, "region": "US", "subId": 1, "currency": "USD"},
        ], before=before)
    assert result[(930001, "US", 1, "USD")]["points"] == 0


@pytest.mark.asyncio
async def test_invalid_comparison_unit_has_no_baseline():
    from app.domains.games.price_comparison import history_context

    async with get_session_factory()() as session:
        result = await history_context(session, [
            {"appid": 930001, "region": "US", "subId": 0, "currency": "USD"},
            {"appid": 930001, "region": "US", "subId": 1, "currency": ""},
        ], before=datetime.now())
    assert result == {}


@pytest.mark.asyncio
async def test_current_known_accepts_delta_refresh_but_rejects_failed_or_changed_observation():
    from app.domains.games.price_comparison import history_context

    before = datetime(2026, 10, 30, 12)
    async with get_session_factory()() as session:
        for appid in (930001, 930002, 930003):
            await _history(session, before - timedelta(days=2), appid=appid)
            session.add(GameCurrentPrice(
                appid=appid, region_code="US", sub_id=1, currency="USD", price=1000,
                price_status="ok", attempt_outcome="failed" if appid == 930002 else "success",
                steam_answer="ok", updated_at=before, last_success_at=before,
            ))
        await _history(session, before - timedelta(days=1), appid=930003, price=1500)
        await session.commit()
        result = await history_context(session, [
            {"appid": a, "region": "US", "subId": 1, "currency": "USD",
             "price": 1000, "observedAt": before}
            for a in (930001, 930002, 930003)
        ], before=before)
    assert result[(930001, "US", 1, "USD")]["currentKnown"] is True
    observation_id = result[(930001, "US", 1, "USD")]["observationId"]
    assert isinstance(observation_id, int)
    assert result[(930002, "US", 1, "USD")]["currentKnown"] is False
    assert result[(930002, "US", 1, "USD")]["observationId"] is None
    assert result[(930003, "US", 1, "USD")]["currentKnown"] is False
    async with get_session_factory()() as session:
        current = await session.get(GameCurrentPrice, (930001, "US"))
        current.updated_at = current.last_success_at = before + timedelta(hours=1)
        await session.commit()
        refreshed = await history_context(session, [{"appid": 930001, "region": "US",
            "subId": 1, "currency": "USD", "price": 1000}], before=current.updated_at)
    assert refreshed[(930001, "US", 1, "USD")]["observationId"] == observation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("window_days,minimum", [(90, 400), (180, 300), (365, 200), (None, 100)])
async def test_supported_windows_share_one_batch_query(window_days, minimum):
    from sqlalchemy import event
    from app.domains.games.price_comparison import history_context

    before = datetime(2026, 10, 30, 12)
    async with get_session_factory()() as session:
        for days, price in ((400, 100), (300, 200), (120, 300), (30, 400)):
            await _history(session, before - timedelta(days=days), price=price)
        await session.commit()
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        engine = session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            result = await history_context(session, [
                {"appid": appid, "region": "US", "subId": 1, "currency": "USD"}
                for appid in range(930001, 930101)
            ], before=before, window_days=window_days)
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1
    assert len(result) == 100
    assert result[(930001, "US", 1, "USD")]["baselineLow"] == minimum
