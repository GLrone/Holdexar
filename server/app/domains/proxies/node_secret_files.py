"""节点认证明文的落盘约束：权限收紧 + 生命周期收敛。

内核运行时必须读到节点认证明文（trojan 口令、vmess uuid……），因此这些
文件**不能加密**——目标是「静态存储加密」（SQLite 两列）之外的补充：
一旦明文落地，就把它限制在最小可读面，并在不再需要时尽快消失。

三条约束：

- **权限**：POSIX 下 0600；Windows 走用户目录 ACL（data 目录默认即
  用户级，不再额外放开）。
- **生命周期**：运行配置与原始订阅缓存在内核停止后清理；进程正常退出
  时统一清扫。崩溃留下的残留由下次启动清理。
- **位置**：所有明文产物只在数据目录内的固定子目录，不散落到临时目录。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from app.core.logging import log_event

logger = logging.getLogger(__name__)

# 承载节点认证明文的产物（相对数据目录）
SECRET_BEARING = ("clash", "proxypool")
# 允许保留的后缀：订阅原始缓存是来源证据，重建需重新下载，故保留但收紧权限；
# 运行配置可从池文件重建，随时可删。
_TRANSIENT_SUFFIXES = (".runtime.yaml", ".tmp")


def tighten(path: Path) -> None:
    """把单个文件收紧为仅当前用户可读（POSIX 0600；Windows 依赖目录 ACL）。"""
    if sys.platform == "win32":
        return
    try:
        if path.is_file():
            os.chmod(path, 0o600)
    except OSError:
        pass


def tighten_dir(path: Path) -> None:
    """收紧目录：仅属主可进出（POSIX 0700）。"""
    if sys.platform == "win32":
        return
    try:
        if path.is_dir():
            os.chmod(path, 0o700)
    except OSError:
        pass


def tighten_all(data_dir: Path) -> int:
    """把数据目录内所有承载节点凭据的产物收紧权限，返回处理文件数。"""
    root = Path(data_dir)
    count = 0
    for name in SECRET_BEARING:
        base = root / name
        if not base.is_dir():
            continue
        tighten_dir(base)
        for item in base.rglob("*"):
            if item.is_file():
                tighten(item)
                count += 1
    return count


def sweep_transient(data_dir: Path) -> int:
    """删除可重建的明文物（运行配置与临时文件），返回删除数。

    调用时机：内核停止后、进程正常退出、下次启动。
    """
    root = Path(data_dir)
    removed = 0
    for name in SECRET_BEARING:
        base = root / name
        if not base.is_dir():
            continue
        for item in base.rglob("*"):
            if not item.is_file():
                continue
            if any(item.name.endswith(suffix) for suffix in _TRANSIENT_SUFFIXES):
                try:
                    item.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        log_event(
            logger,
            f"已清理 {removed} 个可重建的节点配置明文件",
            tag="清理",
            detail={"条数": removed},
        )
    return removed


__all__ = ["SECRET_BEARING", "sweep_transient", "tighten", "tighten_all", "tighten_dir"]
