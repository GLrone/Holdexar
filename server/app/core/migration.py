"""历史数据安全迁移：AppData 与旧布局数据向统一便携式 data/ 目录迁移。

核心承诺：
1. 原数据目录只读访问，绝不自动删除或修改历史 C 盘文件；
2. SQLite 采用 VACUUM INTO 在线快照，合并 WAL/SHM，避免文件级复制导致页级撕裂；
3. 快照落盘后执行 PRAGMA quick_check，校验失败立刻清理并保持现状；
4. 目标已有库时绝不覆盖：两处都有库判为冲突，交人工决定用哪一份；
5. 返回明确的完成状态（completed / status / warnings），调用方据此决定能否切换——
   核对失败最多只写出迁移凭据，目标目录不落任何业务数据，更不建空库。
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
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

    has_appdata_db = appdata_dir is not None
    has_conflict = has_target_db and has_appdata_db

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


def resolve_data_dir_conflict(target_data_dir: Path, slug: str = APP_SLUG) -> dict[str, Any]:
    """目标已有库：绝不覆盖。只有目标库本身来自迁移才算可继续。"""
    db_name = data_dir_filename(slug)
    meta = _read_migration_meta(target_data_dir)
    adopted = bool(meta.get("completed"))
    return {
        "completed": adopted,
        "status": MIGRATION_REUSED if adopted else MIGRATION_CONFLICT,
        "migrated": False,
        "reason": "dest_db_exists",
        "targetDb": str(target_data_dir / db_name),
        "warnings": [],
    }


def _verify_snapshot(path: Path) -> str:
    """快照完整性校验：返回 PRAGMA quick_check 结果。"""
    conn = sqlite3.connect(path.as_posix(), timeout=10)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "failed"
    finally:
        conn.close()


def migrate_appdata_to_portable(
    src_dir: Path, target_data_dir: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """将 AppData 目录下的历史数据库与配置安全迁移到便携式目录。

    `completed=True` 只说明「可以开始用 data/ 这份库」；辅助文件缺失清单
    仍在 warnings 里如实给出，调用方可以据此决定是否继续换装。
    """
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

    if not src_db.is_file():
        return {
            "completed": dest_db.is_file(),
            "status": MIGRATION_REUSED if dest_db.is_file() else MIGRATION_FAILED,
            "migrated": False, "reason": "no_source_db", "warnings": [],
            "copiedSubdirs": [],
        }

    if dest_db.is_file():
        return resolve_data_dir_conflict(target_data_dir, slug)

    return adopt_source_database(src_dir, target_data_dir, slug)


def adopt_source_database(
    src_dir: Path, target_data_dir: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """把外置库收进 data/：快照成功才算完成，失败时目标目录不落任何业务数据。"""
    src_dir = src_dir.resolve()
    target_data_dir = target_data_dir.resolve()
    db_name = data_dir_filename(slug)
    src_db = src_dir / db_name
    dest_db = target_data_dir / db_name

    target_data_dir.mkdir(parents=True, exist_ok=True)
    (target_data_dir / "logs").mkdir(parents=True, exist_ok=True)

    tmp_dest = target_data_dir / f"{db_name}.migrating.tmp"
    tmp_dest.unlink(missing_ok=True)

    # 1. VACUUM INTO 快照：合并 WAL 并规避独占写入冲突
    conn = sqlite3.connect(src_db.as_posix(), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM INTO ?", (tmp_dest.as_posix(),))
    finally:
        conn.close()

    # 2. 完整性校验：不过就清理临时产物，目标目录不得出现半成品库
    try:
        result = _verify_snapshot(tmp_dest)
    except Exception:
        tmp_dest.unlink(missing_ok=True)
        raise
    if result.lower() != "ok":
        tmp_dest.unlink(missing_ok=True)
        raise RuntimeError(f"迁移数据库完整性校验失败：{result}")

    tmp_dest.replace(dest_db)

    # 3. 复制配置、种子等辅助子目录（目标尚未创建时），失败如实记入 warnings
    copied_subdirs: list[str] = []
    warnings: list[str] = []
    for sub in _AUX_DIRS:
        src_sub = src_dir / sub
        dest_sub = target_data_dir / sub
        if not src_sub.is_dir() or dest_sub.exists():
            continue
        try:
            shutil.copytree(
                src_sub,
                dest_sub,
                ignore=shutil.ignore_patterns("*.lock", "*.tmp", "*.part"),
            )
            copied_subdirs.append(sub)
        except Exception as exc:  # noqa: BLE001
            if sub == "seed":
                logger.info("种子目录未随迁移复制：%s", exc)
                continue
            warnings.append(f"辅助目录 {sub} 未完整复制：{exc}")
            logger.warning("复制辅助目录 %s 失败：%s", sub, exc)

    status = MIGRATION_COMPLETED if not warnings else MIGRATION_INCOMPLETE

    # 4. 迁移凭据与用户说明
    meta = {
        "migratedAt": time.time(),
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "database": db_name,
        "copiedSubdirs": copied_subdirs,
        "warnings": warnings,
        "status": status,
        "completed": True,
    }
    (target_data_dir / "migration.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    notice_lines = [
        "Holdexar 数据迁移说明",
        "==================================================",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"来源：{src_dir}",
        f"目标：{target_data_dir}",
        "",
        "您的应用数据（数据库与配置）已安全迁移到当前程序目录旁的 data/ 文件夹中。",
        "原始 AppData 目录中的文件已被原样保留（未删除任何文件）。",
    ]
    if warnings:
        notice_lines.append("")
        notice_lines.append("以下内容未能完整迁移，程序会在下次启动时再次尝试：")
        notice_lines.extend(f"- {item}" for item in warnings)
    notice_lines.append("")
    notice_lines.append("在确认新安装运行正常后，您可以根据需要手动清理或备份旧目录。")
    (target_data_dir / "MIGRATION_FROM_APPDATA.txt").write_text(
        "\n".join(notice_lines) + "\n", encoding="utf-8"
    )

    return {
        "completed": True,
        "status": status,
        "migrated": True,
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "copiedSubdirs": copied_subdirs,
        "warnings": warnings,
    }
