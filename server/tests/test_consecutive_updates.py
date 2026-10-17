"""连续版本升级端到端演练测试。

验证发布、下载、安装、文件级差量更新、内核独立维护与用户数据无损保留的全链路。
"""
from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from app.core import layout, update_host
from app.core.app_info import APP_NAME
from app.core.differential_patch import create_differential_patch


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


def test_consecutive_updates_with_differential_patches_and_data_isolation(tmp_path: Path) -> None:
    root = tmp_path / "installed_app"
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. 初始状态：v0.3.5
    # 用户数据与独立维护的 mihomo 内核
    user_db = data_dir / "holdexar.db"
    user_db.write_bytes(b"user-valuable-database-content-2.9GB")
    custom_marker = data_dir / "user_marker.txt"
    custom_marker.write_text("my-important-config", encoding="utf-8")

    kernel_file = data_dir / "clash" / "mihomo.exe"
    kernel_file.parent.mkdir(parents=True, exist_ok=True)
    kernel_file.write_bytes(b"mihomo-binary-v1.19.32-custom")

    # 初始版本 v0.3.5
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

    # ── 第一次升级：0.3.5 -> 0.3.6（通过文件级差量包）──────────────────────
    target_036_mock = tmp_path / "build_036"
    for rel, content in {
        f"{APP_NAME}.exe": b"v036-exe",
        "_internal/python313.dll": b"python-dll",  # 未变
        "_internal/app/core/updater.py": b"updater-v036",  # 修改
        "_internal/web/assets/app.js": b"web-v036",  # 修改
        "_internal/new_feat_v036.dll": b"new-feature-dll",  # 新增
    }.items():
        p = target_036_mock / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    patch_035_to_036_zip = tmp_path / "Holdexar-patch-v0.3.5-to-v0.3.6.zip"
    create_differential_patch(
        base_dir=layout.get_version_dir(root, "0.3.5"),
        target_dir=target_036_mock,
        from_version="0.3.5",
        to_version="0.3.6",
        output_zip=patch_035_to_036_zip,
    )

    # 模拟客户端下载解包到 staging
    staging_1 = tmp_path / "staging_1"
    staging_payload_1 = staging_1 / APP_NAME
    staging_payload_1.mkdir(parents=True)
    with zipfile.ZipFile(patch_035_to_036_zip, "r") as zf:
        zf.extractall(staging_payload_1)

    ok1 = update_host.execute_update(
        root=root,
        staging_dir=staging_1,
        target_version="0.3.6",
        source_version="0.3.5",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok1 is True

    # 验证第一次升级成果
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

    # 验证核心原则：用户数据与内核绝对独立保留
    assert user_db.read_bytes() == b"user-valuable-database-content-2.9GB"
    assert custom_marker.read_text(encoding="utf-8") == "my-important-config"
    assert kernel_file.read_bytes() == b"mihomo-binary-v1.19.32-custom"

    # ── 第二次升级：0.3.6 -> 0.3.7（再走一次差量更新）──────────────────────
    target_037_mock = tmp_path / "build_037"
    for rel, content in {
        f"{APP_NAME}.exe": b"v037-exe",
        "_internal/python313.dll": b"python-dll",  # 依然未变
        "_internal/app/core/updater.py": b"updater-v037",  # 再次迭代
        "_internal/web/assets/app.js": b"web-v037",  # 再次迭代
        "_internal/new_feat_v036.dll": b"new-feature-dll",  # 保持
        "_internal/patch_v037.dat": b"extra-patch-data",  # 新增
    }.items():
        p = target_037_mock / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    patch_036_to_037_zip = tmp_path / "Holdexar-patch-v0.3.6-to-v0.3.7.zip"
    create_differential_patch(
        base_dir=v036_dir,
        target_dir=target_037_mock,
        from_version="0.3.6",
        to_version="0.3.7",
        output_zip=patch_036_to_037_zip,
    )

    staging_2 = tmp_path / "staging_2"
    staging_payload_2 = staging_2 / APP_NAME
    staging_payload_2.mkdir(parents=True)
    with zipfile.ZipFile(patch_036_to_037_zip, "r") as zf:
        zf.extractall(staging_payload_2)

    ok2 = update_host.execute_update(
        root=root,
        staging_dir=staging_2,
        target_version="0.3.7",
        source_version="0.3.6",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok2 is True

    # 验证第二次升级成果
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
