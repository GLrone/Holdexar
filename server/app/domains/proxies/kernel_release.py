"""内核资产的版本与下载地址常量（单一来源）。

**为什么独立成纯标准库模块**：同一组常量有两个消费者，且它们的依赖前提不同——
`clash_manager` 跑在服务进程里（依赖齐全），而 `scripts/fetch_kernel.py` 在
`run.py` 首次启动的补资产流程里被调用（此时只保证解释器可用）。常量放这里，
两边 import 都不会顺带拉起 httpx 等第三方依赖。

**版本号只有一个落点**：随包内核（发行包内置）与网络下载（兜底）共用
`MIHOMO_VERSION`，改版本只改这一行——两处各写一份必然漂移成「包里是 A、
下载装的是 B」。
"""
from __future__ import annotations

import platform

# mihomo 版本：随包内核与网络下载共用；改版本只改这里。
MIHOMO_VERSION = "v1.19.31"

# 通道链：国内直连 GitHub 发布资产基本不可用，镜像优先、直连兜底。
# 与 app/core/updater.py、scripts/fetch_seed.py 的镜像链同源，改动请三处对齐。
MIRRORS: tuple[str, ...] = ("https://ghfast.top/", "https://gh-proxy.com/", "")

_GITHUB = "https://github.com"
MIHOMO_REPO = f"{_GITHUB}/MetaCubeX/mihomo"
RULES_DAT_REPO = f"{_GITHUB}/MetaCubeX/meta-rules-dat"

# 内核数据目录必需的 GeoIP / GeoSite 数据：缺失时内核会自行去 GitHub 下载，
# 直连网络下必卡死。键 = 落盘文件名（mihomo 按此名读取），值 = 上游资产名。
# 全部来自 meta-rules-dat 的滚动 release（tag 固定为 latest，文件名固定）。
GEO_ASSETS: dict[str, str] = {
    "Country.mmdb": "Country.mmdb",
    "geoip.dat": "geoip.dat",
    "geosite.dat": "geosite.dat",
    "ASN.mmdb": "GeoLite2-ASN.mmdb",
}

# 随包许可原文（上游逐字，不做改写）：GPL-3.0 要求二进制旁附许可文本。
LICENSE_ASSETS: dict[str, str] = {
    "LICENSE-mihomo.txt": f"{MIHOMO_REPO}/raw/{MIHOMO_VERSION}/LICENSE",
    "LICENSE-meta-rules-dat.txt": f"{RULES_DAT_REPO}/raw/master/LICENSE",
}


def is_windows() -> bool:
    return platform.system() == "Windows"


def kernel_filename() -> str:
    """内核可执行文件名（Windows 带 .exe）。"""
    return "mihomo.exe" if is_windows() else "mihomo"


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


def geo_url(asset_name: str) -> str:
    return f"{RULES_DAT_REPO}/releases/download/latest/{asset_name}"
