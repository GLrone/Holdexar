"""GitHub Releases 标准发布：版本 Release + 固定 tag 的更新清单。

**为什么是两条 Release**：
1. **版本 Release**（tag `v<版本>`）——用户看到的那条：release/ 下的**全部**
   待分发产物（应用包 + 更新清单 + 资产种子 + 公共目录库模板 + 更新说明 +
   Scoop 清单，缺件按「有则带」），带 changelog。这是「标准分发方式」里
   人读的那一面。
2. **清单 Release**（tag `updater`）——机器读的那一面：只挂一个 latest.json，
   地址恒定不随版本变。客户端检查更新只读它（见 app/core/updater.py）。
   注意必须 `--latest=false`：否则它会被 GitHub 标成 "Latest release"，
   把 `releases/latest` API 与 RSS 都带偏。

两条都幂等：已存在的 tag 走 `gh release upload --clobber`，重复发布不会失败。

前置：先跑 scripts/build_release.py 出包（它会顺带生成清单）；
本脚本只负责把产物推上 GitHub，不重新构建。

本机没装 gh 也能用：`--dry-run` 会打印全部 gh 命令（含手工在网页端
创建 Release 时需要照抄的标题/说明/资产清单）。

用法（发布机，需已 `gh auth login`）：
    python scripts/publish_release.py --dry-run     # 只打印将执行的命令
    python scripts/publish_release.py               # 正式发布
    python scripts/publish_release.py --prerelease  # 发预发布版（RC 用；见下）
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

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
RELEASE = ROOT / "release"
SCOOP_JSON = RELEASE / "scoop" / "holdexar.json"
NOTES_FILE = RELEASE / "RELEASE_NOTES.md"
MANIFEST = RELEASE / "latest.json"
# build_release.py 会把种子另存到 release/ 作为独立资产；源码树里的那份是同源副本，
# 缺前者时用它兜底（避免「种子明明在却报没有」）
SEED_DB = RELEASE / "holdexar_seed.db"
SEED_DB_FALLBACK = ROOT / "assets" / "seed" / "holdexar_seed.db"
# 公共目录库模板（server/scripts/export_template_db.py 产物）：导出脚本的落点
# 是 assets/seed/，构建期另存到 release/ 之后由本脚本一并上传
TEMPLATE_DB = RELEASE / "holdexar_template.db"
TEMPLATE_DB_FALLBACK = ROOT / "assets" / "seed" / "holdexar_template.db"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
from app.core.app_info import (  # noqa: E402
    APP_NAME,
    APP_VERSION,
    GITHUB_REPO,
    MANIFEST_ASSET,
    MANIFEST_TAG,
)

ZIP_GLOB = f"{APP_NAME}-win64-v*.zip"


def app_version() -> str:
    """版本号取自单一来源 app.core.app_info.APP_VERSION。"""
    return APP_VERSION


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
    标「Draft」。版本 Release 一旦被建成 Draft（网页端手建 / 早年 `--draft`
    残留），后续每次 `upload --clobber` 只往里塞文件、状态纹丝不动——这正是
    beta.2「只看得见 Draft、看不到 Latest」的成因。检测到就 `release edit
    --draft=false --latest` 转正。
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
    """应用包：优先精确匹配当前版本，否则取 release/ 内最新。"""
    exact = RELEASE / f"{APP_NAME}-win64-v{version}.zip"
    if exact.is_file():
        return exact
    candidates = sorted(RELEASE.glob(ZIP_GLOB), key=lambda p: p.stat().st_mtime)
    if not candidates:
        sys.exit(f"[错误] {RELEASE} 下没有应用包，先跑 scripts/build_release.py")
    return candidates[-1]


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


def collect_artifacts(app_zip: Path) -> list[Path]:
    """版本 Release 的资产全集：release/ 下**全部**待分发产物。

    「全部」是硬要求——漏传一件就是一个半成品发布（源码用户拉不到种子、
    Scoop 渠道跟进不到清单、用户看不到更新说明）。顺序固定（应用包在首，
    人工核对与 Scoop checkver 拼 URL 都靠它）：
        ① 应用包 zip   ② latest.json   ③ 资产种子 holdexar_seed.db
        ④ 公共目录库模板 holdexar_template.db（有则带）
        ⑤ Scoop 渠道清单 release/scoop/holdexar.json（有则带）
    更新说明 RELEASE_NOTES.md 走 --notes-file 成为 Release 正文，不再另挂附件；
    构建中间产物（build/、work/、build*.log）不是发布物，一律不带。
    """
    artifacts: list[Path] = [app_zip, MANIFEST]
    _with_fallback(SEED_DB, SEED_DB_FALLBACK, "资产种子", artifacts)
    _with_fallback(TEMPLATE_DB, TEMPLATE_DB_FALLBACK, "公共目录库模板", artifacts)
    # Scoop 清单是渠道跟进的入口（checkver 读 latest.json、autoupdate 拼 zip URL），
    # 属于分发面；更新说明不作附件——它以 --notes-file 成为 Release 正文，
    # 再挂一份是同一份文字的重复。
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
        print(f"[发布] {tag} 已存在 → 覆盖上传资产")
        run(
            ["gh", "release", "upload", tag, *[str(p) for p in artifacts], "--clobber"],
            dry,
        )
        if release_is_draft(tag):
            # 已存在却是草稿：补发为正发布并置 Latest，否则永远卡在 Draft，
            # 不进首页 Latest 简报、不带 Latest 徽章（beta.2 事故同款）
            print(f"[发布] {tag} 是草稿 → 补发为正发布并置 Latest")
            run(["gh", "release", "edit", tag, "--draft=false", "--latest"], dry)
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

    version = args.version or app_version()
    app_zip = pick_app_zip(version)
    notes_file = Path(args.notes_file)

    missing = [p for p in (app_zip, MANIFEST, notes_file) if not p.is_file()]
    if missing:
        sys.exit(
            "[错误] 缺少发布产物：\n  "
            + "\n  ".join(str(p) for p in missing)
            + "\n先跑 scripts/build_release.py（含清单生成）"
        )

    artifacts = collect_artifacts(app_zip)

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
    if args.scoop_dir:
        sync_scoop(Path(args.scoop_dir), args.dry_run)

    print("\n[完成] 分发入口：")
    print(f"  版本 Release : https://github.com/{GITHUB_REPO}/releases/tag/v{version}")
    print(f"  更新清单     : https://github.com/{GITHUB_REPO}/releases/download/"
          f"{MANIFEST_TAG}/{MANIFEST_ASSET}")
    print(f"  源码取种子   : python scripts/fetch_seed.py")


if __name__ == "__main__":
    main()
