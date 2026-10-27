import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.database import get_session_factory, init_db, write_session
from app.domains.proxypool.bootstrap import subscription_sync_lock
from app.domains.proxypool.models import SubscriptionSnapshot
from app.domains.proxypool.retention import prune_telemetry
from app.domains.proxypool.subscription import snapshot_dir


NOW = datetime(2026, 10, 25)


@pytest.fixture(autouse=True)
def isolated_subscription_locks(monkeypatch):
    from app.domains.proxypool import bootstrap

    monkeypatch.setattr(bootstrap, "_subscription_sync_locks", {})


async def _snapshots(root, *, outside=None):
    await init_db()
    paths = []
    async with write_session() as session:
        for sha in ("0", "1", "2", "3", "1"):
            digest = sha * 64
            path = snapshot_dir(root) / "1" / f"{digest}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture raw")
            paths.append(path)
            session.add(SubscriptionSnapshot(
                subscription_id=1, sha256=digest, format="yaml", node_count=1,
                status="OK", fetched_at=NOW - timedelta(days=300),
                raw_path=str(outside if outside and sha == "0" else path),
            ))
        await session.commit()
    return paths


@pytest.mark.asyncio
async def test_retention_reclaims_only_unreferenced_deleted_snapshot_files(tmp_data_dir):
    paths = await _snapshots(tmp_data_dir)
    async with get_session_factory()() as session:
        result = await prune_telemetry(session, NOW, chunk=1)
        remaining = list((await session.execute(select(SubscriptionSnapshot))).scalars())
    assert result.snapshots == 2
    assert len(remaining) == 3
    assert not paths[0].exists()
    assert all(path.exists() for path in paths[1:])


@pytest.mark.asyncio
async def test_snapshot_cleanup_does_not_follow_unowned_raw_path(tmp_data_dir):
    outside = tmp_data_dir / "user-owned.bin"
    outside.write_bytes(b"user data")
    await _snapshots(tmp_data_dir, outside=outside)
    async with get_session_factory()() as session:
        await prune_telemetry(session, NOW)
    assert outside.read_bytes() == b"user data"


@pytest.mark.asyncio
async def test_protected_subscription_does_not_starve_later_deletions(tmp_data_dir):
    paths = await _snapshots(tmp_data_dir)
    async with write_session() as session:
        await session.execute(delete(SubscriptionSnapshot).where(SubscriptionSnapshot.id < 3))
        for i in range(4):
            session.add(SubscriptionSnapshot(
                subscription_id=2, sha256="a" * 64, format="yaml", node_count=1,
                status="OK", fetched_at=NOW - timedelta(days=300),
            ))
        await session.commit()
    async with get_session_factory()() as session:
        result = await prune_telemetry(session, NOW, chunk=1, max_chunks=1)
    assert result.snapshots == 1
    assert all(path.exists() for path in paths[2:])


@pytest.mark.asyncio
async def test_retention_waits_for_subscription_writer_before_rechecking_refs(tmp_data_dir):
    paths = await _snapshots(tmp_data_dir)
    lock = subscription_sync_lock(1)
    async with lock:
        async def prune():
            async with get_session_factory()() as session:
                return await prune_telemetry(session, NOW)

        pending = asyncio.create_task(prune())
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            async with write_session() as session:
                session.add(SubscriptionSnapshot(
                    subscription_id=1, sha256="0" * 64, format="yaml", node_count=1,
                    status="OK", fetched_at=NOW, raw_path=str(paths[0]),
                ))
                await session.commit()
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
    await pending
    assert paths[0].exists()
