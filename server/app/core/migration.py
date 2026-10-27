"""历史数据只读快照迁移；独立数据库冲突时保留双方。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.app_info import APP_SLUG
from app.core.paths import data_dir_filename, machine_candidates

logger = logging.getLogger(__name__)

MIGRATION_COMPLETED = "completed"
MIGRATION_REUSED = "reused"          # 目标已有库且来自迁移：按现状续用
MIGRATION_CONFLICT = "conflict"      # 两处都有库且目标库来源不明：拒绝静默覆盖
MIGRATION_INCOMPLETE = "incomplete"  # 主库已就位，辅助文件未搬全
MIGRATION_FAILED = "failed"          # 主库未就位

# 辅助文件目录；种子缺失不算未完成（首次启动本来就要联网获取）
_AUX_DIRS = ("config", "seed", "backups", "logs")


def detect_legacy_data_locations(
    install_root: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """检测安装目录与机器级 AppData 的历史数据分布。"""
    target_data = install_root / "data"
    db_name = data_dir_filename(slug)
    target_db = target_data / db_name
    has_target_db = target_db.is_file()

    appdata_dir: Path | None = None
    for cand in machine_candidates(slug):
        if (cand / db_name).is_file():
            appdata_dir = cand
            break

    meta = _read_migration_meta(target_data)
    pending = (meta.get("status") == MIGRATION_INCOMPLETE
               and meta.get("targetDir") == str(target_data.resolve())
               and meta.get("database") == db_name)
    if appdata_dir is None and pending:
        recorded_source = meta.get("sourceDir")
        if isinstance(recorded_source, str) and Path(recorded_source).is_absolute():
            appdata_dir = Path(recorded_source)
    has_appdata_db = appdata_dir is not None and (appdata_dir / db_name).is_file()
    has_conflict = has_target_db and (has_appdata_db or pending)

    return {
        "targetDataDir": target_data,
        "hasTargetDb": has_target_db,
        "appdataDataDir": appdata_dir,
        "hasAppdataDb": has_appdata_db,
        "hasConflict": has_conflict,
        "isFresh": not has_target_db and not has_appdata_db,
    }


def _read_migration_meta(target_data_dir: Path) -> dict[str, Any]:
    meta_file = target_data_dir / "migration.json"
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def resolve_data_dir_conflict(
    target_data_dir: Path,
    src_dir: Path | None = None,
    slug: str = APP_SLUG,
) -> dict[str, Any]:
    """仅复用已完成的同源迁移；独立数据库冲突时保留双方。"""
    target_data_dir = target_data_dir.resolve()
    db_name = data_dir_filename(slug)
    dest_db = target_data_dir / db_name
    meta = _read_migration_meta(target_data_dir)

    if src_dir is None:
        for cand in machine_candidates(slug):
            if (cand / db_name).is_file():
                src_dir = cand
                break

    if src_dir is None or not (src_dir / db_name).is_file():
        pending = meta.get("status") == MIGRATION_INCOMPLETE
        return {
            "completed": dest_db.is_file() and not pending,
            "status": MIGRATION_INCOMPLETE if pending else MIGRATION_REUSED,
            "migrated": False,
            "reason": "no_source_conflict",
            "targetDb": str(dest_db),
            "warnings": meta.get("warnings", []) if pending else [],
        }

    src_dir = src_dir.resolve()
    reused = (
        meta.get("status") in {MIGRATION_COMPLETED, MIGRATION_INCOMPLETE}
        and meta.get("sourceDir") == str(src_dir)
        and meta.get("targetDir") == str(target_data_dir)
        and meta.get("database") == db_name
        and dest_db.is_file()
        and _verify_snapshot(dest_db).lower() == "ok"
    )
    if reused and (
        meta.get("status") == MIGRATION_INCOMPLETE or not meta.get("completed")
        or any(not (target_data_dir / entry.name).is_file()
               for entry in src_dir.glob("secret.*") if entry.is_file())
    ):
        return _finish_migration(src_dir, target_data_dir, db_name, meta, migrated=False)
    return {
        "completed": reused,
        "status": MIGRATION_REUSED if reused else MIGRATION_CONFLICT,
        "migrated": False,
        "reason": "completed_migration" if reused else "independent_databases",
        "sourceDir": str(src_dir),
        "targetDb": str(dest_db),
        "warnings": [],
    }


def _verify_snapshot(path: Path) -> str:
    """快照完整性校验：返回 PRAGMA quick_check 结果。"""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "failed"
    finally:
        conn.close()


def migrate_appdata_to_portable(
    src_dir: Path, target_data_dir: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """数据库与必需附件均就位才允许启动；失败保留来源供接续。"""
    src_dir = src_dir.resolve()
    target_data_dir = target_data_dir.resolve()
    db_name = data_dir_filename(slug)

    if src_dir == target_data_dir:
        return {
            "completed": True, "status": MIGRATION_REUSED, "migrated": False,
            "reason": "same_directory", "warnings": [], "copiedSubdirs": [],
        }

    src_db = src_dir / db_name
    dest_db = target_data_dir / db_name

    if dest_db.exists():
        return resolve_data_dir_conflict(target_data_dir, src_dir, slug)

    if not src_db.is_file():
        return {
            "completed": dest_db.is_file(),
            "status": MIGRATION_REUSED if dest_db.is_file() else MIGRATION_FAILED,
            "migrated": False, "reason": "no_source_db", "warnings": [],
            "copiedSubdirs": [],
        }

    return adopt_source_database(src_dir, target_data_dir, slug)


def adopt_source_database(
    src_dir: Path, target_data_dir: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """以完整快照落位新目标，保留来源与已有目标。"""
    src_dir = src_dir.resolve()
    target_data_dir = target_data_dir.resolve()
    db_name = data_dir_filename(slug)
    src_db = src_dir / db_name
    dest_db = target_data_dir / db_name
    if dest_db.exists():
        return resolve_data_dir_conflict(target_data_dir, src_dir, slug)

    target_data_dir.mkdir(parents=True, exist_ok=True)
    (target_data_dir / "logs").mkdir(parents=True, exist_ok=True)

    tmp_dest = target_data_dir / f"{db_name}.{uuid.uuid4().hex}.migrating.tmp"

    conn = sqlite3.connect(src_db.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
    try:
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("VACUUM INTO ?", (tmp_dest.as_posix(),))
        finally:
            conn.close()
        result = _verify_snapshot(tmp_dest)
        if result.lower() != "ok":
            raise RuntimeError(f"迁移数据库完整性校验失败：{result}")
        with tmp_dest.open("rb+") as stream:
            os.fsync(stream.fileno())
        _install_missing_file(tmp_dest, dest_db)
    finally:
        tmp_dest.unlink(missing_ok=True)

    return _finish_migration(src_dir, target_data_dir, db_name, {"migratedAt": time.time()}, migrated=True)


def _install_missing_file(staging: Path, target: Path) -> None:
    # Windows rename 与 POSIX link 均拒绝覆盖并发创建的目标。
    if os.name == "nt":
        staging.rename(target)
    else:
        os.link(staging, target)


def _finish_migration(src_dir: Path, target_data_dir: Path, db_name: str, meta: dict, *, migrated: bool) -> dict:
    copied_subdirs = list(meta.get("copiedSubdirs") or [])
    required_dirs = set(meta.get("requiredDirs") or []) | {
        sub for sub in _AUX_DIRS if sub != "seed" and (src_dir / sub).is_dir()
    }
    required_keys = set(meta.get("requiredKeys") or []) | {
        entry.name for entry in src_dir.glob("secret.*")
        if entry.is_file() and not entry.name.endswith((".lock", ".tmp", ".part"))
    }
    warnings: list[str] = []

    def copy_missing(source, destination):
        source, destination = Path(source), Path(destination)
        if source.resolve() != source or destination.resolve() != destination:
            raise RuntimeError("迁移附件不能通过链接指向其他位置")
        if destination.exists():
            if not destination.is_file():
                raise RuntimeError("迁移附件目标不是普通文件")
            if source.name.startswith("secret.") and source.read_bytes() != destination.read_bytes():
                raise RuntimeError(f"目标已有不同的密钥文件：{source.name}")
            return str(destination)
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            try:
                _install_missing_file(temporary, destination)
            except FileExistsError:
                if source.name.startswith("secret.") and source.read_bytes() != destination.read_bytes():
                    raise RuntimeError(f"目标已有不同的密钥文件：{source.name}")
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def ignore_transient(directory, names):
        source = Path(directory)
        destination = target_data_dir / source.relative_to(src_dir)
        if source.resolve() != source or destination.resolve() != destination:
            raise RuntimeError("迁移附件目录不能通过链接指向其他位置")
        return shutil.ignore_patterns("*.lock", "*.tmp", "*.part")(directory, names)

    for sub in _AUX_DIRS:
        src_sub = src_dir / sub
        dest_sub = target_data_dir / sub
        if not src_sub.is_dir():
            if sub in required_dirs:
                warnings.append(f"原始辅助目录 {sub} 暂不可用")
            continue
        try:
            shutil.copytree(
                src_sub,
                dest_sub,
                dirs_exist_ok=True,
                copy_function=copy_missing,
                ignore=ignore_transient,
            )
            if sub not in copied_subdirs:
                copied_subdirs.append(sub)
        except Exception as exc:  # noqa: BLE001
            if sub == "seed":
                logger.info("种子目录未随迁移复制：%s", exc)
                continue
            warnings.append(f"辅助目录 {sub} 未完整复制：{exc}")
            logger.warning("复制辅助目录 %s 失败：%s", sub, exc)

    for name in sorted(required_keys):
        source = src_dir / name
        try:
            if not source.is_file():
                raise RuntimeError("原始密钥文件暂不可用")
            copy_missing(source, target_data_dir / source.name)
        except Exception as exc:
            warnings.append(f"密钥文件 {source.name} 未完整复制：{exc}")
            logger.warning("复制密钥文件 %s 失败：%s", source.name, exc)

    status = MIGRATION_COMPLETED if not warnings else MIGRATION_INCOMPLETE

    meta.update({
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "database": db_name,
        "copiedSubdirs": copied_subdirs,
        "requiredDirs": sorted(required_dirs),
        "requiredKeys": sorted(required_keys),
        "warnings": warnings,
        "status": status,
        "completed": not warnings,
    })
    meta_file = target_data_dir / "migration.json"
    meta_tmp = target_data_dir / f"migration.{uuid.uuid4().hex}.tmp"
    try:
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        with meta_tmp.open("rb+") as stream:
            os.fsync(stream.fileno())
        meta_tmp.replace(meta_file)
    finally:
        meta_tmp.unlink(missing_ok=True)

    notice_lines = [
        "Holdexar 数据迁移说明",
        "==================================================",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"来源：{src_dir}",
        f"目标：{target_data_dir}",
        "",
        "数据库已生成安全副本；部分文件尚未就位，程序暂不启动。" if warnings
        else "您的应用数据（数据库与配置）已安全迁移到当前程序目录旁的 data/ 文件夹中。",
        "原始数据目录保持完整，程序不会自动删除。",
    ]
    if warnings:
        notice_lines.append("")
        notice_lines.append("以下内容未能完整迁移，程序会在下次启动时再次尝试：")
        notice_lines.extend(f"- {item}" for item in warnings)
    (target_data_dir / "MIGRATION_FROM_APPDATA.txt").write_text(
        "\n".join(notice_lines) + "\n", encoding="utf-8"
    )

    return {
        "completed": not warnings,
        "status": status,
        "migrated": migrated,
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "copiedSubdirs": copied_subdirs,
        "warnings": warnings,
    }
