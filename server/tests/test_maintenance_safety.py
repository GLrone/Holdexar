from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from app.core import database


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_cancelled_migration_snapshot_closes_thread_before_releasing_maintenance(
    tmp_path, monkeypatch
):
    from app.core import backup

    db = tmp_path / "holdexar.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE result (value TEXT)")
    conn.close()
    monkeypatch.setattr(database, "sqlite_file_path", lambda: db)
    monkeypatch.setattr(database, "_db_file_path", lambda: db)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def snapshot(src, dest):
        conn = sqlite3.connect(src)
        try:
            conn.execute("SELECT * FROM result").fetchall()
            entered.set()
            assert release.wait(3)
            dest.touch()
        finally:
            conn.close()
            closed.set()

    async def migrating():
        async with database.write_maintenance_barrier():
            await database._snapshot_before_migration(17, 18)

    monkeypatch.setattr(backup, "snapshot_to", snapshot)
    task = asyncio.create_task(migrating())
    try:
        await _until(entered.is_set)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert database.write_scheduler_diagnostics()["busy"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert closed.is_set()
    assert not database.write_scheduler_diagnostics()["busy"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_maintenance_reentry_finishes_while_independent_writers_queue(tmp_path, monkeypatch):
    db = tmp_path / "holdexar.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE result (value TEXT)")
    conn.close()
    monkeypatch.setattr(database, "sqlite_file_path", lambda: db)
    entered, proceed = asyncio.Event(), asyncio.Event()

    def insert(value):
        conn = sqlite3.connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO result VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    async def maintain():
        async with database.write_maintenance_barrier():
            entered.set()
            await proceed.wait()
            async with database.write_gate(label="maintenance_initialization"):
                await database.complete_db_thread(insert, "maintenance")

    async def writer(index):
        async with database.write_gate(database.WritePriority.INTERACTIVE, label="queued_api"):
            await database.complete_db_thread(insert, str(index))

    task = asyncio.create_task(maintain())
    await entered.wait()
    writers = [asyncio.create_task(writer(index)) for index in range(4)]
    await asyncio.sleep(0.02)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(task, *writers), 1)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT value FROM result ORDER BY rowid").fetchall() == [
            ("maintenance",), ("0",), ("1",), ("2",), ("3",)
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_maintenance_blocks_real_sqlite_writer_in_another_process(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path

    db = tmp_path / "holdexar.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE result (value TEXT)")
    conn.close()
    monkeypatch.setattr(database, "sqlite_file_path", lambda: db)
    real_lock_path = database._process_write_lock_path
    monkeypatch.setattr(database, "_process_write_lock_path", lambda path=None: real_lock_path(path or db))
    code = """
import sqlite3, sys
from pathlib import Path
from app.core.database import sync_write_gate
path = Path(sys.argv[1])
print('READY', flush=True)
with sync_write_gate(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("INSERT INTO result VALUES ('child')")
        conn.commit()
    finally:
        conn.close()
print('COMMITTED', flush=True)
"""
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    child = None
    try:
        async with database.write_maintenance_barrier():
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, str(db),
                cwd=str(Path(__file__).resolve().parents[1]),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs,
            )
            assert (await asyncio.wait_for(child.stdout.readline(), 5)).strip() == b"READY"
            await asyncio.sleep(0.05)
            assert child.returncode is None
            conn = sqlite3.connect(db)
            try:
                assert conn.execute("SELECT COUNT(*) FROM result").fetchone()[0] == 0
            finally:
                conn.close()
        stdout, stderr = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode == 0, stderr.decode(errors="replace")
        assert b"COMMITTED" in stdout
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT value FROM result").fetchone() == ("child",)
        finally:
            conn.close()
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("marker_kind", ["restore", "unknown"])
async def test_unconfirmed_database_rejects_read_connections_in_child_tasks(cached, marker_kind):
    from app.core import backup
    from sqlalchemy import text

    engine = database.get_engine()
    db = database.sqlite_file_path()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE unconfirmed_result (value TEXT)"))
        await conn.execute(text("INSERT INTO unconfirmed_result VALUES ('unconfirmed-backup-data')"))
    barrier = await database.acquire_write_maintenance_barrier()
    lock, token = backup._acquire_restore_lock(db_path=db, purpose="restore")
    if marker_kind == "unknown":
        lock.write_text("{broken", encoding="utf-8")
    try:
        if not cached:
            await backup._dispose_engine()

        async def read():
            with pytest.raises(RuntimeError, match="原库恢复未完成|身份无法确认"):
                async with database.get_session_factory()() as session:
                    await session.scalar(text("SELECT value FROM unconfirmed_result"))

        with database.maintenance_authorization(token):
            await asyncio.create_task(read())
        replacement = db.with_name("replacement-probe.db")
        backup.snapshot_to(db, replacement)
        replacement.replace(db)
    finally:
        lock.unlink(missing_ok=True)
        barrier.release()
        await database.get_engine().dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_restore_probe_authorization_covers_existing_and_new_connections():
    from app.core import backup

    db = database.sqlite_file_path()
    barrier = await database.acquire_write_maintenance_barrier()
    lock, token = backup._acquire_restore_lock(db_path=db, purpose="restore")
    try:
        assert await backup._rebuild_engine(maintenance_token=token) is True
        assert await backup._rebuild_engine(maintenance_token=token) is True
    finally:
        backup._release_restore_lock(lock, token)
        barrier.release()
        await database.get_engine().dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["maintenance", "update"])
async def test_online_backup_and_update_verification_keep_read_access(purpose):
    from app.core import backup
    from sqlalchemy import text

    db = database.sqlite_file_path()
    lock, token = backup._acquire_restore_lock(db_path=db, purpose=purpose)
    try:
        async with database.get_session_factory()() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        backup._release_restore_lock(lock, token)
        await database.get_engine().dispose()
