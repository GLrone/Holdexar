"""历史数据安全迁移：AppData 与旧布局数据向统一便携式 data/ 目录迁移。

核心承诺：
1. 原数据目录只读访问，绝不自动删除或修改历史 C 盘文件；
2. SQLite 采用 VACUUM INTO 在线快照，合并 WAL/SHM，避免文件级复制导致页级撕裂；
3. 迁移落盘后执行 PRAGMA quick_check 完整性校验，校验失败立刻清理并保持现状；
4. 冲突安装（两处均有库）禁止静默覆盖；
5. 迁移成功后在目标目录留下 migration.json 与清理指引说明文件。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from app.core.app_info import APP_SLUG
from app.core.paths import data_dir_filename, machine_candidates

logger = logging.getLogger(__name__)


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


def migrate_appdata_to_portable(
    src_dir: Path, target_data_dir: Path, slug: str = APP_SLUG
) -> dict[str, Any]:
    """将 AppData 目录下的历史数据库与配置安全迁移到便携式目录。"""
    src_dir = src_dir.resolve()
    target_data_dir = target_data_dir.resolve()
    if src_dir == target_data_dir:
        return {"migrated": False, "reason": "same_directory"}

    db_name = data_dir_filename(slug)
    src_db = src_dir / db_name
    dest_db = target_data_dir / db_name

    if not src_db.is_file():
        return {"migrated": False, "reason": "no_source_db"}

    if dest_db.is_file():
        return {"migrated": False, "reason": "dest_db_exists"}

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

    # 2. 完整性校验
    verify_conn = sqlite3.connect(tmp_dest.as_posix(), timeout=10)
    try:
        cur = verify_conn.execute("PRAGMA quick_check")
        row = cur.fetchone()
        result = row[0] if row else "failed"
        if result.lower() != "ok":
            tmp_dest.unlink(missing_ok=True)
            raise RuntimeError(f"迁移数据库完整性校验失败：{result}")
    finally:
        verify_conn.close()

    tmp_dest.replace(dest_db)

    # 3. 复制配置、种子等辅助子目录（若目标尚未创建）
    copied_subdirs: list[str] = []
    for sub in ("config", "seed", "backups", "logs"):
        src_sub = src_dir / sub
        dest_sub = target_data_dir / sub
        if src_sub.is_dir() and not dest_sub.exists():
            try:
                shutil.copytree(
                    src_sub,
                    dest_sub,
                    ignore=shutil.ignore_patterns("*.lock", "*.tmp", "*.part"),
                )
                copied_subdirs.append(sub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("复制辅助目录 %s 失败：%s", sub, exc)

    # 4. 写入迁移凭据与用户说明
    meta = {
        "migratedAt": time.time(),
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "database": db_name,
        "copiedSubdirs": copied_subdirs,
        "status": "success",
    }
    (target_data_dir / "migration.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    notice_file = target_data_dir / "MIGRATION_FROM_APPDATA.txt"
    notice_file.write_text(
        "Holdexar 数据迁移说明\n"
        "==================================================\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"来源：{src_dir}\n"
        f"目标：{target_data_dir}\n\n"
        "您的应用数据（数据库与配置）已安全迁移到当前程序目录旁的 data/ 文件夹中。\n"
        "原始 AppData 目录中的文件已被原样保留（未删除任何文件）。\n"
        "在确认新安装运行正常后，您可以根据需要手动清理或备份旧目录。\n",
        encoding="utf-8",
    )

    return {
        "migrated": True,
        "sourceDir": str(src_dir),
        "targetDir": str(target_data_dir),
        "copiedSubdirs": copied_subdirs,
    }
