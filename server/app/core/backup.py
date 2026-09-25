"""SQLite 物理备份：VACUUM INTO 在线快照 + 三段式恢复。

快照语义（VACUUM INTO）：对源库只读，把已提交状态（含 WAL 内容）完整快照到
独立新文件，产出文件自洽可独立打开，生产库写入全程不受阻。直接 copy .db 主
文件不满足这几点——WAL 模式下会丢 -wal 里已提交未 checkpoint 的事务，复制
期间生产库仍在写入还会造成页级撕裂、备份文件可能不自洽。

恢复三段式（防误恢复伤生产库）：
1. 临时目录打开备份文件 + integrity_check + 表结构比对（与当前库同名表集合一致才继续）
2. 停调度器 + 停爬取任务 → 关闭引擎连接（释放文件句柄）
3. 生产库三份轮换（.restoring/.rollback），替换后清陈旧 WAL、重建引擎
   → 失败自动回滚原库

保留策略分两类轮转：自动备份份数 ≤ BACKUP_KEEP 且总量 ≤ 主库体积 ×
BACKUP_TOTAL_CAP_RATIO（至少保留 BACKUP_MIN_KEEP 份）；手动备份（文件名带
-manual 标记）单独轮转、仅保留最新一份——两类互不占位。
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)

BACKUP_KEEP = 5  # 自动备份最大份数
BACKUP_MIN_KEEP = 2  # 总量受限时仍强制保留的最少份数
BACKUP_TOTAL_CAP_RATIO = 2.0  # 自动备份总量上限 = 主库体积 × 该倍数
_BACKUP_DIR_NAME = "backups"
_LOCK_FILE = ".restore.lock"
MANUAL_LABEL = "manual"  # 手动备份的文件名标记：独立轮转，仅保留最新一份


def backup_dir() -> Path:
    d = get_settings().data_dir / _BACKUP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    return get_settings().data_dir / get_settings().db_filename


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ─── 创建 / 校验 / 列表 ─────────────────────────────────────


async def create_backup(label: str | None = None, manual: bool = False) -> dict:
    """在线快照备份（VACUUM INTO）：含 WAL 已提交事务，不阻塞写入。

    manual=True 产出手动备份（文件名带 -manual 标记）：独立轮转、仅保留最新
    一份，不占用自动备份的轮转位。
    产出文件名：holdexar-YYYYmmdd-HHMMSS[-label][-manual].db
    返回 {path, sizeBytes, integrityOk, games}；备份失败抛 RuntimeError。
    """
    import asyncio

    src = _db_path()
    if not src.is_file():
        raise RuntimeError(f"数据库文件不存在: {src}")
    if label and not all(c.isalnum() or c in "-_" for c in label):
        raise ValueError("label 只允许字母数字和 -_")
    name = f"{src.stem}-{_ts()}"
    if label:
        name += f"-{label}"
    if manual and label != MANUAL_LABEL:
        name += f"-{MANUAL_LABEL}"
    dest = backup_dir() / (name + ".db")
    if dest.exists():
        raise RuntimeError(f"备份文件已存在（同秒重复备份？）: {dest.name}")

    await asyncio.to_thread(snapshot_to, src, dest)

    # 自洽校验：快照必须能独立打开且 integrity 通过
    integrity_ok, games = await asyncio.to_thread(_verify_file, dest)
    if not integrity_ok:
        dest.unlink(missing_ok=True)
        raise RuntimeError("备份自洽校验失败（integrity_check 未通过），已删除坏档")

    _rotate_old_backups()

    logger.info(
        "[备份] 完成：%s（%.1f MB，games=%d）", dest.name, dest.stat().st_size / 1024 / 1024, games
    )
    return {
        "path": str(dest),
        "name": dest.name,
        "sizeBytes": dest.stat().st_size,
        "integrityOk": True,
        "games": games,
        "createdAt": datetime.now().isoformat(),
    }


def snapshot_to(src: Path, dest: Path) -> None:
    """VACUUM INTO 快照：独立短连接，只读源库，产出自洽新文件。

    isolation_level=None 必须显式设——VACUUM 不能在事务内执行，
    sqlite3 默认隔离模式会隐式开事务导致 OperationalError。

    公共入口：`create_backup` / `restore_backup` 的轮换档与 `database`
    的迁移前快照共用这一份实现（VACUUM 不能在事务内执行，见上）。
    """
    conn = sqlite3.connect(src.as_posix(), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("VACUUM INTO ?", (dest.as_posix(),))
    finally:
        conn.close()


def _verify_file(path: Path) -> tuple[bool, int]:
    """独立打开 + integrity_check + 核心表行数。返回 (是否自洽, games 行数)。

    损坏文件（非 SQLite 格式）执行 integrity_check 会直接抛
    DatabaseError——按不自洽处理，不让异常穿透到调用方。
    """
    if not path.is_file():
        return False, 0
    try:
        conn = sqlite3.connect(path.as_posix())
    except sqlite3.Error:
        return False, 0
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            return False, 0  # 非 SQLite 格式 / 页头损坏
        if not row or row[0] != "ok":
            return False, 0
        games = 0
        try:
            games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        except sqlite3.OperationalError:
            pass  # 空库/无 games 表：备份合法但计 0
        return True, games
    finally:
        conn.close()


async def verify_backup(name: str) -> dict:
    """校验一份备份的自洽性（不动任何文件）。"""
    import asyncio

    path = safe_backup_path(backup_dir(), name)
    integrity_ok, games = await asyncio.to_thread(_verify_file, path)
    return {"name": name, "integrityOk": integrity_ok, "games": games,
            "sizeBytes": path.stat().st_size if path.is_file() else 0}


def list_backups() -> list[dict]:
    """备份列表（新→旧）：名称/大小/校验状态懒校验（只查元数据，不开库）。"""
    items = []
    for p in sorted(backup_dir().glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "name": p.name,
            "sizeBytes": st.st_size,
            "createdAt": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return items


def _is_manual_backup(filename: str) -> bool:
    return filename.endswith(f"-{MANUAL_LABEL}.db")


def _main_db_bytes() -> int:
    try:
        return _db_path().stat().st_size
    except OSError:
        return 0


def _rotate_old_backups() -> None:
    """自动备份轮转受双重上限：份数 ≤ BACKUP_KEEP，总量 ≤ 主库体积 ×
    BACKUP_TOTAL_CAP_RATIO；无论上限多紧至少保留 BACKUP_MIN_KEEP 份。
    手动备份单独轮转，仅保留最新一份。

    两类互不占位：手动备份是用户显式建立的安全点，不因自动轮转被清；
    自动备份也不因手动备份的存在而少留。
    """
    files = sorted(backup_dir().glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    auto = [p for p in files if not _is_manual_backup(p.name)]
    manual = [p for p in files if _is_manual_backup(p.name)]

    cap = BACKUP_TOTAL_CAP_RATIO * _main_db_bytes()

    kept, total = 0, 0
    for p in auto:
        size = p.stat().st_size
        if kept >= BACKUP_MIN_KEEP and (kept >= BACKUP_KEEP or total + size > cap):
            p.unlink(missing_ok=True)
            logger.info("[备份] 轮转清理：%s", p.name)
            continue
        kept += 1
        total += size
    for old in manual[1:]:
        old.unlink(missing_ok=True)
        logger.info("[备份] 手动备份轮转（仅保留最新一份）：%s", old.name)


# ─── 恢复（三段式，防误恢复伤生产库）──────────────────────────


def safe_backup_path(base: Path, name: str) -> Path:
    """路径穿越防护：备份文件名只允许合法字符且必须落在备份目录内。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法备份文件名: {name!r}")
    if not all(c.isalnum() or c in "-_." for c in name) or not name.endswith(".db"):
        raise ValueError(f"非法备份文件名: {name!r}")
    p = base / name
    if p.parent != base:
        raise ValueError(f"非法备份文件名: {name!r}")
    return p


async def restore_backup(name: str) -> dict:
    """从备份恢复生产库（三段式）。

    1. 备份文件独立校验（integrity + 表集合与当前库一致）
    2. 停调度器 + 等爬取任务落地 → 关闭引擎（释放 SQLite 文件句柄）
    3. 轮换替换（.restoring/.rollback），重建引擎 → 失败自动回滚

    成功后调用方需自行决定是否重载进程内缓存（调度器会自动重启）。
    """
    import asyncio

    path = safe_backup_path(backup_dir(), name)
    if not path.is_file():
        raise FileNotFoundError(f"备份不存在: {name}")

    lock_path = backup_dir() / _LOCK_FILE
    if lock_path.exists():
        raise RuntimeError("已有恢复操作在进行中（锁文件存在）")

    src = _db_path()
    rollback = src.with_suffix(".rollback.db")
    staging = src.with_suffix(".restoring.db")

    # ── 段 1：备份文件校验 + 表集合比对 ──
    integrity_ok, _ = await asyncio.to_thread(_verify_file, path)
    if not integrity_ok:
        raise RuntimeError(f"备份文件校验失败: {name}")
    backup_tables = await asyncio.to_thread(_table_names, path)
    current_tables = await asyncio.to_thread(_table_names, src)
    if current_tables and not backup_tables >= current_tables:
        missing = current_tables - backup_tables
        raise RuntimeError(
            f"备份缺表（版本过旧，恢复会丢数据）: {', '.join(sorted(missing))}"
        )

    # ── 段 2：停写入面（调度器 + 爬取 + 引擎连接）──
    lock_path.write_text(datetime.now().isoformat(), encoding="utf-8")
    try:
        from ..core.scheduler import start_scheduler, stop_scheduler
        from app.domains.crawl import service as crawl_service

        stop_scheduler()
        active = crawl_service._active
        if active is not None and not active.task.done():
            logger.info("[恢复] 等待进行中的爬取任务结束…")
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=60)
            except asyncio.TimeoutError:
                logger.warning("[恢复] 爬取任务 60s 未结束，继续恢复（任务将被中断）")
        # 调度器停令已下（shutdown wait=False），给在途 job 一点落地时间，
        # 避免未归还连接导致 dispose 后仍持有文件句柄
        await asyncio.sleep(2)

        # 关闭 SQLAlchemy 引擎（async engine 的同步底座），释放文件句柄
        await _dispose_engine()

        # ── 段 3：文件轮换替换 + 引擎重建 ──
        rollback.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        # 回滚档同样用快照语义（shutil.copy 只复制主文件会漏 -wal 内容）
        await asyncio.to_thread(snapshot_to, src, rollback)
        shutil.copy2(path, staging)          # 备份 → 就位档（备份文件本身自洽）
        os.replace(staging, src)             # 原子替换主库文件
        # 陈旧 WAL 清理：旧库的 -wal/-shm 若残留，新库打开时会尝试重放
        # 旧帧导致损坏——替换后必须删除（此刻无连接持有，安全）
        src.with_name(src.name + "-wal").unlink(missing_ok=True)
        src.with_name(src.name + "-shm").unlink(missing_ok=True)

        engine_ok = await _rebuild_engine()
        if not engine_ok:
            # 回滚：把回滚档换回去
            logger.error("[恢复] 引擎重建失败，回滚原库")
            os.replace(rollback, src)
            src.with_name(src.name + "-wal").unlink(missing_ok=True)
            src.with_name(src.name + "-shm").unlink(missing_ok=True)
            engine_ok = await _rebuild_engine()
            if not engine_ok:
                raise RuntimeError("恢复失败且回滚失败——请手动检查 data 目录")
        else:
            rollback.unlink(missing_ok=True)  # 成功后清理回滚档
            logger.info("[恢复] 已从备份 %s 恢复（原库回滚档已清理）", name)

        start_scheduler()
        logger.info("[恢复] 调度器已重启")
        return {"restored": True, "name": name, "rolledBack": False}
    finally:
        lock_path.unlink(missing_ok=True)


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path.as_posix())
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


async def _dispose_engine() -> None:
    """关闭全局引擎并清缓存（下次 get_engine 重建）。

    get_engine/get_session_factory 均为 lru_cache——清缓存后首个调用方
    触发重建；dispose() 让旧连接池主动释放文件句柄（Windows 上不释放
    句柄则 os.replace 主库文件会 PermissionError）。
    """
    from . import database as database_module

    engine = database_module.get_engine()
    await engine.dispose()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()


async def _rebuild_engine() -> bool:
    """重建引擎并做一次探活查询。"""
    try:
        from .database import get_session_factory
        from sqlalchemy import text

        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[恢复] 引擎探活失败")
        return False
