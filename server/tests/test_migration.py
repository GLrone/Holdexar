"""历史数据安全迁移单元测试。

验证 AppData 向便携式 data/ 目录的安全迁移：
1. 正常只读迁移：SQLite VACUUM INTO、辅助目录复制、说明与元数据写入；
2. 源数据完整性保护：原文件只读访问，不删除不修改；
3. 冲突与幂等保护：目标已有库时禁止覆盖；
4. 损坏拦截：完整性校验失败时回滚并清理临时产物。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.core import migration, paths


def _create_sample_db(path: Path, table_name: str = "items") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path.as_posix())
    try:
        conn.execute(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(f"INSERT INTO {table_name} (name) VALUES ('holdexar_val')")
        conn.commit()
    finally:
        conn.close()


def test_detect_locations_fresh(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    res = migration.detect_legacy_data_locations(install_root, slug="test_app")
    assert res["isFresh"] is True
    assert res["hasTargetDb"] is False
    assert res["hasAppdataDb"] is False
    assert res["hasConflict"] is False


def test_detect_and_migrate_appdata(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    install_root.mkdir()
    target_data = install_root / "data"

    local_appdata = tmp_path / "appdata_local"
    local_appdata.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    src_dir = local_appdata / "test_app"
    src_db = src_dir / "test_app.db"
    _create_sample_db(src_db)

    # 构造辅助目录
    (src_dir / "config").mkdir(parents=True, exist_ok=True)
    (src_dir / "config" / "user.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (src_dir / "logs").mkdir(parents=True, exist_ok=True)
    (src_dir / "logs" / "sample.log").write_text("historical log\n", encoding="utf-8")

    # 检测
    locs = migration.detect_legacy_data_locations(install_root, slug="test_app")
    assert locs["isFresh"] is False
    assert locs["hasAppdataDb"] is True
    assert locs["hasTargetDb"] is False
    assert locs["appdataDataDir"] == src_dir

    # 执行迁移
    res = migration.migrate_appdata_to_portable(src_dir, target_data, slug="test_app")
    assert res["migrated"] is True
    assert (target_data / "test_app.db").is_file()
    assert (target_data / "config" / "user.json").read_text(encoding="utf-8") == '{"theme": "dark"}'
    assert (target_data / "migration.json").is_file()
    assert (target_data / "MIGRATION_FROM_APPDATA.txt").is_file()

    assert src_db.is_file()
    assert (src_dir / "config" / "user.json").is_file()

    # 验证迁移后的库可用性
    conn = sqlite3.connect((target_data / "test_app.db").as_posix())
    try:
        row = conn.execute("SELECT name FROM items WHERE id = 1").fetchone()
        assert row[0] == "holdexar_val"
    finally:
        conn.close()


def test_migrate_conflict_keeps_both_databases_when_source_is_larger(tmp_path):
    src_dir = tmp_path / "src"
    target_dir = tmp_path / "target"
    # 源数据库有 games 记录，权重远高于目标库
    _create_sample_db(src_dir / "test_app.db", "games")
    _create_sample_db(target_dir / "test_app.db", "empty_placeholder")

    before = [(directory / "test_app.db").read_bytes() for directory in (src_dir, target_dir)]
    res = migration.migrate_appdata_to_portable(src_dir, target_dir, slug="test_app")
    assert res["completed"] is False
    assert res["status"] == migration.MIGRATION_CONFLICT
    assert [(directory / "test_app.db").read_bytes() for directory in (src_dir, target_dir)] == before


def test_migrate_conflict_keeps_both_databases_when_target_is_larger(tmp_path):
    src_dir = tmp_path / "src"
    target_dir = tmp_path / "target"
    # 目标库有 games 记录
    _create_sample_db(src_dir / "test_app.db", "old_scratch")
    _create_sample_db(target_dir / "test_app.db", "games")

    before = [(directory / "test_app.db").read_bytes() for directory in (src_dir, target_dir)]
    res = migration.migrate_appdata_to_portable(src_dir, target_dir, slug="test_app")
    assert res["completed"] is False
    assert res["status"] == migration.MIGRATION_CONFLICT
    assert [(directory / "test_app.db").read_bytes() for directory in (src_dir, target_dir)] == before


def test_migrate_corrupted_aborts_cleanly(tmp_path):
    src_dir = tmp_path / "src"
    target_dir = tmp_path / "target"
    src_dir.mkdir()
    # 写入非 SQLite 的垃圾字节
    (src_dir / "test_app.db").write_bytes(b"not a valid sqlite file")

    with pytest.raises(Exception):
        migration.migrate_appdata_to_portable(src_dir, target_dir, slug="test_app")

    # 目标目录中不应残留半成品 db 文件
    assert not (target_dir / "test_app.db").exists()
    assert not (target_dir / "test_app.db.migrating.tmp").exists()


def test_migration_snapshots_active_wal_and_keeps_source(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    db = source / "test_app.db"
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE items (value TEXT)")
        writer.execute("INSERT INTO items VALUES ('committed in WAL')")
        writer.commit()
        before = {path.name: path.read_bytes() for path in source.iterdir() if not path.name.endswith("-shm")}
        result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
        assert result["completed"]
        assert db.exists()
        assert {path.name: path.read_bytes() for path in source.iterdir() if not path.name.endswith("-shm")} == before
        assert (source / "test_app.db-shm").exists()
        with sqlite3.connect(target / "test_app.db") as migrated:
            assert migrated.execute("SELECT value FROM items").fetchall() == [("committed in WAL",)]


def test_completed_migration_reuses_existing_provenance(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert (source / "test_app.db").exists()
    with sqlite3.connect(target / "test_app.db") as conn:
        conn.execute("INSERT INTO items (name) VALUES ('new target data')")
    result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert result["completed"] and not result["migrated"]
    assert result["status"] == migration.MIGRATION_REUSED
    with sqlite3.connect(target / "test_app.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == (2,)


def test_snapshot_verification_failure_preserves_source(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    before = (source / "test_app.db").read_bytes()
    monkeypatch.setattr(migration, "_verify_snapshot", lambda _path: "failed")
    with pytest.raises(RuntimeError):
        migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert (source / "test_app.db").read_bytes() == before
    assert not (target / "test_app.db").exists()


def test_target_created_during_snapshot_is_never_overwritten(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    original_verify = migration._verify_snapshot
    def verify(path):
        _create_sample_db(target / "test_app.db", "independent")
        return original_verify(path)
    monkeypatch.setattr(migration, "_verify_snapshot", verify)
    with pytest.raises(FileExistsError):
        migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert (source / "test_app.db").exists()
    with sqlite3.connect(target / "test_app.db") as conn:
        assert conn.execute("SELECT name FROM independent").fetchall() == [("holdexar_val",)]


def test_migration_snapshot_during_committed_wal_writes(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    db = source / "test_app.db"
    started, stop = threading.Event(), threading.Event()
    failures = []
    def write_batches():
        try:
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("CREATE TABLE items (value INTEGER)")
                while not stop.is_set():
                    conn.executemany("INSERT INTO items VALUES (?)", [(number,) for number in range(10)])
                    conn.commit()
                    started.set()
                    stop.wait(0.001)
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=write_batches)
    thread.start()
    try:
        assert started.wait(5)
        result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
        assert result["completed"]
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    with sqlite3.connect(target / "test_app.db") as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count >= 10 and count % 10 == 0
    assert db.is_file()


def test_migration_provenance_write_failure_preserves_both_copies(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    original_replace = Path.replace
    def replace(path, destination):
        if Path(destination).name == "migration.json":
            raise PermissionError("metadata storage unavailable")
        return original_replace(path, destination)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError):
        migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert (source / "test_app.db").is_file()
    with sqlite3.connect(target / "test_app.db") as conn:
        assert conn.execute("SELECT name FROM items").fetchall() == [("holdexar_val",)]
    assert not (target / "migration.json").exists()
    assert not migration.resolve_data_dir_conflict(target, source, slug="test_app")["completed"]


@pytest.mark.parametrize("legacy_completed", [False, True])
def test_partial_migration_resumes_missing_files_without_overwriting(tmp_path, monkeypatch, legacy_completed):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    (source / "config").mkdir()
    (source / "config" / "missing.json").write_text("source settings")
    (source / "config" / "preserved.json").write_text("old settings")
    original_copy = migration.shutil.copy2
    def copy(src, dst, *args, **kwargs):
        if Path(src).name == "missing.json":
            Path(dst).write_bytes(b"partial")
            raise PermissionError("config temporarily unavailable")
        return original_copy(src, dst, *args, **kwargs)
    monkeypatch.setattr(migration.shutil, "copy2", copy)
    first = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert first["completed"] is False
    assert first["status"] == migration.MIGRATION_INCOMPLETE
    assert first["warnings"]
    assert not (target / "config" / "missing.json").exists()
    metadata = target / "migration.json"
    state = json.loads(metadata.read_text(encoding="utf-8"))
    assert state["completed"] is False
    if legacy_completed:
        state["completed"] = True
        metadata.write_text(json.dumps(state), encoding="utf-8")
    (target / "config" / "preserved.json").write_text("new target settings")
    database_before = (target / "test_app.db").read_bytes()
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    second = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert second["completed"] and not second["warnings"]
    assert (target / "config" / "missing.json").read_text() == "source settings"
    assert (target / "config" / "preserved.json").read_text() == "new target settings"
    assert (source / "config" / "missing.json").read_text() == "source settings"
    assert (target / "test_app.db").read_bytes() == database_before
    assert json.loads(metadata.read_text(encoding="utf-8"))["status"] == migration.MIGRATION_COMPLETED


def test_migration_copies_existing_key_material_before_startup(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    names = ("secret.master", "secret.master.staging", "secret.salt", "secret.machine.key")
    for name in names:
        (source / name).write_bytes(name.encode())
    result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert result["completed"]
    for name in names:
        assert (source / name).read_bytes() == (target / name).read_bytes()


def test_launcher_stops_until_missing_key_material_is_retried(tmp_path, monkeypatch):
    from app.core import launcher, layout

    root, source = tmp_path / "install", tmp_path / "source"
    _create_sample_db(source / "holdexar.db")
    (source / "secret.master").write_bytes(b"existing master record")
    exe = layout.get_version_exe(root, "1.0.0")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"program")
    layout.write_current_state(root, "1.0.0")
    original_copy = migration.shutil.copy2
    def copy(src, dst, *args, **kwargs):
        if Path(src).name == "secret.master":
            raise PermissionError("key temporarily unavailable")
        return original_copy(src, dst, *args, **kwargs)
    monkeypatch.setattr(migration.shutil, "copy2", copy)
    monkeypatch.setattr(launcher, "detect_legacy_data_locations", lambda _root: {
        "appdataDataDir": source, "hasAppdataDb": True,
        "hasTargetDb": (root / "data" / "holdexar.db").exists(),
        "hasConflict": (root / "data" / "holdexar.db").exists(),
    })
    errors, launched = [], []
    monkeypatch.setattr(launcher, "_show_fatal", errors.append)
    monkeypatch.setattr(launcher, "launch_version", lambda *args, **kwargs: launched.append(args))
    assert launcher.run_launcher(install_root_override=root, argv=[]) == 1
    assert errors and not launched
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    assert launcher.run_launcher(install_root_override=root, argv=[]) == 0
    assert launched
    assert (root / "data" / "secret.master").read_bytes() == b"existing master record"
    assert (source / "secret.master").read_bytes() == b"existing master record"


def test_existing_different_key_material_is_preserved_and_blocks_migration(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    (source / "secret.master").write_bytes(b"source master")
    target.mkdir()
    (target / "secret.master").write_bytes(b"independent target master")
    result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert not result["completed"]
    assert result["status"] == migration.MIGRATION_INCOMPLETE
    assert (source / "secret.master").read_bytes() == b"source master"
    assert (target / "secret.master").read_bytes() == b"independent target master"


def test_missing_required_key_cannot_turn_partial_migration_into_completed(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    key = source / "secret.master"
    key.write_bytes(b"required key")
    original_copy = migration.shutil.copy2
    monkeypatch.setattr(migration.shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("unavailable")))
    assert not migration.migrate_appdata_to_portable(source, target, slug="test_app")["completed"]
    key.rename(source / "temporarily-unavailable-key")
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
    assert not result["completed"] and result["warnings"]
    assert result["status"] == migration.MIGRATION_INCOMPLETE


def test_pending_migration_stops_launcher_when_original_source_is_absent(tmp_path, monkeypatch):
    from app.core import launcher, layout

    root, local = tmp_path / "install", tmp_path / "appdata"
    source = local / "holdexar"
    _create_sample_db(source / "holdexar.db")
    (source / "secret.master").write_bytes(b"required key")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    original_copy = migration.shutil.copy2
    monkeypatch.setattr(migration.shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("unavailable")))
    assert not migration.migrate_appdata_to_portable(source, root / "data")["completed"]
    offline = tmp_path / "source-offline"
    assert source.resolve().is_relative_to(tmp_path.resolve()) and offline.resolve().is_relative_to(tmp_path.resolve())
    source.rename(offline)
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    exe = layout.get_version_exe(root, "1.0.0")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"program")
    layout.write_current_state(root, "1.0.0")
    errors, launched = [], []
    monkeypatch.setattr(launcher, "_show_fatal", errors.append)
    monkeypatch.setattr(launcher, "launch_version", lambda *args, **kwargs: launched.append(args))
    assert launcher.run_launcher(install_root_override=root, argv=[]) == 1
    assert errors and not launched


def test_migrated_legacy_key_decrypts_existing_credential(tmp_path):
    from app.core import secretbox

    source, target = tmp_path / "source", tmp_path / "target"
    _create_sample_db(source / "test_app.db")
    token = secretbox.encrypt_with("sandbox credential", "migration-test", secretbox.legacy_master(source))
    assert migration.migrate_appdata_to_portable(source, target, slug="test_app")["completed"]
    assert secretbox.decrypt_with(token, "migration-test", secretbox.legacy_master(target)) == "sandbox credential"


def test_partial_migration_does_not_follow_target_directory_junction(tmp_path, monkeypatch):
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction boundary")
    source, target, outside = tmp_path / "source", tmp_path / "target", tmp_path / "outside"
    _create_sample_db(source / "test_app.db")
    (source / "config").mkdir()
    (source / "config" / "settings.json").write_text("source settings")
    original_copy = migration.shutil.copy2
    monkeypatch.setattr(migration.shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("unavailable")))
    assert not migration.migrate_appdata_to_portable(source, target, slug="test_app")["completed"]
    (target / "config").rmdir()
    outside.mkdir()
    (outside / "sentinel").write_text("preserve")
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target / "config"), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    try:
        result = migration.migrate_appdata_to_portable(source, target, slug="test_app")
        assert not result["completed"] and result["warnings"]
        assert not (outside / "settings.json").exists()
        assert (outside / "sentinel").read_text() == "preserve"
    finally:
        (target / "config").rmdir()
