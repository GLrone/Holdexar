import sqlite3

import pytest
from sqlalchemy import text

from app.core import database
from test_schema_migrations import isolated_db as _isolated_db


_MIGRATIONS = list(database._MIGRATIONS)
isolated_db = _isolated_db


def _old_tables(db):
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE price_events (
                id INTEGER NOT NULL, cycle_id INTEGER, appid INTEGER,
                region_code TEXT, event_type TEXT, previous_json JSON,
                current_json JSON, occurred_at DATETIME, created_at DATETIME,
                custom_note TEXT DEFAULT 'preserved', PRIMARY KEY (id));
            CREATE TABLE price_cycles (id INTEGER PRIMARY KEY, custom_note TEXT);
            CREATE TABLE crawl_jobs (id INTEGER NOT NULL, cycle_id INTEGER,
                custom_note TEXT, PRIMARY KEY (id));
            CREATE TABLE notification_candidates (
                id INTEGER PRIMARY KEY, event_id INTEGER, cycle_id INTEGER);
            CREATE INDEX custom_event_index ON price_events(custom_note);
            CREATE TABLE event_audit (event_id INTEGER);
            CREATE TRIGGER custom_event_trigger AFTER INSERT ON price_events
                BEGIN INSERT INTO event_audit VALUES (NEW.id); END;
            INSERT INTO price_events (id, cycle_id, appid) VALUES (1,1,10);
            INSERT INTO price_cycles VALUES (1,'cycle');
            INSERT INTO crawl_jobs VALUES (1,1,'job');
            INSERT INTO notification_candidates VALUES (1,77,66),(2,78,-55);
            CREATE TABLE game_price_history (
                id INTEGER PRIMARY KEY, appid INTEGER, region_code TEXT,
                snapshot_at DATETIME, sub_id INTEGER, price INTEGER,
                is_gold INTEGER);
            PRAGMA user_version = 18;
        ''')
    conn.close()


@pytest.mark.asyncio
async def test_identity_migration_preserves_custom_schema_and_reference_highwater(isolated_db):
    _old_tables(isolated_db)
    database._MIGRATIONS[:] = [m for m in _MIGRATIONS if m[0] == 19]
    await database._run_schema_migrations()

    conn = sqlite3.connect(isolated_db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 19
        assert conn.execute('SELECT custom_note FROM price_events').fetchall() == [('preserved',)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='custom_event_index'").fetchone()
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ux_gph_snapshot'").fetchone()
        conn.execute('DELETE FROM price_events')
        conn.execute('DELETE FROM price_cycles')
        conn.execute('DELETE FROM crawl_jobs')
        event = conn.execute('INSERT INTO price_events (appid) VALUES (20)').lastrowid
        cycle = conn.execute("INSERT INTO price_cycles (custom_note) VALUES ('new')").lastrowid
        job = conn.execute("INSERT INTO crawl_jobs (custom_note) VALUES ('new')").lastrowid
        assert event > 78 and cycle > 66 and job > 55
        assert conn.execute('SELECT event_id FROM event_audit ORDER BY rowid DESC LIMIT 1').fetchone() == (event,)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_identity_migration_failure_restores_all_original_tables(isolated_db, monkeypatch):
    _old_tables(isolated_db)
    database._MIGRATIONS[:] = [m for m in _MIGRATIONS if m[0] == 19]
    engine = database.get_engine()
    from sqlalchemy import event

    def fail_before_job_rebuild(_conn, _cursor, statement, _params, _context, _many):
        if 'CREATE TABLE' in statement.upper() and 'crawl_jobs' in statement:
            raise RuntimeError('injected table rebuild failure')

    event.listen(engine.sync_engine, 'before_cursor_execute', fail_before_job_rebuild)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            await database._run_schema_migrations()
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', fail_before_job_rebuild)
    conn = sqlite3.connect(isolated_db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 18
        for table in ('price_events','price_cycles','crawl_jobs'):
            sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
            assert 'AUTOINCREMENT' not in sql.upper()
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%identity_new%'").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_identity_migration_rejects_external_cascade_before_any_rebuild(isolated_db):
    _old_tables(isolated_db)
    conn = sqlite3.connect(isolated_db)
    try:
        conn.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE extension_references (
                id INTEGER PRIMARY KEY,
                event_id INTEGER REFERENCES price_events(id) ON DELETE CASCADE,
                cycle_id INTEGER REFERENCES price_cycles(id) ON DELETE CASCADE,
                job_id INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                user_payload TEXT);
            INSERT INTO extension_references VALUES
                (1,1,NULL,NULL,'event'),(2,NULL,1,NULL,'cycle'),(3,NULL,NULL,1,'job');
        ''')
    finally:
        conn.close()
    database._MIGRATIONS[:] = [m for m in _MIGRATIONS if m[0] == 19]
    async with database.get_engine().connect() as conn:
        await conn.execute(text('PRAGMA foreign_keys=ON'))
        await conn.commit()
    with pytest.raises(RuntimeError, match='外键'):
        await database._run_schema_migrations()
    conn = sqlite3.connect(isolated_db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 18
        assert conn.execute('SELECT user_payload FROM extension_references ORDER BY id').fetchall() == [
            ('event',),('cycle',),('job',)]
        for table in ('price_events','price_cycles','crawl_jobs'):
            sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
            assert 'AUTOINCREMENT' not in sql.upper()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_autoincrement_text_default_does_not_bypass_identity_migration(isolated_db):
    _old_tables(isolated_db)
    conn = sqlite3.connect(isolated_db)
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='price_events'").fetchone()[0]
        conn.execute('DROP TABLE price_events')
        conn.execute(sql.replace("'preserved'", "'PRIMARY KEY AUTOINCREMENT'"))
        conn.commit()
    finally:
        conn.close()
    database._MIGRATIONS[:] = [m for m in _MIGRATIONS if m[0] == 19]
    await database._run_schema_migrations()
    conn = sqlite3.connect(isolated_db)
    try:
        assert conn.execute('INSERT INTO price_events (appid) VALUES (30)').lastrowid > 78
    finally:
        conn.close()
