import pytest
from sqlalchemy import text

from app.core.database import get_session_factory, init_db, write_session
from app.domains.settings.models import AppSetting
from app.domains.crawl.cycle import PriceCycle


async def _legacy_events(*, referenced=False):
    async with write_session() as session:
        await session.execute(text(
            "CREATE TABLE IF NOT EXISTS orchestration_events "
            "(id INTEGER PRIMARY KEY, ts DATETIME, kind TEXT, level TEXT, "
            "message TEXT, payload_json JSON)"
        ))
        await session.execute(text(
            "INSERT INTO orchestration_events (id, kind, level, message) "
            "VALUES (900001, 'subscription_failed', 'INFO', 'old telemetry')"
        ))
        session.add(AppSetting(key="retirement.sentinel", value_json="keep"))
        if referenced:
            await session.execute(text(
                "CREATE TABLE user_extension (id INTEGER PRIMARY KEY, event_id INTEGER "
                "REFERENCES orchestration_events(id) ON DELETE CASCADE)"
            ))
            await session.execute(text(
                "INSERT INTO user_extension VALUES (1, 900001)"
            ))
        await session.execute(text("PRAGMA user_version=21"))
        await session.commit()


@pytest.mark.asyncio
async def test_new_database_does_not_create_unconsumed_events(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        names = (await session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))).scalars().all()
    assert "orchestration_events" not in names


@pytest.mark.asyncio
async def test_retirement_preserves_business_state_and_stays_retired(tmp_data_dir):
    await init_db()
    await _legacy_events()
    await init_db()
    await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text(
            "SELECT json_extract(value_json, '$') FROM app_settings "
            "WHERE key='retirement.sentinel'"
        ))).scalar_one() == "keep"
        assert (await session.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='orchestration_events'"
        ))).scalar_one() == 0


@pytest.mark.asyncio
async def test_retirement_rejects_unknown_references_without_cascade(tmp_data_dir):
    await init_db()
    await _legacy_events(referenced=True)
    with pytest.raises(RuntimeError, match="外键"):
        await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 21
        assert (await session.execute(text(
            "SELECT event_id FROM user_extension WHERE id=1"
        ))).scalar_one() == 900001
        assert (await session.execute(text(
            "SELECT COUNT(*) FROM orchestration_events WHERE id=900001"
        ))).scalar_one() == 1


@pytest.mark.asyncio
async def test_new_database_has_no_unused_coverage_snapshot(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        columns = (await session.execute(text("PRAGMA table_info(price_cycles)"))).all()
    assert "coverage_json" not in {row[1] for row in columns}


@pytest.mark.asyncio
@pytest.mark.parametrize("ddl", [
    "CREATE VIEW custom_events AS SELECT * FROM orchestration_events",
    "CREATE TRIGGER custom_events AFTER INSERT ON orchestration_events BEGIN SELECT 1; END",
    "CREATE TRIGGER custom_events AFTER INSERT ON app_settings BEGIN SELECT * FROM orchestration_events; END",
])
async def test_event_retirement_rejects_extensions_before_deleting(tmp_data_dir, ddl):
    await init_db()
    await _legacy_events()
    async with write_session() as session:
        await session.execute(text(ddl))
        await session.commit()
    with pytest.raises(RuntimeError, match="扩展"):
        await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 21
        assert (await session.execute(text("SELECT COUNT(*) FROM orchestration_events"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_snapshot_retirement_keeps_frozen_results_and_scan_state(tmp_data_dir):
    await init_db()
    async with write_session() as session:
        columns = (await session.execute(text("PRAGMA table_info(price_cycles)"))).all()
        if "coverage_json" not in {row[1] for row in columns}:
            await session.execute(text("ALTER TABLE price_cycles ADD COLUMN coverage_json JSON"))
        cycle = PriceCycle(kind="manual", status="partial", scope="appids",
                           expected_json={"appids": [620], "regions": ["CN", "US"]},
                           units_expected=2, units_ok=1, units_locked=1,
                           notification_scan_json={"state": "scanning", "cursor": 7})
        session.add(cycle)
        await session.flush()
        cid = cycle.id
        await session.execute(text("UPDATE price_cycles SET coverage_json='{}' WHERE id=:id"),
                              {"id": cid})
        await session.execute(text("PRAGMA user_version=22"))
        await session.commit()
    await init_db()
    await init_db()
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cid)
        columns = (await session.execute(text("PRAGMA table_info(price_cycles)"))).all()
        assert "coverage_json" not in {row[1] for row in columns}
        assert (cycle.units_expected, cycle.units_ok, cycle.units_locked) == (2, 1, 1)
        assert cycle.expected_json == {"appids": [620], "regions": ["CN", "US"]}
        assert cycle.notification_scan_json == {"state": "scanning", "cursor": 7}


@pytest.mark.asyncio
async def test_snapshot_retirement_rolls_back_when_extension_still_reads_it(tmp_data_dir):
    await init_db()
    async with write_session() as session:
        await session.execute(text("ALTER TABLE price_cycles ADD COLUMN coverage_json JSON"))
        await session.execute(text(
            "CREATE VIEW custom_coverage AS SELECT coverage_json FROM price_cycles"
        ))
        await session.execute(text("PRAGMA user_version=22"))
        await session.commit()
    with pytest.raises(Exception, match="custom_coverage"):
        await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 22
        columns = (await session.execute(text("PRAGMA table_info(price_cycles)"))).all()
        assert "coverage_json" in {row[1] for row in columns}
