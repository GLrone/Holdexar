"""安装布局、版本指针与事务状态机单元测试。"""
from __future__ import annotations

import time
import os
import subprocess
from pathlib import Path

import pytest

from app.core import layout
from app.core.app_info import APP_NAME


def _create_mock_version(install_root: Path, version: str, has_exe: bool = True) -> Path:
    vdir = layout.get_version_dir(install_root, version)
    vdir.mkdir(parents=True, exist_ok=True)
    if has_exe:
        (vdir / f"{APP_NAME}.exe").write_bytes(b"mock_exe")
    return vdir


def test_current_state_roundtrip(tmp_path):
    root = tmp_path / "app"
    assert layout.read_current_state(root) is None
    assert layout.get_current_version(root) is None

    st = layout.write_current_state(root, "0.3.5", last_known_good="0.3.4")
    assert st["version"] == "0.3.5"
    assert st["lastKnownGood"] == "0.3.4"

    read_back = layout.read_current_state(root)
    assert read_back is not None
    assert read_back["version"] == "0.3.5"
    assert read_back["lastKnownGood"] == "0.3.4"
    assert layout.get_current_version(root) == "0.3.5"


def test_transaction_lifecycle(tmp_path):
    root = tmp_path / "app"
    assert layout.read_transaction(root) is None

    # 1. 开启事务
    tx = layout.start_transaction(
        root, target_version="0.3.6", source_version="0.3.5", attempt_id="att-1"
    )
    assert tx["attemptId"] == "att-1"
    assert tx["stage"] == layout.STAGE_STAGED
    assert tx["targetVersion"] == "0.3.6"
    assert tx["sourceVersion"] == "0.3.5"

    # 2. 状态推进
    tx2 = layout.update_transaction(root, stage=layout.STAGE_SWITCHING)
    assert tx2["stage"] == layout.STAGE_SWITCHING

    # 3. 记录失败
    tx3 = layout.update_transaction(root, stage=layout.STAGE_FAILED, error="Timeout")
    assert tx3["stage"] == layout.STAGE_FAILED
    assert tx3["error"] == "Timeout"

    # 4. 清理
    layout.clear_transaction(root)
    assert layout.read_transaction(root) is None


def test_version_validation_and_listing(tmp_path):
    root = tmp_path / "app"
    _create_mock_version(root, "0.3.3", has_exe=True)
    _create_mock_version(root, "0.3.4", has_exe=False)  # 损坏版本：缺 exe
    _create_mock_version(root, "0.3.5", has_exe=True)

    assert layout.is_version_valid(root, "0.3.3") is True
    assert layout.is_version_valid(root, "0.3.4") is False
    assert layout.is_version_valid(root, "0.3.5") is True

    installed = layout.list_installed_versions(root)
    assert set(installed) == {"0.3.3", "0.3.5"}


def test_version_retention_and_pruning(tmp_path):
    root = tmp_path / "app"
    # 创建 4 个有效历史版本
    for v in ["0.3.1", "0.3.2", "0.3.3", "0.3.4"]:
        _create_mock_version(root, v, has_exe=True)
        time.sleep(0.01)

    # 设定当前为 0.3.4，上一稳定版为 0.3.3
    layout.write_current_state(root, "0.3.4", last_known_good="0.3.3")

    pruned = layout.prune_versions(root, min_keep=2)
    assert set(pruned) == {"0.3.1", "0.3.2"}

    # 确认 0.3.4 与 0.3.3 保留，0.3.1 与 0.3.2 被删除
    remaining = layout.list_installed_versions(root)
    assert set(remaining) == {"0.3.3", "0.3.4"}
    assert not layout.get_version_dir(root, "0.3.1").exists()
    assert not layout.get_version_dir(root, "0.3.2").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("linked", ["versions", "version", "exe", "internal"])
def test_version_links_cannot_escape_installation(tmp_path, linked):
    root = tmp_path / "install"
    outside = tmp_path / "outside"
    version = "1.0.0"
    outside.mkdir()
    if linked == "versions":
        root.mkdir()
        target = outside
        link = root / "versions"
        (target / version).mkdir()
        (target / version / f"{APP_NAME}.exe").write_bytes(b"outside")
    else:
        (root / "versions").mkdir(parents=True)
        vdir = root / "versions" / version
        if linked == "version":
            target, link = outside, vdir
            (target / f"{APP_NAME}.exe").write_bytes(b"outside")
        else:
            vdir.mkdir()
            if linked == "exe":
                target = outside / f"{APP_NAME}.exe"
                target.write_bytes(b"outside")
                link = vdir / f"{APP_NAME}.exe"
                try:
                    link.symlink_to(target)
                except OSError as exc:
                    pytest.skip(f"File symlink unavailable: {exc}")
            else:
                (vdir / f"{APP_NAME}.exe").write_bytes(b"main")
                (outside / "runtime.dll").write_bytes(b"outside")
                target, link = outside, vdir / "_internal"
    if linked != "exe":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert link.is_junction()
    try:
        assert not layout.is_version_valid(root, version)
        if linked in {"versions", "version"}:
            with pytest.raises(ValueError):
                layout.get_version_dir(root, version)
        if linked == "exe":
            with pytest.raises(ValueError):
                layout.get_version_exe(root, version)
    finally:
        link.unlink() if linked == "exe" else link.rmdir()
    assert target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_version_junction_cannot_bypass_database_allowlist(tmp_path):
    import sqlite3

    root = tmp_path / "install"
    forbidden = _create_mock_version(root, "2.0.0")
    data = root / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "holdexar.db")
    try:
        conn.execute("PRAGMA user_version=32")
    finally:
        conn.close()
    layout.write_current_state(root, "1.0.0", dataCompatibility={
        "dataDir": str(data), "schemaVersion": 32, "versions": ["1.0.0"],
    })
    alias = root / "versions" / "1.0.0"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(forbidden)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        assert alias.is_junction()
        assert not layout.is_version_valid(root, "1.0.0")
        with pytest.raises(ValueError):
            layout.get_version_dir(root, "1.0.0")
        assert layout.find_target_version(root) is None
    finally:
        alias.rmdir()
    assert (forbidden / f"{APP_NAME}.exe").is_file()


def test_executable_link_to_another_filename_is_rejected(tmp_path, monkeypatch):
    directory = _create_mock_version(tmp_path, "1.0.0")
    exe = directory / f"{APP_NAME}.exe"
    other = directory / "other.exe"
    other.write_bytes(b"other program")
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        return original_resolve(other if path == exe else path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert not layout.is_version_valid(tmp_path, "1.0.0")
    with pytest.raises(ValueError):
        layout.get_version_exe(tmp_path, "1.0.0")
