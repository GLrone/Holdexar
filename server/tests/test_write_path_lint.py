"""写路径 AST 门禁的持闸副作用判定。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def test_maintenance_file_effect_budget_does_not_exempt_extra_calls():
    source = '''
def prepare():
    with sync_write_gate():
        first.open("rb+")
        second.open("rb+")
'''
    _, exceptions, _, effects = check_write_paths.audit_source(source, "core/update_data.py")
    assert len(exceptions) == 1
    assert len(effects) == 1
    assert len(check_write_paths.audit_source(source, "core/other.py")[3]) == 2


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_write_paths.py"
SPEC = importlib.util.spec_from_file_location("check_write_paths", SCRIPT)
assert SPEC and SPEC.loader
check_write_paths = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_write_paths
SPEC.loader.exec_module(check_write_paths)


def test_write_context_rejects_task_fanout_and_network_io() -> None:
    source = """
import asyncio
async def update(session, client):
    async with write_gate():
        await asyncio.gather(session.execute('INSERT INTO t VALUES (1)'), client.get('/x'))
"""
    fails, _, _, effects = check_write_paths.audit_source(source)
    assert fails == []
    assert len(effects) == 2


def test_write_context_rejects_aliased_task_creation() -> None:
    source = """
from asyncio import create_task as spawn
async def update():
    async with write_session():
        spawn(refresh())
"""
    _, _, _, effects = check_write_paths.audit_source(source)
    assert len(effects) == 1


def test_database_io_is_allowed_inside_write_context() -> None:
    source = """
async def update(session):
    async with write_session():
        await session.execute('INSERT INTO t VALUES (1)')
        await session.commit()
"""
    fails, _, _, effects = check_write_paths.audit_source(source)
    assert fails == []
    assert effects == []


def test_allow_does_not_exempt_new_statement_of_same_kind():
    source = """
def snapshot_to(conn):
    conn.execute("VACUUM INTO 'backup.db'")
    conn.execute("DELETE FROM games")
"""
    fails, exceptions, _, _ = check_write_paths.audit_source(source, "core/backup.py")
    assert len(exceptions) == 1
    assert len(fails) == 1


def test_script_write_paths_are_included():
    paths = {p.relative_to(REPO_ROOT).as_posix() for p in check_write_paths.source_paths()}
    assert {"scripts/fx_maintenance.py", "scripts/backfill_version_suffix.py",
            "server/scripts/export_template_db.py"} <= paths


def test_migration_name_does_not_implicitly_allow_writes():
    source = """
async def _migrate_unregistered(conn):
    await conn.execute("DELETE FROM games")
"""
    fails, exceptions, _, _ = check_write_paths.audit_source(source, "core/database.py")
    assert len(fails) == 1
    assert exceptions == []


def test_schema_preparation_allow_is_limited():
    source = """
async def init_db(conn):
    await conn.execute("BEGIN IMMEDIATE")
    await conn.execute("DELETE FROM games")
"""
    fails, exceptions, _, _ = check_write_paths.audit_source(source, "core/database.py")
    assert len(fails) == 1
    assert len(exceptions) == 1


def test_driver_sql_write_requires_write_context():
    source = """
async def update(conn):
    await conn.exec_driver_sql("DELETE FROM games")
"""
    fails, _, _, _ = check_write_paths.audit_source(source)
    assert len(fails) == 1


def test_driver_sql_write_is_allowed_in_write_context():
    source = """
async def update(conn):
    async with write_session():
        await conn.exec_driver_sql("UPDATE games SET name='updated'")
"""
    fails, _, _, effects = check_write_paths.audit_source(source)
    assert fails == []
    assert effects == []


@pytest.mark.parametrize("sql", [
    "PRAGMA user_version = 2",
    "PRAGMA main.user_version(2)",
    "PRAGMA writable_schema=ON",
])
def test_schema_pragma_assignment_requires_write_context(sql):
    source = f"async def update(conn):\n    await conn.execute({sql!r})\n"
    fails, _, _, _ = check_write_paths.audit_source(source)
    assert len(fails) == 1


def test_reading_schema_pragma_does_not_require_write_context():
    source = 'async def read(conn):\n    await conn.execute("PRAGMA user_version")\n'
    fails, _, _, _ = check_write_paths.audit_source(source)
    assert fails == []


@pytest.mark.parametrize("setup, call", [
    ("import asyncio\nspawn = asyncio.create_task", "spawn(refresh())"),
    ("import asyncio as aio\nspawn = aio.create_task\nagain = spawn", "again(refresh())"),
    ("import httpx\nrequest = httpx.get", "request('/x')"),
])
def test_write_context_rejects_assigned_effect_alias(setup, call):
    source = f"{setup}\nasync def update():\n    async with write_session():\n        {call}\n"
    assert len(check_write_paths.audit_source(source)[3]) == 1


def test_assigned_alias_does_not_leak_from_another_function():
    source = '''
import asyncio
def configure():
    spawn = asyncio.create_task
async def update(spawn):
    async with write_session():
        spawn()
'''
    assert check_write_paths.audit_source(source)[3] == []


def test_reassigned_alias_does_not_keep_old_effect():
    source = '''
import asyncio
spawn = asyncio.create_task
spawn = database_operation
async def update():
    async with write_session():
        spawn()
'''
    assert check_write_paths.audit_source(source)[3] == []


def test_raw_database_thread_in_gate_requires_cancellation_safe_wait():
    source = '''
import asyncio
async def update():
    async with write_gate():
        await asyncio.to_thread(_merge_history_chunk)
'''
    assert len(check_write_paths.audit_source(source)[3]) == 1


@pytest.mark.parametrize("target, expected", [("_merge_history_chunk", 0), ("client.get", 1)])
def test_safe_database_thread_only_accepts_registered_sqlite_operations(target, expected):
    source = f'''\nasync def update():
    async with write_gate():
        await complete_db_thread({target})
'''
    assert len(check_write_paths.audit_source(source)[3]) == expected


def test_imported_gate_alias_preserves_effect_and_transaction_checks():
    source = '''
from app.core.database import write_session as transaction
import asyncio
async def update(session):
    async with transaction():
        await session.commit()
        asyncio.create_task(refresh())
'''
    fails, _, _, effects = check_write_paths.audit_source(source)
    assert fails == []
    assert len(effects) == 1


def test_imported_thread_wait_alias_still_rejects_network():
    source = '''
from app.core.database import complete_db_thread as wait
async def update(client):
    async with write_session():
        await wait(client.get)
'''
    assert len(check_write_paths.audit_source(source)[3]) == 1


@pytest.mark.parametrize("binding, expected", [
    ("spawn: object = asyncio.create_task", 1),
    ("spawn = asyncio.create_task\nspawn: object = database_operation", 0),
])
def test_annotated_alias_obeys_binding_and_reassignment(binding, expected):
    source = f'''import asyncio
{binding}
async def update():
    async with write_gate():
        spawn()
'''
    assert len(check_write_paths.audit_source(source)[3]) == expected
