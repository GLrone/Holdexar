"""Holdexar 一键发版：版本号 → 敏感面门禁 → 构建 → 推送 GitHub Release → 复验。

**为什么要有这个脚本**：一次发版原本要按顺序记住五件事（改版本号、过门禁、出包、
推两条 Release、核对资产），漏掉任何一步都留下一个「半成品发布」——清单指向不
存在的 zip、tag 打在上一个提交上、或者最糟的：带着凭据出包。本项目已经发生过一次
「凭据随包公开」的事故，因此本脚本把把关放在**构建之前**，并把「发布必须对应一个
已提交的树」做成硬约束而不是提示。

流程（任一步失败即中止，绝不留下半个发布）：

    1. 预检   工作树干净 / 在 main / gh 已登录 / tag 未被占用 / 说明已写
    2. 门禁   scripts/check_secrets.py --all（命中即中止）
    3. 版本   --bump patch|minor|major 改 app_info.APP_VERSION，或 --version 指定
    4. 提交推 版本号变更单独提交并**先推送**（tag 必须落在这个提交上）
    5. 构建   scripts/build_release.py（前端 → PyInstaller → 消毒 → zip → 清单）
    6. 发布   scripts/publish_release.py（版本 Release + updater 清单 Release）
    7. 复验   下载 latest.json，核对 schema / 版本 / sha256 与资产实际一致

**本脚本是唯一发版入口。** 绕过它手动跑 build_release.py + publish_release.py
就没有门禁与复验（两者仍可单独运行，各自支持 --dry-run）。

用法（发布机，用 server/.venv 解释器运行）：
    python scripts/release.py --check                  # 只预检，不改任何东西
    python scripts/release.py --bump patch --dry-run   # 全流程演练（不出网、不推送）
    python scripts/release.py --bump patch             # 正式发版
    python scripts/release.py --version 0.2.0 --yes    # 免交互

前置：gh auth login（说明见 scripts/publish_release.py 顶部）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
SCRIPTS = ROOT / "scripts"
RELEASE = ROOT / "release"
APP_INFO = SERVER / "app" / "core" / "app_info.py"
NOTES_FILE = RELEASE / "RELEASE_NOTES.md"
MANIFEST = RELEASE / "latest.json"
MAIN_BRANCH = "main"

# build_release.py 生成的说明模板占位文案——带着它发版 = 客户端更新提示里
# 显示「（在此写本次更新内容…）」，属于必须拦住的半成品
NOTES_PLACEHOLDER = "在此写本次更新内容"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
from app.core.app_info import (  # noqa: E402
    APP_NAME,
    APP_SLUG,
    APP_VERSION,
    GITHUB_REPO,
    MANIFEST_ASSET,
    MANIFEST_SCHEMA,
    MANIFEST_TAG,
)

VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


# ── 基础设施 ────────────────────────────────────────────────


def pick_python() -> Path:
    venv = SERVER / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return venv
    posix = SERVER / ".venv" / "bin" / "python"
    return posix if posix.is_file() else Path(sys.executable)


def capture(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run(cmd: list[str], dry: bool, label: str = "") -> None:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if label:
        print(f"[{label}] {printable}")
    else:
        print(f"[执行] {printable}")
    if dry:
        print("[dry-run] 未执行")
        return
    subprocess.check_call(cmd)


def die(message: str) -> None:
    sys.exit(f"\n[中止] {message}")


# ── 步骤 1：预检 ────────────────────────────────────────────


def git_dirty() -> list[str]:
    """工作树里未提交的改动（含未跟踪，忽略 .gitignore 命中的）。"""
    code, out = capture(["git", "status", "--porcelain"])
    assert code == 0, "git status 失败——这里不是 git 仓库？"
    return [line for line in out.splitlines() if line.strip()]


def git_branch() -> str:
    _, out = capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip()


def remote_tag_exists(tag: str) -> bool:
    """远端是否已有该 tag（有 = 这个版本已经发过，重发要显式 --allow-republish）。"""
    code, out = capture(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
    if code != 0:
        die(f"读远端 tag 失败（网络或 origin 未配置）：{out.strip()}")
    return bool(out.strip())


def gh_ready() -> bool:
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    code, _ = capture(["gh", "auth", "status"])
    return code == 0


def notes_ready() -> str | None:
    """说明文件就绪则返回正文，否则返回未就绪的原因。"""
    if not NOTES_FILE.is_file():
        return f"缺少 {NOTES_FILE.name}（构建时会自动生成模板，写完再发）"
    body = NOTES_FILE.read_text(encoding="utf-8").strip()
    if not body:
        return f"{NOTES_FILE.name} 是空文件"
    if NOTES_PLACEHOLDER in body:
        return f"{NOTES_FILE.name} 还是模板占位文案，请先写本次更新内容"
    return None


def preflight(version: str, allow_dirty: bool, allow_republish: bool) -> None:
    print(f"\n── 预检 ────────────────────────────────────────")
    print(f"[预检] 仓库 {GITHUB_REPO}   当前版本 {APP_VERSION} → 目标 v{version}")

    errors: list[str] = []

    branch = git_branch()
    print(f"[预检] 分支 {branch}")
    if branch != MAIN_BRANCH:
        errors.append(f"当前在 {branch} 分支；发布必须从 {MAIN_BRANCH} 分支出（tag 与会话记录才好追）")

    dirty = git_dirty()
    if dirty:
        head = "\n        ".join(dirty[:15])
        more = f"\n        …另有 {len(dirty) - 15} 项" if len(dirty) > 15 else ""
        if allow_dirty:
            print(f"[预检] 警告：工作树有 {len(dirty)} 项未提交改动（--allow-dirty 已放行）")
        else:
            errors.append(
                f"工作树有 {len(dirty)} 项未提交改动——发布必须对应一个提交，"
                f"否则 zip 与仓库状态对不上：\n        {head}{more}\n"
                f"        （确要带着脏树发版：--allow-dirty）"
            )

    if not gh_ready():
        errors.append(
            "gh CLI 不可用或未登录：winget install --id GitHub.cli && gh auth login\n"
            "        （没有 gh 也可用 scripts/publish_release.py 打印手工发布清单）"
        )

    reason = notes_ready()
    if reason:
        errors.append(f"更新说明未就绪：{reason}")

    if remote_tag_exists(f"v{version}"):
        if allow_republish:
            print(f"[预检] 警告：远端已有 tag v{version}（--allow-republish 已放行，将覆盖资产）")
        else:
            errors.append(
                f"远端已有 tag v{version}。改版本号，或用 --allow-republish 覆盖该版本的资产"
            )

    if errors:
        print()
        for i, err in enumerate(errors, 1):
            print(f"  ✗ {err}")
        die(f"预检未通过（{len(errors)} 项）")
    print("[预检] 通过")


# ── 步骤 2：敏感面门禁 ──────────────────────────────────────


def secret_gate(dry: bool) -> None:
    """构建**之前**跑门禁：出包前拦下凭据，比推送前拦下更早一步。"""
    print(f"\n── 敏感面门禁 ──────────────────────────────────")
    cmd = [str(pick_python()), str(SCRIPTS / "check_secrets.py"), "--all"]
    if dry:
        run(cmd, dry=True, label="门禁")
        return
    code = subprocess.call(cmd)
    if code != 0:
        die(
            "敏感面门禁命中（见上方明细）。发布已中止。\n"
            "       这层拦的是「凭据/大文件/用户数据进入发布产物或版本库」——\n"
            "       本项目曾因同类问题把活库与密钥公开推送过一次，不要 --no-verify 跳过。"
        )


# ── 步骤 3：版本号 ──────────────────────────────────────────


def bump(kind: str) -> str:
    """按 kind 递增 app_info.APP_VERSION，返回新版本号。"""
    match = SEMVER_RE.match(APP_VERSION)
    if not match:
        die(f"当前版本号 {APP_VERSION!r} 不是 x.y.z 形式，无法自动递增；请用 --version 指定")
    major, minor, patch = (int(g) for g in match.groups())
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def write_version(new: str) -> None:
    """把 APP_VERSION 写回 app_info.py（唯一版本来源，见该文件注释）。"""
    text = APP_INFO.read_text(encoding="utf-8")
    replaced, count = VERSION_RE.subn(f'APP_VERSION = "{new}"', text, count=1)
    if count != 1:
        die(f"在 {APP_INFO} 里找不到 APP_VERSION = \"...\" 这一行，未做任何修改")
    APP_INFO.write_text(replaced, encoding="utf-8")
    print(f"[版本] app_info.APP_VERSION → {new}")


def check_stale_version_refs(version: str) -> None:
    """文档里仍写着旧版本号时给个提示（只提示，不拦）。

    README 的版本徽章这类引用不会让构建失败，但会让页面长期显示错版本——
    本项目的 README 就曾停留在 `v0.1.0-beta`。
    """
    stale: list[str] = []
    for path in (ROOT / "README.md", ROOT / "docs" / "DEV_LOG.md"):
        if not path.is_file():
            continue
        found = set(re.findall(r"v?(\d+\.\d+\.\d+)", path.read_text(encoding="utf-8", errors="replace")))
        old = sorted(v for v in found if v != version)
        if old:
            stale.append(f"{path.relative_to(ROOT)}（提到 {', '.join(old[:4])}）")
    if stale:
        print(f"[版本] 提示：以下文件仍提到别的版本号，发布前顺手核对：{'；'.join(stale)}")


def commit_and_push(version: str, dry: bool, yes: bool) -> None:
    """版本号变更单独提交并推送。

    **必须先推送**：`gh release create <tag>` 是在**远端**创建 tag，指向的提交
    由远端默认分支决定。本地提交没推上去，tag 就会落在上一个提交上，
    于是「下载到的 zip」与「tag 指向的源码」不是一回事。
    """
    tag = f"v{version}"
    print(f"\n── 提交并推送 {tag} ───────────────────────────")
    run(["git", "add", str(APP_INFO)], dry, label="提交")
    run(["git", "commit", "-m", f"chore(release): {tag}"], dry, label="提交")
    if not yes and not dry:
        answer = input(f"推送到 origin/{MAIN_BRANCH} 并继续发布 {tag}？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            die("已按你的要求中止（版本号改动已提交在本地，可 git reset 撤销）")
    run(["git", "push", "origin", MAIN_BRANCH], dry, label="推送")


# ── 步骤 5/6：构建与发布 ────────────────────────────────────


def build(dry: bool) -> None:
    print(f"\n── 构建 ────────────────────────────────────────")
    run([str(pick_python()), str(SCRIPTS / "build_release.py")], dry)


def publish(dry: bool) -> None:
    print(f"\n── 发布 ────────────────────────────────────────")
    cmd = [str(pick_python()), str(SCRIPTS / "publish_release.py")]
    if dry:
        cmd.append("--dry-run")
    run(cmd, dry=False)


# ── 步骤 7：复验 ────────────────────────────────────────────


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-release"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 —— 固定的 github 地址
        return json.loads(resp.read().decode("utf-8"))


def verify(version: str, dry: bool) -> None:
    """复验线上清单：地址可达、schema 对得上、版本与校验值就是刚发的那份。

    只在**远端**取，不看本地文件——本地那份本来就是我们自己写的，校验它证明
    不了「用户下载到的是对的」。这一步才真正回答「发布成功了吗」。
    """
    print(f"\n── 复验 ────────────────────────────────────────")
    manifest_url = (
        f"https://github.com/{GITHUB_REPO}/releases/download/{MANIFEST_TAG}/{MANIFEST_ASSET}"
    )
    if dry:
        print(f"[dry-run] 将拉取 {manifest_url} 并与本地 {MANIFEST.name} 比对")
        return

    local = json.loads(MANIFEST.read_text(encoding="utf-8"))
    try:
        remote = fetch_json(manifest_url)
    except Exception as e:  # noqa: BLE001 —— 复验失败原因都要如实报出来
        die(
            f"拉取线上清单失败：{e}\n       {manifest_url}\n"
            f"       资产刚上传时 GitHub 有缓存延迟，可稍后重跑一次确认"
        )

    problems: list[str] = []
    if remote.get("schema") != MANIFEST_SCHEMA:
        problems.append(f"schema 期望 {MANIFEST_SCHEMA}，线上是 {remote.get('schema')}")
    if remote.get("version") != version:
        problems.append(f"版本期望 {version}，线上是 {remote.get('version')}")
    for role in ("app", "seed"):
        want = next((a for a in local.get("assets", []) if a.get("role") == role), None)
        got = next((a for a in remote.get("assets", []) if a.get("role") == role), None)
        if want is None:
            continue
        if got is None:
            problems.append(f"线上清单缺 {role} 资产（本地有 {want['name']}）")
        elif got.get("sha256") != want.get("sha256"):
            problems.append(f"{role} 资产 sha256 不一致：线上 {got.get('sha256')[:12]}… vs 本地 {want.get('sha256')[:12]}…")

    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        die("线上清单与本次产物不一致——发布可能没生效，或上传的是旧产物")
    print("[复验] 线上清单与本次产物一致")


def summarize(version: str) -> None:
    print("\n[完成] 分发入口：")
    print(f"  版本 Release : https://github.com/{GITHUB_REPO}/releases/tag/v{version}")
    print(f"  更新清单     : https://github.com/{GITHUB_REPO}/releases/download/"
          f"{MANIFEST_TAG}/{MANIFEST_ASSET}")
    print(f"  Scoop 清单   : release/scoop/{APP_SLUG}.json（提交进 bucket 仓库后生效）")
    print(f"  源码取种子   : python scripts/fetch_seed.py")


# ── 主流程 ──────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Holdexar 一键发版（门禁 → 构建 → 推送 Release → 复验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", help="目标版本号（默认沿用 app_info 里的当前值）")
    parser.add_argument(
        "--bump", choices=("patch", "minor", "major"),
        help="自动递增版本号并写回 app_info.py（比 --version 省一次手改）",
    )
    parser.add_argument("--check", action="store_true", help="只跑预检，不改任何东西")
    parser.add_argument("--dry-run", action="store_true", help="走完流程但不推送、不出网")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过推送前的确认")
    parser.add_argument("--allow-dirty", action="store_true", help="允许带着未提交改动发版")
    parser.add_argument("--allow-republish", action="store_true", help="允许覆盖已存在的版本 tag")
    args = parser.parse_args()

    if args.version and args.bump:
        die("--version 与 --bump 只能给一个")

    version = args.version or (bump(args.bump) if args.bump else APP_VERSION)
    if not SEMVER_RE.match(version):
        die(f"版本号 {version!r} 不是 x.y.z 形式")

    version_changed = version != APP_VERSION

    preflight(version, args.allow_dirty, args.allow_republish)

    if args.check:
        print("\n[预检] --check 模式，未做任何改动")
        if version_changed:
            print(f"[预检] 正式发版时 app_info.APP_VERSION 将被写为 {version}（本次未写）")
        return

    secret_gate(args.dry_run)

    if version_changed:
        print(f"\n── 版本 ────────────────────────────────────────")
        if args.dry_run:
            print(f"[dry-run] 将把 app_info.APP_VERSION 写为 {version}")
        else:
            write_version(version)
        check_stale_version_refs(version)

    commit_and_push(version, args.dry_run, args.yes)
    build(args.dry_run)
    publish(args.dry_run)
    verify(version, args.dry_run)
    summarize(version)


if __name__ == "__main__":
    main()
