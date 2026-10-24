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
    """获取与本地资产补齐共用的资产清单。"""
    return (kernel_filename(), *LICENSE_ASSETS, "README.txt")


def release_asset_names() -> tuple[str, ...]:
    """发布包随包的轻量资产（仅许可与说明，内核二进制按需独立下载维护）。"""
    return (*LICENSE_ASSETS, "README.txt")



def bundled_source_note() -> str:
    """获取和构建共用的代理内核来源与许可说明。"""
    return f"""Holdexar 代理内核资产与许可说明（按需组件）
==================================================
mihomo         {MIHOMO_VERSION}
               来源 {MIHOMO_REPO}
               许可 GPL-3.0（原文见同目录 LICENSE-mihomo.txt）
mihomo 以独立进程运行（非链接、非派生），本项目自身以 GPL-3.0 分发；
内核二进制不随安装包内置，由用户按需下载或自备维护；
对应源码即上游仓库上述地址，所用二进制未做任何修改。
"""


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


