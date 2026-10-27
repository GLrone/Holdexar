import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, insert, select, text, update

from app.core.database import get_session_factory
from app.domains.crawl.events import PriceEvent
from app.domains.notifications import service
from app.domains.notifications.models import NotificationCandidate
from test_notifications import APP, RECIPIENT, _candidates, _cycle, _enable, _seed_event


@pytest.fixture
def db():
    return get_session_factory()


@pytest.fixture
def delivery(monkeypatch):
    from app.domains.crawl import service as crawl_service

    calls = []

    async def send(subject, html, *, recipient=None):
        calls.append({"recipient": recipient, "subject": subject})
        return True, None

    async def config():
        return {"to_addr": RECIPIENT}

    async def scope(*_args, **_kwargs):
        return [APP]

    monkeypatch.setattr(service, "send_mail_ex", send)
    monkeypatch.setattr(service, "smtp_config", config)
    monkeypatch.setattr(crawl_service, "plan_scope_appids", scope)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("watched_only", [False, True])
async def test_dispatch_consumes_more_than_one_event_batch(db, delivery, watched_only):
    await _enable(db)
    cid = await _cycle(db)
    now = datetime.now()
    rows = [dict(cycle_id=cid, appid=APP if watched_only else APP + i,
                 region_code=f"r{i}", event_type="PRICE_DROP", occurred_at=now,
                 created_at=now, current_json={"price": i + 1}) for i in range(1001)]
    if not watched_only:
        rows[0]["occurred_at"] = now - timedelta(seconds=1)
    async with db() as session:
        await session.execute(insert(PriceEvent), rows)
        await session.commit()

    result = await service.dispatch(cid)

    expected = 1001 if watched_only else 1
    assert result["created"] == expected
    assert result["sent"] == expected
    assert len(await _candidates(db)) == expected
    assert delivery == [{"recipient": RECIPIENT,
                         "subject": f"Holdexar 本轮价格更新：{expected} 条变化"}]


@pytest.mark.asyncio
async def test_late_failed_receipt_does_not_reopen_unknown(db, delivery):
    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    await service.dispatch(cid)
    claimed = await service._claim(await service._undelivered())
    assert len(claimed) == 1
    await service._recover_stuck_sending(datetime.now() + timedelta(hours=1))

    await service._settle(claimed, "failed", "late", "ConnectionResetError")

    assert (await _candidates(db))[0].status == "unknown"
    assert await service._undelivered() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_fact", ["missing", "identity", "newer"])
async def test_invalid_old_candidate_is_quarantined_without_sending(db, delivery, bad_fact):
    await _enable(db)
    cid = await _cycle(db)
    eid = await _seed_event(db, cid, "PRICE_DROP")
    now = datetime.now()
    async with db() as session:
        session.add(NotificationCandidate(
            event_id=eid if bad_fact != "missing" else eid + 1000,
            cycle_id=cid, appid=APP + 1 if bad_fact == "identity" else APP,
            region_code="RU", event_type="PRICE_DROP", category="price",
            recipient=RECIPIENT, status="pending", attempts=0,
            created_at=now - timedelta(days=1) if bad_fact == "newer" else now,
            updated_at=now))
        await session.commit()

    await service.dispatch(0)

    row = (await _candidates(db))[0]
    assert row.status == "unknown"
    assert row.reason
    assert delivery == []


@pytest.mark.asyncio
async def test_recipient_change_moves_unsent_and_preserves_completed(db, delivery, monkeypatch):
    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    eid = await _seed_event(db, cid, "PRICE_DROP")
    await service.dispatch(cid)
    original = (await _candidates(db))[0]
    new_recipient = "new@example.com"

    async def config():
        return {"to_addr": new_recipient}

    monkeypatch.setattr(service, "smtp_config", config)
    await _enable(db)
    result = await service.dispatch(cid)
    rows = await _candidates(db)
    assert result["sent"] == 1
    assert [(r.id, r.event_id, r.recipient, r.status) for r in rows] == [
        (original.id, eid, new_recipient, "delivered")]
    assert delivery[0]["recipient"] == new_recipient

    async def another():
        return {"to_addr": "third@example.com"}

    monkeypatch.setattr(service, "smtp_config", another)
    await service.dispatch(cid)
    assert len(delivery) == 1
    assert (await _candidates(db))[0].recipient == new_recipient


@pytest.mark.asyncio
async def test_candidate_and_scan_progress_roll_back_together(db, delivery):
    from app.domains.crawl.cycle import PriceCycle

    await _enable(db)
    cid = await _cycle(db)
    now = datetime.now()
    await service.events_service._persist([dict(
        cycle_id=cid, appid=APP, region_code="CN", event_type="PRICE_DROP",
        occurred_at=now, current_json={}, previous_json=None,
    )])
    async with db() as session:
        await session.execute(text("CREATE TRIGGER reject_candidate BEFORE INSERT ON notification_candidates "
                                   "BEGIN SELECT RAISE(ABORT, 'candidate write failed'); END"))
        await session.commit()
    await service.dispatch(cid)
    async with db() as session:
        progress = (await session.get(PriceCycle, cid)).notification_scan_json
        assert progress["state"] == "scanning" and progress["cursor"] == 0
        await session.execute(text("DROP TRIGGER reject_candidate"))
        await session.commit()
    assert await _candidates(db) == []
    assert await service.resume_event_scans() == 1
    assert len(await _candidates(db)) == 1
    assert delivery == []


@pytest.mark.asyncio
async def test_scope_failure_preserves_pending_scan_and_disabled_history_stays_skipped(db, delivery, monkeypatch):
    from app.domains.crawl.cycle import PriceCycle

    await _enable(db)
    cid = await _cycle(db)
    now = datetime.now()
    event = dict(cycle_id=cid, appid=APP, region_code="CN", event_type="PRICE_DROP",
                 occurred_at=now, current_json={}, previous_json=None)
    await service.events_service._persist([event])
    original = service.crawl_service.plan_scope_appids

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("monitor scope unavailable")

    monkeypatch.setattr(service.crawl_service, "plan_scope_appids", unavailable)
    await service.dispatch(cid)
    async with db() as session:
        assert (await session.get(PriceCycle, cid)).notification_scan_json["state"] == "pending"
    monkeypatch.setattr(service.crawl_service, "plan_scope_appids", original)
    assert await service.resume_event_scans() == 1
    skipped = await _cycle(db)
    await service.events_service._persist([dict(event, cycle_id=skipped)])
    await _enable(db, enabled=False)
    await service.dispatch(skipped)
    await _enable(db)
    assert await service.resume_event_scans() == 0
    async with db() as session:
        assert (await session.get(PriceCycle, skipped)).notification_scan_json["state"] == "done"
    assert len(await _candidates(db)) == 1


@pytest.mark.asyncio
async def test_recipient_collision_keeps_failed_budget_and_history(db, delivery, monkeypatch):
    await _enable(db)
    cid = await _cycle(db)
    eid = await _seed_event(db, cid, "PRICE_DROP")
    now = datetime.now()
    async with db() as session:
        session.add_all([NotificationCandidate(
            event_id=eid, cycle_id=cid, appid=APP, region_code="RU",
            event_type="PRICE_DROP", category="price", recipient=recipient,
            status=status, attempts=attempts, last_error=error,
            created_at=now, updated_at=now)
            for recipient, status, attempts, error in [
                ("old@example.com", "failed", 3, "ConnectionResetError"),
                (RECIPIENT, "pending", 0, None)]])
        await session.commit()

    result = await service.dispatch(cid)

    rows = await _candidates(db)
    assert len(rows) == 1
    assert rows[0].recipient == RECIPIENT
    assert rows[0].status == "failed"
    assert rows[0].attempts == 3
    assert rows[0].last_error == "ConnectionResetError"
    assert rows[0].delivery_history_json[0]["recipient"] == "old@example.com"
    assert result["sent"] == 0 and delivery == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["delivered", "unknown", "sending"])
async def test_recipient_change_does_not_move_possibly_sent_records(db, delivery, status):
    await _enable(db)
    cid = await _cycle(db)
    eid = await _seed_event(db, cid, "PRICE_DROP")
    now = datetime.now()
    async with db() as session:
        session.add(NotificationCandidate(
            event_id=eid, cycle_id=cid, appid=APP, region_code="RU",
            event_type="PRICE_DROP", category="price", recipient="old@example.com",
            status=status, attempts=1, created_at=now, updated_at=now))
        await session.commit()

    await service.dispatch(cid)

    rows = await _candidates(db)
    assert len(rows) == 1
    assert rows[0].recipient == "old@example.com"
    assert rows[0].status == status
    assert delivery == []


@pytest.mark.asyncio
async def test_event_deleted_after_scan_does_not_create_an_orphan_candidate(db, delivery):
    prefs = await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    events = await service.events_service.list_events(cycle_id=cid)
    async with db() as session:
        await session.execute(delete(PriceEvent).where(PriceEvent.cycle_id == cid))
        await session.commit()
    assert await service._create_candidates(cid, events, prefs, {APP}, RECIPIENT) == 0
    assert await _candidates(db) == []


@pytest.mark.asyncio
async def test_claim_uses_latest_saved_recipient_and_freezes_it(db, delivery):
    from app.domains.settings.service import set_value

    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    await service.dispatch(cid)
    await set_value("smtp.to_addr", "latest@example.com")
    claimed = await service._claim(recipient=RECIPIENT)
    assert [c.recipient for c in claimed] == ["latest@example.com"]
    version = claimed[0].updated_at
    await set_value("smtp.to_addr", "after@example.com")
    await service._retarget("after@example.com")
    persisted = (await _candidates(db))[0]
    assert (persisted.recipient, persisted.updated_at, persisted.status) == (
        "latest@example.com", version, "sending")
    assert await service._settle(claimed, "delivered", None) == 1


@pytest.mark.asyncio
async def test_stale_dispatch_recipient_cannot_undo_saved_destination(db, delivery):
    from app.domains.settings.service import set_value

    prefs = await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    events = await service.events_service.list_events(cycle_id=cid)
    await set_value("smtp.to_addr", "saved@example.com")
    assert await service._create_candidates(cid, events, prefs, {APP}, RECIPIENT) == 1
    assert (await _candidates(db))[0].recipient == "saved@example.com"
    await service._retarget(RECIPIENT)
    assert (await _candidates(db))[0].recipient == "saved@example.com"


@pytest.mark.asyncio
async def test_mail_accepted_then_settlement_failure_is_not_sent_again(db, delivery, monkeypatch):
    await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    settle = service._settle

    async def fail_settlement(*_args, **_kwargs):
        raise RuntimeError("injected database settlement failure")

    monkeypatch.setattr(service, "_settle", fail_settlement)
    await service.dispatch(cid)
    assert (await _candidates(db))[0].status == "sending"
    monkeypatch.setattr(service, "_settle", settle)
    await service.dispatch(cid)
    assert len(delivery) == 1
    await service._recover_stuck_sending(datetime.now() + timedelta(hours=1))
    await service.dispatch(cid)
    assert (await _candidates(db))[0].status == "unknown"
    assert len(delivery) == 1


@pytest.mark.asyncio
async def test_smtp_settings_move_unsent_immediately_in_same_transaction(db, delivery):
    from app.domains.alerts.notify import update_smtp_config

    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    await service.dispatch(cid)
    await update_smtp_config(host="localhost", port=465, user="sender@example.com",
                             password="local", to_addr="changed@example.com", use_ssl=True)
    row = (await _candidates(db))[0]
    assert (row.recipient, row.status) == ("changed@example.com", "suppressed")
    assert row.delivery_history_json[0]["recipient"] == RECIPIENT

    from app.core import secretbox
    from app.domains.alerts.notify import smtp_config
    from app.domains.settings.models import AppSetting

    async with db() as session:
        assert secretbox.is_encrypted((await session.get(AppSetting, "smtp.password")).value_json)
    assert (await smtp_config())["password"] == "local"


@pytest.mark.asyncio
async def test_retention_preserves_event_cycle_and_job_until_candidate_expires(db):
    from app.domains.crawl import retention
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.crawl.models import CrawlJob
    from app.domains.crawl.service import prune_crawl_jobs
    from app.domains.notifications.retention import prune_notification_candidates

    now = datetime.now()
    old = now - timedelta(days=400)
    cid = await _cycle(db)
    eid = await _seed_event(db, cid, "PRICE_DROP")
    async with db() as session:
        job = CrawlJob(kind="manual", status="done", cycle_id=cid,
                       started_at=old, finished_at=old)
        session.add(job)
        await session.flush()
        jid = job.id
        negative = PriceEvent(cycle_id=-jid, appid=APP, event_type="FREE_PROMO",
                              created_at=old, occurred_at=old)
        session.add(negative)
        await session.flush()
        session.add_all([NotificationCandidate(
            event_id=event_id, cycle_id=source, appid=APP, event_type=event_type,
            region_code=region, category="price", recipient=RECIPIENT,
            status="delivered", attempts=1, created_at=old, updated_at=now)
            for event_id, source, event_type, region in [
                (eid, cid, "PRICE_DROP", "RU"), (negative.id, -jid, "FREE_PROMO", None)]])
        await session.execute(update(PriceEvent).where(PriceEvent.id == eid).values(
            created_at=old, occurred_at=old))
        await session.execute(update(PriceCycle).where(PriceCycle.id == cid).values(
            started_at=old, finished_at=old))
        await session.commit()
    assert (await retention.prune_price_cycles(now=now))["events_deleted"] == 0
    assert await prune_crawl_jobs() == 0
    async with db() as session:
        assert await session.get(PriceCycle, cid) is not None
    await prune_notification_candidates(now=now + timedelta(days=366))
    assert (await retention.prune_price_cycles(now=now))["events_deleted"] == 2
    assert await prune_crawl_jobs() == 1
    assert (await retention.prune_price_cycles(now=now))["rows_deleted"] == 1
    assert (await retention.prune_price_cycles(now=now))["rows_deleted"] == 0


@pytest.mark.asyncio
async def test_retention_preserves_incomplete_job_units_and_its_cycle(db):
    from app.domains.crawl import retention
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.crawl.models import CrawlJob
    from app.domains.crawl.service import prune_crawl_jobs

    old = datetime.now() - timedelta(days=400)
    cid = await _cycle(db)
    async with db() as session:
        job = CrawlJob(kind="manual", status="failed", cycle_id=cid,
                       finished_at=old,
                       stats_json={"targets": {"appids": [APP], "regions": ["CN"]}})
        session.add(job)
        await session.execute(update(PriceCycle).where(PriceCycle.id == cid).values(started_at=old))
        await session.commit()
        jid = job.id
    assert await prune_crawl_jobs() == 0
    assert (await retention.prune_price_cycles())["rows_deleted"] == 0
    async with db() as session:
        await session.execute(update(CrawlJob).where(CrawlJob.id == jid).values(
            stats_json={"targets": {"appids": [], "regions": []}}))
        await session.commit()
    assert await prune_crawl_jobs() == 1
    assert (await retention.prune_price_cycles())["rows_deleted"] == 1


@pytest.mark.asyncio
async def test_event_pagination_is_globally_ordered_and_keeps_starting_upper_bound(db):
    cid = await _cycle(db)
    watched = {APP + i for i in range(501)}
    now = datetime.now()
    async with db() as session:
        await session.execute(insert(PriceEvent), [dict(
            cycle_id=cid, appid=appid, event_type="PRICE_DROP", created_at=now, occurred_at=now)
            for appid in sorted(watched, reverse=True)])
        await session.commit()
    pages = service.events_service.notification_batches(cid, watched, batch=100)
    first = await anext(pages)
    late = await _seed_event(db, cid, "FREE_PROMO", appid=APP)
    received = [*first]
    async for page in pages:
        received.extend(page)
    identities = [row["id"] for row in received]
    assert identities == sorted(identities)
    assert len(set(identities)) == 501
    assert late not in identities


@pytest.mark.asyncio
async def test_candidate_read_projection_preserves_merged_delivery_history(db, delivery):
    from app.domains.alerts.notify import update_smtp_config
    from app.domains.notifications.router import list_candidates

    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    await service.dispatch(cid)
    await update_smtp_config(host="localhost", port=465, user="sender@example.com",
                             password="never-project-this", to_addr="changed@example.com", use_ssl=True)
    response = await list_candidates(limit=10)
    item = response["items"][0]
    assert item["recipient"] == "changed@example.com"
    assert item["deliveryHistory"][0]["recipient"] == RECIPIENT
    assert item["deliveryHistory"][0]["attempts"] == 0
    assert item["deliveryHistory"][0]["at"]
    assert "never-project-this" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation", ["_digest_rows", "digest_mail_html"])
async def test_preparation_failure_releases_claim_without_spending_attempt(db, delivery, monkeypatch, preparation):
    await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP")
    original = getattr(service, preparation)

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected digest preparation failure")

    monkeypatch.setattr(service, preparation, fail)
    await service.dispatch(cid)
    row = (await _candidates(db))[0]
    assert (row.status, row.attempts) == ("pending", 0)
    assert delivery == []
    monkeypatch.setattr(service, preparation, original)
    result = await service.dispatch(cid)
    assert result["sent"] == 1
    assert len(delivery) == 1
    assert (await _candidates(db))[0].attempts == 1


@pytest.mark.asyncio
async def test_failed_second_page_resumes_on_later_cycle_with_frozen_eligibility(db, delivery, monkeypatch):
    await _enable(db, categories={"price": True, "availability": False})
    cid = await _cycle(db)
    now = datetime.now()
    events = [dict(cycle_id=cid, appid=APP, region_code=f"r{i}", event_type="PRICE_DROP",
                   occurred_at=now, previous_json=None, current_json={"price": i + 1})
              for i in range(1001)]
    events.extend([dict(cycle_id=cid, appid=appid, region_code="skip", event_type=event_type,
                        occurred_at=now, previous_json=None, current_json={})
                   for appid, event_type in [(APP, "REGION_LOCKED"), (APP + 1, "PRICE_DROP")]])
    await service.events_service._persist(events)
    original = service._create_candidates
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second candidate page failed")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_create_candidates", fail_second)
    await service.dispatch(cid)
    assert len(await _candidates(db)) == 1000
    assert delivery == []
    await _enable(db)

    async def changed_scope(*_args, **_kwargs):
        return [APP, APP + 1]

    monkeypatch.setattr(service.crawl_service, "plan_scope_appids", changed_scope)
    monkeypatch.setattr(service, "_create_candidates", original)
    result = await service.dispatch(await _cycle(db))
    candidates = await _candidates(db)
    assert result["sent"] == 1001
    assert len(candidates) == 1001
    assert all(c.appid == APP and c.event_type == "PRICE_DROP" for c in candidates)
    assert len(delivery) == 1


@pytest.mark.asyncio
async def test_scan_tail_resumes_once_with_two_consumers_and_replay_does_not_reopen(db, delivery, monkeypatch):
    from app.domains.crawl.cycle import PriceCycle

    await _enable(db, quietEnabled=True, quietStart="00:00", quietEnd="23:59")
    cid = await _cycle(db)
    event = dict(cycle_id=cid, appid=APP, region_code="CN", event_type="PRICE_DROP",
                 occurred_at=datetime.now(), current_json={}, previous_json=None)
    await service.events_service._persist([event])
    original = service.events_service.notification_batches
    tail = dict(event, region_code="RU")

    async def append_tail(*args, **kwargs):
        async for page in original(*args, **kwargs):
            yield page
            await service.events_service._persist([tail])

    monkeypatch.setattr(service.events_service, "notification_batches", append_tail)
    await service.dispatch(cid)
    async with db() as session:
        assert (await session.get(PriceCycle, cid)).notification_scan_json["state"] == "pending"
    assert len(await _candidates(db)) == 1
    monkeypatch.setattr(service.events_service, "notification_batches", original)
    created = await asyncio.gather(service.resume_event_scans(), service.resume_event_scans())
    assert sum(created) == 1
    assert len(await _candidates(db)) == 2
    assert await service.events_service._persist([event, tail]) == []
    async with db() as session:
        assert (await session.get(PriceCycle, cid)).notification_scan_json == {"state": "done", "cursor": 2}


@pytest.mark.asyncio
async def test_pending_scan_protects_old_facts_and_does_not_backfill_legacy(db, delivery):
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.crawl.retention import prune_price_cycles

    await _enable(db)
    cid, legacy = await _cycle(db), await _cycle(db)
    event = dict(cycle_id=cid, appid=APP, region_code="CN", event_type="PRICE_DROP",
                 occurred_at=datetime.now() - timedelta(days=400), current_json={}, previous_json=None)
    await service.events_service._persist([event])
    await _seed_event(db, legacy, "PRICE_DROP")
    async with db() as session:
        (await session.get(PriceCycle, cid)).started_at = event["occurred_at"]
        await session.commit()
    await prune_price_cycles()
    async with db() as session:
        assert await session.get(PriceCycle, cid) is not None
        assert len((await session.execute(select(PriceEvent).where(PriceEvent.cycle_id == cid))).scalars().all()) == 1
    assert await service.resume_event_scans() == 1
    assert {row.cycle_id for row in await _candidates(db)} == {cid}


@pytest.mark.asyncio
@pytest.mark.parametrize("old_setting", ["disabled", "no_recipient"])
async def test_first_scan_does_not_mix_new_settings_with_old_empty_scope(db, delivery, monkeypatch, old_setting):
    from app.domains.crawl.cycle import PriceCycle

    await _enable(db, enabled=old_setting != "disabled")
    cid = await _cycle(db)
    await service.events_service._persist([dict(
        cycle_id=cid, appid=APP, region_code="CN", event_type="PRICE_DROP",
        occurred_at=datetime.now(), current_json={}, previous_json=None,
    )])
    original = service._open_scan
    recipient = "" if old_setting == "no_recipient" else RECIPIENT

    async def config():
        return {"to_addr": recipient}

    async def enable_before_freeze(*args, **kwargs):
        nonlocal recipient
        recipient = RECIPIENT
        await _enable(db)
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "smtp_config", config)
    monkeypatch.setattr(service, "_open_scan", enable_before_freeze)
    await service.dispatch(cid)
    assert await _candidates(db) == []
    async with db() as session:
        assert (await session.get(PriceCycle, cid)).notification_scan_json["state"] == "done"


@pytest.mark.asyncio
async def test_cleared_recipient_during_scan_keeps_page_pending(db, delivery, monkeypatch):
    from app.domains.alerts.notify import update_smtp_config
    from app.domains.crawl.cycle import PriceCycle
    from app.domains.settings.service import set_value

    await _enable(db)
    cid = await _cycle(db)
    await service.events_service._persist([dict(
        cycle_id=cid, appid=APP, region_code=f"r{i}", event_type="PRICE_DROP",
        occurred_at=datetime.now(), current_json={}, previous_json=None,
    ) for i in range(1001)])
    original = service._create_candidates
    calls = 0

    async def clear_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            await set_value("smtp.to_addr", "")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_create_candidates", clear_on_second)
    await service.dispatch(cid)
    assert len(await _candidates(db)) == 1000 and delivery == []
    async with db() as session:
        progress = (await session.get(PriceCycle, cid)).notification_scan_json
        assert progress["state"] == "scanning" and progress["cursor"] == 1000
    await update_smtp_config(host="localhost", port=465, user="sender@example.com", password="local",
                             to_addr="resumed@example.com", use_ssl=True)
    monkeypatch.setattr(service, "_create_candidates", original)
    assert await service.resume_event_scans() == 1
    assert {row.recipient for row in await _candidates(db)} == {"resumed@example.com"}
