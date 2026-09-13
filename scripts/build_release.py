"""Holdexar 发布构建：npm build → PyInstaller onedir → 产物消毒 → 绿色 zip。

用法（发布机，建议用 server/.venv 的解释器运行）：
    python scripts/build_release.py                    # 常规出包（沿用现有种子）
    python scripts/build_release.py --refresh-seed     # 先从生产库重导资产种子
    python scripts/build_release.py --skip-web         # 跳过前端构建（dist 已是最新）

产物（三件，全部要上传 Release；清单由 scripts/build_manifest.py 生成）：
- release/Holdexar-win64-v<版本>.zip —— 应用包，解压双击即用。
  **用户数据不在包内、也不落在程序目录**：打包态数据目录判定见
  app/core/paths.py（默认落 %LOCALAPPDATA% 下的同名目录），因此覆盖解压、
  换目录、整包删除重装都不会碰到用户数据。
- release/holdexar_seed.db —— 公共数据种子独立资产（汇率档案 + games 人工策划列
  + 3 年价格历史切片），供源码 clone 用户（run.py 首次启动）经
  scripts/fetch_seed.py 自动获取；价格历史按种子版本对老用户库增量合并。
- release/latest.json —— 更新清单，挂到固定 tag `updater` 供客户端检查更新。
  加 Scoop 渠道时另出 release/scoop/（用法见 packaging/scoop/README.md）。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
WEB = ROOT / "web"
DESKTOP = ROOT / "desktop"
SEED_DIR = ROOT / "assets" / "seed"
SEED_DB = SEED_DIR / "holdexar_seed.db"
RELEASE = ROOT / "release"
DIST_DIR = RELEASE / "build"  # PyInstaller distpath（COLLECT 输出 DIST_DIR/<APP_NAME>/）
WORK_DIR = RELEASE / "work"
VENV_PY = SERVER / ".venv" / "Scripts" / "python.exe"

SEED_BASENAME = "holdexar_seed.db"

# 品牌常量与应用版本同源（app.core.app_info 是纯标准库模块，可直接导入）
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
from app.core.app_info import APP_NAME, APP_VERSION  # noqa: E402

APP_DIR = DIST_DIR / APP_NAME


def pick_python() -> Path:
    if VENV_PY.is_file():
        return VENV_PY
    return Path(sys.executable)


def app_version() -> str:
    """版本号取自单一来源 app.core.app_info.APP_VERSION。

    直接导入而非起子进程读 Settings：app_info 是**纯标准库**模块，本就为
    「装依赖之前也能取到品牌/版本常量」而存在。走 Settings 还得先有 pydantic，
    且子进程调用会把版本号解析失败变成一句看不懂的 stderr。
    """
    return APP_VERSION


def refresh_seed() -> None:
    print("[种子] 从生产库重导资产种子 ...")
    subprocess.check_call([str(pick_python()), str(ROOT / "scripts" / "export_seed.py")])


def ensure_seed_dir() -> None:
    """spec datas 引用 assets/seed，目录必须存在（无种子 = 空目录，导入零开销）。"""
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    if not SEED_DB.is_file():
        print("[警告] 未找到资产种子（assets/seed/holdexar_seed.db），"
              "本包将不带汇率档案/人工列；发布前建议先跑 --refresh-seed")


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


def sanitize() -> None:
    """产物消毒：任何用户数据/日志残留即删除并复检，仍有则中止发布。"""
    assert APP_DIR.is_dir(), f"打包产物缺失：{APP_DIR}"
    residue: list[Path] = []
    for path in APP_DIR.rglob("*"):
        rel = path.relative_to(APP_DIR)
        parts = rel.parts
        is_seed = len(parts) >= 2 and parts[-2] == "seed" and path.name == SEED_BASENAME
        if path.is_dir() and path.name == "data":
            residue.append(path)
        elif path.is_file() and not is_seed:
            if path.suffix.lower() in (".log", ".db", ".sqlite3", ".db-wal", ".db-shm"):
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
            and p.suffix.lower() in (".log", ".db", ".sqlite3", ".db-wal", ".db-shm")
            and not (len(p.relative_to(APP_DIR).parts) >= 2
                     and p.relative_to(APP_DIR).parts[-2] == "seed"
                     and p.name == SEED_BASENAME)
        )
    ]
    if residue:
        sys.exit(f"[错误] 产物消毒复检仍有残留，中止发布：{residue}")
    print(f"[消毒] 产物干净（{sum(1 for _ in APP_DIR.rglob('*'))} 个文件，无 data/日志/db 残留）")


def make_zip(version: str) -> tuple[Path, str]:
    """压缩 onedir → release/Holdexar-win64-v<版本>.zip（包内根目录 Holdexar/）。

    资产名**刻意不带时间戳**：固定的 `Holdexar-win64-v<版本>.zip` 让下载端
    不必再反查 release 资产列表，也让 Scoop / winget 这类渠道的自动跟进
    （checkver + autoupdate）能按版本号直接拼出 URL。同版本重复打包就地覆盖。

    返回 (zip 路径, sha256)——校验值写进发布清单，由 scripts/build_manifest.py
    收进 latest.json 后公布。
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


def stage_seed_asset() -> Path | None:
    """把种子另存为 release/ 下的**独立 Release 资产**。

    发布包内已含一份（zip 用户零操作）；独立资产是给**源码 clone 用户**的：
    run.py 首次启动时代码调 scripts/fetch_seed.py 从这里拉。种子不进 git
    （12MB 二进制 × 每版本一份会撑爆仓库历史），只能靠 Release 资产分发。
    """
    if not SEED_DB.is_file():
        return None
    RELEASE.mkdir(parents=True, exist_ok=True)
    dest = RELEASE / SEED_BASENAME
    shutil.copy2(SEED_DB, dest)
    print(f"[发布] 种子资产：{dest.name}（{dest.stat().st_size / 1048576:.1f} MB）")
    return dest


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
        "（在此写本次更新内容。发布前编辑本文件——客户端「检查更新」会直接展示这段文字。）\n",
        encoding="utf-8",
    )
    print(f"[发布] 说明模板已生成：{body_file}（发布前请编辑）")
    return body_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 一键发布构建")
    parser.add_argument("--refresh-seed", action="store_true", help="出包前从生产库重导资产种子")
    parser.add_argument("--skip-web", action="store_true", help="跳过前端构建（web/dist 已最新）")
    args = parser.parse_args()

    if args.refresh_seed:
        refresh_seed()
    ensure_seed_dir()
    if not args.skip_web:
        build_web()

    python = pick_python()
    ensure_pyinstaller(python)
    run_pyinstaller(python)
    sanitize()
    version = app_version()
    stage_seed_asset()
    zip_path, sha256 = make_zip(version)
    print(f"[完成] 应用包 sha256 {sha256}")
    write_release_body(version)

    # 清单（latest.json + Scoop）统一由 build_manifest.py 生成——顺着跑一步，
    # 免得「出包完成但忘了生成清单」这种半成品发布
    subprocess.check_call([str(python), str(ROOT / "scripts" / "build_manifest.py")])


if __name__ == "__main__":
    main()
