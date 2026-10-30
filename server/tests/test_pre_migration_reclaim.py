"""迁移前快照保留窗回收的单元测试（纯文件操作，不需要引擎）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.core import database as dbm


def _seed_bak(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "holdexar.db"
    src.write_bytes(b"x" * 64)
    bak = src.with_name(src.name + dbm._PRE_MIGRATION_SUFFIX)
    bak.write_bytes(b"b" * 64)
    return src, bak


def test_reclaim_deletes_expired_snapshot(tmp_path):
    src, bak = _seed_bak(tmp_path)
    old = time.time() - (dbm._PRE_MIGRATION_RETENTION.total_seconds() + 3600)
    os.utime(bak, (old, old))
    dbm._reclaim_stale_pre_migration_snapshots(src)
    assert not bak.exists()


def test_reclaim_keeps_snapshot_within_retention(tmp_path):
    src, bak = _seed_bak(tmp_path)
    dbm._reclaim_stale_pre_migration_snapshots(src)
    assert bak.exists()


def test_reclaim_noop_without_snapshot(tmp_path):
    src = tmp_path / "holdexar.db"
    src.write_bytes(b"x" * 64)
    dbm._reclaim_stale_pre_migration_snapshots(src)
    assert not src.with_name(src.name + dbm._PRE_MIGRATION_SUFFIX).exists()
