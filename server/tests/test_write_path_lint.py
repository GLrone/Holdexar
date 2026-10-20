"""写路径 AST 门禁的持闸副作用判定。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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
