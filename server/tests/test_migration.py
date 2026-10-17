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

    # 验证原文件毫发无损
    assert src_db.is_file()
    assert (src_dir / "config" / "user.json").is_file()

    # 验证迁移后的库可用性
    conn = sqlite3.connect((target_data / "test_app.db").as_posix())
    try:
        row = conn.execute("SELECT name FROM items WHERE id = 1").fetchone()
        assert row[0] == "holdexar_val"
    finally:
        conn.close()


def test_migrate_conflict_prevented(tmp_path):
    src_dir = tmp_path / "src"
    target_dir = tmp_path / "target"
    _create_sample_db(src_dir / "test_app.db", "src_table")
    _create_sample_db(target_dir / "test_app.db", "target_table")

    # 目标已存在库，拒绝覆盖
    res = migration.migrate_appdata_to_portable(src_dir, target_dir, slug="test_app")
    assert res["migrated"] is False
    assert res["reason"] == "dest_db_exists"


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
