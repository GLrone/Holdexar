# -*- mode: python ; coding: utf-8 -*-
"""Holdexar PyInstaller onedir spec（绿色 zip 发布）。

打包链：scripts/build_release.py（npm build → 本 spec → 产物消毒 → zip）。
datas 布局与 app/core/config.py 的 sys.frozen 分支对齐：
  web/         ← web/dist        （web_dist_dir 命中 _MEIPASS/web）
  clash/       ← assets/clash    （clash_dir 命中 _MEIPASS/clash；mihomo 内核 +
                                  GeoIP 数据 + 上游许可原文，随包分发）
  app.ico      ← _MEIPASS 根     （desktop/main.py frozen icon 分支）
  server/app   ← 后端源码落盘    （uvicorn.run("app.main:app") 字符串导入的
                                  磁盘兜底，main.py frozen 态把它插进 sys.path）
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
from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

from app.core.app_info import APP_NAME, APP_SUBTITLE, APP_VERSION  # noqa: E402
from app.core.optional_deps import PYINSTALLER_EXCLUDES  # noqa: E402

hiddenimports = [
    # uvicorn.run("app.main:app") 字符串导入 + auto 协议模块运行时选择
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


def numeric_version(text):
    """语义化版本 → PE 要求的四段数字（0.3.0-beta → (0, 3, 0, 0)）。"""
    parts = [int(n) for n in re.findall(r"\d+", text)[:4]]
    return tuple((parts + [0, 0, 0, 0])[:4])


# 文件版本取 PE 四段数字（属性页按数字版本读），产品版本保留语义化原文
file_version = ".".join(str(n) for n in numeric_version(APP_VERSION))

# exe 的 VERSIONINFO 资源：字段值取 APP_VERSION 单一来源，发版无需另改。
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=numeric_version(APP_VERSION),
        prodvers=numeric_version(APP_VERSION),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", APP_NAME),
                StringStruct("FileDescription", f"{APP_NAME} — {APP_SUBTITLE}"),
                StringStruct("FileVersion", file_version),
                StringStruct("InternalName", APP_NAME),
                StringStruct("LegalCopyright",
                             f"Copyright (c) {APP_NAME} contributors. Licensed under GPL-3.0."),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", APP_VERSION),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    ["main.py"],
    pathex=[SERVER],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "web", "dist"), "web"),
        # 种子不随包（按需下载落 data/seed，见 core/seed_fetch.py）；
        # 内核随包分发：mihomo 与 GeoIP 数据齐备是发布前提，由
        # build_release.ensure_clash_assets() 在打包前校验（缺一即中止）
        (os.path.join(ROOT, "assets", "clash"), "clash"),
        (os.path.join(SERVER, "app"), os.path.join("server", "app")),
        ("app.ico", "."),
        # 许可与第三方声明随包分发：GPL-3.0 要求实体副本随附许可原文（§4），
        # 包内那些 dist-info 只覆盖各自的依赖，本项目自己的 LICENSE 与聚合
        # 声明必须单独入包，缺了等于发出去一份没有许可的软件。
        (os.path.join(ROOT, "LICENSE"), "."),
        (os.path.join(ROOT, "docs", "THIRD_PARTY_NOTICES.md"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter 无引用；其余为依赖链拖入的零引用库（名单与运行时兜底同源，
    # 见 app/core/optional_deps.py）
    excludes=["tkinter", *PYINSTALLER_EXCLUDES],
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
