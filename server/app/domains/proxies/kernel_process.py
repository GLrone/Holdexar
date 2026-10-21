"""内核子进程的唯一执行边界：受控环境、预检错误分类、统一的启动与一次性调用入口。"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

_OVERRIDE_PREFIXES = ("CLASH_", "MIHOMO_")
_OVERRIDE_NAMES = frozenset({
    "SAFE_PATHS", "DISABLE_LOOPBACK_DETECTOR", "SKIP_SYSTEM_IPV6_CHECK",
})

PRECHECK_ADMISSION = "admission"
PRECHECK_TIMEOUT = "timeout"
PRECHECK_REJECTED = "rejected"
PRECHECK_UNLAUNCHABLE = "unlaunchable"
PRECHECK_CHANGED = "changed"


class KernelPrecheckError(ValueError):
    """内核启动前的拒绝；`kind` 区分准入拒绝、内核拒载、超时、无法执行与文件被替换。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def kernel_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """内核子进程环境：去掉能覆盖配置、改写运行行为的变量，其余原样保留。"""
    source = os.environ if base is None else base
    return {
        key: value for key, value in source.items()
        if not (
            key.upper().startswith(_OVERRIDE_PREFIXES)
            or key.upper() in _OVERRIDE_NAMES
        )
    }


def run_kernel_cli(
    exe_path: str | Path, args: Sequence[str], *, timeout: float,
) -> subprocess.CompletedProcess:
    """跑一次性内核命令（版本探测、配置预检）并捕获输出。"""
    return subprocess.run(
        [str(exe_path), *args], capture_output=True, timeout=timeout,
        encoding="utf-8", errors="replace", env=kernel_env(),
    )


def spawn_kernel(
    exe_path: str | Path, args: Sequence[str], *, log_file: IO[bytes] | None = None,
) -> subprocess.Popen:
    """拉起长驻内核进程；输出落 `log_file`（可空）。"""
    return subprocess.Popen(
        [str(exe_path), *args],
        stdout=log_file,
        stderr=subprocess.STDOUT if log_file is not None else None,
        env=kernel_env(),
    )


def confirm_config_text(path: str | Path, expected: str) -> None:
    """启动前确认磁盘上的配置就是通过预检的那份文本。"""
    try:
        actual = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise KernelPrecheckError(
            PRECHECK_CHANGED, f"启动前读不回运行配置：{type(exc).__name__}",
        ) from None
    if actual != expected:
        raise KernelPrecheckError(
            PRECHECK_CHANGED, "运行配置在预检之后被改动，拒绝启动",
        )
