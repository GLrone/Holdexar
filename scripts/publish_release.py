"""GitHub Releases 标准发布：版本 Release + 固定 tag 的更新清单。

- **版本 Release**（tag `v<版本>`）：用户看到的那条，release/ 下**全部**
  待分发产物（应用包 + 更新清单 + 资产种子 + 公共目录库模板 + Scoop 清单，
  缺件按「有则带」），说明走 --notes-file。必须显式 `--latest`。
- **清单 Release**（tag `updater`）：机器读的那面，只挂 latest.json，
  地址恒定不随版本变（客户端检查更新只读它）。必须 `--latest=false`，
  否则它会抢走 `releases/latest`。

两条都幂等：已存在的 tag 走 `gh release upload --clobber`，重复发布不会失败。

前置：先跑 scripts/build_release.py 出包（它会顺带生成清单）；
本脚本只负责把产物推上 GitHub，不重新构建。
本机没装 gh 也能用：`--dry-run` 会打印全部 gh 命令（含手工在网页端
创建 Release 时需要照抄的标题/说明/资产清单）。

用法（发布机，需已 `gh auth login`）：
    python scripts/publish_release.py --dry-run     # 只打印将执行的命令
    python scripts/publish_release.py               # 正式发布
    python scripts/publish_release.py --prerelease  # 预发布标记留给真 RC（Beta 见下）
    python scripts/publish_release.py --scoop-dir D:\\scoop-bucket   # 顺带更新 Scoop 清单

发 Beta 的推荐做法：标题写「Holdexar vX.Y.Z Beta」，**不加** --prerelease。
预发布标记不能当 Latest（GitHub 限制），会让 releases/latest 指空，
客户端更新检查的降级路径（走该接口）就没了着落。

没装 gh：
    winget install --id GitHub.cli      # 或 scoop install gh
    gh auth login
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
RELEASE = ROOT / "release"
SCOOP_JSON = RELEASE / "scoop" / "holdexar.json"
NOTES_FILE = RELEASE / "RELEASE_NOTES.md"
MANIFEST = RELEASE / "latest.json"
# build_release.py 会把种子 gz 另存到 release/ 作为独立资产；源码树里的 raw 副本
# 可现场压缩兜底（避免「种子明明在却报没有」）
SEED_GZ = RELEASE / "holdexar_seed.db.gz"
SEED_RAW_FALLBACK = ROOT / "assets" / "seed" / "holdexar_seed.db"
# 公共目录库模板（server/scripts/export_template_db.py 产物）：导出脚本的落点
# 是 assets/seed/，构建期另存到 release/ 之后由本脚本一并上传
TEMPLATE_DB = RELEASE / "holdexar_template.db"
TEMPLATE_DB_FALLBACK = ROOT / "assets" / "seed" / "holdexar_template.db"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
from app.core.app_info import (  # noqa: E402
    APP_NAME,
    APP_VERSION,
    DATA_CDN_BASE,
    GITHUB_REPO,
    MANIFEST_ASSET,
    MANIFEST_TAG,
)

ZIP_GLOB = f"{APP_NAME}-win64-v*.zip"


def run(cmd: list[str], dry: bool) -> None:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if dry:
        print(f"[dry-run] {printable}")
        return
    print(f"[执行] {printable}")
    subprocess.check_call(cmd)


def gh_available() -> bool:
    try:
        return subprocess.run(
            ["gh", "--version"], capture_output=True, check=True
        ).returncode == 0
    except (OSError, subprocess.CalledProcessError):
        return False


def release_exists(tag: str) -> bool:
    """已存在 → 走 upload --clobber（幂等）；gh 缺失时按「不存在」处理。"""
    try:
        proc = subprocess.run(
            ["gh", "release", "view", tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return False
    return proc.returncode == 0


def release_is_draft(tag: str) -> bool:
    """已存在且为草稿 → 覆盖上传后须补发为正发布，否则永远卡在 Draft：

    Draft 不进仓库首页 Latest 简报、不带 Latest 徽章，只在 Release list 里
    标「Draft」。版本 Release 一旦被建成 Draft，后续每次 `upload --clobber`
    只往里塞文件、状态纹丝不动。检测到就 `release edit --draft=false --latest`
    转正。
    """
    try:
        proc = subprocess.run(
            ["gh", "release", "view", tag, "--json", "isDraft"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    try:
        return bool(json.loads(proc.stdout).get("isDraft", False))
    except json.JSONDecodeError:
        return False


def pick_app_zip(version: str) -> Path:
    """应用包：必须精确匹配当前版本，禁止静默选择其他版本的旧 ZIP。"""
    exact = RELEASE / f"{APP_NAME}-win64-v{version}.zip"
    if exact.is_file():
        return exact
    sys.exit(
        f"[错误] 未找到当前版本 v{version} 的应用包（期望 {exact.name}）。\n"
        f"       不允许静默选择其他版本的旧 ZIP；请先运行 python scripts/build_release.py 出包"
    )


def _with_fallback(primary: Path, fallback: Path, label: str, found: list[Path]) -> None:
    """资产就位即登记；缺失时退源码树副本，两者都无则记警告跳过。"""
    if primary.is_file():
        found.append(primary)
        return
    if fallback.is_file():
        print(f"[提示] 用源码树副本兜底{label}：{fallback}")
        found.append(fallback)
        return
    print(f"[警告] 未找到{label}（{primary} / {fallback}），本次发布不含该件")


def _ensure_seed_gz(dry: bool = False) -> None:
    """种子分发形态统一为 .gz：release/ 下没有 gz 时用 raw 副本现场压缩。"""
    if SEED_GZ.is_file():
        return
    if not SEED_RAW_FALLBACK.is_file():
        print(f"[警告] 未找到资产种子（{SEED_GZ.name} / {SEED_RAW_FALLBACK}），本次发布不含该件")
        return
    if dry:
        print(f"[dry-run] 发现源种子 {SEED_RAW_FALLBACK}，将在正式发布时现场压缩为 {SEED_GZ.name}（本次未生成）")
        return
    import gzip

    with SEED_RAW_FALLBACK.open("rb") as fin, gzip.open(SEED_GZ, "wb", compresslevel=9) as fout:
        while chunk := fin.read(1 << 20):
            fout.write(chunk)
    print(f"[发布] 种子 gz 现场压缩：{SEED_RAW_FALLBACK} → {SEED_GZ.name}")


def sync_cdn_seed(dry: bool) -> None:
    """把种子分发档推到自建分发（对象存储），并核对线上哈希与本地一致。

    客户端下载种子的首选通道是它（大体积资产在该入口上通常比走 GitHub 快一个
    数量级）；推不上去不影响发布——客户端的候选表里还有 GitHub 及其镜像，
    探测阶段会自动选到能用的那条。

    上传工具与凭据是维护机本机文件（不入库），公开克隆里没有——缺了就只打印
    手工指引，不报错。
    """
    if not SEED_GZ.is_file():
        print(f"[自建分发] 跳过：没有 {SEED_GZ.name}")
        return
    if not DATA_CDN_BASE:
        print("[自建分发] 跳过：未配置分发地址")
        return
    target = f"{DATA_CDN_BASE}/seed/{SEED_GZ.name}"
    size = SEED_GZ.stat().st_size / 1048576
    uploader = ROOT / "scripts" / "upload_cloud_packs.py"
    if not uploader.is_file():
        print(f"[自建分发] 手工上传 {SEED_GZ.name}（{size:.1f} MB）→ {target}")
        return
    if dry:
        print(f"[dry-run] 上传 {SEED_GZ.name}（{size:.1f} MB）→ {target}")
        return
    subprocess.check_call(
        [sys.executable, str(uploader), "--seed"]
    )


def collect_artifacts(app_zip: Path, dry: bool = False) -> list[Path]:
    """版本 Release 的资产全集：release/ 下全部待分发产物。"""
    artifacts: list[Path] = [app_zip, MANIFEST]
    _ensure_seed_gz(dry)
    if SEED_GZ.is_file():
        artifacts.append(SEED_GZ)
    _with_fallback(TEMPLATE_DB, TEMPLATE_DB_FALLBACK, "公共目录库模板", artifacts)
    if SCOOP_JSON.is_file():
        artifacts.append(SCOOP_JSON)
    else:
        print(f"[警告] 未找到 {SCOOP_JSON.name}，本次发布不含 Scoop 清单")
    return artifacts


def publish_version_release(
    version: str, artifacts: list[Path], notes_file: Path, prerelease: bool, dry: bool
) -> None:
    tag = f"v{version}"
    title = f"{APP_NAME} v{version}"
    if release_exists(tag):
        print(f"[发布] {tag} 已存在 → 覆盖上传资产并更新发布说明")
        run(
            ["gh", "release", "upload", tag, *[str(p) for p in artifacts], "--clobber"],
            dry,
        )
        edit_cmd = [
            "gh", "release", "edit", tag,
            "--title", title,
            "--notes-file", str(notes_file),
        ]
        if release_is_draft(tag):
            print(f"[发布] {tag} 是草稿 → 补发为正发布并置 Latest")
            edit_cmd.extend(["--draft=false", "--latest"])
        run(edit_cmd, dry)
        return
    cmd = [
        "gh", "release", "create", tag,
        *[str(p) for p in artifacts],
        "--repo", GITHUB_REPO,
        "--title", title,
        "--notes-file", str(notes_file),
    ]
    if prerelease:
        cmd.append("--prerelease")
        print(
            "[提示] 预发布标记的版本 Release 不能当 Latest，GitHub 会把\n"
            "       releases/latest 指空——想发 Beta 又保住 Latest，用「标题带\n"
            "       Beta + 不加 --prerelease」（预发布标记留给真正的 RC）。"
        )
    else:
        # 版本 Release 才是用户看到的「最新版本」：显式置 Latest。不加这句时，
        # 清单 Release（updater）会被 GitHub 算成 Latest——它是机器页，
        # 会让 releases/latest 与仓库首页指错地方
        cmd.append("--latest")
    run(cmd, dry)


def publish_manifest_release(dry: bool) -> None:
    """清单 Release：固定 tag、只挂 latest.json、永远不是 Latest release。"""
    if release_exists(MANIFEST_TAG):
        print(f"[发布] {MANIFEST_TAG} 已存在 → 覆盖清单资产")
        run(
            ["gh", "release", "upload", MANIFEST_TAG, str(MANIFEST), "--clobber"],
            dry,
        )
        return
    run(
        [
            "gh", "release", "create", MANIFEST_TAG,
            str(MANIFEST),
            "--repo", GITHUB_REPO,
            "--title", f"{APP_NAME} 更新清单",
            # 面向用户：讲清「这是什么、删了会怎样」，不写实现细节
            "--notes",
            "本页是 Holdexar 应用内「检查更新」读取的数据：最新版本号、安装包\n"
            "下载地址与文件校验值。\n"
            "\n"
            "它不是版本发布页，请勿删除——删除后客户端将无法检测到新版本。",
            # 也标预发布：GitHub 的 Latest 取「最新的非预发布」，
            # 只加 --latest=false 挡不住它 —— 清单页会把 releases/latest
            # 抢过去（那是机器页，客户端降级路径拿它当版本页会扑空）
            "--prerelease",
            # 双保险：预发布本就不会被标 Latest
            "--latest=false",
        ],
        dry,
    )


def sync_scoop(dest_dir: Path, dry: bool) -> None:
    """把生成的 Scoop 清单复制进本地 bucket 仓库（提交由用户自行完成）。"""
    if not SCOOP_JSON.is_file():
        sys.exit(f"[错误] 缺少 {SCOOP_JSON}，先跑 scripts/build_manifest.py")
    dest = dest_dir / "bucket" / SCOOP_JSON.name
    if dry:
        print(f"[dry-run] 复制 {SCOOP_JSON} → {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCOOP_JSON, dest)
    print(f"[Scoop] 清单已写入 {dest}")
    print(f"[Scoop] 请在 bucket 仓库提交推送后，用户即可 scoop install {APP_NAME.lower()}")


def manual_checklist(version: str, artifacts: list[Path], prerelease: bool) -> None:
    """没有 gh 时的人工兜底：网页端创建两条 Release 的照抄清单。"""
    tag = f"v{version}"
    print("\n[手工发布] 本机未检测到 gh，按下面两步在网页端操作：")
    print(f"  ① 版本 Release：https://github.com/{GITHUB_REPO}/releases/new")
    print(f"     Tag: {tag}（新建）   Target: 默认分支")
    print(f"     标题: {APP_NAME} v{version}")
    if prerelease:
        print("     勾选「Set as a pre-release」")
    print(f"     说明: 复制 {NOTES_FILE.name} 全文")
    print("     附件:")
    for p in artifacts:
        print(f"       - {p}")
    print("  ② 清单 Release（**必须勾选 Set as the latest release = 不勾**）：")
    print(f"     Tag: {MANIFEST_TAG}（新建）   标题: {APP_NAME} 更新清单")
    print(f"     附件: 只传 {MANIFEST}")
    print("     说明: 本页是应用内「检查更新」读取的数据（版本号 / 下载地址 /")
    print("           校验值），不是版本发布页，请勿删除。")
    print("     注意：务必不要把它设成 Latest release——否则 releases/latest")
    print("           与 RSS 会被带偏。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar GitHub Release 发布")
    parser.add_argument("--version", help="覆盖版本号（默认读后端配置）")
    parser.add_argument("--notes-file", default=str(NOTES_FILE), help="Release 说明 Markdown")
    parser.add_argument("--prerelease", action="store_true", help="标记为预发布版")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    parser.add_argument("--skip-manifest", action="store_true", help="不刷新 updater 清单 Release")
    parser.add_argument("--scoop-dir", help="Scoop bucket 仓库本地路径（顺带写入清单）")
    args = parser.parse_args()

    version = args.version or APP_VERSION
    app_zip = pick_app_zip(version)
    notes_file = Path(args.notes_file)

    missing = [p for p in (app_zip, MANIFEST, notes_file) if not p.is_file()]
    if missing:
        sys.exit(
            "[错误] 缺少发布产物：\n  "
            + "\n  ".join(str(p) for p in missing)
            + "\n先跑 scripts/build_release.py（含清单生成）"
        )

    # 正文就绪检查（直接调用 publish_release.py 时不能绕过）
    from release import notes_ready
    notes_err = notes_ready(notes_file)
    if notes_err:
        sys.exit(f"[错误] 发布说明未就绪：{notes_err}")

    # 清单版本、notes 及应用包一致性检查
    try:
        manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if manifest_data.get("version") != version:
            sys.exit(
                f"[错误] {MANIFEST.name} 声明的版本 ({manifest_data.get('version')}) 与目标版本 ({version}) 不一致。\n"
                f"       请重新运行 python scripts/build_manifest.py 刷新清单"
            )
        if manifest_data.get("notes") != notes_file.read_text(encoding="utf-8").strip():
            sys.exit(
                f"[错误] {MANIFEST.name} 中的 notes 与 {notes_file.name} 内容不一致。\n"
                f"       请重新运行 python scripts/build_manifest.py 刷新清单"
            )
        app_asset = next((a for a in manifest_data.get("assets", []) if a.get("role") == "app"), None)
        if not app_asset:
            sys.exit(f"[错误] {MANIFEST.name} 中缺少 role='app' 的资产声明")
        if app_asset.get("name") != app_zip.name:
            sys.exit(f"[错误] {MANIFEST.name} 声明的应用包名 ({app_asset.get('name')}) 与实际文件 ({app_zip.name}) 不一致")
        if app_asset.get("size") != app_zip.stat().st_size:
            sys.exit(f"[错误] {MANIFEST.name} 声明的应用包体积 ({app_asset.get('size')}) 与实际文件 ({app_zip.stat().st_size}) 不一致")
    except json.JSONDecodeError as e:
        sys.exit(f"[错误] {MANIFEST.name} 解析失败：{e}")

    artifacts = collect_artifacts(app_zip, dry=args.dry_run)

    print(f"[发布] 仓库 {GITHUB_REPO}  版本 v{version}")
    for p in artifacts:
        print(f"       资产 {p.name}（{p.stat().st_size / 1048576:.1f} MB）")

    has_gh = gh_available()
    if not has_gh:
        if args.dry_run:
            print("[提示] 未检测到 gh：以下命令仅作参考")
        else:
            manual_checklist(version, artifacts, args.prerelease)
            sys.exit(
                "[中止] 未检测到 gh CLI。装好后再跑本脚本，"
                "或按上面的清单在网页端手工发布。\n"
                "       安装：winget install --id GitHub.cli  然后 gh auth login"
            )

    publish_version_release(version, artifacts, notes_file, args.prerelease, args.dry_run)
    if not args.skip_manifest:
        publish_manifest_release(args.dry_run)
    sync_cdn_seed(args.dry_run)
    if args.scoop_dir:
        sync_scoop(Path(args.scoop_dir), args.dry_run)

    print("\n[完成] 分发入口：")
    print(f"  版本 Release : https://github.com/{GITHUB_REPO}/releases/tag/v{version}")
    print(f"  更新清单     : https://github.com/{GITHUB_REPO}/releases/download/"
          f"{MANIFEST_TAG}/{MANIFEST_ASSET}")
    print(f"  源码取种子   : python scripts/fetch_seed.py")


if __name__ == "__main__":
    main()
