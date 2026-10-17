"""Holdexar 安装布局与多版本状态机。

维护统一便携式目录结构：
Holdexar/
├── Holdexar.exe (Launcher)
├── updater/UpdateHost.exe
├── versions/<version>/ (完整独立版本运行文件)
├── data/ (所有用户数据与配置，永不动)
├── state/ (current.json 版本指针, transaction.json 事务状态)
├── update-staging/ (临时解包载荷)
└── logs/ (update.log, update-diagnosis.json)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.app_info import APP_NAME

logger = logging.getLogger(__name__)

STATE_DIR_NAME = "state"
CURRENT_STATE_FILE = "current.json"
TRANSACTION_FILE = "transaction.json"
VERSIONS_DIR_NAME = "versions"
UPDATER_DIR_NAME = "updater"
DATA_DIR_NAME = "data"
STAGING_DIR_NAME = "update-staging"
LOGS_DIR_NAME = "logs"

# 事务状态机枚举
STAGE_STAGED = "staged"
STAGE_SWITCHING = "switching"
STAGE_VERIFYING = "verifying"
STAGE_COMMITTED = "committed"
STAGE_FAILED = "failed"
STAGE_ROLLED_BACK = "rolled_back"


def get_state_dir(install_root: Path) -> Path:
    d = install_root / STATE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_current_state(install_root: Path) -> dict[str, Any] | None:
    """读取 state/current.json。"""
    f = install_root / STATE_DIR_NAME / CURRENT_STATE_FILE
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version"):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 current.json 失败：%s", exc)
    return None


def get_current_version(install_root: Path) -> str | None:
    st = read_current_state(install_root)
    return st.get("version") if st else None


def write_current_state(
    install_root: Path,
    version: str,
    last_known_good: str | None = None,
    extra: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """原子更新 state/current.json 指针。

    额外字段（previousVersion / pendingVersion / attemptId …）与指针同一次落盘：
    回退目标必须和「当前指向哪个版本」一起原子生效，否则回退时读到的是旧目标。
    """
    sdir = get_state_dir(install_root)
    target = sdir / CURRENT_STATE_FILE
    tmp = sdir / f"{CURRENT_STATE_FILE}.tmp"

    state: dict[str, Any] = {
        "version": version,
        "installedAt": time.time(),
        "lastKnownGood": last_known_good,
    }
    if extra:
        state.update(extra)
    state.update(fields)

    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return state


def start_transaction(
    install_root: Path,
    target_version: str,
    source_version: str | None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """开启换装事务。"""
    sdir = get_state_dir(install_root)
    aid = attempt_id or str(uuid.uuid4())
    now = time.time()
    tx = {
        "attemptId": aid,
        "stage": STAGE_STAGED,
        "targetVersion": target_version,
        "sourceVersion": source_version,
        "startTime": now,
        "updatedAt": now,
        "error": None,
    }
    target = sdir / TRANSACTION_FILE
    tmp = sdir / f"{TRANSACTION_FILE}.tmp"
    tmp.write_text(json.dumps(tx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return tx


def update_transaction(
    install_root: Path,
    stage: str,
    error: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """更新换装事务状态。"""
    sdir = get_state_dir(install_root)
    target = sdir / TRANSACTION_FILE
    tx = read_transaction(install_root) or {}
    tx["stage"] = stage
    tx["updatedAt"] = time.time()
    if error is not None:
        tx["error"] = error
    tx.update(kwargs)

    tmp = sdir / f"{TRANSACTION_FILE}.tmp"
    tmp.write_text(json.dumps(tx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return tx


def read_transaction(install_root: Path) -> dict[str, Any] | None:
    f = install_root / STATE_DIR_NAME / TRANSACTION_FILE
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def clear_transaction(install_root: Path) -> None:
    f = install_root / STATE_DIR_NAME / TRANSACTION_FILE
    f.unlink(missing_ok=True)


def get_versions_dir(install_root: Path) -> Path:
    d = install_root / VERSIONS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_version_dir(install_root: Path, version: str) -> Path:
    return install_root / VERSIONS_DIR_NAME / version


def is_managed_version_name(name: str) -> bool:
    """版本目录名能否安全用作相对路径（载荷目录名带分隔符时不得跳出 versions/）。"""
    text = str(name or "")
    if not text or text in {".", ".."}:
        return False
    if text != text.strip():
        return False
    return not any(sep in text for sep in ("/", "\\", ":"))


def get_version_exe(install_root: Path, version: str) -> Path:
    return get_version_dir(install_root, version) / f"{APP_NAME}.exe"


def is_version_valid(install_root: Path, version: str) -> bool:
    """验证版本目录及其主可执行文件是否完整存在。"""
    vdir = get_version_dir(install_root, version)
    if not vdir.is_dir():
        return False
    exe = get_version_exe(install_root, version)
    return exe.is_file()


def list_installed_versions(install_root: Path) -> list[str]:
    """获取所有已安装且主程序完好的版本目录列表。"""
    vroot = install_root / VERSIONS_DIR_NAME
    if not vroot.is_dir():
        return []
    res: list[str] = []
    for entry in vroot.iterdir():
        if entry.is_dir() and is_version_valid(install_root, entry.name):
            res.append(entry.name)
    return res


def prune_versions(
    install_root: Path,
    keep_versions: set[str] | list[str] | None = None,
    min_keep: int = 2,
) -> list[str]:
    """保留当前在用版本及上一有效版本，清理更早的历史版本。"""
    all_vers = list_installed_versions(install_root)
    curr = read_current_state(install_root)
    active_ver = curr.get("version") if curr else None
    last_good = curr.get("lastKnownGood") if curr else None

    protected: set[str] = set(keep_versions or [])
    if active_ver:
        protected.add(active_ver)
    if last_good:
        protected.add(last_good)

    # 若保护清单不足 min_keep，按目录修改时间保留最新的几个
    if len(protected) < min_keep and all_vers:
        sorted_by_mtime = sorted(
            all_vers,
            key=lambda v: get_version_dir(install_root, v).stat().st_mtime,
            reverse=True,
        )
        for v in sorted_by_mtime:
            protected.add(v)
            if len(protected) >= min_keep:
                break

    pruned: list[str] = []
    for v in all_vers:
        if v not in protected:
            target = get_version_dir(install_root, v)
            try:
                shutil.rmtree(target, ignore_errors=True)
                if not target.exists():
                    pruned.append(v)
                    logger.info("已清理旧版本目录：%s", v)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理版本目录 %s 失败：%s", v, exc)

    return pruned


def confirm_version(install_root: Path, version: str) -> dict[str, Any]:
    """把 version 正式确认为当前版本：指针回正、清空回退目标与验收中标记。

    验收前已把指针指向目标版本（启动器只认指针），提交时不再重写指针落点，
    只补齐「已验收」这一事实：`pendingVersion` 清空、`lastKnownGood` 指向源版本。
    """
    sdir = get_state_dir(install_root)
    target = sdir / CURRENT_STATE_FILE
    tmp = sdir / f"{CURRENT_STATE_FILE}.tmp"
    state = read_current_state(install_root) or {}
    state.pop("pendingVersion", None)
    state["version"] = version
    state["confirmedAt"] = time.time()
    if not state.get("lastKnownGood"):
        state["lastKnownGood"] = state.get("previousVersion")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return state


def abort_version(install_root: Path, attempted: str) -> bool:
    """撤掉一个安装完成却未通过验收的版本，把指针交还给 lastKnownGood。

    返回 True = 指针已指向可回退版本。调用方必须通过 `is_version_valid`
    确认该版本目录仍可用；无法交还时保持原指针（宁可停在已知损坏的版本上
    让下次启动重新走自愈，也不能回退到一个不存在的版本）。
    """
    state = read_current_state(install_root) or {}
    fallback = state.get("lastKnownGood") or state.get("previousVersion")
    if not fallback or fallback == attempted:
        return False
    if not is_version_valid(install_root, fallback):
        return False
    write_current_state(install_root, fallback, last_known_good=None)
    try:
        shutil.rmtree(get_version_dir(install_root, attempted), ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理未通过验收的版本 %s 失败：%s", attempted, exc)
    return True


def previous_version_entry(install_root: Path) -> Path | None:
    """旧式扁平安装的恢复入口：根目录程序此前被移入的版本目录。"""
    state = read_current_state(install_root) or {}
    name = state.get("previousVersion")
    if not name or not is_managed_version_name(str(name)):
        return None
    return get_version_exe(install_root, str(name))
