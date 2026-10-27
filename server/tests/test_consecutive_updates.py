"""连续版本升级端到端演练测试（全量包通道）。

验证发布、下载暂存、安装、连续两次换装与用户数据无损保留的全链路。
全量包是唯一更新通道（差量链无构建集成、无上传、交接门外壳，已整链退役）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.core import layout, update_host
from app.core.app_info import APP_NAME


def _create_version_installation(
    root: Path,
    version: str,
    files: dict[str, bytes],
) -> Path:
    """在 root/versions/<version>/ 创建模拟版本目录。"""
    vdir = layout.get_version_dir(root, version)
    vdir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = vdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return vdir


def _stage_full_package(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    """模拟客户端下载解包后的暂存目录（zip 根目录即 Holdexar/）。"""
    staging = tmp_path / name
    payload = staging / APP_NAME
    for rel, content in files.items():
        p = payload / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return staging


def test_consecutive_full_package_updates_preserve_user_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import update_data

    def _fake_prepare(root, data_dir, attempt):
        layout.update_transaction(root, layout.read_transaction(root)["stage"],
                                  dataPrepared=True, dataAccepted=False,
                                  dataDir=str(data_dir), dataDbPath=str(data_dir / "holdexar.db"),
                                  dataToken="test-token", dataRecoveryPhase="prepared")

    def _fake_accept(root):
        tx = layout.read_transaction(root) or {}
        layout.update_transaction(root, tx["stage"],
                                  dataAccepted=True, dataRecoveryPhase="accepted")

    monkeypatch.setattr(update_host, "supports_data_protocol", lambda exe: True)
    monkeypatch.setattr(update_host, "probe_version_health", lambda *a, **kw: (True, "ready"))
    monkeypatch.setattr(update_data, "prepare", _fake_prepare)
    monkeypatch.setattr(update_data, "accept", _fake_accept)

    root = tmp_path / "installed_app"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. 初始状态：v0.3.5 + 用户数据与独立维护的 mihomo 内核
    user_db = data_dir / "holdexar.db"
    user_db.write_bytes(b"user-valuable-database-content-2.9GB")
    custom_marker = data_dir / "user_marker.txt"
    custom_marker.write_text("my-important-config", encoding="utf-8")

    kernel_file = data_dir / "clash" / "mihomo.exe"
    kernel_file.parent.mkdir(parents=True, exist_ok=True)
    kernel_file.write_bytes(b"mihomo-binary-v1.19.32-custom")

    _create_version_installation(
        root,
        "0.3.5",
        {
            f"{APP_NAME}.exe": b"v035-exe",
            "_internal/python313.dll": b"python-dll",
            "_internal/app/core/updater.py": b"updater-v035",
            "_internal/web/assets/app.js": b"web-v035",
            "_internal/dep_v035.dll": b"deprecated-later",
        },
    )
    (root / f"{APP_NAME}.exe").write_bytes(b"launcher-v035")
    (root / "updater").mkdir(parents=True, exist_ok=True)
    (root / "updater" / "UpdateHost.exe").write_bytes(b"updatehost-v035")
    layout.write_current_state(root, "0.3.5")

    # ── 第一次升级：0.3.5 -> 0.3.6（全量包）──────────────────────────────
    staging_1 = _stage_full_package(tmp_path, "staging_1", {
        f"{APP_NAME}.exe": b"v036-exe",
        "_internal/python313.dll": b"python-dll",
        "_internal/app/core/updater.py": b"updater-v036",
        "_internal/web/assets/app.js": b"web-v036",
        "_internal/new_feat_v036.dll": b"new-feature-dll",
    })

    ok1 = update_host.execute_update(
        root=root,
        staging_dir=staging_1,
        target_version="0.3.6",
        source_version="0.3.5",
        launch_on_finish=False,
    )
    assert ok1 is True

    curr1 = layout.read_current_state(root)
    assert curr1["version"] == "0.3.6"
    assert curr1["lastKnownGood"] == "0.3.5"
    assert layout.is_version_valid(root, "0.3.6") is True

    v036_dir = layout.get_version_dir(root, "0.3.6")
    assert (v036_dir / f"{APP_NAME}.exe").read_bytes() == b"v036-exe"
    assert (v036_dir / "_internal/python313.dll").read_bytes() == b"python-dll"
    assert (v036_dir / "_internal/app/core/updater.py").read_bytes() == b"updater-v036"
    assert (v036_dir / "_internal/new_feat_v036.dll").read_bytes() == b"new-feature-dll"
    assert not (v036_dir / "_internal/dep_v035.dll").exists()

    # 核心原则：用户数据与内核绝对独立保留
    assert user_db.read_bytes() == b"user-valuable-database-content-2.9GB"
    assert custom_marker.read_text(encoding="utf-8") == "my-important-config"
    assert kernel_file.read_bytes() == b"mihomo-binary-v1.19.32-custom"

    # ── 第二次升级：0.3.6 -> 0.3.7（再走一次全量包换装）──────────────────
    staging_2 = _stage_full_package(tmp_path, "staging_2", {
        f"{APP_NAME}.exe": b"v037-exe",
        "_internal/python313.dll": b"python-dll",
        "_internal/app/core/updater.py": b"updater-v037",
        "_internal/web/assets/app.js": b"web-v037",
        "_internal/new_feat_v036.dll": b"new-feature-dll",
        "_internal/patch_v037.dat": b"extra-patch-data",
    })

    ok2 = update_host.execute_update(
        root=root,
        staging_dir=staging_2,
        target_version="0.3.7",
        source_version="0.3.6",
        launch_on_finish=False,
    )
    assert ok2 is True

    curr2 = layout.read_current_state(root)
    assert curr2["version"] == "0.3.7"
    assert curr2["lastKnownGood"] == "0.3.6"
    assert layout.is_version_valid(root, "0.3.7") is True

    v037_dir = layout.get_version_dir(root, "0.3.7")
    assert (v037_dir / f"{APP_NAME}.exe").read_bytes() == b"v037-exe"
    assert (v037_dir / "_internal/patch_v037.dat").read_bytes() == b"extra-patch-data"

    # 再次验证核心原则：用户数据与内核绝对独立保留
    assert user_db.read_bytes() == b"user-valuable-database-content-2.9GB"
    assert custom_marker.read_text(encoding="utf-8") == "my-important-config"
    assert kernel_file.read_bytes() == b"mihomo-binary-v1.19.32-custom"
