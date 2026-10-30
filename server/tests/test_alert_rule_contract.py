from datetime import datetime, timedelta
import importlib
import importlib.util

import pytest
from sqlalchemy import func, select

from app.domains.alerts import service


def rules():
    assert importlib.util.find_spec("app.domains.alerts.rules") is not None, "shared pure rule evaluator is missing"
    return importlib.import_module("app.domains.alerts.rules")


def current(**updates):
    return dict(appid=42, region="US", currency="USD", subId=7, price=1500,
                originalPrice=3000, discount=50, status="ok", observedAt="2026-10-30T10:00:00",
                cnyFen=10000, positiveRate=9200, reviewCount=200, **updates)


def test_preview_entry_exists():
    assert callable(getattr(service, "preview_rule", None)), "read-only rule preview is missing"


@pytest.mark.parametrize("price,matched", [(1499, True), (1500, False), (1501, False)])
def test_new_low_is_strict(price, matched):
    result = rules().evaluate_rule(dict(targetType="new_low", config={"currency": "USD"}),
                                   current() | {"price": price}, {"baselineLow": 1500, "points": 3})
    assert result["matched"] is matched
    assert result["comparison"]["complete"] is False


def test_new_low_requires_prior_history_and_reports_exclusion():
    result = rules().evaluate_rule(dict(targetType="new_low", config={}), current(), {})
    assert not result["matched"]
    assert "no_baseline" in [item["code"] for item in result["exclusions"]]


def test_native_currency_and_legacy_price_are_distinct():
    evaluate = rules().evaluate_rule
    native = evaluate(dict(targetType="price", targetValue=2000, config={"currency": "USD"}), current(), {})
    legacy = evaluate(dict(targetType="price", targetValue=2000), current(), {})
    mismatch = evaluate(dict(targetType="price", targetValue=2000, config={"currency": "CNY"}), current(), {})
    assert native["matched"] and not legacy["matched"] and not mismatch["matched"]
    assert legacy["observed"]["thresholdBasis"] == "legacy_cny"
    assert "currency_mismatch" in [item["code"] for item in mismatch["exclusions"]]


def test_featured_conditions_are_and_and_missing_data_is_explained():
    config = dict(scope=["wishlist"], minDiscount=60, minReviews=100, minPositiveRate=80,
                  includeOwned=False, missingReviews="exclude", allowFullPrice=False)
    result = rules().evaluate_featured(config, current(), {}, {"sources": ["family_wishlist"], "owned": False})
    assert not result["matched"]
    assert "min_discount" in [item["code"] for item in result["exclusions"]]
    updated = rules().evaluate_featured(config | {"minDiscount": 50}, current(), {}, {"sources": ["family_wishlist"]})
    assert updated["matched"]
    missing = rules().evaluate_featured(config | {"minDiscount": 50}, current() | {"positiveRate": None}, {}, {"sources": ["family_wishlist"]})
    assert not missing["matched"]
    assert "review_data_missing" in [item["code"] for item in missing["exclusions"]]


def test_featured_scope_uses_actual_favorite_and_exclusion_facts():
    evaluate = rules().evaluate_featured
    config = {"scope": ["followed"], "minDiscount": 0, "allowFullPrice": True}
    assert evaluate(config, current(), {}, {"sources": ["favorite"]})["matched"]
    assert not evaluate(config, current(), {}, {"sources": ["manual"]})["matched"]
    assert not evaluate(config, current(), {}, {"sources": ["favorite"], "ignored": True})["matched"]


@pytest.mark.parametrize("config", [{"currency": "USD", "windowDays": 17}, {"currency": "INVALID"}])
def test_invalid_rule_config_is_rejected(config):
    with pytest.raises(ValueError):
        rules().validate_rule(dict(appid=42, region="US", targetType="price", targetValue=1500, config=config))


NOW = datetime(2026, 10, 30, 10)


async def seed_prices():
    from app.core.database import get_session_factory, write_session
    from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory
    from app.domains.monitoring.models import MonitorSource

    async with write_session() as session:
        session.add(Game(appid=42, name="Example", positive_rate=9200, review_count=200))
        session.add(GameCurrentPrice(appid=42, region_code="US", currency="USD", sub_id=7,
                    price=1500, original_price=3000, discount_percent=50, price_status="ok",
                    cny_fen=10000, attempt_outcome="success", steam_answer="ok",
                    last_success_at=NOW, updated_at=NOW))
        session.add_all([GamePriceHistory(appid=42, region_code="US", currency="USD", sub_id=7,
                       price=value, original_price=3000, source="steam", price_status="ok",
                       snapshot_at=at) for at, value in ((NOW - timedelta(days=2), 3000), (NOW, 1500))])
        session.add(MonitorSource(target_type="game", target_id=42, source="favorite", active=True))
        await session.commit()
    return get_session_factory()


@pytest.fixture
def delivery(monkeypatch):
    from app.domains.notifications import policy, service as notifications

    captured = []
    prefs = {"enabled": True, "categories": {}, "manualRegion": "US", "featured": {"scope": ["followed"]}}

    async def load():
        return prefs

    async def enqueue(source_kind, facts, *, session=None):
        assert source_kind == "alert_event" and session is not None
        captured.extend(facts)
        return len(facts)

    async def dispatch(cycle_id=None):
        return {}

    monkeypatch.setattr(policy, "load_prefs", load)
    monkeypatch.setattr(notifications, "enqueue_facts", enqueue, raising=False)
    monkeypatch.setattr(notifications, "dispatch", dispatch)
    return captured, prefs


@pytest.mark.asyncio
async def test_save_matches_existing_observation_and_replay_is_idempotent(delivery):
    from app.domains.alerts.models import AlertEvent

    factory = await seed_prices()
    saved = await service.add_alert(42, "US", "price", 2000, config={"currency": "USD"}, repeat_mode="always")
    preview = await service.preview_rule(saved)
    assert preview["matched"]
    assert await service.check_appids([42]) == []
    async with factory() as session:
        events = (await session.execute(select(AlertEvent))).scalars().all()
    assert len(events) == 1 and len(delivery[0]) == 1
    assert events[0].details_json["evaluation"]["observed"] == preview["observed"]
    assert delivery[0][0]["payload"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_preview_never_creates_rules_or_events(delivery):
    from app.domains.alerts.models import AlertEvent, PriceAlert

    factory = await seed_prices()
    result = await service.preview_rule(dict(appid=42, region="US", targetType="new_low", config={"windowDays": 90}))
    featured = await service.preview_featured({"featured": {"scope": ["followed"], "minDiscount": 50}})
    assert result["matched"] and featured["matched"] == 1
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(PriceAlert))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(AlertEvent))).scalar_one() == 0
    assert delivery[0] == []


@pytest.mark.asyncio
async def test_disabled_notifications_do_not_consume_once_rule(delivery):
    from app.domains.alerts.models import AlertEvent

    factory = await seed_prices()
    delivery[1]["enabled"] = False
    saved = await service.add_alert(42, "US", "price", 2000, config={"currency": "USD"})
    assert saved["active"] and saved["lastTriggeredAt"] is None
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(AlertEvent))).scalar_one() == 0


@pytest.mark.asyncio
async def test_featured_compatibility_entries_share_one_fact(delivery):
    from app.domains.alerts.models import AlertEvent

    factory = await seed_prices()
    assert len(await service.check_new_lows([42])) == 1
    assert await service.check_wishlist_deals() == []
    async with factory() as session:
        assert (await session.execute(select(func.count()).select_from(AlertEvent))).scalar_one() == 1
    assert len(delivery[0]) == 1 and delivery[0][0]["eventType"] == "FEATURED"


@pytest.mark.asyncio
async def test_featured_unchanged_price_refresh_does_not_repeat(delivery):
    from app.core.database import write_session
    from app.domains.games.models import GameCurrentPrice

    await seed_prices()
    assert len(await service.check_featured([42])) == 1
    async with write_session() as session:
        price = (await session.execute(select(GameCurrentPrice).where(GameCurrentPrice.appid == 42))).scalar_one()
        price.updated_at = NOW + timedelta(hours=1)
        price.last_success_at = price.updated_at
        await session.commit()
    assert await service.check_featured([42]) == []


async def seed_referenced_event(status):
    from app.core.database import write_session
    from app.domains.alerts.models import AlertEvent
    from app.domains.notifications.models import NotificationCandidate

    async with write_session() as session:
        event = AlertEvent(alert_id=1, appid=42, region="US", price=1500, triggered_at=NOW)
        session.add(event)
        await session.flush()
        session.add(NotificationCandidate(source_kind="alert_event", event_id=event.id,
                    cycle_id=0, appid=42, region_code="US", event_type="RULE_PRICE",
                    category="rules", recipient="capture@example.test", status=status))
        await session.commit()
        return event.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "delivered", "unknown"])
async def test_candidate_references_protect_rule_facts(status):
    identity = await seed_referenced_event(status)
    with pytest.raises(ValueError, match="通知"):
        await service.delete_event(identity)


@pytest.mark.asyncio
async def test_clear_events_only_removes_unreferenced_records():
    from app.core.database import get_session_factory, write_session
    from app.domains.alerts.models import AlertEvent

    identity = await seed_referenced_event("pending")
    async with write_session() as session:
        session.add(AlertEvent(alert_id=2, appid=42, region="US", price=1000))
        await session.commit()
    assert await service.clear_events() == {"removed": 1, "protected": 1}
    async with get_session_factory()() as session:
        assert (await session.execute(select(AlertEvent.id))).scalars().all() == [identity]


@pytest.mark.asyncio
async def test_featured_mail_delivers_observed_region_price(monkeypatch):
    """精选命中经候选摘要真实发信：正文携带观测区价格与游戏名，重跑不重发。"""
    from app.domains.notifications import policy
    from app.domains.notifications import service as notifications
    from app.domains.settings.service import set_value

    await policy.save_prefs({"enabled": True, "manualRegion": "US",
                             "featured": {"scope": ["followed"]}})
    await set_value("smtp.to_addr", "capture@example.test")
    captured = []

    async def send(subject, html, *, recipient=None):
        captured.append((subject, html, recipient))
        return True, None

    async def config():
        return {"to_addr": "capture@example.test"}

    monkeypatch.setattr(notifications, "send_mail_ex", send)
    monkeypatch.setattr(notifications, "smtp_config", config)
    await seed_prices()
    assert len(await service.check_featured([42])) == 1
    assert len(captured) == 1
    assert "Example" in captured[0][1] and "$15.00" in captured[0][1]
    assert captured[0][2] == "capture@example.test"
    assert (await notifications.dispatch())["sent"] == 0
