"""独立更新宿主（UpdateHost）单元测试。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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


# ── 健康验收：只认本次拉起、目标版本、目标数据目录、本次事务的进程 ──────────


def _health_payload(root: Path, version: str = "0.3.6", aid: str = "att-1") -> dict:
    return {
        "app": APP_NAME,
        "version": version,
        "pid": 4242,
        "dataDir": str(root / "data"),
        "executable": str(layout.get_version_exe(root, version)),
        "updateAttemptId": aid,
    }


def test_verify_health_accepts_matching_identity(tmp_path):
    root = tmp_path / "app"
    reason = update_host.verify_health_response(
        _health_payload(root),
        target_version="0.3.6",
        attempt_id="att-1",
        exe=layout.get_version_exe(root, "0.3.6"),
        data_dir=root / "data",
    )
    assert reason is None


def test_verify_health_rejects_other_version_instance(tmp_path):
    """端口上恰好是另一个同版本以外的实例：版本不符即拒，不能算健康。"""
    root = tmp_path / "app"
    reason = update_host.verify_health_response(
        _health_payload(root, version="0.3.5"),
        target_version="0.3.6",
        attempt_id="att-1",
        exe=layout.get_version_exe(root, "0.3.6"),
        data_dir=root / "data",
    )
    assert reason is not None and "版本" in reason


def test_verify_health_rejects_foreign_attempt(tmp_path):
    root = tmp_path / "app"
    reason = update_host.verify_health_response(
        _health_payload(root, aid="someone-else"),
        target_version="0.3.6",
        attempt_id="att-1",
        exe=layout.get_version_exe(root, "0.3.6"),
        data_dir=root / "data",
    )
    assert reason is not None and "事务标识" in reason


def test_verify_health_rejects_other_data_dir(tmp_path):
    root = tmp_path / "app"
    payload = _health_payload(root)
    payload["dataDir"] = str(tmp_path / "elsewhere" / "data")
    reason = update_host.verify_health_response(
        payload,
        target_version="0.3.6",
        attempt_id="att-1",
        exe=layout.get_version_exe(root, "0.3.6"),
        data_dir=root / "data",
    )
    assert reason is not None and "数据目录" in reason


def test_verify_health_rejects_other_executable(tmp_path):
    root = tmp_path / "app"
    payload = _health_payload(root)
    payload["executable"] = str(layout.get_version_exe(root, "0.3.5"))
    reason = update_host.verify_health_response(
        payload,
        target_version="0.3.6",
        attempt_id="att-1",
        exe=layout.get_version_exe(root, "0.3.6"),
        data_dir=root / "data",
    )
    assert reason is not None and "进程镜像" in reason


def test_probe_rejects_health_from_foreign_process(tmp_path, monkeypatch):
    """本次拉起的进程还活着，但端口上应答的是别人的实例：验收必须失败。"""
    root = tmp_path / "app"
    vdir = layout.get_version_dir(root, "0.3.6")
    vdir.mkdir(parents=True, exist_ok=True)
    exe = vdir / f"{APP_NAME}.exe"
    exe.write_bytes(b"bin")

    class _Proc:
        pid = 999001
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    class _Resp:
        status_code = 200

        def json(self):
            return {"app": APP_NAME, "version": "0.3.6", "pid": 777,
                    "updateAttemptId": "att-1"}

    monkeypatch.setattr(update_host.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(update_host, "_listen_ports", lambda pid: ([28765], 777))
    monkeypatch.setattr(update_host, "process_image_path", lambda pid: str(exe))
    monkeypatch.setattr(update_host.httpx, "get", lambda *a, **k: _Resp())

    healthy, reason = update_host.probe_version_health(
        root, "0.3.6", attempt_id="att-1", timeout=0.6,
    )
    assert healthy is False
    assert update_host.HEALTH_BINDING_ERROR in reason


def test_health_failure_rolls_back_files_and_pointer(tmp_path, monkeypatch):
    """验收失败：撤下目标版本、指针交还源版本（源版本即使不是标准版本名也一样）。"""
    root = tmp_path / "app"
    staging = tmp_path / "staging"
    legacy = layout.get_version_dir(root, "legacy_version")
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / f"{APP_NAME}.exe").write_bytes(b"old")
    layout.write_current_state(root, "legacy_version")

    _create_mock_app_payload(staging, "0.3.6", has_exe=True)
    monkeypatch.setattr(
        update_host, "probe_version_health", lambda *a, **k: (False, "服务崩溃")
    )

    ok = update_host.execute_update(
        root=root, staging_dir=staging, target_version="0.3.6",
        source_version="legacy_version", launch_on_finish=False,
    )
    assert ok is False
    assert layout.read_current_state(root)["version"] == "legacy_version"
    assert not layout.get_version_dir(root, "0.3.6").exists()


# ── 载荷与版本目录身份 ──────────────────────────────────────────────────


def test_install_rejects_payload_version_mismatch(tmp_path):
    """载荷 versions/ 下的目录名与目标版本不一致即为错包，不得拿别的目录冒充。"""
    root = tmp_path / "app"
    staging = tmp_path / "staging"
    payload = staging / APP_NAME / "versions"
    (payload / "0.9.9").mkdir(parents=True)
    (payload / "0.9.9" / f"{APP_NAME}.exe").write_bytes(b"x")

    with pytest.raises(RuntimeError):
        update_host.install_version_payload(root, staging, "0.3.6")
    assert not layout.get_version_dir(root, "0.3.6").exists()


def test_install_rejects_traversal_version_name(tmp_path):
    root = tmp_path / "app"
    staging = tmp_path / "staging"
    (staging / APP_NAME).mkdir(parents=True)
    with pytest.raises(RuntimeError):
        update_host.install_version_payload(root, staging, "../escape")


def test_install_version_payload_differential_patch(tmp_path):
    from app.core.differential_patch import create_differential_patch
    import zipfile

    root = tmp_path / "app"
    base_ver = "0.3.5"
    target_ver = "0.3.6"

    # 基准版本已安装在 versions/0.3.5
    base_dir = layout.get_version_dir(root, base_ver)
    base_dir.mkdir(parents=True)
    (base_dir / f"{APP_NAME}.exe").write_bytes(b"base-launcher-content")
    (base_dir / "_internal").mkdir()
    (base_dir / "_internal" / "keep.dll").write_bytes(b"keep-content")
    (base_dir / "_internal" / "remove.dll").write_bytes(b"remove-content")

    # 目标新版本内容
    target_mock = tmp_path / "target_mock"
    target_mock.mkdir()
    (target_mock / f"{APP_NAME}.exe").write_bytes(b"new-launcher-content")
    (target_mock / "_internal").mkdir()
    (target_mock / "_internal" / "keep.dll").write_bytes(b"keep-content")
    (target_mock / "_internal" / "added.dll").write_bytes(b"added-content")

    # 生成差量包并解包到 staging
    patch_zip = tmp_path / "patch.zip"
    create_differential_patch(base_dir, target_mock, base_ver, target_ver, patch_zip)

    staging = tmp_path / "staging"
    staging_payload = staging / APP_NAME
    staging_payload.mkdir(parents=True)
    with zipfile.ZipFile(patch_zip, "r") as zf:
        zf.extractall(staging_payload)

    # 执行差量安装
    dest = update_host.install_version_payload(root, staging, target_ver)

    assert dest == layout.get_version_dir(root, target_ver)
    assert layout.is_version_valid(root, target_ver)
    assert (dest / f"{APP_NAME}.exe").read_bytes() == b"new-launcher-content"
    assert (dest / "_internal" / "keep.dll").read_bytes() == b"keep-content"
    assert (dest / "_internal" / "added.dll").read_bytes() == b"added-content"
    assert not (dest / "_internal" / "remove.dll").exists()



# ── 旧版首次迁移：任何阶段失败都留着可双击的旧版入口 ─────────────────────


def _flat_legacy(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{APP_NAME}.exe").write_bytes(b"old_exe")
    (target / "_internal").mkdir()
    (target / "_internal" / "python313.dll").write_bytes(b"old_dll")
    (target / "data").mkdir()
    (target / "data" / "holdexar.db").write_bytes(b"user_database")


def test_legacy_launcher_lands_before_legacy_moves(tmp_path):
    target = tmp_path / "installed"
    staging = tmp_path / "staging"
    _flat_legacy(target)
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)

    ok = update_host.prepare_launcher_only(
        target, staging, source_version="0.3.2-beta", attempt_id="att-l"
    )
    assert ok is True
    # 根目录换成了新启动器，旧版主程序已备份且版本目录尚未开始搬
    assert (target / f"{APP_NAME}.exe").read_bytes() == b"app_binary"
    assert (target / "__legacy__" / f"{APP_NAME}.exe").is_file()
    assert (target / "_internal").is_dir()
    curr = layout.read_current_state(target)
    assert curr["version"] == "0.3.2-beta"
    assert curr["previousVersion"] == "0.3.2-beta"


def test_legacy_migration_failure_keeps_root_exe(tmp_path, monkeypatch):
    """数据迁移失败即终止：根目录启动器在、旧版主程序仍在根目录、版本目录未建。"""
    target = tmp_path / "installed"
    staging = tmp_path / "staging"
    appdata = tmp_path / "appdata" / "holdexar"
    _flat_legacy(target)
    # 旧版数据在 AppData（外置），安装根 data/ 还没有库
    (target / "data" / "holdexar.db").unlink()
    appdata.mkdir(parents=True)
    (appdata / "holdexar.db").write_bytes(b"legacy_database")
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)
    monkeypatch.setattr(update_host, "_launch_entry", lambda root: True)

    def _boom(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(
        "app.core.migration.migrate_appdata_to_portable", _boom, raising=True
    )

    ok = update_host.bridge_legacy_update(
        staging=staging,
        target=target,
        data_dir=appdata,
        current_version="0.3.2-beta",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is False
    assert (target / f"{APP_NAME}.exe").is_file()          # 新版启动入口在位
    assert (target / "__legacy__" / f"{APP_NAME}.exe").is_file()  # 旧入口有备份
    assert not (target / "versions").exists() or not any(
        (target / "versions").iterdir()
    )
    # 迁移失败不得在 data/ 建新库替代历史数据
    assert not (target / "data" / "holdexar.db").exists()


def test_legacy_migration_conflict_stops_update(tmp_path, monkeypatch):
    """两处都有库：拒绝覆盖，换装不开始。"""
    target = tmp_path / "installed"
    staging = tmp_path / "staging"
    appdata = tmp_path / "appdata" / "holdexar"
    _flat_legacy(target)
    appdata.mkdir(parents=True)
    (appdata / "holdexar.db").write_bytes(b"legacy_database")
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)
    monkeypatch.setattr(update_host, "_launch_entry", lambda root: True)

    ok = update_host.bridge_legacy_update(
        staging=staging,
        target=target,
        data_dir=appdata,
        current_version="0.3.2-beta",
        skip_health_check=True,
        launch_on_finish=False,
    )
    assert ok is False
    # 安装根那份库不被旧库覆盖
    assert (target / "data" / "holdexar.db").read_bytes() == b"user_database"


def test_legacy_conversion_failure_restores_flat_layout(tmp_path, monkeypatch):
    """版本目录转换失败：把已移动的条目搬回根目录，根目录仍有旧版主程序。"""
    target = tmp_path / "installed"
    _flat_legacy(target)

    real_move = update_host._move_legacy_entry

    def _move(root, source, dest):
        if str(source).endswith(f"{APP_NAME}.exe") and dest.parent.name == "0.3.2-beta":
            raise PermissionError("文件被占用")
        return real_move(root, source, dest)

    monkeypatch.setattr(update_host, "_move_legacy_entry", _move)

    ok, reason = update_host.convert_legacy_layout(
        target, source_version="0.3.2-beta"
    )
    assert ok is False
    assert "文件被占用" in reason
    # _internal 已搬入版本目录后被发现主程序搬不动，必须搬回根目录
    assert (target / "_internal" / "python313.dll").is_file()
    assert (target / f"{APP_NAME}.exe").read_bytes() == b"old_exe"


def test_legacy_conversion_writes_typed_version_dir(tmp_path):
    target = tmp_path / "installed"
    _flat_legacy(target)

    ok, _ = update_host.convert_legacy_layout(target, source_version="v0.3.2-beta")
    assert ok is True
    assert (target / "versions" / "0.3.2-beta" / f"{APP_NAME}.exe").is_file()
    assert (target / "versions" / "0.3.2-beta" / "_internal" / "python313.dll").is_file()
    curr = layout.read_current_state(target)
    assert curr["version"] == "0.3.2-beta"
    assert curr["pendingVersion"] == "0.3.2-beta"


def test_legacy_conversion_preserves_true_legacy_binary_after_launcher_prep(tmp_path):
    """关键回归：prepare_launcher_only 之后执行 convert_legacy_layout，旧版版本目录内
    必须是真正的旧版主程序（b'old_exe'），而不是被新版 Launcher（b'app_binary'）篡改。"""
    target = tmp_path / "installed"
    staging = tmp_path / "staging"
    _flat_legacy(target)
    _create_mock_app_payload(staging, "0.3.6", has_exe=True)

    ok = update_host.prepare_launcher_only(
        target, staging, source_version="0.3.2-beta", attempt_id="att-1"
    )
    assert ok is True
    # 根目录此时是 Launcher
    assert (target / f"{APP_NAME}.exe").read_bytes() == b"app_binary"
    assert (target / "__legacy__" / f"{APP_NAME}.exe").read_bytes() == b"old_exe"

    ok_conv, reason = update_host.convert_legacy_layout(
        target, source_version="0.3.2-beta", attempt_id="att-1"
    )
    assert ok_conv is True
    # 检查版本目录内的 EXE：必须是真正的旧版主程序！
    dest_exe = target / "versions" / "0.3.2-beta" / f"{APP_NAME}.exe"
    assert dest_exe.is_file()
    assert dest_exe.read_bytes() == b"old_exe"

    # 根目录依然是 Launcher
    assert (target / f"{APP_NAME}.exe").read_bytes() == b"app_binary"


def test_legacy_version_name_rejects_data_directory_names(tmp_path):
    """安全门禁：即使传入 data 或 holdexar 字符串，也不能被认作版本名。"""
    root = tmp_path / "installed"
    root.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()

    assert update_host._legacy_version_name(root, "data", staging) == "legacy_version"
    assert update_host._legacy_version_name(root, "Holdexar", staging) == "legacy_version"
    assert update_host._legacy_version_name(root, "versions", staging) == "legacy_version"


def test_safe_replace_executable_fallback(tmp_path, monkeypatch):
    """运行中文档锁场景：shutil.copy2 报 PermissionError 时自动重命名原文件后再覆盖。"""
    src = tmp_path / "new.exe"
    dest = tmp_path / "running.exe"
    src.write_bytes(b"new_content")
    dest.write_bytes(b"old_content")

    real_copy = update_host.shutil.copy2
    copied_once = False

    def mock_copy(s, d):
        nonlocal copied_once
        if not copied_once:
            copied_once = True
            raise PermissionError("WinError 32: The process cannot access the file")
        return real_copy(s, d)

    monkeypatch.setattr(update_host.shutil, "copy2", mock_copy)
    update_host._safe_replace_executable(src, dest)
    assert dest.read_bytes() == b"new_content"
    old_files = list(tmp_path.glob("running.old_*.exe"))
    assert len(old_files) == 1
    update_host.cleanup_old_executables(tmp_path)
    assert len(list(tmp_path.glob("running.old_*.exe"))) == 0
