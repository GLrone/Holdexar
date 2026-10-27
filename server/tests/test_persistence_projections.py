"""派生汇总从现有事实读取，变更与回滚不依赖第二次同步。"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError

from app.core.database import get_session_factory, init_db, write_session
from app.domains.bills import service as bills
from app.domains.bills.models import BillGameTx, BillImport
from app.domains.crawl.cycle import PriceCycle, _stats_of
from app.domains.wishlist import service as wishlist
from app.domains.wishlist.models import TrackedAccount, WishlistItem


@pytest.mark.asyncio
async def test_bill_summaries_share_transactions_and_rollback():
    async with write_session() as session:
        session.add_all([BillImport(id=1, nickname="populated"), BillImport(id=2, nickname="empty")])
        session.add_all([
            BillGameTx(import_id=1, date="2026-01-01", tx_type="购买", currency="CNY",
                       amount=30, sign=1, cny_fen=3000),
            BillGameTx(import_id=1, date="2026-01-02", tx_type="退款", currency="CNY",
                       amount=10, sign=-1, cny_fen=-1000, is_refund=True),
            BillGameTx(import_id=1, date="2026-01-03", tx_type="购买", currency="USD",
                       amount=1, sign=1, cny_fen=None),
        ])
        await session.commit()
    rows = await bills.list_imports()
    assert [r["id"] for r in rows] == [2, 1]
    assert {k: rows[0][k] for k in ("gameNetFen", "orders", "fxMissing")} == {
        "gameNetFen": 0, "orders": 0, "fxMissing": 0,
    }
    assert {k: rows[1][k] for k in ("gameNetFen", "gameSpendFen", "gameRefundFen", "orders", "fxMissing")} == {
        "gameNetFen": 2000, "gameSpendFen": 3000, "gameRefundFen": 1000, "orders": 2, "fxMissing": 1,
    }
    async with write_session() as session:
        await session.execute(update(BillGameTx).where(BillGameTx.currency == "USD").values(cny_fen=700))
        await session.rollback()
    assert (await bills.overview(1))["summary"]["fxMissing"] == 1
    async with write_session() as session:
        await session.execute(update(BillGameTx).where(BillGameTx.currency == "USD").values(cny_fen=700))
        await session.commit()
    assert (await bills.list_imports())[1]["gameNetFen"] == 2700
    assert (await bills.overview(1))["summary"]["fxMissing"] == 0


@pytest.mark.asyncio
async def test_account_counts_share_active_members_without_count_sync(monkeypatch):
    async def no_family():
        return {}

    monkeypatch.setattr(wishlist, "_family_personas", no_family)
    async with write_session() as session:
        session.add_all([
            TrackedAccount(steamid="76561198000000001", persona_name="a", created_at=datetime(2026, 1, 1)),
            TrackedAccount(steamid="76561198000000002", persona_name="b", created_at=datetime(2026, 1, 2)),
            TrackedAccount(steamid="76561198000000003", persona_name="c", created_at=datetime(2026, 1, 3)),
            WishlistItem(steamid="76561198000000001", appid=620, active=True, owned=True),
            WishlistItem(steamid="76561198000000001", appid=570, active=True, owned=False, wishlisted=True),
            WishlistItem(steamid="76561198000000001", appid=440, active=False, owned=True),
            WishlistItem(steamid="76561198000000002", appid=620, active=True, owned=False),
        ])
        await session.commit()
    rows = {r["steamid"]: r for r in await wishlist.list_accounts()}
    assert [(rows[f"7656119800000000{i}"]["itemCount"], rows[f"7656119800000000{i}"]["ownedCount"]) for i in (1, 2, 3)] == [(2, 1), (1, 0), (0, 0)]
    async with write_session() as session:
        await session.execute(update(WishlistItem).where(WishlistItem.appid == 620).values(active=False))
        await session.commit()
    rows = {r["steamid"]: r for r in await wishlist.list_accounts()}
    assert (rows["76561198000000001"]["itemCount"], rows["76561198000000001"]["ownedCount"]) == (1, 0)
    assert rows["76561198000000002"]["itemCount"] == 0


@pytest.mark.asyncio
async def test_cycle_list_batches_job_membership_and_keeps_order():
    from sqlalchemy import event
    from app.core.database import get_engine
    from app.domains.crawl.cycle import list_cycles
    from app.domains.crawl.models import CrawlJob

    async with write_session() as session:
        session.add_all([PriceCycle(id=i, kind="manual") for i in (1, 2, 3)])
        session.add_all([
            CrawlJob(id=3, cycle_id=3, kind="repair", status="done"),
            CrawlJob(id=1, cycle_id=2, kind="batch", status="failed"),
            CrawlJob(id=2, cycle_id=3, kind="manual", status="pending"),
            CrawlJob(id=4, cycle_id=1, kind="scheduled", status="done"),
            CrawlJob(id=5, cycle_id=None, kind="manual", status="done"),
        ])
        await session.commit()
    sql = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        sql.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        rows = await list_cycles(2)
        assert [r["id"] for r in rows] == [3, 2]
        assert [j["id"] for j in rows[0]["jobs"]] == [2, 3]
        assert rows[1]["jobs"] == [{"id": 1, "kind": "batch", "status": "failed"}]
        assert len(sql) == 2
        sql.clear()
        assert await list_cycles(0) == []
        assert len(sql) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_projection_columns_stay_absent_after_restart(tmp_data_dir):
    await init_db()
    await init_db()
    async with get_session_factory()() as session:
        for table, retired in (
            ("bill_imports", {"game_net_fen", "game_spend_fen", "game_refund_fen", "orders", "fx_missing"}),
            ("tracked_accounts", {"item_count"}),
            ("price_cycles", {"duration_seconds", "stage_ms_json", "coverage", "coverage_confirmed"}),
            ("achievement_games", {"platinum"}),
            ("challenge_progress", {"target"}),
            ("agent_sessions", {"title", "context_snapshot", "context_version", "context_updated_at"}),
        ):
            columns = {r[1] for r in await session.execute(text(f"PRAGMA table_info({table})"))}
            assert not retired & columns


def test_cycle_timings_use_recorded_stage_boundaries():
    start = datetime(2026, 1, 1)
    cycle = PriceCycle(units_expected=1, units_ok=1, units_locked=0, started_at=start,
                       running_at=start + timedelta(milliseconds=200),
                       finalizing_at=start + timedelta(milliseconds=1000),
                       finished_at=start + timedelta(milliseconds=1200))
    result = _stats_of(cycle)
    assert result["durationSeconds"] == 1.2
    assert result["stageMs"] == {"planning": 200, "running": 800, "repairing": 0, "finalizing": 200}
    cycle.units_expected = None
    assert _stats_of(cycle) is None


async def _add_legacy_projection_columns(session):
    for table, names in (
        ("bill_imports", ("game_net_fen", "game_spend_fen", "game_refund_fen", "orders", "fx_missing")),
        ("tracked_accounts", ("item_count",)),
    ):
        for name in names:
            await session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER DEFAULT 999"))
    await session.execute(text("PRAGMA user_version=25"))


@pytest.mark.asyncio
async def test_v25_upgrade_keeps_identity_transactions_and_user_metadata():
    async with write_session() as session:
        session.add(BillImport(id=11, nickname="保留", source_file="keep.json", warnings_json='["保留批注"]'))
        session.add(BillGameTx(import_id=11, date="2026-01-01", tx_type="购买", currency="CNY",
                               amount=1, sign=1, cny_fen=100))
        session.add(TrackedAccount(steamid="76561198000000001", label="保留账户"))
        session.add(WishlistItem(steamid="76561198000000001", appid=620, active=True))
        await session.flush()
        await _add_legacy_projection_columns(session)
        await session.commit()
    await init_db()
    await init_db()
    assert (await bills.list_imports())[0]["gameNetFen"] == 100
    assert (await bills.overview(11))["warnings"] == ["保留批注"]
    async with get_session_factory()() as session:
        assert (await session.get(TrackedAccount, "76561198000000001")).label == "保留账户"
        assert (await session.execute(text("SELECT appid FROM wishlist_items"))).scalar_one() == 620


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [
    "CREATE VIEW custom_account AS SELECT * FROM tracked_accounts",
    "CREATE TABLE custom_account (n INTEGER REFERENCES tracked_accounts(item_count))",
])
async def test_projection_upgrade_rolls_back_if_extension_needs_old_columns(extension):
    async with write_session() as session:
        await _add_legacy_projection_columns(session)
        await session.execute(text(extension))
        await session.commit()
    with pytest.raises(RuntimeError, match="扩展|外键"):
        await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 25
        assert "game_net_fen" in {r[1] for r in await session.execute(text("PRAGMA table_info(bill_imports)"))}
        assert "item_count" in {r[1] for r in await session.execute(text("PRAGMA table_info(tracked_accounts)"))}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "mismatch", "extension"])
async def test_v26_cycle_timing_upgrade_keeps_results_or_rolls_back(error):
    from app.domains.crawl.cycle import get
    start = datetime(2026, 1, 1)
    async with write_session() as session:
        session.add(PriceCycle(id=12, kind="manual", status="completed", started_at=start,
                               running_at=start, finalizing_at=start, finished_at=start + timedelta(seconds=1),
                               units_expected=2, units_ok=1, units_locked=0,
                               expected_json={"appids": [620], "regions": ["CN", "US"]}))
        await session.flush()
        await session.execute(text("ALTER TABLE price_cycles ADD COLUMN duration_seconds FLOAT"))
        await session.execute(text("ALTER TABLE price_cycles ADD COLUMN stage_ms_json JSON"))
        await session.execute(text(
            "UPDATE price_cycles SET duration_seconds=:seconds,"
            "stage_ms_json=:stages WHERE id=12"
        ), {"seconds": 9 if error == "mismatch" else 1,
            "stages": json.dumps({"planning": 0, "running": 0, "repairing": 0, "finalizing": 1000})})
        if error == "extension":
            await session.execute(text("CREATE VIEW custom_timing AS SELECT * FROM price_cycles"))
        await session.execute(text("PRAGMA user_version=26"))
        await session.commit()
    if error:
        with pytest.raises(RuntimeError, match="不一致|扩展"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 26
            assert (await session.execute(text("SELECT duration_seconds FROM price_cycles WHERE id=12"))).scalar_one() == (9 if error == "mismatch" else 1)
    else:
        await init_db()
        await init_db()
        result = await get(12)
        assert result["stats"]["coverage"] == 0.5
        assert result["stats"]["durationSeconds"] == 1.0
        assert result["expectedUnits"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "platinum", "target", "unknown_challenge", "extension", "foreign_key"])
async def test_v28_achievement_projection_upgrade_preserves_facts_or_rolls_back(error):
    from app.domains.achievements.models import AchievementGame
    from app.domains.achievements.challenges import ChallengeProgress

    async with write_session() as session:
        session.add(AchievementGame(steamid="76561198000000001", appid=620,
                                    total_achievements=2, unlocked=2, source="manual"))
        session.add(ChallengeProgress(steamid="76561198000000001", challenge_id="platinum_first",
                                      progress=1, status="claimed", claimed_at=datetime(2026, 1, 1)))
        await session.flush()
        await session.execute(text("ALTER TABLE achievement_games ADD COLUMN platinum BOOLEAN DEFAULT 1"))
        await session.execute(text("ALTER TABLE challenge_progress ADD COLUMN target INTEGER DEFAULT 1"))
        if error == "platinum":
            await session.execute(text("UPDATE achievement_games SET platinum=0"))
        if error == "target":
            await session.execute(text("UPDATE challenge_progress SET target=42"))
        if error == "unknown_challenge":
            await session.execute(text("UPDATE challenge_progress SET challenge_id='unknown'"))
        if error == "extension":
            await session.execute(text("CREATE VIEW custom_platinum AS SELECT platinum FROM achievement_games"))
        if error == "foreign_key":
            await session.execute(text("CREATE TABLE custom_target (n INTEGER REFERENCES challenge_progress(target))"))
        await session.execute(text("PRAGMA user_version=28"))
        await session.commit()
    if error:
        with pytest.raises(RuntimeError, match="不一致|扩展|外键"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 28
            assert (await session.execute(text("SELECT platinum FROM achievement_games"))).scalar_one() == (0 if error == "platinum" else 1)
            assert (await session.execute(text("SELECT target FROM challenge_progress"))).scalar_one() == (42 if error == "target" else 1)
    else:
        await init_db()
        await init_db()
        async with get_session_factory()() as session:
            row = await session.get(AchievementGame, ("76561198000000001", 620))
            assert row.platinum is True and row.source == "manual"
            challenge = await session.get(ChallengeProgress, ("76561198000000001", "platinum_first"))
            assert challenge.status == "claimed" and challenge.claimed_at == datetime(2026, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "title", "snapshot", "version", "time", "partial", "extension"])
async def test_v29_unused_session_upgrade_keeps_execution_identity_or_rolls_back(error):
    from app.domains.agent import service as agent
    from app.domains.settings import service as settings

    created = await agent.create_run(prompt="keep event history")
    await settings.set_values({"bills.probe_skipped": True, "bills.sync_snapshot": {"probedSkip": True}, "user.keep": 7})
    async with write_session() as session:
        for name, ddl in (("title", "VARCHAR(200)"), ("context_snapshot", "JSON"),
                          ("context_version", "INTEGER DEFAULT 0"), ("context_updated_at", "DATETIME")):
            if error != "partial" or name != "context_version":
                await session.execute(text(f"ALTER TABLE agent_sessions ADD COLUMN {name} {ddl}"))
        for kind, value in (("title", "title='keep'"), ("snapshot", "context_snapshot='{}'"),
                            ("version", "context_version=1"), ("time", "context_updated_at='2026-01-01'")):
            if error == kind:
                await session.execute(text(f"UPDATE agent_sessions SET {value}"))
        if error == "extension":
            await session.execute(text("CREATE VIEW custom_session AS SELECT title FROM agent_sessions"))
        await session.execute(text("PRAGMA user_version=29"))
        await session.commit()
        before = [list(r) for r in await session.execute(text("SELECT * FROM agent_events ORDER BY id"))]
    if error:
        with pytest.raises(RuntimeError, match="未知|不完整|扩展"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 29
            assert "title" in {r[1] for r in await session.execute(text("PRAGMA table_info(agent_sessions)"))}
        assert await settings.get_value("bills.probe_skipped") is True
    else:
        await init_db()
        await init_db()
        assert await settings.get_value("bills.probe_skipped") is None
        assert await settings.get_value("bills.sync_snapshot") == {"probedSkip": True}
        assert await settings.get_value("user.keep") == 7
        assert (await agent.get_run(created["run_id"]))["session_id"] == created["session_id"]
        async with get_session_factory()() as session:
            assert [list(r) for r in await session.execute(text("SELECT * FROM agent_events ORDER BY id"))] == before
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []


@pytest.mark.parametrize("expected,ok,locked,ratios", [
    (0, 0, 0, (None, None)),
    (32, 1, 0, (0.0312, 0.0312)),
    (3, 1, 1, (0.3333, 0.6667)),
])
def test_cycle_coverage_ratios_share_python_round_and_null(expected, ok, locked, ratios):
    from app.domains.crawl.coverage import coverage_ratios

    assert coverage_ratios(expected, ok, locked) == ratios
    cycle = PriceCycle(units_expected=expected, units_ok=ok, units_locked=locked)
    stats = _stats_of(cycle)
    assert (stats["coverage"], stats["coverageConfirmed"]) == ratios


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    None, "partial", "mismatch", "missing", "no_stats", "zero", "negative", "fractional",
    "extension", "foreign_key", "index",
])
async def test_v31_coverage_projection_upgrade_keeps_results_or_rolls_back(error):
    from app.domains.crawl import coverage as coverage_mod
    from app.domains.crawl.cycle import get

    async with write_session() as session:
        session.add_all([
            PriceCycle(id=21, kind="manual", status="completed", units_expected=32,
                       units_ok=1, units_locked=1, units_failed=0, units_unobserved=30),
            PriceCycle(id=22, kind="manual", status="completed", units_expected=0,
                       units_ok=0, units_locked=0),
            PriceCycle(id=23, kind="manual", status="failed"),
        ])
        await session.flush()
        await session.execute(text("ALTER TABLE price_cycles ADD COLUMN coverage REAL"))
        if error != "partial":
            await session.execute(text("ALTER TABLE price_cycles ADD COLUMN coverage_confirmed REAL"))
            await session.execute(text("UPDATE price_cycles SET coverage=0.0312,coverage_confirmed=0.0625 WHERE id=21"))
        for kind, statement in (
            ("mismatch", "UPDATE price_cycles SET coverage=0.0313 WHERE id=21"),
            ("missing", "UPDATE price_cycles SET units_locked=NULL WHERE id=21"),
            ("no_stats", "UPDATE price_cycles SET coverage=0 WHERE id=23"),
            ("zero", "UPDATE price_cycles SET coverage=0 WHERE id=22"),
            ("negative", "UPDATE price_cycles SET units_expected=-32 WHERE id=21"),
            ("fractional", "UPDATE price_cycles SET units_ok=1.5 WHERE id=21"),
            ("extension", "CREATE VIEW custom_coverage AS SELECT coverage FROM price_cycles"),
            ("foreign_key", "CREATE TABLE custom_coverage (n REAL REFERENCES price_cycles(coverage))"),
            ("index", "CREATE INDEX custom_coverage ON price_cycles(coverage_confirmed)"),
        ):
            if error == kind:
                await session.execute(text(statement))
        await session.execute(text("PRAGMA user_version=31"))
        await session.commit()
        before = [tuple(r) for r in await session.execute(text("SELECT * FROM price_cycles ORDER BY id"))]
    if error:
        with pytest.raises((RuntimeError, OperationalError), match="不完整|不一致|扩展|外键|index"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 31
            assert [tuple(r) for r in await session.execute(text("SELECT * FROM price_cycles ORDER BY id"))] == before
    else:
        await init_db()
        await init_db()
        stats = (await get(21))["stats"]
        frozen = await coverage_mod.cycle_coverage(21)
        assert (stats["coverage"], stats["coverageConfirmed"]) == (0.0312, 0.0625)
        assert (frozen["coverage"], frozen["coverageConfirmed"]) == (0.0312, 0.0625)
        assert (await get(22))["stats"]["coverage"] is None
        assert (await coverage_mod.cycle_coverage(22))["coverageConfirmed"] is None
        assert (await get(23))["stats"] is None
        async with get_session_factory()() as session:
            columns = [r[1] for r in await session.execute(text("PRAGMA table_info(price_cycles)"))]
            assert not {"coverage", "coverage_confirmed"} & set(columns)
            assert [tuple(r) for r in await session.execute(text("SELECT * FROM price_cycles ORDER BY id"))] == [r[:-2] for r in before]


@pytest.mark.asyncio
async def test_cycle_stats_write_and_frozen_read_share_counts_without_price_query():
    from sqlalchemy import event
    from app.core.database import get_engine
    from app.domains.crawl.coverage import cycle_coverage
    from app.domains.crawl.cycle import get, write_stats

    async with write_session() as session:
        session.add(PriceCycle(id=31, kind="manual", status="completed"))
        await session.commit()
    sql = []
    commits = []
    def record(_c, _u, statement, _p, _ctx, _many):
        sql.append(statement)
    engine = get_engine().sync_engine
    def committed(_c):
        commits.append(True)
    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "commit", committed)
    try:
        assert await write_stats(31, {
            "targetsTotal": 1, "targetsDone": 1, "unitsExpected": 3,
            "unitsOk": 1, "unitsLocked": 1, "unitsFailed": 1,
            "unitsMissing": 1, "unitsBlocked": 0, "unitsUnobserved": 0, "staleCount": 0,
        })
        updates = [s for s in sql if s.lstrip().upper().startswith("UPDATE")]
        assert len(updates) == 1 and len(commits) == 1
        assert "coverage" not in updates[0]
        sql.clear()
        stats = (await get(31))["stats"]
        cov = await cycle_coverage(31)
        assert stats["coverage"] == cov["coverage"] == 0.3333
        assert stats["coverageConfirmed"] == cov["coverageConfirmed"] == 0.6667
        assert len(sql) == 2 and all("game_current_prices" not in s for s in sql)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "commit", committed)
