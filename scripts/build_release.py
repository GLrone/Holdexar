r"""Holdexar 发布构建：npm build → Go Launcher + PyInstaller 应用/更新宿主 → 消毒 → zip。

用法（发布机，建议用 server/.venv 的解释器运行）：
    Go 工具链版本须满足 desktop/go_launcher/go.mod，依赖按 go.sum 锁定。
    python scripts/build_release.py                    # 常规出包
    python scripts/build_release.py --skip-web         # 跳过前端构建（dist 已是最新）
    python scripts/build_release.py --output-dir <目录> # 隔离出包，不覆盖 release/
产物（全部要上传 Release；清单由 scripts/build_manifest.py 生成）：
- release/Holdexar-win64-v<版本>.zip —— 应用包，解压双击即用。轻量包模式：
  **随包附带 mihomo 许可原文与来源说明**（assets/clash → _MEIPASS/clash），
  **不捆绑内核二进制**（按需独立维护或自备），**不附全量价格数据库**（可携带目录与人工资料小种子）
  （游戏目录与价格历史由客户端采集逐步建立）。
  **用户数据支持完全便携**：打包态优先落在程序目录 `data/` 下（完全便携同盘携带），
  目录不可写时中止启动，已登记的数据目录保持原有归属。
- release/latest.json —— 更新清单，挂到固定 tag `updater` 供客户端检查更新。
  加 Scoop 渠道时另出 release/scoop/（用法见 packaging/scoop/README.md）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
WEB = ROOT / "web"
DESKTOP = ROOT / "desktop"
CLASH_DIR = ROOT / "assets" / "clash"
# 小种子（目录/人工列/捆绑主档，不含价格）：随包携带的离线豁免资产
MINI_SEED_SRC = ROOT / "assets" / "seed" / "holdexar_mini_seed.db"
MINI_SEED_BASENAME = "holdexar_mini_seed.db"
RELEASE = ROOT / "release"
DIST_DIR = RELEASE / "build"  # PyInstaller distpath（COLLECT 输出 DIST_DIR/<APP_NAME>/）
WORK_DIR = RELEASE / "work"
VENV_PY = SERVER / ".venv" / "Scripts" / "python.exe"

# 品牌常量与应用版本同源（app.core.app_info 是纯标准库模块，可直接导入；
# kernel_release 同为纯标准库，故可在装依赖之前取到内核资产清单）
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.app_info import APP_NAME, APP_VERSION  # noqa: E402
from app.domains.proxies.kernel_release import (  # noqa: E402
    bundled_source_note,
    kernel_filename,
    release_asset_names,
)
from desktop.version_resource import verify_exe_version_resources, write_version_file  # noqa: E402



APP_DIR = DIST_DIR / APP_NAME

AUX_EXCLUDES = [
    "fastapi", "uvicorn", "starlette", "pydantic", "pydantic_settings",
    "pydantic_core", "sqlalchemy", "aiosqlite", "greenlet", "httpx",
    "httpcore", "h11", "anyio", "sniffio", "PIL", "Pillow",
    "cryptography", "botocore", "asyncpg", "webview", "pywebview",
    "tkinter", "setuptools", "distutils", "pkg_resources", "packaging",
    "watchfiles", "pygments", "openpyxl", "apscheduler",
    "app.crawler", "app.domains", "app.main",
]


def pick_python() -> Path:
    if VENV_PY.is_file():
        return VENV_PY
    return Path(sys.executable)


def ensure_clash_assets() -> None:
    """随包许可原文与来源说明就位（内核二进制按需独立维护，不随应用包分发）。"""
    CLASH_DIR.mkdir(parents=True, exist_ok=True)
    (CLASH_DIR / "README.txt").write_text(bundled_source_note(), encoding="utf-8")
    # 优先从仓库受版本控制的 packaging/licenses/ 复制许可原文
    local_lic = ROOT / "packaging" / "licenses" / "LICENSE-mihomo.txt"
    dest_lic = CLASH_DIR / "LICENSE-mihomo.txt"
    if local_lic.is_file() and not dest_lic.is_file():
        shutil.copy2(local_lic, dest_lic)

    expected = release_asset_names()
    missing = [name for name in expected if not (CLASH_DIR / name).is_file()]
    if missing:
        # 尝试调用 fetch_kernel 仅下载随包许可文本
        try:
            import scripts.fetch_kernel as fk
            fk.fetch(license_only=True)
            missing = [name for name in expected if not (CLASH_DIR / name).is_file()]
        except Exception:
            pass
    if missing:
        sys.exit(
            f"[错误] 内核许可与说明资产缺失：{', '.join(missing)}\n"
            "       先跑 python scripts/fetch_kernel.py --license-only 补齐 assets/clash/ 后再出包。"
        )
    print(f"[内核] 轻量包模式：不捆绑 {kernel_filename()} 二进制（节省约 35MB），运行时按需下载独立维护")



def build_web() -> None:
    print("[前端] npm run build（含 lint 门禁 + 路由完整性）...")
    subprocess.check_call(["npm", "run", "build"], cwd=str(WEB), shell=True)


def ensure_pyinstaller(python: Path) -> None:
    probe = subprocess.run([str(python), "-m", "PyInstaller", "--version"], capture_output=True)
    if probe.returncode == 0:
        return
    print("[依赖] 安装 PyInstaller（构建机依赖，不进 requirements.txt）...")
    subprocess.check_call([str(python), "-m", "pip", "install", "pyinstaller"])


def run_pyinstaller(python: Path) -> None:
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    print("[打包] PyInstaller onedir ...")
    subprocess.check_call([
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(DIST_DIR), "--workpath", str(WORK_DIR),
        str(DESKTOP / "holdexar.spec"),
    ])


def build_go_launcher(version: str, python: Path) -> Path:
    """编译唯一生产启动器，并复用发行资源生成与校验链。"""
    go = shutil.which("go")
    if not go:
        raise RuntimeError("构建 Go Launcher 需要安装与 desktop/go_launcher/go.mod 匹配的 Go 工具链")
    destination = DIST_DIR / "_launcher_build" / f"{APP_NAME}.exe"
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "GOOS": "windows", "GOARCH": "amd64",
                   "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOWORK": "off"}
    subprocess.check_call([
        go, "build", "-mod=readonly", "-trimpath", "-ldflags=-s -w -H=windowsgui",
        "-o", str(destination), ".",
    ], cwd=str(DESKTOP / "go_launcher"), env=environment)
    subprocess.check_call([
        str(python), str(DESKTOP / "version_resource.py"),
        "--stamp-launcher", str(destination), "--version", version,
    ], cwd=str(ROOT))
    return destination


def assemble_versioned_layout(version: str, python: Path) -> None:
    """将 PyInstaller onedir 产物组装为新架构布局：
    Holdexar/
    ├── Holdexar.exe (Launcher)
    ├── updater/UpdateHost.exe
    ├── versions/<version>/ (完整独立版本运行文件)
    └── state/current.json
    """
    print(f"[布局] 组装多版本安装布局（版本 {version}）...")
    temp_app = DIST_DIR / "_app_temp"
    if temp_app.exists():
        shutil.rmtree(temp_app)
    shutil.move(str(APP_DIR), str(temp_app))

    print("[启动器] 编译 Go Holdexar.exe (Launcher)...")
    launcher_exe = build_go_launcher(version, python)
    aux_exclude_flags = [arg for exc in AUX_EXCLUDES for arg in ("--exclude-module", exc)]

    # 编译独立更新宿主 UpdateHost.exe
    print("[更新宿主] 编译 UpdateHost.exe...")
    updater_dist = DIST_DIR / "_updater_build"
    updater_ver_file = WORK_DIR / "updater_version_info.txt"
    write_version_file(updater_ver_file, role="updater", version=version)
    subprocess.check_call([
        str(python), "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean",
        "--paths", str(SERVER),
        "--icon", str(DESKTOP / "app.ico"),
        "--version-file", str(updater_ver_file),
        "--distpath", str(updater_dist),
        "--workpath", str(WORK_DIR / "updater"),
        "--specpath", str(WORK_DIR),
        "--name", "UpdateHost",
        *aux_exclude_flags,
        str(DESKTOP / "update_host_main.py"),
    ])

    # 重建 APP_DIR 并组装布局
    APP_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 移动完整应用运行文件至 versions/<version>/
    v_dir = APP_DIR / "versions" / version
    v_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_app), str(v_dir))

    # 2. 根目录放置轻量启动器 Holdexar.exe
    shutil.copy2(launcher_exe, APP_DIR / f"{APP_NAME}.exe")

    # 3. updater 目录放置独立 UpdateHost.exe
    updater_dir = APP_DIR / "updater"
    updater_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(updater_dist / "UpdateHost.exe", updater_dir / "UpdateHost.exe")

    # 4. 版本目录携带小种子（目录/人工列/捆绑主档离线资产；消毒按严格名放行）
    if MINI_SEED_SRC.is_file():
        seed_dir = v_dir / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MINI_SEED_SRC, seed_dir / MINI_SEED_BASENAME)
        print(f"[布局] 小种子随包：{MINI_SEED_BASENAME}"
              f"（{MINI_SEED_SRC.stat().st_size / 1048576:.1f} MB）")
    else:
        print("[提示] 未找到小种子（assets/seed/holdexar_mini_seed.db），"
              "本轮包不含离线目录资产")

    # 5. state/current.json 版本指针
    state_dir = APP_DIR / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    current_state = {
        "version": version,
        "installedAt": 0,
        "lastKnownGood": None,
    }
    (state_dir / "current.json").write_text(
        json.dumps(current_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5. 校验最终发行包内三个 EXE 的 Windows 版本属性
    print("[校验] 检查发行包内三个 EXE 的 Windows 版本属性...")
    pe_info = verify_exe_version_resources(APP_DIR, version)
    for role, props in pe_info.items():
        print(f"  ✓ [{role}] {props.get('FileDescription')} | FileVersion={props.get('FileVersion')} | ProductVersion={props.get('ProductVersion')}")

    # 清理构建临时产物
    shutil.rmtree(launcher_exe.parent, ignore_errors=True)
    shutil.rmtree(updater_dist, ignore_errors=True)
    print(f"[布局] 多版本结构组装完成：{APP_DIR}")



def sanitize() -> None:
    """产物消毒：任何用户数据/日志/db 与内核二进制残留即删除并复检，仍有则中止发布。

    产物 .db 只放行小种子（严格文件名），其余都是残留。
    内核二进制独立维护（按需下载），产物里出现任何 mihomo 可执行文件均清理。
    """
    assert APP_DIR.is_dir(), f"打包产物缺失：{APP_DIR}"
    # 清理所有 __pycache__ 与 .pyc 残留
    for p in list(APP_DIR.rglob("__pycache__")):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    for p in list(APP_DIR.rglob("*.pyc")):
        p.unlink(missing_ok=True)
    residue: list[Path] = []
    for path in APP_DIR.rglob("*"):
        if path.is_dir() and path.name == "data":
            residue.append(path)
        elif path.is_file() and (
            (
                path.suffix.lower() in (".log", ".db", ".sqlite3", ".db-wal", ".db-shm")
                and path.name != MINI_SEED_BASENAME
            )
            or path.name.lower() in ("mihomo.exe", "mihomo")
        ):
            residue.append(path)
    for path in residue:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    residue = [
        p for p in APP_DIR.rglob("*")
        if (p.is_dir() and p.name == "data")
        or (
            p.is_file()
            and (
                (
                    p.suffix.lower() in (".log", ".db", ".sqlite3", ".db-wal", ".db-shm")
                    and p.name != MINI_SEED_BASENAME
                )
                or p.name.lower() in ("mihomo.exe", "mihomo")
            )
        )
    ]
    if residue:
        sys.exit(f"[错误] 产物消毒复检仍有残留，中止发布：{residue}")
    print(f"[消毒] 产物干净（{sum(p.is_file() for p in APP_DIR.rglob('*'))} 个文件，无 data/日志/db/内核二进制 残留）")



def make_zip(version: str) -> tuple[Path, str]:
    """压缩 onedir → release/Holdexar-win64-v<版本>.zip（包内根目录 Holdexar/）。

    资产名不带时间戳：Scoop / winget 的自动跟进（checkver + autoupdate）按
    版本号直接拼 URL，同版本重复打包就地覆盖。返回 (zip 路径, sha256)——
    校验值由 scripts/build_manifest.py 收进 latest.json 后公布。
    """
    RELEASE.mkdir(parents=True, exist_ok=True)
    zip_path = RELEASE / f"{APP_NAME}-win64-v{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"[压缩] {zip_path.name} ...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DIST_DIR).as_posix())
    size = zip_path.stat().st_size
    h = hashlib.sha256()
    with zip_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    print(f"[完成] {zip_path}")
    print(f"[完成] 体积 {size / 1048576:.1f} MB  sha256 {h.hexdigest()}")
    return zip_path, h.hexdigest()


def write_release_body(version: str) -> Path:
    """Release 说明模板：release/RELEASE_NOTES.md —— **人工编辑的唯一入口**。

    发布前在这里写 changelog。两个下游都读它：
    - scripts/build_manifest.py 抽正文进 latest.json 的 notes（客户端直接展示）
    - scripts/publish_release.py 拿它当 Release body
    已存在则不覆盖（保留上一轮写的说明；改写请直接编辑该文件）。
    """
    RELEASE.mkdir(parents=True, exist_ok=True)
    body_file = RELEASE / "RELEASE_NOTES.md"
    if body_file.is_file():
        print(f"[发布] 说明沿用现有 {body_file.name}（发布前如需改写请编辑该文件）")
        return body_file
    body_file.write_text(
        f"# Holdexar v{version}\n\n"
        "> 建议直接使用应用内「检查更新」升级；全新用户下载本页压缩包解压双击即用。\n"
        "> 用户数据默认保存在程序根目录 `data/` 下（完全便携）。\n\n"
        "Windows x64 便携版，解压双击即用。游戏目录与价格由应用采集逐步建立。\n\n"
        "## 本次更新\n\n"
        "### 新增功能 (New Features)\n"
        "- （在此写本次更新内容。发布前编辑本文件——客户端「检查更新」会直接展示这段文字。\n"
        "条目格式「模块/页面 —— 做了什么 + 用户得到什么」；只写用户可见面，用产品语言，不出现代码名。\n"
        "写完删掉本括号。）\n\n"
        "### 问题修复 (Bug Fixes)\n\n\n"
        "### 体验优化 (Improvements)\n\n\n"
        "## 安装与更新\n\n"
        "**方式一 · 应用内更新（推荐）**\n"
        "- 打开应用，点击左下角更新气泡或设置页中的「检查更新」，按提示一键下载并重启完成升级。\n\n"
        "**方式二 · 便携包升级**\n"
        f"1. 下载下方资产中的 `Holdexar-win64-v{version}.zip`。\n"
        "2. 解压到新目录，并将原程序目录中的 `data/` 文件夹复制到新目录中。\n"
        "3. 双击新目录下的 `Holdexar.exe` 启动即可（历史 AppData 数据会在首次启动时自动安全迁移）。\n\n"
        "## 资产说明\n\n"
        "| 资产 | 用途 |\n"
        "|---|---|\n"
        f"| `Holdexar-win64-v{version}.zip` | 应用包。解压后双击 `Holdexar.exe` 即可运行 |\n\n"
        "## 已知问题\n\n"
        "- 暂无阻断性已知问题；如遇异常可前往「运行日志」页面导出日志并提交 Issue。\n",
        encoding="utf-8",
    )
    print(f"[发布] 说明模板已生成：{body_file}（发布前请编辑）")
    return body_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 一键发布构建")
    parser.add_argument("--skip-web", action="store_true", help="跳过前端构建（web/dist 已最新）")
    parser.add_argument("--output-dir", default=None, help="隔离构建输出目录（不覆盖现有 release/）")
    args = parser.parse_args()

    global RELEASE, DIST_DIR, WORK_DIR, APP_DIR
    if args.output_dir:
        RELEASE = Path(args.output_dir).resolve()
        DIST_DIR = RELEASE / "build"
        WORK_DIR = RELEASE / "work"
        APP_DIR = DIST_DIR / APP_NAME

    ensure_clash_assets()
    if not args.skip_web:
        build_web()

    python = pick_python()
    ensure_pyinstaller(python)
    run_pyinstaller(python)
    version = APP_VERSION
    assemble_versioned_layout(version, python)
    sanitize()
    zip_path, sha256 = make_zip(version)
    print(f"[完成] 应用包 sha256 {sha256}")
    write_release_body(version)

    if not args.output_dir:
        subprocess.check_call([str(python), str(ROOT / "scripts" / "build_manifest.py")])
    else:
        print(f"[提示] 隔离构建模式：跳过主清单覆盖（输出位于 {RELEASE}）")


if __name__ == "__main__":
    main()
