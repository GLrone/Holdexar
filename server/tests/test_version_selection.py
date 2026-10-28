"""UpdateHost共享版本选择与相容限制。"""
import sqlite3
import time
from pathlib import Path
import pytest
from app.core import layout
from app.core.app_info import APP_NAME


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

    target = layout.find_target_version(root)
    assert target == "0.3.5"


def test_find_target_version_fallback_to_last_good(tmp_path):
    root = tmp_path / "app"
    # 0.3.6 损坏（无 exe），0.3.5 完好
    _create_version(root, "0.3.6", has_exe=False)
    _create_version(root, "0.3.5", has_exe=True)
    layout.write_current_state(root, "0.3.6", last_known_good="0.3.5")

    # 自动识别 0.3.6 损坏并降级到 0.3.5
    target = layout.find_target_version(root)
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
    target = layout.find_target_version(root)
    assert target == "0.3.4"
    assert layout.get_current_version(root) == "0.3.4"


def test_find_target_version_none_available(tmp_path):
    root = tmp_path / "app"
    _create_version(root, "0.3.3", has_exe=False)
    assert layout.find_target_version(root) is None


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
    assert layout.find_target_version(root) == ("0.3.5" if compatible else None)


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
        layout.find_target_version(root)
