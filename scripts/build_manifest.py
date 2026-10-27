"""更新清单生成：release/latest.json（分发机制的稳定入口）+ Scoop 渠道清单。

清单挂在固定 tag（见 app_info.MANIFEST_TAG），校验值与确切资产名随清单
一并下发，客户端检查更新不依赖 GitHub API；assets[] 声明应用包资产的
体积与校验值。清单 Release 用 `--latest=false` 创建，不会占用
`releases/latest`，种子的 `releases/latest/download/` 通道不受影响。

**清单必须签名**（Ed25519，私钥在发布机、仓库外，公钥内嵌客户端）：
不签名不出清单——客户端只认验签清单，发布无签名清单等于断更。

产出：
- release/latest.json         —— 上传到 tag `updater` 的 Release（版本 Release 也放一份）
- release/scoop/holdexar.json —— Scoop 渠道清单，提交进 bucket 仓库即生效

用法：
    python scripts/build_manifest.py                 # 自动取 release/ 内应用包
    python scripts/build_manifest.py --zip <路径>     # 指定应用包
    python scripts/build_manifest.py --version 0.3.5-beta # 覆盖版本号（默认读后端配置）
    python scripts/build_manifest.py --key <私钥路径>  # 签名私钥（或设 HOLDEXAR_UPDATE_SIGNING_KEY）
    python scripts/build_manifest.py --generate-key <私钥路径>  # 首次生成密钥对

清单 schema 变更时同步递增 app_info.MANIFEST_SCHEMA，客户端按需兼容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
RELEASE = ROOT / "release"
SCOOP_DIR = RELEASE / "scoop"
NOTES_FILE = RELEASE / "RELEASE_NOTES.md"
MANIFEST_OUT = RELEASE / "latest.json"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
from app.core import update_signing  # noqa: E402
from app.core.app_info import (  # noqa: E402
    APP_NAME,
    APP_SUBTITLE,
    APP_VERSION,
    GITHUB_REPO,
    MANIFEST_ASSET,
    MANIFEST_SCHEMA,
    MANIFEST_TAG,
)

ZIP_PATTERN = re.compile(rf"^{re.escape(APP_NAME)}-win64-v[\w.-]+\.zip$", re.IGNORECASE)


def manifest_url(mirror: str = "") -> str:
    """清单的公开地址（mirror 为空 = 直连）。与 app/core/updater.manifest_url 同址。"""
    return (
        f"{mirror}https://github.com/{GITHUB_REPO}/releases/download/"
        f"{MANIFEST_TAG}/{MANIFEST_ASSET}"
    )


def asset_url(name: str, tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{name}"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_zip(explicit: str | None, version: str) -> Path:
    """应用包：显式路径优先，否则取 release/ 内与目标版本精确匹配的包。"""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            sys.exit(f"[错误] 指定的应用包不存在：{path}")
        return path
    exact = RELEASE / f"{APP_NAME}-win64-v{version}.zip"
    if exact.is_file():
        return exact
    sys.exit(
        f"[错误] 未找到当前版本 v{version} 的应用包（期望 {exact.name}）。\n"
        f"       不允许静默选择其他版本的旧 ZIP；请先运行 scripts/build_release.py 出包，或使用 --zip 指定"
    )


def read_notes() -> str:
    """更新说明正文：release/RELEASE_NOTES.md（人工编辑的唯一入口）。"""
    if not NOTES_FILE.is_file():
        return ""
    return NOTES_FILE.read_text(encoding="utf-8").strip()


def build_manifest(version: str, zip_path: Path) -> dict:
    """清单结构：app 元信息 + assets[]（role=app 全量应用包）。

    role 让客户端不必靠文件名模式认资产；schema 号供将来兼容判断。
    """
    assets = [
        {
            "role": "app",
            "name": zip_path.name,
            "size": zip_path.stat().st_size,
            "sha256": sha256_of(zip_path),
        }
    ]

    return {
        "schema": MANIFEST_SCHEMA,
        "app": APP_NAME,
        "version": version,
        "tag": f"v{version}",
        "publishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": read_notes(),
        "assets": assets,
    }


def build_scoop(version: str, zip_path: Path, zip_sha: str) -> dict:
    """Scoop 渠道清单：checkver 直接读 latest.json，渠道自动跟进不依赖 GitHub API。"""
    tag = f"v{version}"
    return {
        "$schema": "https://raw.githubusercontent.com/ScoopInstaller/Scoop/master/schema.json",
        "version": version,
        "description": APP_SUBTITLE,
        "homepage": f"https://github.com/{GITHUB_REPO}",
        # 与本仓库 LICENSE 一致——渠道清单是公开的，声明错了等于对外发一份
        # 错误的许可证声明。
        "license": "GPL-3.0-only",
        "architecture": {
            "64bit": {
                "url": asset_url(zip_path.name, tag),
                "hash": zip_sha,
                # zip 内根目录即 <APP_NAME>/，Scoop 需据此下钻
                "extract_dir": APP_NAME,
            }
        },
        "shortcuts": [[f"{APP_NAME}.exe", APP_NAME]],
        "checkver": {"url": manifest_url(), "jsonpath": "$.version"},
        "autoupdate": {
            "architecture": {
                "64bit": {"url": asset_url(f"{APP_NAME}-win64-v$version.zip", tag)}
            }
        },
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build(explicit_zip: str | None = None, version: str | None = None, key_path: str | None = None) -> dict:
    version = version or APP_VERSION
    zip_path = pick_zip(explicit_zip, version)
    expected_zip_name = f"{APP_NAME}-win64-v{version}.zip"
    if zip_path.name.lower() != expected_zip_name.lower():
        sys.exit(
            f"[错误] 指定的应用包文件名 {zip_path.name} 与目标版本 v{version} 不一致（期望 {expected_zip_name}）"
        )
    manifest = build_manifest(version, zip_path)
    # 清单必须签名：客户端只认内嵌公钥验签过的清单，未签名清单发出去等于废纸
    manifest = update_signing.sign_manifest(manifest, resolve_signing_key(key_path))
    ok, reason = update_signing.verify_manifest(manifest)
    if not ok:
        sys.exit(f"[错误] 清单签名后自校验失败：{reason}")
    scoop = build_scoop(version, zip_path, manifest["assets"][0]["sha256"])

    write_json(MANIFEST_OUT, manifest)
    write_json(SCOOP_DIR / f"{APP_NAME.lower()}.json", scoop)

    if NOTES_FILE.is_file():
        try:
            from release import validate_release_notes
            notes_err = validate_release_notes(manifest["notes"], NOTES_FILE.name)
            if notes_err:
                print(f"[清单] 警告：更新说明未就绪（{notes_err}），发布前请编辑 {NOTES_FILE.name}")
        except Exception:
            pass

    roles = {a["role"]: a for a in manifest["assets"]}
    print(f"[清单] v{version} 应用包 {roles['app']['name']}"
          f"（{roles['app']['size'] / 1048576:.1f} MB）")
    print(f"[清单] → {MANIFEST_OUT}")
    print(f"[清单] → {SCOOP_DIR / f'{APP_NAME.lower()}.json'}")
    print(f"[发布] 下一步：python scripts/publish_release.py（创建 Release 并上传）")
    return manifest


def resolve_signing_key(explicit: str | None) -> Path:
    key = Path(explicit or os.environ.get("HOLDEXAR_UPDATE_SIGNING_KEY", ""))
    if str(key) and key.is_file():
        return key
    sys.exit(
        "[错误] 缺少更新签名私钥（--key 或环境变量 HOLDEXAR_UPDATE_SIGNING_KEY）。\n"
        "       生成：python scripts/build_manifest.py --generate-key <私钥路径>\n"
        "       再把打印出的公钥 hex 登记进 server/app/core/update_signing.py"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 更新清单生成")
    parser.add_argument("--zip", help="应用包路径（默认取 release/ 内最新的）")
    parser.add_argument("--version", help="覆盖版本号（默认读后端 app.core.config）")
    parser.add_argument("--key", help="签名私钥路径（默认读 HOLDEXAR_UPDATE_SIGNING_KEY）")
    parser.add_argument("--generate-key", metavar="私钥路径",
                        help="生成新的 Ed25519 签名密钥对并打印公钥（不生成清单）")
    args = parser.parse_args()
    if args.generate_key:
        out = Path(args.generate_key)
        pub_hex = update_signing.generate_keypair(out)
        print(f"[签名] 私钥已写入：{out}")
        print(f"[签名] 公钥 hex：{pub_hex}")
        print("[签名] 下一步：把公钥登记进 server/app/core/update_signing.py 的 TRUSTED_PUBKEYS")
        return
    build(args.zip, args.version, args.key)


if __name__ == "__main__":
    main()
