from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core import backup, database, db_safety, layout


@pytest.mark.asyncio
async def test_update_verification_rejects_key_file_changes_before_route(update_env, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app import main
    from app.core import access, keyring, rekey

    writes = []
    real_write = keyring.write_record

    def record_write(directory, record):
        writes.append(directory)
        return real_write(directory, record)

    async def reject_transaction(*_args):
        raise RuntimeError("database is protected")

    monkeypatch.setattr(keyring, "write_record", record_write)
    monkeypatch.setattr(rekey, "reencrypt_all", reject_transaction)
    app = main.create_app()
    app.state.update_verification = {"protected": True, "pageReady": False}
    monkeypatch.setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-one")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        client.headers[access.SESSION_HEADER] = access.session_token()
        response = await client.post("/api/v1/system/security/mode", json={
            "mode": "passphrase", "passphrase": "isolated-password",
        })
        assert response.status_code == 503, (response.text, len(writes))
        assert writes == []
        ready = await client.post("/api/v1/system/update-ready", json={"attemptId": "attempt-one"})
        assert ready.status_code == 200
        assert app.state.update_verification["pageReady"] is True
        app.state.update_verification["protected"] = False
        response = await client.post("/api/v1/system/security/mode", json={
            "mode": "passphrase", "passphrase": "isolated-password",
        })
        assert response.status_code != 503
        assert writes
    keyring.reset_state()


@pytest.mark.asyncio
async def test_protected_startup_defers_key_recovery_until_acceptance(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app import main
    from app.core import rekey

    accepted = False
    calls = []
    finished = asyncio.Event()

    @contextmanager
    def authorize(_directory):
        yield "verification-token"

    async def initialize(**_kwargs):
        pass

    async def resume(_directory):
        calls.append("keys")

    async def chain():
        calls.append("chain")
        finished.set()

    monkeypatch.setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-one")
    monkeypatch.setenv("HOLDEXAR_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setattr(module(), "startup_authorization", authorize)
    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(rekey, "resume_pending_migration", resume)
    monkeypatch.setattr(main, "_initialize_runtime", chain)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    monkeypatch.setattr(backup, "ensure_restore_lock_available", lambda _path: None)
    monkeypatch.setattr(layout, "read_transaction", lambda _root: {
        "attemptId": "attempt-one", "dataToken": "verification-token", "dataAccepted": accepted,
    })
    app = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(app):
        await asyncio.sleep(0)
        assert calls == []
        accepted = True
        await asyncio.wait_for(finished.wait(), 2)
        assert calls == ["keys", "chain"]


@pytest.fixture
def update_env(tmp_path, monkeypatch):
    root = tmp_path / "install"
    data = root / "data"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(data))
    for name in ("HOLDEXAR_INSTALL_ROOT", "HOLDEXAR_UPDATE_ATTEMPT_ID", "HOLDEXAR_UPDATE_DATA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    database.get_settings.cache_clear()
    db = data / database.get_settings().db_filename
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("CREATE TABLE original(value TEXT); INSERT INTO original VALUES('before'); PRAGMA user_version=1;")
    layout.start_transaction(root, "2.0.0", "1.0.0", attempt_id="attempt-one")
    layout.write_current_state(root, "1.0.0")
    return root, data, db


def module():
    return importlib.import_module("app.core.update_data")


def prepare(env):
    root, data, _ = env
    return module().prepare(root, data, "attempt-one")


def startup_env(monkeypatch, env, metadata):
    root, _, _ = env
    monkeypatch.setenv("HOLDEXAR_INSTALL_ROOT", str(root))
    monkeypatch.setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-one")
    monkeypatch.setenv("HOLDEXAR_UPDATE_DATA_TOKEN", metadata["dataToken"])


def test_prepare_records_verified_immutable_snapshot(update_env):
    root, data, db = update_env
    metadata = prepare(update_env)
    tx = layout.read_transaction(root)
    snapshot = Path(metadata["dataSnapshot"])
    assert metadata["dataPrepared"] and not tx["dataAccepted"]
    assert snapshot != db and snapshot.is_file()
    assert backup.sha256_of(snapshot) == tx["dataSnapshotSha256"]
    assert backup._verify_file(snapshot, True)[0]
    lock = json.loads((data / "backups" / ".restore.lock").read_text())
    assert lock["purpose"] == "update" and lock["attemptId"] == "attempt-one"


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_prepare_marker_storage_failure_leaves_startup_and_retry_usable(update_env, monkeypatch, stage):
    import errno
    from contextlib import contextmanager
    from types import SimpleNamespace

    root, data, db = update_env
    fdopen = os.fdopen
    streams = []

    def fail(*_args):
        raise OSError(errno.ENOSPC, "marker storage failed")

    @contextmanager
    def failing_stream(*args, **kwargs):
        with fdopen(*args, **kwargs) as stream:
            streams.append(stream)
            proxy = SimpleNamespace(write=stream.write, flush=stream.flush, fileno=stream.fileno)
            setattr(proxy, stage, fail)
            yield proxy

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", failing_stream)
        with pytest.raises(OSError, match="marker storage failed"):
            prepare(update_env)

    assert streams and all(stream.closed for stream in streams)
    assert not (data / "backups" / ".restore.lock").exists()
    assert not layout.read_transaction(root).get("dataPrepared")
    assert module().validate_startup(data) is None
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM original").fetchall() == [("before",)]
    assert prepare(update_env)["dataPrepared"]


def test_recovery_finishes_abandoned_probe_registration(update_env, monkeypatch):
    from app.core import update_host

    root, data, db = update_env
    prepare(update_env)
    layout.update_transaction(root, layout.STAGE_VERIFYING, launchPending=True,
                              launchImage=str(layout.get_version_exe(root, "2.0.0")))
    monkeypatch.setattr(update_host, "_probe_processes", lambda _exe: [], raising=False)
    assert update_host.recover_interrupted_update(root) is True
    assert layout.read_transaction(root)["launchPending"] is False
    assert not module()._marker_path(data).exists()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM original").fetchall() == [("before",)]


@pytest.mark.parametrize("live", [True, False])
def test_unproven_probe_registration_retains_protection(update_env, monkeypatch, live):
    from app.core import update_host

    root, data, _ = update_env
    prepare(update_env)
    layout.update_transaction(root, layout.STAGE_VERIFYING, launchPending=True)

    def inspect(_exe):
        if not live:
            raise OSError("process inventory unavailable")
        return [987654]

    monkeypatch.setattr(update_host, "_probe_processes", inspect, raising=False)
    with pytest.raises(RuntimeError):
        update_host.recover_interrupted_update(root)
    assert layout.read_transaction(root)["launchPending"] is True
    assert module()._marker_path(data).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows process inventory")
def test_unregistered_real_process_blocks_recovery_until_exit(update_env):
    import shutil
    import subprocess
    import sys

    from app.core import update_host

    root, data, db = update_env
    prepare(update_env)
    exe = layout.get_version_exe(root, "2.0.0")
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sys._base_executable, exe)
    env = dict(os.environ, PYTHONHOME=sys.base_prefix,
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    proc = subprocess.Popen(
        [str(exe), "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        layout.update_transaction(root, layout.STAGE_VERIFYING, launchPending=True, launchImage=str(exe))
        with pytest.raises(RuntimeError, match="仍在运行"):
            update_host.recover_interrupted_update(root)
        assert layout.read_transaction(root)["launchPending"] is True
        assert module()._marker_path(data).exists()
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
    assert update_host.recover_interrupted_update(root) is True
    assert not module()._marker_path(data).exists()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM original").fetchall() == [("before",)]


def test_snapshot_failure_keeps_live_database_and_releases_marker(update_env, monkeypatch):
    root, data, db = update_env
    def fail(src, dest):
        dest.write_bytes(b"incomplete")
        raise OSError("snapshot failed")
    monkeypatch.setattr(db_safety, "snapshot_to", fail)
    with pytest.raises(OSError, match="snapshot failed"):
        prepare(update_env)
    assert not (data / "backups" / ".restore.lock").exists()
    assert not layout.read_transaction(root).get("dataPrepared")
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("SELECT value FROM original").fetchone()[0] == "before"


def test_rollback_restores_schema_and_preserves_failed_database(update_env):
    root, _, db = update_env
    metadata = prepare(update_env)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript("ALTER TABLE original ADD COLUMN later TEXT; UPDATE original SET value='after'; PRAGMA user_version=18;")
    result = module().rollback(root, children_stopped=True)
    assert result["restored"] and not result["alreadyRestored"]
    snapshot = Path(metadata["dataSnapshot"])
    assert backup.sha256_of(snapshot) == metadata["dataSnapshotSha256"]
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT * FROM original").fetchall() == [("before",)]
    failed = Path(layout.read_transaction(root)["dataFailedDb"])
    with closing(sqlite3.connect(failed)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    assert module().rollback(root, children_stopped=True)["alreadyRestored"]


def test_rollback_handles_missing_current_database(update_env):
    root, _, db = update_env
    prepare(update_env)
    db.unlink()
    assert module().rollback(root, children_stopped=True)["restored"]
    assert backup._verify_file(db, True)[0]


def test_accept_is_irreversible_and_does_not_erase_later_writes(update_env):
    root, data, db = update_env
    prepare(update_env)
    module().accept(root)
    assert layout.read_transaction(root)["dataAccepted"]
    assert not (data / "backups" / ".restore.lock").exists()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO original VALUES('new business')")
    with pytest.raises(RuntimeError):
        module().rollback(root, children_stopped=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM original").fetchone()[0] == 2


def test_update_marker_is_not_removed_for_dead_owner(update_env, monkeypatch):
    _, data, db = update_env
    prepare(update_env)
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: False)
    with pytest.raises(RuntimeError):
        backup.ensure_restore_lock_available(db)
    assert (data / "backups" / ".restore.lock").exists()


@pytest.mark.parametrize("children_stopped,children", [(False, []), (True, [{"pid": os.getpid(), "stopped": False}])])
def test_rollback_requires_proven_child_exit(update_env, children_stopped, children):
    root, _, _ = update_env
    prepare(update_env)
    layout.update_transaction(root, layout.STAGE_VERIFYING, children=children)
    with pytest.raises(RuntimeError):
        module().rollback(root, children_stopped=children_stopped)


def test_rollback_rejects_changed_snapshot(update_env):
    root, _, _ = update_env
    metadata = prepare(update_env)
    Path(metadata["dataSnapshot"]).write_bytes(b"tampered")
    with pytest.raises(RuntimeError):
        module().rollback(root, children_stopped=True)


def test_startup_rejects_wrong_token_and_missing_marker(update_env, monkeypatch):
    metadata = prepare(update_env)
    startup_env(monkeypatch, update_env, metadata)
    monkeypatch.setenv("HOLDEXAR_UPDATE_DATA_TOKEN", "wrong")
    with pytest.raises(RuntimeError):
        module().validate_startup(update_env[1])
    (update_env[1] / "backups" / ".restore.lock").unlink()
    with pytest.raises(RuntimeError):
        module().validate_startup(update_env[1])


@pytest.mark.asyncio
async def test_update_authorization_only_allows_initializing_task(update_env, monkeypatch):
    _, data, db = update_env
    metadata = prepare(update_env)
    startup_env(monkeypatch, update_env, metadata)
    with pytest.raises(RuntimeError):
        with database.sync_write_gate(db):
            pytest.fail("independent writer entered")
    with pytest.raises(RuntimeError):
        async with database.write_gate():
            pytest.fail("ordinary API entered")
    with module().startup_authorization(data) as token:
        async with database.write_maintenance_barrier():
            async with database.write_gate(label="authorized_seed"):
                pass
            async def child():
                with pytest.raises(RuntimeError):
                    async with database.write_gate():
                        pytest.fail("child borrowed parent authorization")
            await asyncio.create_task(child())
    with pytest.raises(RuntimeError):
        async with database.write_gate():
            pytest.fail("authorization escaped context")
    await database.get_engine().dispose()


@pytest.mark.asyncio
async def test_maintenance_holds_scheduler_and_seeds_reenter(update_env):
    async with database.write_maintenance_barrier():
        assert database.scheduler_task_owns_gate()
        async with database.write_gate(label="seed"):
            assert database.write_scheduler_diagnostics()["depth"] == 2
    assert not database.write_scheduler_diagnostics()["busy"]
    await database.get_engine().dispose()


def test_unregistered_launch_blocks_rollback_and_startup(update_env, monkeypatch):
    root, data, _ = update_env
    metadata = prepare(update_env)
    startup_env(monkeypatch, update_env, metadata)
    layout.update_transaction(root, layout.STAGE_VERIFYING, dataProbeRegistrationRequired=True, launchPending=True)
    monkeypatch.setattr(module(), "_REGISTRATION_WAIT_SECONDS", 0.01, raising=False)
    with pytest.raises(RuntimeError):
        module().validate_startup(data)
    with pytest.raises(RuntimeError):
        module().rollback(root, children_stopped=True)


def test_registered_launch_is_allowed(update_env, monkeypatch):
    root, data, _ = update_env
    metadata = prepare(update_env)
    startup_env(monkeypatch, update_env, metadata)
    layout.update_transaction(root, layout.STAGE_VERIFYING, dataProbeRegistrationRequired=True,
                              launchPending=False, children=[{"pid": os.getpid(), "stopped": False}])
    assert module().validate_startup(data) == metadata["dataToken"]


@pytest.mark.asyncio
async def test_authorized_init_includes_schema_and_all_seeds(update_env, monkeypatch):
    _, data, db = update_env
    metadata = prepare(update_env)
    startup_env(monkeypatch, update_env, metadata)
    try:
        with module().startup_authorization(data) as token:
            await database.init_db(maintenance_token=token)
        with closing(sqlite3.connect(db)) as conn:
            assert conn.execute("SELECT count(*) FROM crawl_regions").fetchone()[0] > 0
            assert conn.execute("SELECT count(*) FROM fx_rates").fetchone()[0] > 0
        with pytest.raises(RuntimeError):
            async with database.write_gate():
                pytest.fail("business write entered before acceptance")
    finally:
        await database.get_engine().dispose()


def test_originally_missing_database_is_removed_on_rollback(update_env):
    root, _, db = update_env
    db.unlink()
    metadata = prepare(update_env)
    assert not metadata["dataHadDatabase"] and metadata["dataSnapshot"] is None
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE created_by_new_version(value TEXT)")
    assert module().rollback(root, children_stopped=True)["restored"]
    assert not db.exists()
    assert Path(layout.read_transaction(root)["dataFailedDb"]).exists()


@pytest.mark.parametrize("after_move", [False, True])
def test_rollback_resumes_after_main_file_move_interruption(update_env, monkeypatch, after_move):
    root, _, db = update_env
    metadata = prepare(update_env)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE original SET value='migrated'")
    replace = os.replace
    failed = False
    def interrupted(source, target):
        nonlocal failed
        if Path(target) == db and not failed:
            failed = True
            if after_move:
                replace(source, target)
            raise OSError("injected restore interruption")
        return replace(source, target)
    monkeypatch.setattr(module().os, "replace", interrupted)
    with pytest.raises(OSError, match="injected restore interruption"):
        module().rollback(root, children_stopped=True)
    assert Path(metadata["dataSnapshot"]).exists()
    assert module().rollback(root, children_stopped=True)["restored"]
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM original").fetchone()[0] == "before"


def test_accept_journal_failure_keeps_protection(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    def fail(*args, **kwargs):
        raise OSError("journal unavailable")
    monkeypatch.setattr(layout, "update_transaction", fail)
    with pytest.raises(OSError, match="journal unavailable"):
        module().accept(root)
    assert (data / "backups" / ".restore.lock").exists()
    assert not layout.read_transaction(root)["dataAccepted"]


def test_active_external_sqlite_connection_blocks_rollback(update_env):
    root, data, db = update_env
    prepare(update_env)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            module().rollback(root, children_stopped=True)
        assert (data / "backups" / ".restore.lock").exists()
        assert layout.read_transaction(root)["dataRecoveryPhase"] == "prepared"
    finally:
        holder.close()


def test_correct_sync_token_still_requires_os_lock(update_env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _, _, db = update_env
    metadata = prepare(update_env)
    monkeypatch.setattr(db_safety, "_WRITE_WAIT_TIMEOUT", 0.02)
    def contender():
        with database.sync_write_gate(db, maintenance_token=metadata["dataToken"]):
            pytest.fail("another writer bypassed OS lock")
    with database.sync_write_gate(db, maintenance_token=metadata["dataToken"]):
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(TimeoutError):
                executor.submit(contender).result(timeout=1)


@pytest.mark.asyncio
async def test_async_writer_rechecks_marker_after_os_lock(update_env, monkeypatch):
    _, _, db = update_env
    acquire = database._CrossProcessWriteLock.acquire
    locks = []
    async def acquire_then_mark(lock):
        result = await acquire(lock)
        locks.append(backup._acquire_restore_lock(db_path=db, purpose="update"))
        return result
    monkeypatch.setattr(database._CrossProcessWriteLock, "acquire", acquire_then_mark)
    try:
        with pytest.raises(RuntimeError):
            async with database.write_gate():
                pytest.fail("marker appeared while acquiring OS lock")
        assert not database.write_scheduler_diagnostics()["busy"]
    finally:
        for path, token in locks:
            backup._release_restore_lock(path, token)
        await database.get_engine().dispose()


def test_unexpected_sidecar_during_recovery_keeps_protection(update_env, monkeypatch):
    root, data, db = update_env
    prepare(update_env)
    replace = os.replace
    def stop_before_restore(source, target):
        if Path(target) == db:
            raise OSError("stop before restore")
        return replace(source, target)
    monkeypatch.setattr(module().os, "replace", stop_before_restore)
    with pytest.raises(OSError):
        module().rollback(root, children_stopped=True)
    monkeypatch.setattr(module().os, "replace", replace)
    db.with_name(db.name + "-wal").write_bytes(b"unknown writer evidence")
    with pytest.raises(RuntimeError):
        module().rollback(root, children_stopped=True)
    assert (data / "backups" / ".restore.lock").exists()


def test_restored_journal_does_not_release_unknown_changed_database(update_env, monkeypatch):
    root, data, db = update_env
    prepare(update_env)
    module().rollback(root, children_stopped=True)
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE original SET value='unknown writer'")
    with pytest.raises(RuntimeError):
        module().finish_rollback(root)
    assert (data / "backups" / ".restore.lock").exists()


@pytest.mark.asyncio
async def test_data_restore_keeps_protection_until_program_pointer_is_rolled_back(update_env):
    root, data, db = update_env
    prepare(update_env)
    layout.write_current_state(root, "2.0.0", last_known_good="1.0.0")
    module().rollback(root, children_stopped=True)
    assert layout.read_transaction(root)["dataRecoveryPhase"] == "restored"
    assert (data / "backups" / ".restore.lock").exists()
    try:
        with pytest.raises(RuntimeError):
            await database.init_db()
        with pytest.raises(RuntimeError):
            module().validate_startup(data)
    finally:
        await database.get_engine().dispose()


@pytest.mark.parametrize("pointer,stage", [("2.0.0", layout.STAGE_ROLLED_BACK), ("1.0.0", layout.STAGE_VERIFYING)])
def test_finish_rollback_requires_source_pointer_and_terminal_journal(update_env, pointer, stage):
    root, data, _ = update_env
    prepare(update_env)
    module().rollback(root, children_stopped=True)
    layout.write_current_state(root, pointer)
    layout.update_transaction(root, stage)
    with pytest.raises(RuntimeError):
        module().finish_rollback(root)
    assert (data / "backups" / ".restore.lock").exists()


def test_released_rollback_preserves_later_writes_despite_pid_reuse(update_env, monkeypatch):
    root, data, db = update_env
    prepare(update_env)
    module().rollback(root, children_stopped=True)
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK)
    module().finish_rollback(root)
    assert layout.read_transaction(root)["dataRollbackReleased"] is True
    assert not (data / "backups" / ".restore.lock").exists()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO original VALUES('after rollback')")
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK, children=[{"pid": os.getpid(), "stopped": True}])
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: True)
    assert module().rollback(root, children_stopped=False)["alreadyRestored"]
    assert module().finish_rollback(root)["released"]
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM original ORDER BY rowid").fetchall() == [("before",), ("after rollback",)]


def test_finish_rollback_resumes_marker_release_after_durable_commit(update_env, monkeypatch):
    root, data, db = update_env
    prepare(update_env)
    module().rollback(root, children_stopped=True)
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK)
    release = db_safety._release_restore_lock
    def fail(*args):
        raise OSError("release interrupted")
    monkeypatch.setattr(db_safety, "_release_restore_lock", fail)
    with pytest.raises(OSError, match="release interrupted"):
        module().finish_rollback(root)
    assert layout.read_transaction(root)["dataRollbackReleased"] is True
    assert (data / "backups" / ".restore.lock").exists()
    monkeypatch.setattr(db_safety, "_release_restore_lock", release)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO original VALUES('new data')")
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK, children=[{"pid": os.getpid()}])
    assert module().finish_rollback(root)["released"]
    assert not (data / "backups" / ".restore.lock").exists()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM original").fetchone()[0] == 2


def test_abort_preparation_after_journal_failure_only_releases_matching_marker(update_env, monkeypatch):
    root, data, db = update_env
    original = backup.sha256_of(db)
    record = layout.update_transaction
    def fail(*args, **kwargs):
        raise OSError("journal unavailable")
    monkeypatch.setattr(layout, "update_transaction", fail)
    with pytest.raises(OSError):
        prepare(update_env)
    assert (data / "backups" / ".restore.lock").exists()
    monkeypatch.setattr(layout, "update_transaction", record)
    assert module().abort_preparation(root, data_dir=data)["aborted"]
    assert not (data / "backups" / ".restore.lock").exists()
    assert backup.sha256_of(db) == original


@pytest.mark.parametrize("unsafe", ["pointer", "children", "launchPending", "marker"])
def test_abort_preparation_refuses_uncertain_state(update_env, monkeypatch, unsafe):
    root, data, _ = update_env
    record = layout.update_transaction
    def fail(*args, **kwargs):
        raise OSError("journal unavailable")
    monkeypatch.setattr(layout, "update_transaction", fail)
    with pytest.raises(OSError):
        prepare(update_env)
    monkeypatch.setattr(layout, "update_transaction", record)
    if unsafe == "pointer":
        layout.write_current_state(root, "2.0.0")
    elif unsafe == "children":
        layout.update_transaction(root, layout.STAGE_STAGED, children=[{"pid": 987654321, "stopped": True}])
    elif unsafe == "launchPending":
        layout.update_transaction(root, layout.STAGE_STAGED, launchPending=True)
    else:
        marker_path = data / "backups" / ".restore.lock"
        marker = json.loads(marker_path.read_text())
        marker["attemptId"] = "unrelated-attempt"
        marker_path.write_text(json.dumps(marker))
    with pytest.raises(RuntimeError):
        module().abort_preparation(root, data_dir=data)
    assert (data / "backups" / ".restore.lock").exists()


def test_released_flag_requires_durable_journal_before_marker_release(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    module().rollback(root, children_stopped=True)
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK)
    fsync = os.fsync
    def fail_terminal_flush(fd):
        if layout.read_transaction(root).get("dataRollbackReleased"):
            raise OSError("terminal flush unavailable")
        return fsync(fd)
    monkeypatch.setattr(module().os, "fsync", fail_terminal_flush)
    with pytest.raises(OSError, match="terminal flush unavailable"):
        module().finish_rollback(root)
    assert layout.read_transaction(root)["dataRollbackReleased"]
    with pytest.raises(OSError, match="terminal flush unavailable"):
        module().finish_rollback(root)
    assert (data / "backups" / ".restore.lock").exists()
    monkeypatch.setattr(module().os, "fsync", fsync)
    assert module().finish_rollback(root)["released"]
    assert not (data / "backups" / ".restore.lock").exists()


@pytest.mark.parametrize("schema,same_directory,expected", [(1, True, ["1.0.0", "2.0.0"]), (18, True, ["2.0.0"]), (1, False, ["2.0.0"])])
def test_accept_records_compatibility_for_actual_schema(update_env, schema, same_directory, expected):
    root, data, db = update_env
    layout.write_current_state(root, "1.0.0", dataCompatibility={
        "dataDir": str(data if same_directory else data.parent / "other"),
        "schemaVersion": 1, "versions": ["1.0.0"],
    })
    prepare(update_env)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"PRAGMA user_version={schema}")
    module().accept(root)
    assert layout.read_transaction(root)["dataSchemaVersion"] == schema
    mapping = layout.read_current_state(root)["dataCompatibility"]
    assert mapping == {"dataDir": str(data), "schemaVersion": schema, "versions": expected}


def test_accepted_metadata_repair_does_not_read_database_or_remove_later_versions(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    module().accept(root)
    layout.write_current_state(root, "3.0.0", dataCompatibility={
        "dataDir": str(data), "schemaVersion": 1, "versions": ["2.0.0", "3.0.0"],
    })
    def no_database_read(*args, **kwargs):
        raise AssertionError("accepted metadata repair must not read the database")
    monkeypatch.setattr(sqlite3, "connect", no_database_read)
    module().accept(root)
    assert layout.read_current_state(root)["dataCompatibility"]["versions"] == ["2.0.0", "3.0.0"]


def test_accepted_missing_mapping_is_repaired_from_journal_without_database(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    module().accept(root)
    state = layout.read_current_state(root)
    state.pop("dataCompatibility")
    (root / layout.STATE_DIR_NAME / layout.CURRENT_STATE_FILE).write_text(json.dumps(state))
    def no_database_read(*args, **kwargs):
        raise AssertionError("accepted metadata repair must not read the database")
    monkeypatch.setattr(sqlite3, "connect", no_database_read)
    module().accept(root)
    assert layout.read_data_compatibility(root) == {"dataDir": str(data), "schemaVersion": 1, "versions": ["2.0.0"]}


def test_compatibility_flush_failure_keeps_marker_and_can_resume_acceptance(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    fsync = os.fsync
    def fail_mapping_flush(fd):
        current = layout.read_current_state(root)
        if current.get("dataCompatibility"):
            raise OSError("compatibility flush failed")
        return fsync(fd)
    monkeypatch.setattr(module().os, "fsync", fail_mapping_flush)
    with pytest.raises(OSError, match="compatibility flush failed"):
        module().accept(root)
    assert layout.read_transaction(root)["dataAccepted"]
    assert (data / "backups" / ".restore.lock").exists()
    with pytest.raises(OSError):
        module().accept(root)
    monkeypatch.setattr(module().os, "fsync", fsync)
    module().accept(root)
    assert not (data / "backups" / ".restore.lock").exists()


def test_schema_reader_is_read_only_and_never_creates_missing_database(update_env):
    _, data, db = update_env
    assert module().read_schema_version(data) == 1
    db.unlink()
    with pytest.raises(RuntimeError):
        module().read_schema_version(data)
    assert not db.exists()


def test_startup_cleans_only_safe_dead_maintenance_marker(update_env, monkeypatch):
    _, data, _ = update_env
    marker = module()._marker_path(data)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 901, "purpose": "maintenance"}))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: False)
    assert module().validate_startup(data) is None
    assert not marker.exists()


def test_startup_preserves_dangerous_restore_marker_with_recovery_message(update_env, monkeypatch):
    _, data, _ = update_env
    marker = module()._marker_path(data)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"pid": 901, "purpose": "restore", "rollbackPath": "original.rollback.db"}))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: False)
    with pytest.raises(RuntimeError, match="原库"):
        module().validate_startup(data)
    assert marker.exists()


def test_accepted_repair_keeps_restore_protection_and_original_location(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    module().accept(root)
    original = data / "original.rollback.db"
    original.write_bytes(b"preserved original")
    marker = module()._marker_path(data)
    marker.write_text(json.dumps({"pid": 901, "purpose": "restore", "rollbackPath": str(original)}))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: False)
    with pytest.raises(RuntimeError, match="原库") as error:
        module().accept(root)
    assert str(original) in str(error.value)
    assert marker.exists()
    assert original.read_bytes() == b"preserved original"


def test_accepted_repair_cleans_safe_dead_maintenance_only(update_env, monkeypatch):
    root, data, _ = update_env
    prepare(update_env)
    module().accept(root)
    marker = module()._marker_path(data)
    marker.write_text(json.dumps({"pid": 901, "purpose": "maintenance"}))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda pid: False)
    module().accept(root)
    assert not marker.exists()
