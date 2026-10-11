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

归档形态：快照压成 gzip（`.db.gz`）并随手写一份同名 `.json` 元数据档
（sha256 + 游戏数 + 原始体积）。校验走哈希比对，不必整库走一遍
integrity_check；恢复时解档后仍做完整自检（段 1），旧的未压缩 `.db` 备份照旧可用。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .logging import log_event

logger = logging.getLogger(__name__)

BACKUP_KEEP = 5  # 自动备份最大份数
BACKUP_MIN_KEEP = 2  # 总量受限时仍强制保留的最少份数
BACKUP_TOTAL_CAP_RATIO = 2.0  # 自动备份总量上限 = 主库体积 × 该倍数
BACKUP_SUFFIX = ".db"
ARCHIVE_SUFFIX = ".db.gz"  # 压缩归档后缀
META_SUFFIX = ".gz.json"  # 归档元数据（sha256/游戏数/原始体积）
COMPRESS_LEVEL = 6  # 每日常规档的压缩档：更高档省得少、慢得多
_BACKUP_DIR_NAME = "backups"
_LOCK_FILE = ".restore.lock"
MANUAL_LABEL = "manual"  # 手动备份的文件名标记：独立轮转，仅保留最新一份
_REPLACE_ATTEMPTS = 6  # 主库替换重试次数（每次重试前重关一次引擎）
_REPLACE_RETRY_SECONDS = 0.3


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
    产出文件名：holdexar-YYYYmmdd-HHMMSS[-label][-manual].db.gz
    返回 {path, name, sizeBytes, rawBytes, sha256, integrityOk, games}；备份失败抛 RuntimeError。
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
    dest = backup_dir() / (name + ARCHIVE_SUFFIX)
    if dest.exists():
        raise RuntimeError(f"备份文件已存在（同秒重复备份？）: {dest.name}")
    snapshot = backup_dir() / (name + BACKUP_SUFFIX)

    await asyncio.to_thread(snapshot_to, src, snapshot)

    # 自洽校验：快照必须能独立打开且 integrity 通过
    integrity_ok, games = await asyncio.to_thread(_verify_file, snapshot)
    if not integrity_ok:
        snapshot.unlink(missing_ok=True)
        raise RuntimeError("备份自洽校验失败（integrity_check 未通过），已删除坏档")

    raw_bytes = snapshot.stat().st_size
    try:
        digest = await asyncio.to_thread(_compress_to_archive, snapshot, dest)
    except Exception:
        dest.unlink(missing_ok=True)
        snapshot.unlink(missing_ok=True)
        raise
    snapshot.unlink(missing_ok=True)
    _write_archive_meta(dest, digest, games, raw_bytes)

    _rotate_old_backups()

    log_event(
        logger,
        "数据库备份完成",
        tag="成功",
        detail={
            "文件名": dest.name,
            "体积MB": round(dest.stat().st_size / 1024 / 1024, 1),
            "压缩前MB": round(raw_bytes / 1024 / 1024, 1),
            "游戏数": games,
        },
    )
    return {
        "path": str(dest),
        "name": dest.name,
        "sizeBytes": dest.stat().st_size,
        "rawBytes": raw_bytes,
        "sha256": digest,
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


# ─── 归档压缩与哈希 ─────────────────────────────────────────


def sha256_of(path: Path) -> str:
    """整文件 sha256（分块读，不一次吞进内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compress_to_archive(snapshot: Path, dest: Path) -> str:
    """快照压成 gzip 归档，返回归档哈希（压缩与备份同为本机职责，哈希随档走）。"""
    with snapshot.open("rb") as fin, gzip.open(dest, "wb", compresslevel=COMPRESS_LEVEL) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)
    return sha256_of(dest)


def _decompress_archive(archive: Path, dest: Path) -> None:
    """归档解回可打开的库文件。"""
    with gzip.open(archive, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)


def _archive_meta_path(archive: Path) -> Path:
    return archive.with_name(archive.name + META_SUFFIX)


def _write_archive_meta(archive: Path, digest: str, games: int, raw_bytes: int) -> None:
    _archive_meta_path(archive).write_text(
        json.dumps(
            {"sha256": digest, "games": games, "rawBytes": raw_bytes},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_archive_meta(archive: Path) -> dict:
    """归档元数据；缺失或损坏返回空字典（此时只能走解档深校验）。"""
    try:
        meta = json.loads(_archive_meta_path(archive).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


async def compact_main_db() -> dict:
    """原地收缩主库（VACUUM）：把删数据/删索引留下的空闲页交还文件系统。

    必须停写入面再收缩：VACUUM 要独占库文件，常驻的连接池与后台任务会让它
    直接撞 `database is locked`。流程与恢复同源——停调度器 → 等在途爬取落地
    → 关引擎释放句柄 → 收缩 → 重建引擎 → 重启调度器；失败也保证引擎回来，
    不留「服务没引擎」的半死状态。
    """
    import asyncio

    src = _db_path()
    if not src.is_file():
        raise RuntimeError(f"数据库文件不存在: {src}")
    lock_path = backup_dir() / _LOCK_FILE
    if lock_path.exists():
        raise RuntimeError("已有维护操作在进行中（锁文件存在）")

    from ..core.scheduler import start_scheduler, stop_scheduler
    from app.domains.crawl import service as crawl_service

    before = _footprint(src)
    before_main = src.stat().st_size
    lock_path.write_text(datetime.now().isoformat(), encoding="utf-8")
    engine_ok = False
    try:
        stop_scheduler()
        active = crawl_service._active
        if active is not None and not active.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=60)
            except asyncio.TimeoutError:
                log_event(
                    logger,
                    "空间回收前爬取任务 60 秒未结束，继续收缩",
                    level=logging.WARNING,
                )
        await asyncio.sleep(2)
        await _dispose_engine()
        try:
            await asyncio.to_thread(_vacuum_file, src)
        finally:
            engine_ok = await _rebuild_engine()
        after = _footprint(src)
        if not engine_ok:
            raise RuntimeError("空间回收后数据库引擎重建失败——请检查数据目录")
        log_event(
            logger,
            "数据库空闲空间已回收",
            tag="成功",
            detail={
                "收缩前MB": round(before / 1024 / 1024, 1),
                "收缩后MB": round(after / 1024 / 1024, 1),
            },
        )
        return {
            "beforeBytes": before,
            "afterBytes": after,
            "freedBytes": before - after,
            "mainDbBeforeBytes": before_main,
            "mainDbAfterBytes": src.stat().st_size,
        }
    finally:
        start_scheduler()
        lock_path.unlink(missing_ok=True)


def _footprint(src: Path) -> int:
    """库文件的磁盘占用：主库 + WAL + shm。

    只看主文件会误判——VACUUM 会把 WAL 里未落盘的内容并进主库，主文件可能
    反而变大，而总占用是下降的。
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = src.with_name(src.name + suffix)
        if p.is_file():
            total += p.stat().st_size
    return total


def _vacuum_file(src: Path) -> None:
    """独立短连接执行 VACUUM：不能在事务内跑，故 isolation_level=None。"""
    conn = sqlite3.connect(src.as_posix(), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # 临时文件默认落在系统临时目录（常是系统盘），而 VACUUM 需要约一个库
        # 大小的临时空间——先钉到库自己所在的目录，避免跨盘瓶颈与系统盘告警
        # PRAGMA 不接受绑定参数，字面量按 SQL 字符串转义
        conn.execute(
            f"PRAGMA temp_store_directory = '{str(src.parent).replace(chr(39), chr(39) * 2)}'"
        )
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as e:
            # 原地 VACUUM 要把整库重写一遍，需要约一个库大小的临时空间；
            # 磁盘不够时 SQLite 只报 "disk is full"，读不出根因
            if "full" in str(e).lower():
                raise RuntimeError(
                    "磁盘空间不足，无法收缩（需要约一个数据库文件大小的空闲空间）"
                ) from e
            raise
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
    """校验一份备份的自洽性（不动任何文件）。

    归档档比对随档存下的 sha256（只读档、不解档不整库自检）；元数据缺失时才
    解档走一遍完整性自检。旧的未压缩 .db 照旧独立打开自检。
    """
    import asyncio

    path = safe_backup_path(backup_dir(), name)
    size = path.stat().st_size if path.is_file() else 0
    if not size:
        return {"name": name, "integrityOk": False, "games": 0, "sizeBytes": 0}
    if path.suffix != ".gz":
        integrity_ok, games = await asyncio.to_thread(_verify_file, path)
        return {"name": name, "integrityOk": integrity_ok, "games": games, "sizeBytes": size}

    meta = _read_archive_meta(path)
    expected = str(meta.get("sha256") or "")
    if expected:
        actual = await asyncio.to_thread(sha256_of, path)
        return {
            "name": name,
            "integrityOk": actual.lower() == expected.lower(),
            "games": int(meta.get("games") or 0),
            "sizeBytes": size,
            "sha256": actual,
            "rawBytes": int(meta.get("rawBytes") or 0),
        }

    probe = path.with_name(path.name + ".verify.tmp")  # 不进备份列表的解档探针
    try:
        await asyncio.to_thread(_decompress_archive, path, probe)
        integrity_ok, games = await asyncio.to_thread(_verify_file, probe)
    finally:
        probe.unlink(missing_ok=True)
    return {"name": name, "integrityOk": integrity_ok, "games": games, "sizeBytes": size}


def _archive_files() -> list[Path]:
    """备份目录里的归档（新→旧）：含旧的未压缩 .db，不含元数据与探针档。"""
    d = backup_dir()
    files = [p for p in d.glob(f"*{BACKUP_SUFFIX}") if p.is_file()]
    files += [p for p in d.glob(f"*{ARCHIVE_SUFFIX}") if p.is_file()]
    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


def remove_archive(archive: Path) -> None:
    """删档连带删元数据：不留无人认领的哈希。"""
    archive.unlink(missing_ok=True)
    _archive_meta_path(archive).unlink(missing_ok=True)


def list_backups() -> list[dict]:
    """备份列表（新→旧）：只查元数据不开库。"""
    items = []
    for p in _archive_files():
        st = p.stat()
        items.append({
            "name": p.name,
            "sizeBytes": st.st_size,
            "createdAt": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return items


def _is_manual_backup(filename: str) -> bool:
    return filename.removesuffix(".gz").endswith(f"-{MANUAL_LABEL}{BACKUP_SUFFIX}")


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
    files = _archive_files()
    auto = [p for p in files if not _is_manual_backup(p.name)]
    manual = [p for p in files if _is_manual_backup(p.name)]

    cap = BACKUP_TOTAL_CAP_RATIO * _main_db_bytes()

    kept, total = 0, 0
    for p in auto:
        size = p.stat().st_size
        if kept >= BACKUP_MIN_KEEP and (kept >= BACKUP_KEEP or total + size > cap):
            remove_archive(p)
            log_event(logger, "清理超量的自动备份归档", detail={"文件名": p.name})
            continue
        kept += 1
        total += size
    for old in manual[1:]:
        remove_archive(old)
        log_event(logger, "手动备份仅保留最新一份，清理旧档", detail={"文件名": old.name})


# ─── 恢复（三段式，防误恢复伤生产库）──────────────────────────


def safe_backup_path(base: Path, name: str) -> Path:
    """路径穿越防护：备份文件名只允许合法字符且必须落在备份目录内。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法备份文件名: {name!r}")
    if not all(c.isalnum() or c in "-_." for c in name) or not name.endswith(
        (BACKUP_SUFFIX, ARCHIVE_SUFFIX)
    ):
        raise ValueError(f"非法备份文件名: {name!r}")
    p = base / name
    if p.parent != base:
        raise ValueError(f"非法备份文件名: {name!r}")
    return p


async def restore_backup(name: str) -> dict:
    """从备份恢复生产库（三段式）。

    1. 归档档先比哈希，解档后与旧式 .db 一样独立校验（integrity + 表集合与当前库一致）
    2. 在线快照出原库回滚档 .rollback.db → 停调度器 + 等爬取任务落地 → 关闭引擎
    3. 轮换替换（.restoring 就位 → 顶替主库），重建引擎 → 失败自动回滚

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
    probe = backup_dir() / (name + ".verify.tmp")  # 解档待验，不进备份列表
    compressed = path.suffix == ".gz"

    # ── 段 1：哈希比对 → 解档 → 完整性与表集合比对 ──
    if compressed:
        expected = str(_read_archive_meta(path).get("sha256") or "")
        if expected and (
            (await asyncio.to_thread(sha256_of, path)).lower() != expected.lower()
        ):
            raise RuntimeError(f"备份哈希不符（归档不完整或已损坏）: {name}")
        await asyncio.to_thread(_decompress_archive, path, probe)
    verify_target = probe if compressed else path
    integrity_ok, _ = await asyncio.to_thread(_verify_file, verify_target)
    if not integrity_ok:
        probe.unlink(missing_ok=True)
        raise RuntimeError(f"备份文件校验失败: {name}")
    backup_tables = await asyncio.to_thread(_table_names, verify_target)
    current_tables = await asyncio.to_thread(_table_names, src)
    if current_tables and not backup_tables >= current_tables:
        missing = current_tables - backup_tables
        probe.unlink(missing_ok=True)
        raise RuntimeError(
            f"备份缺表（版本过旧，恢复会丢数据）: {', '.join(sorted(missing))}"
        )

    # ── 段 2：回滚档 → 停写入面（调度器 + 爬取 + 引擎连接）──
    # 回滚档必须在停引擎之前做：VACUUM INTO 是在线快照、不需独占；停引擎后再复制
    # 大库的那数十秒里，任何请求都会重建引擎并重持主库句柄，替换必然被拒。
    lock_path.write_text(datetime.now().isoformat(), encoding="utf-8")
    replaced = False
    try:
        from ..core.scheduler import start_scheduler, stop_scheduler
        from app.domains.crawl import service as crawl_service

        rollback.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        await asyncio.to_thread(snapshot_to, src, rollback)

        stop_scheduler()
        active = crawl_service._active
        if active is not None and not active.task.done():
            log_event(logger, "数据库恢复前等待进行中的爬取任务结束")
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=60)
            except asyncio.TimeoutError:
                log_event(
                    logger,
                    "爬取任务 60 秒未结束，继续恢复，该任务将被中断",
                    level=logging.WARNING,
                )
        # 调度器停令已下（shutdown wait=False），给在途 job 一点落地时间，
        # 避免未归还连接导致 dispose 后仍持有文件句柄
        await asyncio.sleep(2)

        # 关闭 SQLAlchemy 引擎（async engine 的同步底座），释放文件句柄
        await _dispose_engine()

        # ── 段 3：文件轮换替换 + 引擎重建 ──
        if compressed:
            os.replace(probe, staging)       # 已验过的解档直接就位（同卷改名）
        else:
            shutil.copy2(path, staging)      # 备份 → 就位档（备份文件本身自洽）
        await _replace_main_db(staging, src)
        replaced = True
        # 陈旧 WAL 清理：旧库的 -wal/-shm 若残留，新库打开时会尝试重放
        # 旧帧导致损坏——替换后必须删除（此刻无连接持有，安全）
        src.with_name(src.name + "-wal").unlink(missing_ok=True)
        src.with_name(src.name + "-shm").unlink(missing_ok=True)

        engine_ok = await _rebuild_engine()
        if not engine_ok:
            # 回滚：把回滚档换回去
            log_event(logger, "恢复后数据库引擎重建失败，回滚原库", level=logging.ERROR)
            await _replace_main_db(rollback, src)
            src.with_name(src.name + "-wal").unlink(missing_ok=True)
            src.with_name(src.name + "-shm").unlink(missing_ok=True)
            engine_ok = await _rebuild_engine()
            if not engine_ok:
                raise RuntimeError("恢复失败且回滚失败——请手动检查 data 目录")
        else:
            rollback.unlink(missing_ok=True)  # 成功后清理回滚档
            log_event(
                logger,
                "已从备份恢复数据库，原库回滚档已清理",
                tag="成功",
                detail={"备份文件名": name},
            )

        start_scheduler()
        log_event(logger, "数据库恢复完成，调度器已重启")
        return {"restored": True, "name": name, "rolledBack": False}
    finally:
        probe.unlink(missing_ok=True)
        if not replaced:
            # 主库未被改动，就位档与回滚档都是纯残留
            staging.unlink(missing_ok=True)
            rollback.unlink(missing_ok=True)
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
    已无缓存引擎时直接返回：此处 get_engine() 会凭空建出一个持有库文件
    句柄的新引擎，反而制造冲突。
    """
    from . import database as database_module

    if database_module.get_engine.cache_info().currsize == 0:
        return
    engine = database_module.get_engine()
    await engine.dispose()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()


async def _replace_main_db(staging: Path, src: Path) -> None:
    """原子替换主库文件，容忍并发请求在关引擎后又建回句柄。

    Windows 上任何连接持有主库文件即拒绝替换；请求进来会经 lru_cache 重建
    引擎，故每次失败先重关引擎（新池连接随之释放）再重试，超出次数如实报错。
    """
    import asyncio

    last: PermissionError | None = None
    for _ in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(staging, src)
            return
        except PermissionError as e:
            last = e
            await _dispose_engine()
            await asyncio.sleep(_REPLACE_RETRY_SECONDS)
    raise RuntimeError(
        f"主库文件被占用，替换未完成（已重试 {_REPLACE_ATTEMPTS} 次）"
    ) from last


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
        log_event(logger, "数据库引擎探活失败", level=logging.ERROR, exc_info=True)
        return False
