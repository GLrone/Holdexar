"""资格和排序直接消费来源，生命周期时刻仍由原领域事务记录。"""
from datetime import datetime

import pytest
from sqlalchemy import event, select, text, update

from app.core.database import get_engine, get_session_factory, init_db, write_session
from app.domains.monitoring import service
from app.domains.monitoring.models import MonitorExclusion, MonitorSource, MonitorTarget


@pytest.mark.asyncio
async def test_qualification_and_priority_follow_facts_without_target_state_write():
    original = datetime(2026, 1, 1)
    async with write_session() as session:
        session.add(MonitorTarget(target_type="game", target_id=620, created_at=original, activated_at=original))
        session.add(MonitorSource(target_type="game", target_id=620, source="custom", priority=777, active=True))
        await session.commit()
    assert await service.state_of("game", 620) == "active"
    assert await service.priority_map("game", [620]) == {620: 777}
    async with write_session() as session:
        session.add(MonitorExclusion(target_type="game", target_id=620, active=True, created_at=original))
        await session.commit()
    assert await service.state_of("game", 620) == "excluded"
    assert await service.active_ids("game") == []
    assert await service.priority_map("game", [620]) == {620: 777}
    async with write_session() as session:
        await session.execute(update(MonitorSource).values(active=False))
        await session.execute(update(MonitorExclusion).values(active=False))
        await session.commit()
    assert await service.state_of("game", 620) == "released"
    assert await service.priority_map("game", [620]) == {620: 0}
    assert await service.state_of("game", 570) is None
    async with get_session_factory()() as session:
        row = (await session.execute(select(MonitorTarget))).scalar_one()
        assert row.created_at == row.activated_at == original


@pytest.mark.asyncio
async def test_unchanged_sources_do_not_recompute_each_target_or_write():
    desired = {i: {"manual"} for i in range(100, 120)}
    await service.sync_sources("game", desired)
    statements = []

    def record(_c, _u, statement, _params, _ctx, _many):
        statements.append(statement.lstrip().split()[0].upper())

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await service.sync_sources("game", desired) == {i: "active" for i in desired}
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements.count("SELECT") <= 4
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE"}


@pytest.mark.asyncio
async def test_stats_reuses_type_totals_for_global_totals():
    async with write_session() as session:
        session.add_all([MonitorTarget(target_type=t, target_id=i) for t, i in (
            ("game", 620), ("game", 570), ("game", 600), ("bundle", 10),
        )])
        session.add_all([MonitorSource(target_type=t, target_id=i, source=s, priority=p, active=True)
                         for t, i, s, p in (("game", 620, "manual", 60), ("game", 620, "favorite", 100),
                                            ("game", 600, "owned", 40), ("bundle", 10, "import", 30))])
        session.add(MonitorExclusion(target_type="game", target_id=600, active=True))
        await session.commit()
    statements = []

    def record(_c, _u, statement, _params, _ctx, _many):
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        stats = await service.stats()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert stats["monitoring"] == {"active": 2, "released": 1, "excluded": 1, "total": 4}
    assert stats["byType"] == {"game/active": 1, "game/released": 1, "game/excluded": 1, "bundle/active": 1}
    assert len(statements) == 5


@pytest.mark.asyncio
async def test_projection_retirement_stays_retired_after_restart(tmp_data_dir):
    await init_db()
    await init_db()
    async with get_session_factory()() as session:
        columns = {r[1] for r in await session.execute(text("PRAGMA table_info(monitor_targets)"))}
    assert not columns & {"state", "priority"}
    assert {"id", "target_type", "target_id", "created_at", "updated_at", "activated_at", "released_at", "excluded_at"} <= columns


@pytest.mark.asyncio
async def test_sync_failure_rolls_back_source_identity_and_lifecycle():
    engine = get_engine().sync_engine

    def fail(_c, _u, statement, _params, _ctx, _many):
        if statement.lstrip().upper().startswith("UPDATE MONITOR_TARGETS"):
            raise RuntimeError("lifecycle write failed")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="lifecycle write failed"):
            await service.sync_sources("game", {620: {"manual"}})
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert await service.state_of("game", 620) is None
    assert await service.sources_of("game", 620) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["track", "follow", "bundle_follow"])
@pytest.mark.parametrize("fail_source", [False, True])
async def test_explicit_restore_and_source_change_share_one_transaction(action, fail_source):
    from app.domains.wishlist.follows import follow
    from app.domains.bundles.service import follow_bundle

    target_type = "bundle" if action == "bundle_follow" else "game"
    await service.set_exclusion(target_type, 620, True, "keep on failure")
    engine = get_engine().sync_engine
    commits = []

    def record_commit(_connection):
        commits.append(True)

    def fail(_c, _u, statement, _params, _ctx, _many):
        if fail_source and statement.lstrip().upper().startswith("INSERT INTO MONITOR_SOURCES"):
            raise RuntimeError("source write failed")

    event.listen(engine, "before_cursor_execute", fail)
    event.listen(engine, "commit", record_commit)
    try:
        call = (follow_bundle(620) if action == "bundle_follow" else
                follow(620) if action == "follow" else service.track("game", 620))
        if fail_source:
            with pytest.raises(RuntimeError, match="source write failed"):
                await call
        else:
            await call
    finally:
        event.remove(engine, "before_cursor_execute", fail)
        event.remove(engine, "commit", record_commit)
    assert len(commits) == (0 if fail_source else 1)
    assert await service.is_excluded(target_type, 620) == fail_source
    assert await service.state_of(target_type, 620) == ("excluded" if fail_source else "active")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "mismatch", "extension", "foreign_key", "index", "partial",
                                  "unique", "partial_index", "descending", "collation", "other_table"])
async def test_v27_projection_upgrade_preserves_lifecycle_or_rolls_back(error):
    original = datetime(2026, 1, 1)
    async with write_session() as session:
        session.add(MonitorTarget(id=123, target_type="game", target_id=620, created_at=original,
                                  updated_at=original, activated_at=original, released_at=original, excluded_at=original))
        session.add(MonitorSource(target_type="game", target_id=620, source="custom", priority=777, active=True))
        await session.flush()
        await session.execute(text("ALTER TABLE monitor_targets ADD COLUMN state TEXT DEFAULT 'active'"))
        if error != "partial":
            await session.execute(text("ALTER TABLE monitor_targets ADD COLUMN priority INTEGER DEFAULT 777"))
            await session.execute(text("CREATE INDEX ix_monitor_target_state ON monitor_targets(target_type,state)"))
            await session.execute(text("CREATE INDEX ix_monitor_target_order ON monitor_targets(state,priority,target_id)"))
        if error == "mismatch":
            await session.execute(text("UPDATE monitor_targets SET state='excluded'"))
        elif error == "extension":
            await session.execute(text("CREATE VIEW custom_monitor AS SELECT * FROM monitor_targets"))
        elif error == "foreign_key":
            await session.execute(text("CREATE TABLE custom_monitor (state TEXT REFERENCES monitor_targets(state))"))
        elif error == "index":
            await session.execute(text("DROP INDEX ix_monitor_target_order"))
            await session.execute(text("CREATE INDEX ix_monitor_target_order ON monitor_targets(created_at)"))
        elif error in {"unique", "partial_index", "descending", "collation", "other_table"}:
            await session.execute(text("DROP INDEX ix_monitor_target_order"))
            ddls = {
                "unique": "CREATE UNIQUE INDEX ix_monitor_target_order ON monitor_targets(state,priority,target_id)",
                "partial_index": "CREATE INDEX ix_monitor_target_order ON monitor_targets(state,priority,target_id) WHERE target_type='game'",
                "descending": "CREATE INDEX ix_monitor_target_order ON monitor_targets(state DESC,priority,target_id)",
                "collation": "CREATE INDEX ix_monitor_target_order ON monitor_targets(state COLLATE NOCASE,priority,target_id)",
                "other_table": "CREATE INDEX ix_monitor_target_order ON custom_owner(state,priority,target_id)",
            }
            if error == "other_table":
                await session.execute(text("CREATE TABLE custom_owner(state TEXT,priority INTEGER,target_id INTEGER)"))
            await session.execute(text(ddls[error]))
        await session.execute(text("PRAGMA user_version=27"))
        indexes_before = [tuple(r) for r in await session.execute(text(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE name IN "
            "('ix_monitor_target_state','ix_monitor_target_order') ORDER BY name"
        ))]
        await session.commit()
    if error:
        with pytest.raises(RuntimeError, match="不一致|不完整|扩展|外键|结构未知"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 27
            assert "state" in {r[1] for r in await session.execute(text("PRAGMA table_info(monitor_targets)"))}
            assert [tuple(r) for r in await session.execute(text(
                "SELECT name,tbl_name,sql FROM sqlite_master WHERE name IN "
                "('ix_monitor_target_state','ix_monitor_target_order') ORDER BY name"
            ))] == indexes_before
            if error != "partial":
                assert (await session.execute(text("SELECT count(*) FROM sqlite_master WHERE type='index' AND name='ix_monitor_target_state'"))).scalar_one() == 1
    else:
        await init_db()
        await init_db()
        assert await service.state_of("game", 620) == "active"
        assert await service.priority_map("game", [620]) == {620: 777}
    async with get_session_factory()() as session:
        target = await session.get(MonitorTarget, 123)
        assert (target.created_at, target.updated_at, target.activated_at, target.released_at, target.excluded_at) == (original,) * 5
