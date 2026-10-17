"""Holdexar 稳定启动器（Launcher）核心逻辑。

职责：
1. 确定统一安装根目录与基础目录结构；
2. 首次启动检查并触发老版本 AppData 数据安全迁移；
3. 读取 state/current.json 获取目标版本；
4. 验证目标版本完整性（损坏则自动尝试 lastKnownGood 或最近有效版本回退）；
5. 携带安装根目录与隔离数据目录上下文，拉起目标版本并安全退出。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from app.core.app_info import ENV_PREFIX
from app.core.layout import (
    get_state_dir,
    get_version_exe,
    is_version_valid,
    list_installed_versions,
    read_current_state,
    write_current_state,
)
from app.core.migration import detect_legacy_data_locations, migrate_appdata_to_portable
from app.core.paths import install_root

logger = logging.getLogger(__name__)


def find_target_version(root: Path) -> str | None:
    """定位当前应启动的目标版本，必要时自动执行自愈回滚。"""
    curr = read_current_state(root)
    if curr:
        active = curr.get("version")
        if active and is_version_valid(root, active):
            return active

        # 目标版本损坏或丢失，尝试上一有效版本
        last_good = curr.get("lastKnownGood")
        if last_good and is_version_valid(root, last_good):
            logger.warning(
                "当前版本 %s 损坏，自动回退到上一有效版本 %s", active, last_good
            )
            write_current_state(root, last_good, last_known_good=None)
            return last_good

    # 状态丢失或指定版本均不可用：扫描 versions/ 中最新有效版本
    installed = list_installed_versions(root)
    if not installed:
        return None

    # 按目录修改时间选取最新安装的有效版本
    best = max(
        installed,
        key=lambda v: (root / "versions" / v).stat().st_mtime,
    )
    write_current_state(root, best)
    return best


def launch_version(
    root: Path,
    target_version: str,
    argv: list[str] | None = None,
    detached: bool = True,
) -> subprocess.Popen:
    """拉起目标版本的 Holdexar 主程序。"""
    exe = get_version_exe(root, target_version)
    if not exe.is_file():
        raise FileNotFoundError(f"版本主程序不存在：{exe}")

    child_env = dict(os.environ)
    child_env[f"{ENV_PREFIX}INSTALL_ROOT"] = str(root)
    child_env[f"{ENV_PREFIX}DATA_DIR"] = str(root / "data")
    child_env["WEBVIEW2_USER_DATA_FOLDER"] = str(root / "data" / "cache" / "webview")

    flags = 0
    if detached and os.name == "nt":
        detached_flag = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags = detached_flag | new_group_flag

    args = [str(exe)]
    if argv is not None:
        args.extend(argv)
    elif len(sys.argv) > 1:
        args.extend(sys.argv[1:])

    return subprocess.Popen(
        args,
        cwd=str(exe.parent),
        env=child_env,
        close_fds=True,
        creationflags=flags,
    )


def run_launcher(
    argv: list[str] | None = None,
    install_root_override: Path | None = None,
    detached: bool = True,
) -> int:
    """启动器主流程。"""
    root = install_root_override or install_root()

    # 1. 确保基础目录结构齐全
    get_state_dir(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "versions").mkdir(parents=True, exist_ok=True)
    (root / "updater").mkdir(parents=True, exist_ok=True)
    (root / "update-staging").mkdir(parents=True, exist_ok=True)

    # 2. 检查老版本 AppData 数据并只读平滑迁移
    try:
        locs = detect_legacy_data_locations(root)
        if locs.get("hasAppdataDb") and not locs.get("hasTargetDb"):
            appdata_dir = locs.get("appdataDataDir")
            if appdata_dir:
                migrate_appdata_to_portable(appdata_dir, root / "data")
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动器检查历史迁移异常：%s", exc)

    # 3. 定位有效版本
    target_ver = find_target_version(root)
    if not target_ver:
        err_msg = f"未能在 {root / 'versions'} 找到任何可运行的有效版本。\n请检查安装目录或重新解压完整发行包。"
        print(f"[错误] {err_msg}", file=sys.stderr)
        try:
            if os.name == "nt":
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, err_msg, "Holdexar 启动失败", 0x10)
        except Exception:  # noqa: BLE001
            pass
        return 1

    # 4. 拉起目标版本
    try:
        launch_version(root, target_ver, argv=argv, detached=detached)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("拉起版本 %s 失败：%s", target_ver, exc)
        return 1
