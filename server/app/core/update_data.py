"""升级验收期的数据保护与既有更新事务协作。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.core import layout
from app.core.app_info import APP_SLUG
from app.core import db_safety

_REGISTRATION_WAIT_SECONDS = 3.0


def _db_path(data_dir: Path) -> Path:
    return data_dir.resolve() / f"{APP_SLUG}.db"


def read_schema_version(data_dir: Path) -> int:
    return db_safety.read_schema_version(data_dir, f"{APP_SLUG}.db")


def _marker_path(data_dir: Path) -> Path:
    return data_dir.resolve() / db_safety._BACKUP_DIR_NAME / db_safety._LOCK_FILE


def _read_marker(data_dir: Path) -> dict | None:
    path = _marker_path(data_dir)
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("升级数据保护记录无法读取，已停止自动处理") from exc
    if not isinstance(marker, dict):
        raise RuntimeError("升级数据保护记录无效")
    return marker


def _transaction(root: Path) -> dict:
    tx = layout.read_transaction(root)
    if not isinstance(tx, dict) or not tx.get("attemptId"):
        raise RuntimeError("升级事务不存在或无法读取")
    return tx


def _record(root: Path, **fields) -> dict:
    tx = _transaction(root)
    tx = layout.update_transaction(root, tx["stage"], **fields)
    with (root / layout.STATE_DIR_NAME / layout.TRANSACTION_FILE).open("rb+") as stream:
        os.fsync(stream.fileno())
    return tx


def _matching_marker(root: Path, tx: dict) -> dict:
    data_dir = Path(tx["dataDir"])
    marker = _read_marker(data_dir)
    if (
        not marker or marker.get("purpose") != "update"
        or marker.get("token") != tx.get("dataToken")
        or not tx.get("dataToken")
        or marker.get("attemptId") != tx["attemptId"]
        or marker.get("installRoot") != str(root.resolve())
        or str(_db_path(data_dir)) != tx.get("dataDbPath")
    ):
        raise RuntimeError("升级数据保护与当前事务不匹配，已停止自动处理")
    return marker


@contextmanager
def _read_only_source(db: Path):
    connection = sqlite3.connect(str(db), timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        connection.close()


def prepare(root: Path, data_dir: Path, attempt: str) -> dict:
    root, data_dir = root.resolve(), data_dir.resolve()
    tx = _transaction(root)
    if tx["attemptId"] != attempt or tx.get("dataPrepared") or tx.get("dataAccepted"):
        raise RuntimeError("升级数据准备与当前事务不匹配")
    data_dir.mkdir(parents=True, exist_ok=True)
    db = _db_path(data_dir)
    name = hashlib.sha256(attempt.encode("utf-8")).hexdigest()[:24]
    snapshot = data_dir / db_safety._BACKUP_DIR_NAME / f"update-{name}.original.db"
    if snapshot.exists():
        raise RuntimeError("本次升级的原始快照已存在，拒绝覆盖")
    with db_safety.sync_write_gate(db):
        marker, token = db_safety._acquire_restore_lock(
            db_path=db, purpose="update", install_root=root, attempt_id=attempt,
        )
        snapshot_ready = False
        try:
            had_database = db.is_file()
            digest = None
            if had_database:
                with _read_only_source(db):
                    db_safety.snapshot_to(db, snapshot)
                if not db_safety._verify_file(snapshot, True)[0]:
                    raise RuntimeError("升级原始数据库快照校验失败")
                digest = db_safety.sha256_of(snapshot)
                with snapshot.open("rb+") as stream:
                    os.fsync(stream.fileno())
            snapshot_ready = True
            return _record(
                root, dataDir=str(data_dir), dataDbPath=str(db),
                dataHadDatabase=had_database,
                dataSnapshot=str(snapshot) if had_database else None,
                dataSnapshotSha256=digest, dataToken=token,
                dataPrepared=True, dataAccepted=False, dataRecoveryPhase="prepared",
            )
        except BaseException:
            if not snapshot_ready:
                db_safety._release_restore_lock(marker, token)
            raise


def validate_startup(data_dir: Path) -> str | None:
    data_dir = data_dir.resolve()
    marker = _read_marker(data_dir)
    if marker is not None and marker.get("purpose") != "update":
        db_safety.ensure_restore_lock_available(_db_path(data_dir))
        marker = _read_marker(data_dir)
    attempt = os.environ.get("HOLDEXAR_UPDATE_ATTEMPT_ID")
    if marker is None and not attempt:
        return None
    install = os.environ.get("HOLDEXAR_INSTALL_ROOT")
    token = os.environ.get("HOLDEXAR_UPDATE_DATA_TOKEN")
    if not marker or not attempt or not install or not token:
        raise RuntimeError("升级数据仍受保护，缺少本次启动授权")
    root = Path(install).resolve()
    tx = _transaction(root)
    deadline = time.monotonic() + _REGISTRATION_WAIT_SECONDS
    while tx.get("dataProbeRegistrationRequired"):
        if not db_safety._pid_is_running(marker.get("pid", 0)):
            raise RuntimeError("升级宿主已退出，拒绝初始化受保护数据库")
        registered = any(
            isinstance(child, dict) and child.get("pid") == os.getpid()
            and child.get("stopped") is False
            for child in tx.get("children", [])
        )
        if not tx.get("launchPending") and registered:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("升级启动尚未完成进程登记，拒绝初始化数据库")
        time.sleep(0.02)
        tx = _transaction(root)
    _matching_marker(root, tx)
    if (
        tx["attemptId"] != attempt or tx.get("dataToken") != token
        or tx.get("dataDir") != str(data_dir)
        or not tx.get("dataPrepared") or tx.get("dataAccepted")
        or tx.get("dataRecoveryPhase") != "prepared"
    ):
        raise RuntimeError("本次启动的升级数据授权无效")
    return token


@contextmanager
def startup_authorization(data_dir: Path):
    token = validate_startup(data_dir)
    if token is None:
        yield None
    else:
        from app.core import database

        with database.maintenance_authorization(token):
            yield token


def _require_children_stopped(tx: dict, children_stopped: bool) -> None:
    if not children_stopped or tx.get("launchPending"):
        raise RuntimeError("尚未确认升级验收进程全部退出，拒绝恢复数据")
    for child in tx.get("children", []):
        pid = child.get("pid") if isinstance(child, dict) else None
        if not isinstance(pid, int) or pid <= 0 or db_safety._pid_is_running(pid):
            raise RuntimeError("升级验收进程尚未退出或身份无法确认")


def _check_snapshot(tx: dict) -> Path | None:
    if not tx.get("dataHadDatabase"):
        return None
    path = Path(tx.get("dataSnapshot") or "")
    if (
        not path.is_file() or db_safety.sha256_of(path) != tx.get("dataSnapshotSha256")
        or not db_safety._verify_file(path, True)[0]
    ):
        raise RuntimeError("升级原始快照已损坏或不完整，拒绝覆盖当前数据")
    return path


def _assert_files_idle(db: Path) -> None:
    if os.name != "nt":
        if db.exists():
            with _read_only_source(db):
                pass
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if not path.exists():
            continue
        handle = create(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise RuntimeError("数据库仍被其他进程占用，已保留现场并停止恢复")
        close(handle)


def rollback(root: Path, *, children_stopped: bool) -> dict:
    root = root.resolve()
    tx = _transaction(root)
    if tx.get("dataAccepted"):
        raise RuntimeError("升级数据已接受，禁止自动还原旧数据")
    if tx.get("dataRollbackReleased"):
        _release_completed_rollback(root, tx)
        return {"restored": True, "alreadyRestored": True, "dataDir": tx["dataDir"]}
    if not tx.get("dataPrepared"):
        raise RuntimeError("升级原始快照尚未准备完成，拒绝覆盖数据")
    _require_children_stopped(tx, children_stopped)
    db = Path(tx["dataDbPath"])
    _matching_marker(root, tx)
    with db_safety.sync_write_gate(db, maintenance_token=tx["dataToken"]):
        tx = _transaction(root)
        _matching_marker(root, tx)
        if tx.get("dataAccepted"):
            raise RuntimeError("升级数据已接受，禁止自动还原旧数据")
        snapshot = _check_snapshot(tx)
        phase = tx["dataRecoveryPhase"]
        if phase == "prepared":
            _assert_files_idle(db)
            name = hashlib.sha256(tx["attemptId"].encode("utf-8")).hexdigest()[:24]
            failed = Path(tx.get("dataSnapshot") or (_marker_path(db.parent).parent / f"update-{name}.original.db"))
            failed = failed.with_name(failed.name.replace(".original.db", ".failed.db"))
            existing = [suffix for suffix in ("", "-wal", "-shm") if db.with_name(db.name + suffix).exists()]
            tx = _record(root, dataRecoveryPhase="preserving", dataFailedDb=str(failed), dataRecoveryFiles=existing)
            phase = "preserving"
        if phase == "preserving":
            _assert_files_idle(db)
            failed = Path(tx["dataFailedDb"])
            if any(db.with_name(db.name + suffix).exists() and suffix not in tx["dataRecoveryFiles"]
                   for suffix in ("", "-wal", "-shm")):
                raise RuntimeError("恢复现场出现未知文件，已停止覆盖")
            for suffix in tx["dataRecoveryFiles"]:
                source = db.with_name(db.name + suffix)
                target = failed.with_name(failed.name + suffix)
                if source.exists() and target.exists():
                    raise RuntimeError("恢复现场出现冲突文件，拒绝覆盖")
                if source.exists():
                    os.replace(source, target)
                elif not target.exists():
                    raise RuntimeError("恢复现场文件缺失，拒绝继续覆盖")
            tx = _record(root, dataRecoveryPhase="restoring")
            phase = "restoring"
        if phase in ("restoring", "restored"):
            if any(db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm")):
                raise RuntimeError("恢复目标出现未知数据库侧车文件，已停止覆盖")
        if phase == "restoring":
            if db.exists():
                if snapshot is None or db_safety.sha256_of(db) != tx["dataSnapshotSha256"]:
                    raise RuntimeError("恢复目标出现未知数据，拒绝覆盖")
            elif snapshot is not None:
                staging = snapshot.with_name(snapshot.name + ".restore-copy")
                shutil.copyfile(snapshot, staging)
                if db_safety.sha256_of(staging) != tx["dataSnapshotSha256"]:
                    raise RuntimeError("数据库恢复副本校验失败")
                with staging.open("rb+") as stream:
                    os.fsync(stream.fileno())
                os.replace(staging, db)
            if snapshot is not None and not db_safety._verify_file(db, True)[0]:
                raise RuntimeError("恢复后的数据库校验失败")
            _record(root, dataRecoveryPhase="restored")
        elif phase != "restored":
            raise RuntimeError("数据库恢复阶段无法识别")
        elif (snapshot is None and db.exists()) or (
            snapshot is not None and (not db.is_file() or db_safety.sha256_of(db) != tx["dataSnapshotSha256"])
        ):
            raise RuntimeError("已恢复数据库出现未知变更，已保留保护")
        return {"restored": True, "alreadyRestored": phase == "restored", "dataDir": str(db.parent)}


def _release_completed_rollback(root: Path, tx: dict) -> None:
    db = Path(tx["dataDbPath"])
    if _read_marker(db.parent) is None:
        return
    _matching_marker(root, tx)
    with db_safety.sync_write_gate(db, maintenance_token=tx["dataToken"]):
        current = _transaction(root)
        if current["attemptId"] != tx["attemptId"] or not current.get("dataRollbackReleased"):
            raise RuntimeError("回退完成记录已变化，保留数据保护")
        _matching_marker(root, current)
        with (root / layout.STATE_DIR_NAME / layout.TRANSACTION_FILE).open("rb+") as stream:
            os.fsync(stream.fileno())
        db_safety._release_restore_lock(_marker_path(db.parent), current["dataToken"])


def _check_rollback_terminal(root: Path, tx: dict) -> None:
    if (
        tx.get("dataAccepted") or tx.get("dataRecoveryPhase") != "restored"
        or tx.get("stage") != layout.STAGE_ROLLED_BACK
        or not tx.get("sourceVersion")
        or layout.get_current_version(root) != tx["sourceVersion"]
    ):
        raise RuntimeError("原程序指向与数据库回退尚未全部完成，保留数据保护")
    _require_children_stopped(tx, True)


def finish_rollback(root: Path) -> dict:
    root = root.resolve()
    tx = _transaction(root)
    if tx.get("dataAccepted"):
        raise RuntimeError("升级数据已接受，禁止释放回退保护")
    if tx.get("dataRollbackReleased"):
        _release_completed_rollback(root, tx)
        return {"released": True, "alreadyReleased": True, "dataDir": tx["dataDir"]}
    _check_rollback_terminal(root, tx)
    _matching_marker(root, tx)
    db = Path(tx["dataDbPath"])
    with db_safety.sync_write_gate(db, maintenance_token=tx["dataToken"]):
        tx = _transaction(root)
        _check_rollback_terminal(root, tx)
        _matching_marker(root, tx)
        _assert_files_idle(db)
        if any(db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm")):
            raise RuntimeError("已恢复数据库出现未知侧车文件，保留数据保护")
        if (not tx.get("dataHadDatabase") and db.exists()) or (
            tx.get("dataHadDatabase") and (not db.is_file() or db_safety.sha256_of(db) != tx["dataSnapshotSha256"])
        ):
            raise RuntimeError("已恢复数据库出现未知变更，保留数据保护")
        with (root / layout.STATE_DIR_NAME / layout.CURRENT_STATE_FILE).open("rb+") as stream:
            os.fsync(stream.fileno())
        _record(root, dataRollbackReleased=True)
        db_safety._release_restore_lock(_marker_path(db.parent), tx["dataToken"])
        return {"released": True, "alreadyReleased": False, "dataDir": tx["dataDir"]}


def _check_preparation_abort(root: Path, data_dir: Path, tx: dict) -> None:
    if (
        tx.get("dataPrepared") or tx.get("dataAccepted")
        or tx.get("launchPending") or tx.get("children")
        or not tx.get("sourceVersion")
        or layout.get_current_version(root) != tx["sourceVersion"]
        or layout.get_current_version(root) == tx.get("targetVersion")
        or (tx.get("dataDir") and Path(tx["dataDir"]).resolve() != data_dir)
    ):
        raise RuntimeError("升级准备状态无法确认安全，保留数据保护")


def _preparation_marker(root: Path, data_dir: Path, tx: dict) -> dict | None:
    marker = _read_marker(data_dir)
    if marker is None:
        return None
    if (
        marker.get("purpose") != "update" or not marker.get("token")
        or marker.get("attemptId") != tx["attemptId"]
        or marker.get("installRoot") != str(root)
        or (tx.get("dataToken") and marker["token"] != tx["dataToken"])
    ):
        raise RuntimeError("升级准备保护与当前事务不匹配，拒绝解除")
    return marker


def abort_preparation(root: Path, *, data_dir: Path | None = None) -> dict:
    root = root.resolve()
    tx = _transaction(root)
    data_dir = (data_dir or Path(tx.get("dataDir") or root / layout.DATA_DIR_NAME)).resolve()
    _check_preparation_abort(root, data_dir, tx)
    marker = _preparation_marker(root, data_dir, tx)
    if marker is None:
        return {"aborted": True, "alreadyReleased": True, "dataDir": str(data_dir)}
    with db_safety.sync_write_gate(_db_path(data_dir), maintenance_token=marker["token"]):
        current = _transaction(root)
        if current["attemptId"] != tx["attemptId"]:
            raise RuntimeError("升级准备事务已变化，拒绝解除保护")
        _check_preparation_abort(root, data_dir, current)
        current_marker = _preparation_marker(root, data_dir, current)
        if current_marker is None or current_marker["token"] != marker["token"]:
            raise RuntimeError("升级准备保护已变化，拒绝解除")
        db_safety._release_restore_lock(_marker_path(data_dir), marker["token"])
        return {"aborted": True, "alreadyReleased": False, "dataDir": str(data_dir)}


def _accepted_compatibility(root: Path, tx: dict, schema: int, *, repairing: bool) -> dict:
    if not isinstance(tx.get("targetVersion"), str) or not layout.is_managed_version_name(tx["targetVersion"]):
        raise RuntimeError("已验收程序版本无效，保留数据保护")
    previous = layout.read_data_compatibility(root)
    same_database = previous is not None and Path(previous["dataDir"]) == Path(tx["dataDir"])
    same_schema = same_database and previous["schemaVersion"] == schema
    if repairing and previous is not None and not same_schema:
        raise RuntimeError("数据相容记录已属于其他数据状态，拒绝用旧验收记录覆盖")
    versions = list(previous["versions"]) if same_schema else []
    if tx["targetVersion"] not in versions:
        versions.append(tx["targetVersion"])
    return {"dataDir": tx["dataDir"], "schemaVersion": schema, "versions": versions}


def accept(root: Path) -> dict:
    root = root.resolve()
    tx = _transaction(root)
    if not tx.get("dataPrepared") or tx.get("dataRecoveryPhase") not in ("prepared", "accepted"):
        raise RuntimeError("当前升级数据不可接受")
    db = Path(tx["dataDbPath"])
    marker = _read_marker(db.parent)
    if marker is not None and marker.get("purpose") != "update":
        db_safety.ensure_restore_lock_available(db)
        marker = _read_marker(db.parent)
    if marker is not None or not tx.get("dataAccepted"):
        _matching_marker(root, tx)
    with db_safety.sync_write_gate(db, maintenance_token=tx["dataToken"]):
        current = _transaction(root)
        if current["attemptId"] != tx["attemptId"]:
            raise RuntimeError("升级验收事务已变化，保留数据保护")
        tx = current
        marker = _read_marker(db.parent)
        if marker is not None or not tx.get("dataAccepted"):
            _matching_marker(root, tx)
        accepted = bool(tx.get("dataAccepted"))
        schema = tx.get("dataSchemaVersion") if accepted else read_schema_version(db.parent)
        if type(schema) is not int or schema < 0:
            raise RuntimeError("缺少已接受数据的结构版本凭据，拒绝猜测相容程序")
        compatibility = _accepted_compatibility(root, tx, schema, repairing=accepted and marker is None)
        if not accepted or marker is not None:
            tx = _record(root, dataAccepted=True, dataSchemaVersion=schema, dataRecoveryPhase="accepted")
        layout.write_data_compatibility(root, compatibility)
        if marker is not None:
            db_safety._release_restore_lock(_marker_path(db.parent), tx["dataToken"])
        return tx
