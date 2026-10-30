from datetime import datetime

import pytest

from app.domains.notifications import policy


@pytest.mark.asyncio
async def test_partial_preferences_preserve_other_saved_choices():
    await policy.save_prefs({
        "enabled": True, "categories": {"price": False, "low": False},
        "quietEnabled": True, "quietStart": "21:30", "includeDetails": False,
    })
    await policy.save_prefs({"categories": {"availability": False}})
    saved = await policy.load_prefs()
    assert saved["enabled"] is True
    assert saved["categories"]["price"] is False
    assert saved["categories"]["low"] is False
    assert saved["categories"]["availability"] is False
    assert saved["quietStart"] == "21:30"
    assert saved["quietEnabled"] is True
    assert saved["includeDetails"] is False


def test_daily_and_weekly_delivery_times_are_explicit():
    prefs = policy._merge_prefs({"digestTime": "08:00", "digestWeekday": 0})
    monday = datetime(2026, 10, 26, 7, 30)
    assert policy.delivery_due(monday, "daily", prefs) == datetime(2026, 10, 26, 8)
    assert policy.delivery_due(monday.replace(hour=9), "daily", prefs) == datetime(2026, 10, 27, 8)
    assert policy.delivery_due(monday.replace(hour=9), "weekly", prefs) == datetime(2026, 11, 2, 8)


@pytest.mark.asyncio
async def test_type_patch_preserves_frequency_and_other_types():
    await policy.save_prefs({"types": {
        "PRICE_DROP": {"frequency": "weekly"}, "EPIC_FREE": {"enabled": False},
    }})
    await policy.save_prefs({"types": {"PRICE_DROP": {"enabled": False}}})
    saved = await policy.load_prefs()
    assert saved["types"]["PRICE_DROP"] == {"enabled": False, "frequency": "weekly"}
    assert saved["types"]["EPIC_FREE"]["enabled"] is False


@pytest.mark.asyncio
async def test_invalid_frequency_does_not_replace_saved_preferences():
    await policy.save_prefs({"enabled": True})
    with pytest.raises(ValueError):
        await policy.save_prefs({"types": {"PRICE_DROP": {"frequency": "hourly"}}})
    assert (await policy.load_prefs())["enabled"] is True


@pytest.mark.asyncio
async def test_rule_and_content_identity_share_one_delivery_lifecycle(monkeypatch):
    from sqlalchemy import select
    from app.core.database import get_session_factory, write_session
    from app.domains.alerts.models import AlertEvent
    from app.domains.notifications import service
    from app.domains.notifications.models import FactNotice, NotificationCandidate
    from app.domains.settings.service import set_value

    await policy.save_prefs({"enabled": True})
    await set_value("smtp.to_addr", "capture@example.invalid")
    now = datetime.now()
    payload = {"eventType": "RULE_PRICE", "ruleId": 42, "price": 999,
               "currency": "USD", "gameName": "Fixture", "labels": ["指定价格"]}
    async with write_session() as session:
        rule = AlertEvent(id=1, alert_id=42, appid=42, region="US", price=999,
                          triggered_at=now, details_json=payload)
        content = FactNotice(id=1, source="epic_free", kind="free_rotation", fact_key="epic:test",
                             data={"titles": ["Fixture free"]}, occurred_at=now)
        session.add_all([rule, content])
        await session.flush()
        assert await service.enqueue_facts("alert_event", [dict(
            id=1, appid=42, region="US", eventType="RULE_PRICE", payload=payload,
        )], session=session) == 1
        assert await service.enqueue_facts("fact_notice", [dict(
            id=1, appid=0, region=None, eventType="EPIC_FREE",
            payload={"title": "Fixture free", "labels": ["免费游戏"]},
        )], session=session) == 1
        await session.commit()

    calls = []
    async def send(subject, html, *, recipient=None):
        calls.append((subject, html, recipient))
        return True, None

    async def config():
        return {"to_addr": "capture@example.invalid"}

    monkeypatch.setattr(service, "send_mail_ex", send)
    monkeypatch.setattr(service, "smtp_config", config)
    result = await service.dispatch()
    assert result["sent"] == 2
    assert len(calls) == 1
    assert "USD" in calls[0][1] or "$9.99" in calls[0][1]
    assert "Fixture free" in calls[0][1]
    async with get_session_factory()() as session:
        rows = (await session.execute(select(NotificationCandidate))).scalars().all()
    assert len(rows) == 2 and all(row.status == "delivered" for row in rows)
    assert (await service.dispatch())["sent"] == 0


@pytest.mark.asyncio
async def test_due_and_paused_types_are_not_claimed():
    from app.core.database import write_session
    from app.domains.alerts.models import AlertEvent
    from app.domains.notifications import service
    from app.domains.settings.service import set_value

    await policy.save_prefs({"enabled": True, "types": {"RULE_PRICE": {"frequency": "daily"}}})
    await set_value("smtp.to_addr", "capture@example.invalid")
    async with write_session() as session:
        event = AlertEvent(alert_id=9, appid=42, region="US", triggered_at=datetime.now(),
                           details_json={"eventType": "RULE_PRICE", "ruleId": 9})
        session.add(event)
        await session.flush()
        await service.enqueue_facts("alert_event", [dict(
            id=event.id, appid=42, region="US", eventType="RULE_PRICE", payload=event.details_json,
        )], session=session)
        await session.commit()
    assert await service._claim(recipient="capture@example.invalid") == []
