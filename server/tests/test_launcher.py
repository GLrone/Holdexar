"""启动器（Launcher）单元测试。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.core import launcher, layout
from app.core.app_info import APP_NAME, ENV_PREFIX


def _create_version(install_root: Path, version: str, has_exe: bool = True) -> Path:
    vdir = layout.get_version_dir(install_root, version)
    vdir.mkdir(parents=True, exist_ok=True)
    if has_exe:
        (vdir / f"{APP_NAME}.exe").write_bytes(b"mock_binary")
    return vdir


def test_find_target_version_normal(tmp_path):
    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.5", last_known_good="0.3.4")

    target = launcher.find_target_version(root)
    assert target == "0.3.5"


def test_find_target_version_fallback_to_last_good(tmp_path):
    root = tmp_path / "app"
    # 0.3.6 损坏（无 exe），0.3.5 完好
    _create_version(root, "0.3.6", has_exe=False)
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.6", last_known_good="0.3.5")

    # 自动识别 0.3.6 损坏并降级到 0.3.5
    target = launcher.find_target_version(root)
    assert target == "0.3.5"

    # 指针自动自愈为 0.3.5
    st = layout.read_current_state(root)
    assert st["version"] == "0.3.5"


def test_find_target_version_no_state_picks_latest(tmp_path):
    root = tmp_path / "app"
    _create_version(root, "0.3.3", has_exe=True)
    time.sleep(0.02)
    _create_version(root, "0.3.4", has_exe=True)

    # 没有 current.json
    target = launcher.find_target_version(root)
    assert target == "0.3.4"
    assert layout.get_current_version(root) == "0.3.4"


def test_find_target_version_none_available(tmp_path):
    root = tmp_path / "app"
    _create_version(root, "0.3.3", has_exe=False)
    assert launcher.find_target_version(root) is None


def test_launch_version_env_and_args(tmp_path, monkeypatch):
    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)

    captured_cmd = None
    captured_env = None
    captured_cwd = None

    class MockPopen:
        def __init__(self, cmd, cwd=None, env=None, **kwargs):
            nonlocal captured_cmd, captured_env, captured_cwd
            captured_cmd = cmd
            captured_cwd = cwd
            captured_env = env

    monkeypatch.setattr(launcher.subprocess, "Popen", MockPopen)

    launcher.launch_version(
        root, "0.3.5", argv=["--server", "--port", "1234"], detached=False
    )

    expected_exe = str(layout.get_version_exe(root, "0.3.5"))
    assert captured_cmd == [expected_exe, "--server", "--port", "1234"]
    assert captured_cwd == str(layout.get_version_dir(root, "0.3.5"))
    assert captured_env[f"{ENV_PREFIX}INSTALL_ROOT"] == str(root)
    assert captured_env[f"{ENV_PREFIX}DATA_DIR"] == str(root / "data")
    assert captured_env["WEBVIEW2_USER_DATA_FOLDER"] == str(root / "data" / "cache" / "webview")


def test_run_launcher_data_conflict_stops(tmp_path, monkeypatch):
    """双库冲突检测：目标目录与 AppData 同时存在且未完成收口时，Launcher 拒绝启动。"""
    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.5")

    # 创建目标库
    target_data = root / "data"
    target_data.mkdir(parents=True)
    (target_data / "holdexar.db").write_bytes(b"target_db")

    # 模拟发现 AppData 库，形成冲突
    appdata_dir = tmp_path / "appdata"
    appdata_dir.mkdir(parents=True)
    (appdata_dir / "holdexar.db").write_bytes(b"appdata_db")

    monkeypatch.setattr(
        launcher,
        "detect_legacy_data_locations",
        lambda r: {
            "targetDataDir": target_data,
            "hasTargetDb": True,
            "appdataDataDir": appdata_dir,
            "hasAppdataDb": True,
            "hasConflict": True,
            "isFresh": False,
        },
    )
    monkeypatch.setattr(launcher, "_show_fatal", lambda msg: None)

    code = launcher.run_launcher(install_root_override=root, detached=False)
    assert code == 1


def test_run_launcher_data_conflict_auto_resolved(tmp_path, monkeypatch):
    """双库冲突时 Launcher 自动进行仲裁裁决，收编最全库并安全拉起。"""
    import sqlite3
    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.5")

    target_data = root / "data"
    target_data.mkdir(parents=True)
    target_db = target_data / "holdexar.db"
    conn = sqlite3.connect(target_db.as_posix())
    conn.execute("CREATE TABLE placeholder (id INT)")
    conn.commit()
    conn.close()

    appdata_dir = tmp_path / "appdata"
    appdata_dir.mkdir(parents=True)
    appdata_db = appdata_dir / "holdexar.db"
    conn = sqlite3.connect(appdata_db.as_posix())
    conn.execute("CREATE TABLE games (id INT, title TEXT)")
    conn.execute("INSERT INTO games VALUES (1, 'Test Game')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        launcher,
        "detect_legacy_data_locations",
        lambda r: {
            "targetDataDir": target_data,
            "hasTargetDb": True,
            "appdataDataDir": appdata_dir,
            "hasAppdataDb": True,
            "hasConflict": True,
            "isFresh": False,
        },
    )
    launched = []
    monkeypatch.setattr(
        launcher,
        "launch_version",
        lambda root, ver, argv=None, detached=True: launched.append((ver, detached)),
    )

    code = launcher.run_launcher(install_root_override=root, detached=False)
    assert code == 0
    assert len(launched) == 1
    assert launched[0][0] == "0.3.5"
    assert not appdata_dir.exists()


def test_run_launcher_cleans_old_executables(tmp_path, monkeypatch):
    """启动器主流程自动清理 updater/ 与根目录下历史换装遗留的 *.old_* 临时文件。"""
    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.5")

    updater_dir = root / "updater"
    updater_dir.mkdir(parents=True)
    old_host = updater_dir / "UpdateHost.old_abc123.exe"
    old_host.write_bytes(b"old_host")

    old_launcher = root / f"{APP_NAME}.old_def456.exe"
    old_launcher.write_bytes(b"old_launcher")

    monkeypatch.setattr(launcher, "launch_version", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher,
        "detect_legacy_data_locations",
        lambda r: {
            "isFresh": True,
            "hasTargetDb": False,
            "hasAppdataDb": False,
            "hasConflict": False,
        },
    )
    code = launcher.run_launcher(install_root_override=root, detached=False)
    assert code == 0
    assert not old_host.exists()
    assert not old_launcher.exists()
