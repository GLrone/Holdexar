"""底层数据库与文件安全基础工具（纯标准库，零第三方依赖）。

职责：
1. 跨进程独占写锁（msvcrt.locking / fcntl.flock）；
2. 维护/升级/恢复排他锁（restore.lock 状态判定与清理）；
3. SQLite 快照制作（VACUUM INTO）与完整性校验（PRAGMA integrity_check）；
4. 数据库结构版本快照查询（PRAGMA user_version）；
5. 进程存活性与系统文件占用探测。
"""
from __future__ import annotations

import ctypes
from datetime import datetime
import errno
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.app_info import APP_SLUG

logger = logging.getLogger(__name__)

_BACKUP_DIR_NAME = "backups"
_LOCK_FILE = ".restore.lock"
_WRITE_WAIT_TIMEOUT = 60.0

_process_write_locks: dict[str, threading.Lock] = {}
_process_write_locks_guard = threading.Lock()


def _pid_is_running(pid: int) -> bool:
    """探测指定 PID 是否仍在运行（区分 Windows 与 POSIX）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        handle = open_process(0x00101000, False, pid)
        if handle:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            try:
                wait = kernel32.WaitForSingleObject
                wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                wait.restype = wintypes.DWORD
                return wait(handle, 0) != 0
            finally:
                close_handle(handle)
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return getattr(exc, "winerror", None) != 87 and exc.errno != 3


def _ordinary_maintenance_marker(metadata: Any) -> bool:
    return isinstance(metadata, dict) and (
        "purpose" not in metadata or metadata["purpose"] == "maintenance"
    )


def _restore_lock_is_stale(lock_path: Path) -> bool:
    try:
        payload = lock_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        metadata = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        metadata = None
    if isinstance(metadata, dict) and metadata.get("purpose") in ("update", "restore"):
        return False
    if (_ordinary_maintenance_marker(metadata)
            and type(metadata.get("pid")) is int and metadata["pid"] > 0):
        return not _pid_is_running(metadata["pid"])
    return False


def _remove_stale_restore_lock(lock_path: Path) -> bool:
    try:
        before = lock_path.stat()
    except FileNotFoundError:
        return True
    if not _restore_lock_is_stale(lock_path):
        return False
    try:
        after = lock_path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (
            after.st_ino, after.st_mtime_ns, after.st_size
        ):
            return False
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True


def _marker_path(data_dir: Path) -> Path:
    return data_dir.resolve() / _BACKUP_DIR_NAME / _LOCK_FILE


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


def _acquire_restore_lock(
    *, lock_path: Path | None = None, db_path: Path | None = None, purpose: str = "maintenance",
    install_root: Path | None = None, attempt_id: str | None = None,
    token: str | None = None,
) -> tuple[Path, str]:
    if lock_path is None:
        lock_path = (db_path.parent / _BACKUP_DIR_NAME if db_path is not None else Path(tempfile.gettempdir())) / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_restore_lock(lock_path)
    token = token or f"{os.getpid()}-{time.time_ns()}-{os.urandom(6).hex()}"
    payload = json.dumps({
        "pid": os.getpid(),
        "created_at": datetime.now().isoformat(),
        "token": token,
        "purpose": purpose,
        "installRoot": str(install_root.resolve()) if install_root is not None else None,
        "attemptId": attempt_id,
        "dbPath": str(db_path.resolve()) if db_path is not None else None,
    }, ensure_ascii=False, indent=2)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("数据库维护正在进行中") from exc
    created = os.fstat(fd)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            os.close(fd)
    except BaseException:
        _remove_failed_restore_lock(lock_path, created, token)
        raise
    return lock_path, token


def _remove_failed_restore_lock(lock_path: Path, created: os.stat_result, token: str) -> None:
    try:
        current = lock_path.stat()
        if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
            return
        try:
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = None
        if isinstance(metadata, dict) and metadata.get("token") != token:
            return
        after = lock_path.stat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
            after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size
        ):
            return
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _release_restore_lock(lock_path: Path, token: str) -> None:
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(metadata, dict) and metadata.get("token") == token:
        lock_path.unlink(missing_ok=True)


def ensure_restore_lock_available(
    db_path: Path | None = None, *, lock_path: Path | None = None, maintenance_token: str | None = None
) -> None:
    """启动时清理可证明陈旧的锁；仍有进程持有时拒绝打开数据库。"""
    if lock_path is None:
        if db_path is None:
            return
        lock_path = db_path.parent / _BACKUP_DIR_NAME / _LOCK_FILE
    if not lock_path.exists():
        return
    if maintenance_token:
        try:
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = None
        if isinstance(metadata, dict) and metadata.get("token") == maintenance_token:
            return
    if _remove_stale_restore_lock(lock_path):
        logger.warning("已清理中断维护留下的旧锁文件")
        return
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = None
    if isinstance(metadata, dict) and metadata.get("purpose") == "restore":
        rollback = metadata.get("rollbackPath")
        original = Path(rollback) if isinstance(rollback, str) and rollback else None
        snapshot = (f"待校验的原库副本保留在 {original}。"
                    if original is not None and original.is_file()
                    else "未找到原库副本，当前数据状态尚未确认。")
        raise RuntimeError(
            f"上次原库恢复未完成，已停止写入；{snapshot}"
            "请保留整个数据目录并联系维护者恢复原库，勿删除维护保护文件"
        )
    if (not isinstance(metadata, dict) or type(metadata.get("pid")) is not int
            or metadata["pid"] <= 0 or not (
                _ordinary_maintenance_marker(metadata) or metadata.get("purpose") == "update")):
        raise RuntimeError("数据库维护保护身份无法确认，已停止写入；请保留数据目录和保护记录，勿按文件年龄清理")
    raise RuntimeError("数据库维护正在进行中，拒绝启动或备份")


def ensure_database_read_available(db_path: Path, *, maintenance_token: str | None = None) -> None:
    lock_path = db_path.parent / _BACKUP_DIR_NAME / _LOCK_FILE
    if not lock_path.exists():
        return
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = None
    if isinstance(metadata, dict) and metadata.get("purpose") == "update":
        return
    if (_ordinary_maintenance_marker(metadata) and type(metadata.get("pid")) is int
            and metadata["pid"] > 0):
        return
    ensure_restore_lock_available(db_path, maintenance_token=maintenance_token)


def snapshot_to(src: Path, dest: Path) -> None:
    """VACUUM INTO 快照：纯只读源库，生成完整一致快照。"""
    conn = sqlite3.connect(src.as_posix(), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM INTO ?", (dest.as_posix(),))
    finally:
        conn.close()


def sha256_of(path: Path) -> str:
    """文件 sha256（分块读取）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_file(path: Path, deep: bool = False) -> tuple[bool, int]:
    """校验 SQLite 文件完整性。"""
    if not path.is_file():
        return False, 0
    try:
        conn = sqlite3.connect(path.as_posix())
    except sqlite3.Error:
        return False, 0
    try:
        pragma = "PRAGMA integrity_check" if deep else "PRAGMA quick_check"
        rows = conn.execute(pragma).fetchall()
        if rows != [("ok",)]:
            return False, 0
        try:
            games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        except sqlite3.Error:
            games = 0
        return True, int(games)
    except sqlite3.Error:
        return False, 0
    finally:
        conn.close()


def read_schema_version(data_dir: Path, db_filename: str | None = None) -> int:
    """读取目标数据目录下 SQLite 数据库的 PRAGMA user_version。"""
    name = db_filename or f"{APP_SLUG}.db"
    db = data_dir.resolve() / name
    try:
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("数据库结构版本无法读取，已停止选择程序") from exc


def _process_write_lock_path(database_path: Path | None = None, *, identity: str = "default") -> Path:
    """同一数据库的跨进程写锁文件路径。"""
    identity = str(Path(database_path).resolve()) if database_path is not None else identity
    digest = hashlib.sha256(os.path.normcase(identity).encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"holdexar-write-{digest}.lock"


def _local_process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _process_write_locks_guard:
        return _process_write_locks.setdefault(key, threading.Lock())


def _try_lock_file(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        if getattr(exc, "winerror", None) in (33, 36, 158):
            return False
        raise


class _CrossProcessWriteLock:
    """SQLite 写者共享的进程内与跨进程独占锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.local_lock: threading.Lock | None = None
        self.fd: int | None = None
        self.locked = False

    async def acquire(self) -> _CrossProcessWriteLock:
        import asyncio

        try:
            async with asyncio.timeout(_WRITE_WAIT_TIMEOUT):
                local_lock = _local_process_lock(self.path)
                while not local_lock.acquire(blocking=False):
                    await asyncio.sleep(0.025)
                self.local_lock = local_lock
                self._open_file()
                while not _try_lock_file(self.fd):
                    await asyncio.sleep(0.025)
                self.locked = True
                return self
        except BaseException:
            self._release_handles(unlock=self.locked)
            raise

    def acquire_sync(self) -> _CrossProcessWriteLock:
        deadline = time.monotonic() + _WRITE_WAIT_TIMEOUT
        local_lock = _local_process_lock(self.path)
        if not local_lock.acquire(timeout=_WRITE_WAIT_TIMEOUT):
            raise TimeoutError("等待数据库写入超时")
        self.local_lock = local_lock
        try:
            self._open_file()
            while not _try_lock_file(self.fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待数据库写入超时")
                time.sleep(0.025)
            self.locked = True
            return self
        except BaseException:
            self._release_handles(unlock=self.locked)
            raise

    def _open_file(self) -> None:
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b"\0")

    def release(self) -> None:
        self._release_handles(unlock=self.locked)

    def _release_handles(self, *, unlock: bool = False) -> None:
        try:
            if self.fd is not None:
                fd = self.fd
                try:
                    if unlock:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                    self.fd = None
                    self.locked = False
        finally:
            if self.local_lock is not None:
                try:
                    self.local_lock.release()
                except RuntimeError:
                    pass
                self.local_lock = None


def _check_write_maintenance(database_path: Path | None = None, *, maintenance_token: str | None = None) -> None:
    if database_path is not None:
        ensure_restore_lock_available(database_path, maintenance_token=maintenance_token)


@contextmanager
def sync_write_gate(database_path: Path | None = None, *, maintenance_token: str | None = None,
                    lock_path: Path | None = None):
    """同步独占写锁。"""
    _check_write_maintenance(database_path, maintenance_token=maintenance_token)
    lock = _CrossProcessWriteLock(lock_path or _process_write_lock_path(database_path)).acquire_sync()
    try:
        _check_write_maintenance(database_path, maintenance_token=maintenance_token)
        yield
    finally:
        lock.release()
