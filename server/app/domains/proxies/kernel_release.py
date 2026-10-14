"""内核版本、下载地址与随包资产清单，仅依赖标准库。"""
from __future__ import annotations

import platform

# mihomo 版本：随包内核与网络下载共用；改版本只改这里。
MIHOMO_VERSION = "v1.19.32"

# 通道链：国内直连 GitHub 发布资产基本不可用，镜像优先、直连兜底。
# 与 app/core/updater.py、scripts/fetch_seed.py 的镜像链同源，改动请三处对齐。
MIRRORS: tuple[str, ...] = ("https://ghfast.top/", "https://gh-proxy.com/", "")

_GITHUB = "https://github.com"
MIHOMO_REPO = f"{_GITHUB}/MetaCubeX/mihomo"

# 随包许可原文（上游逐字，不做改写）：GPL-3.0 要求二进制旁附许可文本。
LICENSE_ASSETS: dict[str, str] = {
    "LICENSE-mihomo.txt": f"{MIHOMO_REPO}/raw/{MIHOMO_VERSION}/LICENSE",
}


def is_windows() -> bool:
    return platform.system() == "Windows"


def kernel_filename() -> str:
    """内核可执行文件名（Windows 带 .exe）。"""
    return "mihomo.exe" if is_windows() else "mihomo"


def bundled_asset_names() -> tuple[str, ...]:
    """获取、构建与打包共用的随包资产清单。"""
    return (kernel_filename(), *LICENSE_ASSETS, "README.txt")


def mihomo_asset() -> tuple[str, str]:
    """当前平台的 mihomo 发布资产：返回 (资产名, 打包形状)。

    形状：Windows = zip（内含 exe）；Linux = gz 单文件。
    """
    if is_windows():
        return f"mihomo-windows-amd64-compatible-{MIHOMO_VERSION}.zip", "zip"
    return f"mihomo-linux-amd64-compatible-{MIHOMO_VERSION}.gz", "gz"


def mihomo_url() -> str:
    asset, _ = mihomo_asset()
    return f"{MIHOMO_REPO}/releases/download/{MIHOMO_VERSION}/{asset}"


