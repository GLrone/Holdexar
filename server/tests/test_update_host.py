"""独立更新宿主（UpdateHost）单元测试。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core import layout, update_host
from app.core.app_info import APP_NAME, STAGING_MANIFEST_NAME


@pytest.fixture(autouse=True)
def _mock_payload_protocol(request, monkeypatch):
    if "protocol_detection" not in request.node.name:
        monkeypatch.setattr(update_host, "supports_data_protocol", lambda exe: True)


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
    monkeypatch.setattr(update_host, "_http_get_json", lambda *a, **k: (200, {"app": APP_NAME, "version": "0.3.6", "pid": 777, "updateAttemptId": "att-1"}))
    monkeypatch.setattr("app.core.update_process.ProbeTree.assign", lambda *a: None)
    monkeypatch.setattr("app.core.update_process.ProbeTree.stop", lambda *a: None)

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


def test_install_version_payload_flat_package(tmp_path):
    """扁平单版本载荷直接作为版本目录落位（差量链退役后的唯一通道之一）。"""
    root = tmp_path / "app"
    target_ver = "0.3.6"

    staging = tmp_path / "staging"
    payload = staging / APP_NAME
    (payload / "_internal").mkdir(parents=True)
    (payload / f"{APP_NAME}.exe").write_bytes(b"new-launcher-content")
    (payload / "_internal" / "keep.dll").write_bytes(b"keep-content")
    (payload / "_internal" / "added.dll").write_bytes(b"added-content")

    dest = update_host.install_version_payload(root, staging, target_ver)

    assert dest == layout.get_version_dir(root, target_ver)
    assert layout.is_version_valid(root, target_ver)
    assert (dest / f"{APP_NAME}.exe").read_bytes() == b"new-launcher-content"
    assert (dest / "_internal" / "keep.dll").read_bytes() == b"keep-content"
    assert (dest / "_internal" / "added.dll").read_bytes() == b"added-content"


def test_safe_replace_executable_fallback(tmp_path, monkeypatch):
    """运行中文档锁场景：原子替换被占用时保留旧映像再落位。"""
    src = tmp_path / "new.exe"
    dest = tmp_path / "running.exe"
    src.write_bytes(b"new_content")
    dest.write_bytes(b"old_content")

    real_replace = update_host.os.replace
    blocked_once = False

    def mock_replace(s, d):
        nonlocal blocked_once
        if d == dest and not blocked_once:
            blocked_once = True
            raise PermissionError("WinError 32: The process cannot access the file")
        return real_replace(s, d)

    monkeypatch.setattr(update_host.os, "replace", mock_replace)
    update_host._safe_replace_executable(src, dest)
    assert dest.read_bytes() == b"new_content"
    old_files = list(tmp_path.glob("running.old_*.exe"))
    assert len(old_files) == 1
    update_host.cleanup_old_executables(tmp_path)
    assert len(list(tmp_path.glob("running.old_*.exe"))) == 0


@pytest.mark.parametrize("failure_point", ["before", "migration", "health", "page"])
def test_failed_upgrade_restores_database_before_relaunching_source(tmp_path, monkeypatch, failure_point):
    root = tmp_path / "app"
    staging = tmp_path / "staging"
    old = layout.get_version_dir(root, "0.3.5")
    old.mkdir(parents=True)
    (old / f"{APP_NAME}.exe").write_bytes(b"old")
    layout.write_current_state(root, "0.3.5")
    _create_mock_app_payload(staging, "0.3.6")
    data_dir = root / "data"
    data_dir.mkdir()
    db_path = data_dir / "holdexar.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE user_record (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO user_record VALUES (1, 'before')")
        conn.execute("PRAGMA user_version=17")
    conn.close()

    calls = []

    def fail_after_migration(*_args, **_kwargs):
        calls.append(True)
        if failure_point == "before" or len(calls) > 1:
            return False, "injected failure"
        with sqlite3.connect(db_path) as conn:
            conn.execute("ALTER TABLE user_record ADD COLUMN new_version TEXT")
            if failure_point != "migration":
                conn.execute("PRAGMA user_version=18")
        conn.close()
        return failure_point == "page", "injected failure"

    relaunched = []

    def observe_source(_root, _version):
        with sqlite3.connect(db_path) as conn:
            relaunched.append((conn.execute("PRAGMA user_version").fetchone()[0],
                               conn.execute("SELECT value FROM user_record").fetchone()[0]))

    monkeypatch.setattr(update_host, "probe_version_health", fail_after_migration)
    monkeypatch.setattr(update_host, "_relaunch_previous", observe_source)
    assert update_host.execute_update(
        root, staging, target_version="0.3.6", source_version="0.3.5",
        data_dir=data_dir, launch_on_finish=False,
    ) is False
    assert relaunched == [(17, "before")]
    with sqlite3.connect(db_path) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(user_record)")] == ["id", "value"]


def test_protocol_detection_does_not_run_unknown_desktop_switch(tmp_path, monkeypatch):
    exe = tmp_path / "old.exe"
    exe.write_bytes(b"old")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return update_host.subprocess.CompletedProcess(command, 0, "--server --apply-update", "")

    monkeypatch.setattr(update_host.subprocess, "run", run)
    assert update_host.supports_data_protocol(exe) is False
    assert commands == [[str(exe), "--update-protocol", "--help"]]


def test_protocol_detection_requires_matching_response(tmp_path, monkeypatch):
    exe = tmp_path / "new.exe"
    exe.write_bytes(b"new")
    def run(command, **kwargs):
        Path(kwargs["env"]["HOLDEXAR_UPDATE_PROTOCOL_RESULT"]).write_text('{"updateDataProtocol":1}')
        return update_host.subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(update_host.subprocess, "run", run)
    assert update_host.supports_data_protocol(exe) is True


def test_waiting_old_instance_failure_does_not_install(tmp_path, monkeypatch):
    root = tmp_path / "app"
    staging = tmp_path / "staging"
    _create_mock_app_payload(staging, "0.3.6")
    monkeypatch.setattr(update_host, "wait_process_exit", lambda *args, **kwargs: False)
    assert update_host.execute_update(root, staging, target_version="0.3.6", wait_pid=901) is False
    assert layout.read_current_state(root) is None
    assert not layout.get_version_dir(root, "0.3.6").exists()


def test_recovery_does_not_release_startup_when_pointer_finish_failed(tmp_path, monkeypatch):
    root = tmp_path / "app"
    layout.start_transaction(root, "0.3.6", "0.3.5", attempt_id="previous")
    layout.update_transaction(root, "recovery_required", dataPrepared=True,
                              dataRecoveryPhase="restored", dataDir=str(root / "data"))
    monkeypatch.setattr(update_host, "_fail", lambda *a, **kw: False)
    with pytest.raises(RuntimeError):
        update_host.recover_interrupted_update(root)


@pytest.mark.parametrize("previous_phase", ["preparing", "restored", "accepted"])
def test_new_update_cannot_overwrite_unfinished_data_ownership(tmp_path, monkeypatch, previous_phase):
    from app.core import update_data

    root = tmp_path / "app"
    staging = tmp_path / "staging"
    old = layout.get_version_dir(root, "0.3.5")
    old.mkdir(parents=True)
    (old / f"{APP_NAME}.exe").write_bytes(b"old")
    layout.write_current_state(root, "0.3.5")
    _create_mock_app_payload(staging, "0.3.7")
    layout.start_transaction(root, "0.3.6", "0.3.5", attempt_id="previous")
    layout.update_transaction(root, "recovery_required", dataPrepared=previous_phase != "preparing",
                              dataRecoveryPhase=previous_phase, dataAccepted=previous_phase == "accepted",
                              dataDir=str(root / "data"))

    def unfinished(*args, **kwargs):
        raise RuntimeError("previous ownership cannot finish")

    monkeypatch.setattr(update_data, "abort_preparation", unfinished)
    monkeypatch.setattr(update_data, "rollback", unfinished)
    monkeypatch.setattr(update_data, "accept", unfinished)
    assert update_host.execute_update(root, staging, target_version="0.3.7", skip_health_check=True,
                                      launch_on_finish=False) is False
    assert layout.read_transaction(root)["attemptId"] == "previous"
    assert not layout.get_version_dir(root, "0.3.7").exists()


@pytest.mark.parametrize("target,source", [("0.3.5", "0.3.5"), ("0.3.6", "0.3.4")])
def test_stale_update_request_cannot_replace_active_program(tmp_path, target, source):
    root = tmp_path / "app"
    active = layout.get_version_dir(root, "0.3.5")
    active.mkdir(parents=True)
    exe = active / f"{APP_NAME}.exe"
    exe.write_bytes(b"preserve-active")
    layout.write_current_state(root, "0.3.5")
    layout.start_transaction(root, "0.3.5", "0.3.4", attempt_id="accepted-previous")
    assert update_host.execute_update(root, tmp_path / "missing-payload", target_version=target,
                                      source_version=source, skip_health_check=True, launch_on_finish=False) is False
    assert exe.read_bytes() == b"preserve-active"
    assert layout.read_transaction(root)["attemptId"] == "accepted-previous"


def test_accepted_update_only_falls_back_to_verified_compatible_program(tmp_path):
    root = tmp_path / "app"
    old = layout.get_version_dir(root, "0.3.5")
    old.mkdir(parents=True)
    (old / f"{APP_NAME}.exe").write_bytes(b"compatible")
    layout.write_current_state(root, "0.3.6", last_known_good="0.3.5")
    data = root / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "holdexar.db")
    conn.execute("PRAGMA user_version=18")
    conn.close()
    layout.write_data_compatibility(root, {"dataDir": str(data), "schemaVersion": 18,
                                          "versions": ["0.3.5", "0.3.6"]})
    layout.start_transaction(root, "0.3.6", "0.3.5", attempt_id="accepted")
    layout.update_transaction(root, layout.STAGE_COMMITTED, dataPrepared=True, dataAccepted=True,
                              dataRecoveryPhase="accepted", dataSchemaVersion=18, dataToken="accepted",
                              dataDir=str(data), dataDbPath=str(data / "holdexar.db"))
    assert update_host.recover_interrupted_update(root) is True
    assert layout.get_current_version(root) == "0.3.5"
    diag = json.loads((root / "logs" / "update-diagnosis.json").read_text(encoding="utf-8"))
    assert diag["success"] is False
    assert diag["currentVersion"] == "0.3.5"


@pytest.mark.parametrize("diagnosis_state", ["switching", "committed", "missing", "other_attempt"])
def test_accepted_recovery_closes_success_diagnosis_without_repeating_ack(tmp_path, diagnosis_state):
    from app.core import updater

    root = tmp_path / "app"
    data = root / "data"
    data.mkdir(parents=True)
    target = updater.APP_VERSION
    for version in ("0.3.4", target):
        exe = layout.get_version_exe(root, version)
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"compatible")
    layout.write_current_state(root, target, last_known_good="0.3.4")
    with sqlite3.connect(data / "holdexar.db") as conn:
        conn.execute("PRAGMA user_version=18")
    layout.write_data_compatibility(root, {"dataDir": str(data), "schemaVersion": 18, "versions": [target]})
    layout.start_transaction(root, target, "0.3.4", attempt_id="accepted")
    layout.update_transaction(root, layout.STAGE_VERIFYING, dataPrepared=True, dataAccepted=True,
                              dataRecoveryPhase="accepted", dataSchemaVersion=18, dataToken="accepted",
                              dataDir=str(data), dataDbPath=str(data / "holdexar.db"))
    diag_file = root / "logs" / "update-diagnosis.json"
    if diagnosis_state != "missing":
        diag_file.parent.mkdir()
        diag_file.write_text(json.dumps({
            "attemptId": "other" if diagnosis_state == "other_attempt" else "accepted",
            "stage": diagnosis_state, "targetVersion": target, "notes": "release notes",
            "success": diagnosis_state == "committed", "acknowledged": diagnosis_state == "committed",
        }), encoding="utf-8")

    assert update_host.recover_interrupted_update(root) is True
    tx = layout.read_transaction(root)
    assert tx["stage"] == layout.STAGE_COMMITTED
    diag = json.loads(diag_file.read_text(encoding="utf-8"))
    assert diag["attemptId"] == "accepted"
    assert diag["targetVersion"] == target
    assert diag["currentVersion"] == target
    assert diag["sourceVersion"] == "0.3.4"
    assert diag["stage"] == layout.STAGE_COMMITTED
    assert diag["success"] is True
    assert diag["acknowledged"] is (diagnosis_state == "committed")
    assert diag.get("notes", "") == ("release notes" if diagnosis_state in ("switching", "committed") else "")
    assert update_host.recover_interrupted_update(root) is True
    assert json.loads(diag_file.read_text(encoding="utf-8"))["acknowledged"] == diag["acknowledged"]


@pytest.mark.parametrize("target", ["0.3.5-BETA", "0.3.5-beta."])
def test_windows_target_alias_cannot_replace_active_program(tmp_path, target):
    import os

    if os.name != "nt":
        pytest.skip("Windows path identity")
    root = tmp_path / "app"
    active = layout.get_version_exe(root, "0.3.5-beta")
    active.parent.mkdir(parents=True)
    active.write_bytes(b"preserve-active")
    assert layout.get_version_dir(root, target).samefile(active.parent)
    layout.write_current_state(root, "0.3.5-beta")
    layout.start_transaction(root, "0.3.5-beta", "0.3.4-beta", attempt_id="accepted-previous")
    staging = tmp_path / "staging"
    _create_mock_app_payload(staging, target)
    assert update_host.execute_update(root, staging, target_version=target,
                                      skip_health_check=True, launch_on_finish=False) is False
    assert active.read_bytes() == b"preserve-active"
    assert layout.read_transaction(root)["attemptId"] == "accepted-previous"


def test_update_rejects_version_folder_as_install_root(tmp_path):
    root = tmp_path / "app"
    active = layout.get_version_exe(root, "0.3.5")
    active.parent.mkdir(parents=True)
    active.write_bytes(b"active")
    layout.write_current_state(root, "0.3.5")
    staging = tmp_path / "staging"
    _create_mock_app_payload(staging, "0.3.6")

    assert update_host.execute_update(
        root=active.parent, staging_dir=staging, launch_on_finish=False,
    ) is False
    assert layout.get_current_version(root) == "0.3.5"
    assert active.read_bytes() == b"active"


def test_update_preserves_explicit_external_data_directory(tmp_path, monkeypatch):
    root = tmp_path / "app"
    active = layout.get_version_exe(root, "0.3.5")
    active.parent.mkdir(parents=True)
    active.write_bytes(b"active")
    layout.write_current_state(root, "0.3.5")
    data = tmp_path / "external-data"
    staging = data / "update-staging"
    _create_mock_app_payload(staging, "0.3.6")
    with sqlite3.connect(data / "holdexar.db") as db:
        db.execute("CREATE TABLE user_record(value TEXT)")
        db.execute("INSERT INTO user_record VALUES ('preserved')")
    monkeypatch.setattr(update_host, "probe_version_health", lambda *args, **kwargs: (True, "ready"))

    assert update_host.execute_update(
        root=root, staging_dir=staging, data_dir=data, launch_on_finish=False,
    ) is True
    assert layout.read_transaction(root)["dataDir"] == str(data.resolve())
    assert not (root / "data" / "holdexar.db").exists()
    with sqlite3.connect(data / "holdexar.db") as db:
        assert db.execute("SELECT value FROM user_record").fetchone() == ("preserved",)


def test_direct_install_cannot_replace_current_version(tmp_path):
    root = tmp_path / "app"
    active = layout.get_version_exe(root, "0.3.5")
    active.parent.mkdir(parents=True)
    active.write_bytes(b"preserve-active")
    layout.write_current_state(root, "0.3.5")
    staging = tmp_path / "staging"
    _create_mock_app_payload(staging, "0.3.5")
    with pytest.raises(RuntimeError):
        update_host.install_version_payload(root, staging, "0.3.5")
    assert active.read_bytes() == b"preserve-active"


def test_strict_diagnosis_writer_does_not_hide_storage_failure(tmp_path, monkeypatch):
    write = Path.write_text

    def fail(path, *args, **kwargs):
        if path.name == "update-diagnosis.tmp":
            raise OSError("diagnosis disk failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError, match="disk failure"):
        update_host._write_diagnosis(tmp_path, {"stage": "committed"}, strict=True)
    assert not (tmp_path / "logs/update-diagnosis.json").exists()


def test_verify_staging_payload_accepts_matching_files(tmp_path):
    import hashlib

    payload = tmp_path / APP_NAME
    payload.mkdir()
    (payload / "version.txt").write_text("1.2.3", encoding="utf-8")
    manifest = {"files": {"version.txt": hashlib.sha256(b"1.2.3").hexdigest()}}
    assert update_host._verify_staging_payload(tmp_path, manifest) == ""


def test_verify_staging_payload_rejects_tampered_file(tmp_path):
    import hashlib

    payload = tmp_path / APP_NAME
    payload.mkdir()
    (payload / "version.txt").write_text("1.2.3", encoding="utf-8")
    manifest = {"files": {"version.txt": hashlib.sha256(b"tampered").hexdigest()}}
    assert "校验不符" in update_host._verify_staging_payload(tmp_path, manifest)


def test_verify_staging_payload_rejects_missing_file(tmp_path):
    payload = tmp_path / APP_NAME
    payload.mkdir()
    manifest = {"files": {"gone.exe": "0" * 64}}
    assert "缺失" in update_host._verify_staging_payload(tmp_path, manifest)


def test_verify_staging_payload_skips_legacy_manifest(tmp_path):
    assert update_host._verify_staging_payload(tmp_path, {"tag": "v1"}) == ""
