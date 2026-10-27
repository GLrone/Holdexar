from datetime import datetime, timedelta

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, select, text
from sqlalchemy.schema import CreateTable

from app.core.database import get_session_factory, init_db, write_session
from app.domains.proxypool.models import ProxyNode

OLD_HEALTH = {"exit_ip", "status", "latency_ms", "fail_count", "last_checked_at", "created_at"}


async def _legacy_proxy_tables(*, mismatch=False, referenced=False):
    node_table = ProxyNode.__table__.to_metadata(MetaData())
    if "fingerprint" not in node_table.c:
        node_table.append_column(Column("fingerprint", String(64), nullable=False, unique=True))
    node_table.append_column(Column("legacy_extra", String, server_default="keep,me"))
    cooldown_table = Table("clash_nodes", MetaData(),
                           Column("id", Integer, primary_key=True), Column("subscription_id", Integer),
                           Column("name", String(255)), Column("cooldown_until", DateTime))
    for name in OLD_HEALTH - set(cooldown_table.c.keys()):
        field_type = DateTime if name.endswith("_at") else Integer if name in {"fail_count", "latency_ms"} else String
        cooldown_table.append_column(Column(name, field_type, nullable=True))
    async with write_session() as session:
        await session.execute(text("DROP TABLE proxy_nodes"))
        await session.execute(CreateTable(node_table))
        await session.execute(text("DROP TABLE IF EXISTS clash_nodes"))
        await session.execute(CreateTable(cooldown_table))
        await session.execute(node_table.insert().values(
            id=901, node_id="node-id", fingerprint="other-id" if mismatch else "node-id",
            runtime_name="1|x", proxy_type="http", state="ACTIVE",
        ))
        await session.execute(cooldown_table.insert().values(
            id=902, subscription_id=1, name="x", status="bad", fail_count=12,
            cooldown_until=datetime(2026, 11, 1),
        ))
        await session.execute(text("CREATE INDEX ix_legacy_extra ON proxy_nodes(legacy_extra)"))
        await session.execute(text("CREATE UNIQUE INDEX ux_proxy_nodes_fingerprint ON proxy_nodes(fingerprint)"))
        await session.execute(text("CREATE TABLE extension_audit (value TEXT)"))
        await session.execute(text(
            "CREATE TRIGGER extension_note AFTER UPDATE OF legacy_extra ON proxy_nodes "
            "BEGIN INSERT INTO extension_audit VALUES (NEW.legacy_extra); END"
        ))
        if referenced:
            await session.execute(text(
                "CREATE TABLE extension_ref (node INTEGER REFERENCES proxy_nodes(id) ON DELETE CASCADE)"
            ))
            await session.execute(text("INSERT INTO extension_ref VALUES (901)"))
        await session.execute(text("PRAGMA user_version=23"))
        await session.commit()


@pytest.mark.asyncio
async def test_empty_schema_has_one_node_identity_and_only_cooldown(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        node_columns = {row[1] for row in await session.execute(text("PRAGMA table_info(proxy_nodes)"))}
        cooldown_columns = {row[1] for row in await session.execute(text("PRAGMA table_info(clash_nodes)"))}
    assert "fingerprint" not in node_columns
    assert cooldown_columns == set()
    assert "cooldown_until" in node_columns


@pytest.mark.asyncio
async def test_upgrade_keeps_identity_cooldown_and_unknown_extension(tmp_data_dir):
    await init_db()
    await _legacy_proxy_tables()
    await init_db()
    await init_db()
    async with write_session() as session:
        assert (await session.execute(text(
            "SELECT id,node_id,runtime_name,state,legacy_extra FROM proxy_nodes"
        ))).one() == (901, "node-id", "1|x", "ACTIVE", "keep,me")
        cooldown = await session.get(ProxyNode, 901)
        assert cooldown.cooldown_until == datetime(2026, 11, 1)
        assert not (await session.execute(text("PRAGMA table_info(clash_nodes)"))).all()
        columns = {row[1] for row in await session.execute(text("PRAGMA table_info(proxy_nodes)"))}
        assert "fingerprint" not in columns
        assert (await session.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='ux_proxy_nodes_fingerprint'"
        ))).scalar_one() == 0
        assert (await session.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ix_legacy_extra'"
        ))).scalar_one() == 1
        await session.execute(text("UPDATE proxy_nodes SET legacy_extra='changed' WHERE id=901"))
        assert (await session.execute(text("SELECT value FROM extension_audit"))).scalar_one() == "changed"
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch,referenced", [(True, False), (False, True)])
async def test_upgrade_rejects_unproven_identity_or_external_reference(tmp_data_dir, mismatch, referenced):
    await init_db()
    await _legacy_proxy_tables(mismatch=mismatch, referenced=referenced)
    with pytest.raises(RuntimeError, match="身份|外键"):
        await init_db()
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 23
        assert (await session.execute(text(
            "SELECT fingerprint FROM proxy_nodes WHERE id=901"
        ))).scalar_one() == ("other-id" if mismatch else "node-id")
        if referenced:
            assert (await session.execute(text("SELECT node FROM extension_ref"))).scalar_one() == 901


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["matched", "orphan_expired", "orphan_active", "ambiguous",
                                  "view", "trigger", "foreign_key", "unknown_column", "unknown_index"])
async def test_v30_cooldown_upgrade_keeps_deadline_or_rolls_back(tmp_data_dir, monkeypatch, case):
    from app.crawler import utils
    clock = datetime(2026, 1, 1, 8)
    monkeypatch.setattr(utils, "get_beijing_time_obj", lambda: clock)
    await init_db()
    until = clock - timedelta(minutes=1) if case == "orphan_expired" else clock + timedelta(minutes=30)
    async with write_session() as session:
        session.add(ProxyNode(node_id="known", runtime_name="1|one", subscription_id=1,
                              original_name="one", proxy_type="http", state="DEAD", consecutive_failures=3))
        if case == "ambiguous":
            session.add(ProxyNode(node_id="duplicate", runtime_name="1|one#2", subscription_id=1,
                                  original_name="one", proxy_type="http", state="DEAD", consecutive_failures=3))
        await session.flush()
        await session.execute(text("CREATE TABLE clash_nodes (id INTEGER PRIMARY KEY, subscription_id INTEGER, name TEXT, cooldown_until DATETIME)"))
        await session.execute(text("INSERT INTO clash_nodes VALUES (1,1,:name,:until)"),
                              {"name": "unknown" if case.startswith("orphan") else "one", "until": until})
        if case == "view":
            await session.execute(text("CREATE VIEW custom_cooldown AS SELECT * FROM clash_nodes"))
        if case == "trigger":
            await session.execute(text("CREATE TRIGGER custom_cooldown AFTER UPDATE ON clash_nodes BEGIN SELECT 1; END"))
        if case == "foreign_key":
            await session.execute(text("CREATE TABLE custom_cooldown (n INTEGER REFERENCES clash_nodes(id))"))
            await session.execute(text("INSERT INTO custom_cooldown VALUES(1)"))
        if case == "unknown_column":
            await session.execute(text("ALTER TABLE clash_nodes ADD COLUMN user_extra TEXT"))
        if case == "unknown_index":
            await session.execute(text("CREATE UNIQUE INDEX ix_clash_nodes_sub_name ON clash_nodes(subscription_id,name)"))
        await session.execute(text("PRAGMA user_version=30"))
        await session.commit()
        before = [tuple(r) for r in await session.execute(text("SELECT * FROM clash_nodes"))]
    if case not in ("matched", "orphan_expired"):
        with pytest.raises(RuntimeError, match="有效冷却|扩展|外键|未知"):
            await init_db()
        async with get_session_factory()() as session:
            assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 30
            assert [tuple(r) for r in await session.execute(text("SELECT * FROM clash_nodes"))] == before
            assert (await session.get(ProxyNode, 1)).cooldown_until is None
    else:
        await init_db()
        await init_db()
        async with get_session_factory()() as session:
            assert not (await session.execute(text("PRAGMA table_info(clash_nodes)"))).all()
            node = (await session.execute(select(ProxyNode).where(ProxyNode.node_id == "known"))).scalar_one()
            assert (node.state, node.consecutive_failures) == ("DEAD", 3)
            assert node.cooldown_until == (until if case == "matched" else None)
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
