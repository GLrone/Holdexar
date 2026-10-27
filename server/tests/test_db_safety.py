from __future__ import annotations

import asyncio
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import backup, database, db_safety


@pytest.mark.parametrize("stage", ["write", "partial_write", "flush", "fsync", "close"])
def test_failed_marker_write_closes_real_handle_and_allows_retry(tmp_path, monkeypatch, stage):
    lock = tmp_path / "backups" / ".restore.lock"
    streams = []
    fdopen = os.fdopen

    class FailingStream:
        def __init__(self, stream):
            self.stream = stream
            streams.append(stream)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()
            if stage == "close":
                raise OSError(errno.ENOSPC, "marker close failed")

        def write(self, payload):
            if stage == "write":
                raise OSError(errno.ENOSPC, "marker write failed")
            if stage == "partial_write":
                self.stream.write(payload[:12])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "marker partial_write failed")
            return self.stream.write(payload)

        def flush(self):
            if stage == "flush":
                raise OSError(errno.ENOSPC, "marker flush failed")
            self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def fail_sync(_fd):
        raise OSError(errno.ENOSPC, "marker fsync failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", lambda *a, **kw: FailingStream(fdopen(*a, **kw)))
        if stage == "fsync":
            patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(OSError, match=f"marker {stage} failed"):
            db_safety._acquire_restore_lock(lock_path=lock, purpose="update")

    assert streams and all(stream.closed for stream in streams)
    assert not lock.exists()
    acquired, token = db_safety._acquire_restore_lock(lock_path=lock, purpose="update")
    db_safety._release_restore_lock(acquired, token)
    assert not lock.exists()


def test_fdopen_failure_closes_raw_descriptor_and_removes_owned_marker(tmp_path, monkeypatch):
    lock = tmp_path / ".restore.lock"
    descriptors = []

    def fail_fdopen(fd, *_args, **_kwargs):
        descriptors.append(fd)
        raise OSError(errno.EMFILE, "fdopen failed")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen failed"):
        db_safety._acquire_restore_lock(lock_path=lock)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not lock.exists()


@pytest.mark.parametrize("replace_file", [True, False])
def test_failed_marker_write_preserves_replacement_owner(tmp_path, monkeypatch, replace_file):
    lock = tmp_path / ".restore.lock"
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps({"token": "another-owner", "purpose": "restore"}))
    fdopen = os.fdopen
    close = os.close
    descriptors = []

    class ReplacedStream:
        def __init__(self, stream):
            self.stream = stream
            descriptors.append(stream.fileno())

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def write(self, _payload):
            raise OSError(errno.ENOSPC, "marker write failed")

    def close_and_replace(fd):
        close(fd)
        if fd in descriptors:
            if replace_file:
                os.replace(replacement, lock)
            else:
                lock.write_bytes(replacement.read_bytes())

    monkeypatch.setattr(os, "close", close_and_replace)
    monkeypatch.setattr(os, "fdopen", lambda *a, **kw: ReplacedStream(fdopen(*a, **kw)))
    with pytest.raises(OSError, match="marker write failed"):
        db_safety._acquire_restore_lock(lock_path=lock)
    assert json.loads(lock.read_text())["token"] == "another-owner"


def test_backup_and_database_bind_shared_safety_implementation(tmp_path):
    for name in ("_release_restore_lock", "snapshot_to", "sha256_of", "_verify_file"):
        assert getattr(backup, name) is getattr(db_safety, name), name
    assert database._CrossProcessWriteLock is db_safety._CrossProcessWriteLock


@pytest.mark.asyncio
async def test_backend_and_update_writers_share_exclusion_and_release(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(db_safety, "_WRITE_WAIT_TIMEOUT", 0.05)
    monkeypatch.setattr(database, "_WRITE_WAIT_TIMEOUT", 0.05)
    assert database._process_write_lock_path(db) == db_safety._process_write_lock_path(db)

    def update_writer():
        with db_safety.sync_write_gate(db):
            return "entered"

    lock = await database._CrossProcessWriteLock(database._process_write_lock_path(db)).acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(TimeoutError):
                executor.submit(update_writer).result(timeout=2)
    finally:
        lock.release()
    assert await asyncio.to_thread(update_writer) == "entered"

    with db_safety.sync_write_gate(db):
        with pytest.raises(TimeoutError):
            await database._CrossProcessWriteLock(database._process_write_lock_path(db)).acquire()
    retry = await database._CrossProcessWriteLock(database._process_write_lock_path(db)).acquire()
    retry.release()
