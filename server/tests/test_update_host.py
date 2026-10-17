"""独立更新宿主（UpdateHost）单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from app.core import layout, update_host
from app.core.app_info import APP_NAME, STAGING_MANIFEST_NAME


def _create_mock_app_payload(staging_dir: Path, target_version: str, has_exe: bool = True) -> Path:
    payload = staging_dir / APP_NAME
    payload.mkdir(parents=True, exist_ok=True)
    if has_exe:
        (payload / f"{APP_NAME}.exe").write_bytes(b"app_binary")
    (payload / "version.txt").write_text(target_version, encoding="utf-8")

    manifest = {
        "tag": f"v{target_version}",
        "attempt_id": "att-mock-1",
        "ready": True,
    }
    (staging_dir / STAGING_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def test_update_host_success(tmp_path):
    root = tmp_path / "app"
    staging = tmp_path / "staging"

    # 准备初始状态：版本 0.3.5
    v035 = layout.get_version_dir(root, "0.3.5")
    v035.mkdir(parents=True, exist_ok=True)
    (v035 / f"{APP_NAME}.exe").write_bytes(b"v035_binary")
    layout.write_current_state(root, "0.3.5")

    # 准备新载荷：0.3.6
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)

    # 执行更新（跳过进程拉起与网络探活）
    ok = update_host.execute_update(
        root=root,
        staging_dir=staging,
        target_version="0.3.6",
        source_version="0.3.5",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is True

    # 验证新版本落位
    assert layout.is_version_valid(root, "0.3.6") is True

    # 验证指针原子切换
    curr = layout.read_current_state(root)
    assert curr["version"] == "0.3.6"
    assert curr["lastKnownGood"] == "0.3.5"

    # 验证暂存清理
    assert not staging.exists()

    # 验证事务终态
    tx = layout.read_transaction(root)
    assert tx["stage"] == layout.STAGE_COMMITTED

    # 验证日志与诊断报告生成
    diag_file = root / "logs" / "update-diagnosis.json"
    assert diag_file.is_file()
    diag = json.loads(diag_file.read_text(encoding="utf-8"))
    assert diag["success"] is True
    assert diag["targetVersion"] == "0.3.6"


def test_update_host_rollback_on_missing_exe(tmp_path):
    root = tmp_path / "app"
    staging = tmp_path / "staging"

    # 初始 0.3.5
    v035 = layout.get_version_dir(root, "0.3.5")
    v035.mkdir(parents=True, exist_ok=True)
    (v035 / f"{APP_NAME}.exe").write_bytes(b"v035_binary")
    layout.write_current_state(root, "0.3.5")

    # 损坏载荷：无主程序
    _create_mock_app_payload(staging, "0.3.6", has_exe=False)

    ok = update_host.execute_update(
        root=root,
        staging_dir=staging,
        target_version="0.3.6",
        source_version="0.3.5",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is False

    # 指针保持/回滚至 0.3.5
    curr = layout.read_current_state(root)
    assert curr["version"] == "0.3.5"

    tx = layout.read_transaction(root)
    assert tx["stage"] == layout.STAGE_ROLLED_BACK


def test_update_host_rollback_on_health_check_failure(tmp_path, monkeypatch):
    root = tmp_path / "app"
    staging = tmp_path / "staging"

    v035 = layout.get_version_dir(root, "0.3.5")
    v035.mkdir(parents=True, exist_ok=True)
    (v035 / f"{APP_NAME}.exe").write_bytes(b"v035_binary")
    layout.write_current_state(root, "0.3.5")

    _create_mock_app_payload(staging, "0.3.6", has_exe=True)

    # 模拟健康检查失败
    monkeypatch.setattr(
        update_host, "probe_version_health", lambda *args, **kwargs: (False, "服务崩溃")
    )

    ok = update_host.execute_update(
        root=root,
        staging_dir=staging,
        target_version="0.3.6",
        source_version="0.3.5",
        skip_health_check=False,
        launch_on_finish=False,
    )
    assert ok is False

    # 自动回滚到 0.3.5
    curr = layout.read_current_state(root)
    assert curr["version"] == "0.3.5"

    tx = layout.read_transaction(root)
    assert tx["stage"] == layout.STAGE_ROLLED_BACK
    assert "健康检查失败" in tx["error"]


def test_update_host_retention_prunes_older_versions(tmp_path):
    root = tmp_path / "app"
    staging = tmp_path / "staging"

    # 已有 0.3.4 和 0.3.5
    for v in ["0.3.4", "0.3.5"]:
        vdir = layout.get_version_dir(root, v)
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{APP_NAME}.exe").write_bytes(b"binary")
    layout.write_current_state(root, "0.3.5", last_known_good="0.3.4")

    # 安装 0.3.6
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)

    ok = update_host.execute_update(
        root=root,
        staging_dir=staging,
        target_version="0.3.6",
        source_version="0.3.5",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is True

    # 保留 0.3.6 与 0.3.5，清理 0.3.4
    remaining = layout.list_installed_versions(root)
    assert set(remaining) == {"0.3.5", "0.3.6"}
    assert not layout.get_version_dir(root, "0.3.4").exists()


def test_bridge_legacy_update_converts_flat_layout(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    staging = tmp_path / "staging"

    # 构造老版本扁平安装目录：0.3.2-beta
    (target / f"{APP_NAME}.exe").write_bytes(b"old_exe")
    (target / "_internal").mkdir()
    (target / "_internal" / "python313.dll").write_bytes(b"old_dll")
    (target / "data").mkdir()
    (target / "data" / "holdexar.db").write_bytes(b"user_database")

    # 构造新版本 0.3.3-beta 暂存包
    _create_mock_app_payload(staging, "0.3.3-beta", has_exe=True)

    ok = update_host.bridge_legacy_update(
        staging=staging,
        target=target,
        current_version="0.3.2-beta",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is True

    # 验证用户数据完好
    assert (target / "data" / "holdexar.db").read_bytes() == b"user_database"

    # 验证旧版已被组织进 versions/0.3.2-beta/
    assert (target / "versions" / "0.3.2-beta" / "_internal" / "python313.dll").is_file()
    assert (target / "versions" / "0.3.2-beta" / f"{APP_NAME}.exe").is_file()

    # 验证新版已安装至 versions/0.3.3-beta/
    assert (target / "versions" / "0.3.3-beta" / f"{APP_NAME}.exe").is_file()

    # 验证版本指针
    curr = layout.read_current_state(target)
    assert curr["version"] == "0.3.3-beta"
    assert curr["lastKnownGood"] == "0.3.2-beta"
