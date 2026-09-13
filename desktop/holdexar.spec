# -*- mode: python ; coding: utf-8 -*-
"""Holdexar PyInstaller onedir spec（绿色 zip 发布）。

打包链：scripts/build_release.py（npm build → 本 spec → 产物消毒 → zip）。
datas 布局与 app/core/config.py 的 sys.frozen 分支对齐：
  web/         ← web/dist        （web_dist_dir 命中 _MEIPASS/web）
  seed/        ← assets/seed     （seed_dir 命中 _MEIPASS/seed）
  app.ico      ← _MEIPASS 根     （desktop/main.py frozen icon 分支）
  server/app   ← 后端源码落盘    （uvicorn.run("app.main:app") 字符串导入的
                                  磁盘兜底，main.py frozen 态把它插进 sys.path）
"""
import os
import sys

SPECDIR = SPECPATH  # PyInstaller 注入：本 spec 所在目录（desktop/）
ROOT = os.path.abspath(os.path.join(SPECDIR, ".."))
SERVER = os.path.join(ROOT, "server")

# collect_submodules('app') 在 spec 执行期需要 app 可导入——pathex 只影响
# Analysis 的分析路径，不影响本文件顶层的 import 机制，必须显式注入
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

hiddenimports = [
    # uvicorn.run("app.main:app") 字符串导入 + auto 协议模块运行时选择
    "uvicorn.logging",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "app.main",
    # SQLAlchemy asyncio 方言运行时动态加载 dbapi，静态分析收不到（L1 冒烟实证）
    "aiosqlite",
    "greenlet",
    # pywebview Windows 动态选平台（EdgeChromium / winforms 登录窗 Cookie 通道）
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]
hiddenimports += collect_submodules("app")

a = Analysis(
    ["main.py"],
    pathex=[SERVER],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "web", "dist"), "web"),
        (os.path.join(ROOT, "assets", "seed"), "seed"),
        (os.path.join(SERVER, "app"), os.path.join("server", "app")),
        ("app.ico", "."),
        # 许可与第三方声明随包分发：MIT 的正文要求「副本或实质部分中须包含版权
        # 声明与许可」，包内那些 dist-info 只覆盖各自的依赖，本项目自己的 LICENSE
        # 与聚合声明此前一个都没进包——等于发出去一份没有许可的软件。
        (os.path.join(ROOT, "LICENSE"), "."),
        (os.path.join(ROOT, "docs", "THIRD_PARTY_NOTICES.md"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
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
)

coll = COLLECT(exe, a.binaries, a.datas, name="Holdexar")
