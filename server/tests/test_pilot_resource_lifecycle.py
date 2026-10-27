import os
from pathlib import Path

import pytest

from app.domains.pilot import attachments, flywheel
from app.domains.pilot.session import SessionStore


def _conversation(root, sid):
    base = root / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{sid}.jsonl").write_text('{"t":"turn","q":"keep"}\n', encoding="utf-8")
    directory = root / "attachments" / sid
    directory.mkdir(parents=True)
    (directory / "012345abcdef.txt").write_text("attached", encoding="utf-8")
    return directory


@pytest.mark.asyncio
async def test_store_deletes_only_its_own_attachment_root(tmp_path, monkeypatch):
    own = _conversation(tmp_path / "own", "s-one")
    other = _conversation(tmp_path / "other", "s-one")
    monkeypatch.setattr(attachments, "base_dir", lambda: other.parent)
    store = SessionStore(tmp_path / "own" / "sessions")
    assert await store.delete_with_reason("s-one") == "deleted"
    assert not own.exists()
    assert other.exists()


def test_capacity_sweep_batches_decisions_and_deletes_attachments(tmp_path, monkeypatch):
    from app.domains.pilot import session

    monkeypatch.setattr(session, "SESSION_MAX_FILES", 1)
    for i in range(3):
        _conversation(tmp_path, f"s-{i}")
        os.utime(tmp_path / "sessions" / f"s-{i}.jsonl", (i + 1, i + 1))
        flywheel.append_decision(tmp_path / "sessions", {"sid": f"s-{i}"})
    calls = []
    original = flywheel.purge_sessions

    def record(base, ids):
        calls.append(set(ids))
        return original(base, ids)

    monkeypatch.setattr(flywheel, "purge_sessions", record)
    SessionStore(tmp_path / "sessions")
    assert calls == [{"s-0", "s-1"}]
    assert sorted(p.name for p in (tmp_path / "attachments").iterdir()) == ["s-2"]
    assert flywheel.decisions_path(tmp_path / "sessions").read_text().count('"sid"') == 1


@pytest.mark.asyncio
async def test_attachment_cleanup_failure_does_not_leave_dangling_conversation(tmp_path, monkeypatch):
    directory = _conversation(tmp_path, "s-one")
    original = Path.unlink

    def fail_attachment(path, *args, **kwargs):
        if path.parent == directory:
            raise PermissionError("fixture blocked attachment")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_attachment)
    store = SessionStore(tmp_path / "sessions")
    assert await store.delete_with_reason("s-one") == "deleted"
    assert not (store.base / "s-one.jsonl").exists()
    assert directory.exists()


@pytest.mark.asyncio
async def test_conversation_delete_failure_keeps_all_attachments(tmp_path, monkeypatch):
    directory = _conversation(tmp_path, "s-one")
    original = Path.unlink
    store = SessionStore(tmp_path / "sessions")

    def fail_conversation(path, *args, **kwargs):
        if path == store.base / "s-one.jsonl":
            raise PermissionError("fixture blocked conversation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_conversation)
    assert await store.delete_with_reason("s-one") == "delete_failed"
    assert (store.base / "s-one.jsonl").exists()
    assert (directory / "012345abcdef.txt").read_text() == "attached"
