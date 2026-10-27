# -*- mode: python ; coding: utf-8 -*-
"""Holdexar PyInstaller onedir spec（绿色 zip 发布）。

打包链：scripts/build_release.py（npm build → 本 spec → 产物消毒 → zip）。
datas 布局与 app/core/config.py 的 sys.frozen 分支对齐：
  web/         ← web/dist        （web_dist_dir 命中 _MEIPASS/web）
  clash/       ← assets/clash    （仅许可原文与来源说明）
  app.ico      ← _MEIPASS 根     （desktop/main.py frozen icon 分支）
  后端模块由 PYZ 归档加载，主壳直接传入 app 对象，不另附 server/app 源码。
"""
import os
import re
import sys

SPECDIR = SPECPATH  # PyInstaller 注入：本 spec 所在目录（desktop/）
ROOT = os.path.abspath(os.path.join(SPECDIR, ".."))
SERVER = os.path.join(ROOT, "server")

# collect_submodules('app') 在 spec 执行期需要 app 可导入——pathex 只影响
# Analysis 的分析路径，不影响本文件顶层的 import 机制，必须显式注入
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from PyInstaller.utils.hooks import collect_submodules  # noqa: E402
from app.core.app_info import APP_NAME, APP_VERSION  # noqa: E402

from app.domains.proxies.kernel_release import release_asset_names  # noqa: E402


hiddenimports = [
    # uvicorn 协议与生命周期模块运行时选择
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "app.main",
    # SQLAlchemy asyncio 方言运行时动态加载 dbapi，静态分析收不到
    "aiosqlite",
    "greenlet",
    # 凭据静态加密（secretbox：AES-256-GCM；rust 后端随内置 hook 收集，
    # 显式登记防漏）
    "cryptography",
    # pywebview Windows 动态选平台（EdgeChromium / winforms 登录窗 Cookie 通道）
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]
hiddenimports += collect_submodules("app")


if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from desktop.version_resource import get_vs_version_info  # noqa: E402

version_info = get_vs_version_info("desktop")


a = Analysis(
    ["main.py"],
    pathex=[SERVER],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "web", "dist"), "web"),
        *[
            (os.path.join(ROOT, "assets", "clash", name), "clash")
            for name in release_asset_names()
            if os.path.exists(os.path.join(ROOT, "assets", "clash", name))
        ],

        ("app.ico", "."),
        # 许可与第三方声明随包分发：GPL-3.0 要求实体副本随附许可原文（§4），
        # 包内那些 dist-info 只覆盖各自的依赖，本项目自己的 LICENSE 与聚合
        # 声明必须单独入包，缺了等于发出去一份没有许可的软件。
        (os.path.join(ROOT, "LICENSE"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "PIL", "asyncpg", "openpyxl", "packaging", "pygments", "setuptools", "watchfiles",
        "boto3", "botocore", "s3transfer", "jmespath", "requests", "urllib3", "alembic", "mako",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Holdexar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩显著提高杀软误报率，公开分发禁用
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["app.ico"],
    version=version_info,
)

coll = COLLECT(exe, a.binaries, a.datas, name="Holdexar")
