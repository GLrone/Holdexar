from __future__ import annotations

import pytest

from app.core import layout


@pytest.mark.parametrize("name", ["0.3.5.", "0.3.5 "])
def test_managed_version_rejects_windows_normalized_suffix(name):
    assert not layout.is_managed_version_name(name)


def test_abort_cannot_delete_fallback_through_alias(tmp_path):
    import os

    if os.name != "nt":
        pytest.skip("Windows path identity")
    exe = layout.get_version_exe(tmp_path, "0.3.5-beta")
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fallback")
    layout.write_current_state(tmp_path, "0.3.5-BETA", last_known_good="0.3.5-beta")
    assert layout.abort_version(tmp_path, "0.3.5-BETA") is False
    assert exe.read_bytes() == b"fallback"


@pytest.fixture
def compatibility_env(tmp_path):
    root = tmp_path / "install"
    layout.write_current_state(root, "1.0.0")
    mapping = {"dataDir": str((root / "data").resolve()), "schemaVersion": 17, "versions": ["1.0.0"]}
    return root, mapping


def test_pointer_switch_preserves_only_durable_data_compatibility(compatibility_env):
    root, mapping = compatibility_env
    layout.write_current_state(root, "1.0.0", dataCompatibility=mapping, pendingVersion="2.0.0", attemptId="obsolete")
    state = layout.write_current_state(root, "2.0.0")
    assert state["dataCompatibility"] == mapping
    assert "pendingVersion" not in state and "attemptId" not in state


def test_compatibility_writer_preserves_pointer_metadata(compatibility_env):
    root, mapping = compatibility_env
    original = layout.write_current_state(root, "2.0.0", last_known_good="1.0.0", pendingVersion="2.0.0", attemptId="active")
    written = layout.write_data_compatibility(root, mapping)
    assert written == {**original, "dataCompatibility": mapping}
    assert layout.read_data_compatibility(root, data_dir=root / "data") == mapping


def test_confirm_and_abort_do_not_drop_compatibility(compatibility_env):
    root, mapping = compatibility_env
    layout.write_current_state(root, "2.0.0", last_known_good="1.0.0", dataCompatibility=mapping)
    assert layout.confirm_version(root, "2.0.0")["dataCompatibility"] == mapping
    old_exe = layout.get_version_exe(root, "1.0.0")
    old_exe.parent.mkdir(parents=True)
    old_exe.write_bytes(b"fixture executable")
    assert layout.abort_version(root, "2.0.0")
    assert layout.read_data_compatibility(root) == mapping


@pytest.mark.parametrize("mapping", [None, {}, {"dataDir": "", "schemaVersion": 17, "versions": ["1.0.0"]},
    {"dataDir": "relative", "schemaVersion": 17, "versions": ["1.0.0"]},
    {"dataDir": "E:/example/data", "schemaVersion": True, "versions": ["1.0.0"]},
    {"dataDir": "E:/example/data", "schemaVersion": 17, "versions": []},
    {"dataDir": "E:/example/data", "schemaVersion": 17, "versions": ["../escape"]}])
def test_existing_invalid_mapping_is_not_legacy_uninitialized(compatibility_env, mapping):
    root, _ = compatibility_env
    layout.write_current_state(root, "1.0.0", dataCompatibility=mapping)
    with pytest.raises(RuntimeError):
        layout.read_data_compatibility(root)


def test_absent_mapping_is_legacy_but_wrong_directory_is_rejected(compatibility_env):
    root, mapping = compatibility_env
    assert layout.read_data_compatibility(root) is None
    layout.write_data_compatibility(root, mapping)
    with pytest.raises(RuntimeError):
        layout.read_data_compatibility(root, data_dir=root / "different-data")
