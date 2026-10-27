"""启动器（Launcher）单元测试。"""
from __future__ import annotations

import os
import sqlite3
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


@pytest.mark.skipif(os.name != "nt", reason="Windows error presentation")
def test_server_failure_does_not_wait_for_a_desktop_dialog(monkeypatch, capsys):
    import ctypes

    calls = []
    monkeypatch.setattr(sys, "argv", ["Holdexar.exe", "--server"])
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *args: calls.append(args))
    launcher._show_fatal("startup rejected")
    assert calls == []
    assert "startup rejected" in capsys.readouterr().err


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


def test_run_launcher_independent_data_conflict_preserves_both(tmp_path, monkeypatch):
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
    monkeypatch.setattr(launcher, "_show_fatal", lambda msg: None)
    monkeypatch.setattr(
        launcher,
        "launch_version",
        lambda root, ver, argv=None, detached=True: launched.append((ver, detached)),
    )

    code = launcher.run_launcher(install_root_override=root, detached=False)
    assert code == 1
    assert not launched
    assert target_db.is_file() and appdata_db.is_file()


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


@pytest.mark.parametrize("compatible", [False, True])
def test_file_fallback_respects_accepted_database_versions(tmp_path, compatible):
    root = tmp_path / "app"
    _create_version(root, "0.3.5")
    layout.write_current_state(root, "0.3.6", last_known_good="0.3.5")
    data = root / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "holdexar.db")
    conn.execute("PRAGMA user_version=18")
    conn.close()
    layout.write_data_compatibility(root, {
        "dataDir": str(data), "schemaVersion": 18,
        "versions": ["0.3.5", "0.3.6"] if compatible else ["0.3.6"],
    })
    assert launcher.find_target_version(root) == ("0.3.5" if compatible else None)


def test_launch_uses_accepted_external_data_directory(tmp_path, monkeypatch):
    root = tmp_path / "app"
    _create_version(root, "0.3.6")
    layout.write_current_state(root, "0.3.6")
    external = tmp_path / "external-data"
    external.mkdir()
    conn = sqlite3.connect(external / "holdexar.db")
    conn.execute("PRAGMA user_version=18")
    conn.close()
    layout.write_data_compatibility(root, {"dataDir": str(external), "schemaVersion": 18, "versions": ["0.3.6"]})
    environments = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: environments.append(kw["env"]))
    launcher.launch_version(root, "0.3.6", argv=[])
    assert environments[0][f"{ENV_PREFIX}DATA_DIR"] == str(external)


def test_schema_drift_stops_file_fallback(tmp_path):
    root = tmp_path / "app"
    _create_version(root, "0.3.6")
    layout.write_current_state(root, "0.3.6")
    data = root / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "holdexar.db")
    conn.execute("PRAGMA user_version=19")
    conn.close()
    layout.write_data_compatibility(root, {"dataDir": str(data), "schemaVersion": 18, "versions": ["0.3.6"]})
    with pytest.raises(RuntimeError):
        launcher.find_target_version(root)


def test_background_launcher_returns_supervised_child_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "app"
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.5")
    calls = []

    def launch(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(wait=lambda: 75)

    monkeypatch.setattr(launcher, "launch_version", launch)
    monkeypatch.setattr(launcher, "detect_legacy_data_locations", lambda root: {})
    assert launcher.run_launcher(install_root_override=root, argv=["--server"]) == 75
    assert calls[0]["detached"] is False
