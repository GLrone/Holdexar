"""更新清单生成：release/latest.json（分发机制的稳定入口）+ Scoop 渠道清单。

清单挂在固定 tag（见 app_info.MANIFEST_TAG），校验值与确切资产名随清单
一并下发，客户端检查更新不依赖 GitHub API；assets[] 同时声明种子资产的
体积与校验值。清单 Release 用 `--latest=false` 创建，不会占用
`releases/latest`，种子的 `releases/latest/download/` 通道不受影响。

产出：
- release/latest.json         —— 上传到 tag `updater` 的 Release（版本 Release 也放一份）
- release/scoop/holdexar.json —— Scoop 渠道清单，提交进 bucket 仓库即生效

用法：
    python scripts/build_manifest.py                 # 自动取 release/ 内应用包
    python scripts/build_manifest.py --zip <路径>     # 指定应用包
    python scripts/build_manifest.py --version 0.1.0 # 覆盖版本号（默认读后端配置）

清单 schema 变更时同步递增 app_info.MANIFEST_SCHEMA，客户端按需兼容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
RELEASE = ROOT / "release"
SEED_DB = ROOT / "assets" / "seed" / "holdexar_seed.db"
SCOOP_DIR = RELEASE / "scoop"
NOTES_FILE = RELEASE / "RELEASE_NOTES.md"
MANIFEST_OUT = RELEASE / "latest.json"

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
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


def app_version() -> str:
    """版本号取自单一来源 app.core.app_info.APP_VERSION。"""
    return APP_VERSION


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_zip(explicit: str | None) -> Path:
    """应用包：显式路径优先，否则取 release/ 内**版本最高**的那个。"""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            sys.exit(f"[错误] 指定的应用包不存在：{path}")
        return path
    candidates = [p for p in RELEASE.glob("*.zip") if ZIP_PATTERN.match(p.name)]
    if not candidates:
        sys.exit(
            f"[错误] {RELEASE} 下没有应用包。先跑 scripts/build_release.py，"
            f"或用 --zip 指定路径"
        )
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def read_notes() -> str:
    """更新说明正文：release/RELEASE_NOTES.md（人工编辑的唯一入口）。"""
    if not NOTES_FILE.is_file():
        return ""
    return NOTES_FILE.read_text(encoding="utf-8").strip()


def build_manifest(version: str, zip_path: Path) -> dict:
    """清单结构：app 元信息 + assets[]（role 区分应用包与种子）。

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
    if SEED_DB.is_file():
        assets.append(
            {
                "role": "seed",
                "name": SEED_DB.name,
                "size": SEED_DB.stat().st_size,
                "sha256": sha256_of(SEED_DB),
            }
        )
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
        "license": "MIT",
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


def build(explicit_zip: str | None = None, version: str | None = None) -> dict:
    zip_path = pick_zip(explicit_zip)
    version = version or app_version()
    manifest = build_manifest(version, zip_path)
    scoop = build_scoop(version, zip_path, manifest["assets"][0]["sha256"])

    write_json(MANIFEST_OUT, manifest)
    write_json(SCOOP_DIR / f"{APP_NAME.lower()}.json", scoop)

    roles = {a["role"]: a for a in manifest["assets"]}
    print(f"[清单] v{version} 应用包 {roles['app']['name']}"
          f"（{roles['app']['size'] / 1048576:.1f} MB）")
    if "seed" in roles:
        print(f"[清单] v{version} 种子 {roles['seed']['name']}"
              f"（{roles['seed']['size'] / 1048576:.1f} MB）")
    else:
        print("[清单] 警告：未找到种子资产，本次清单不含 seed 条目")
    print(f"[清单] → {MANIFEST_OUT}")
    print(f"[清单] → {SCOOP_DIR / f'{APP_NAME.lower()}.json'}")
    print(f"[发布] 下一步：python scripts/publish_release.py（创建 Release 并上传）")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 更新清单生成")
    parser.add_argument("--zip", help="应用包路径（默认取 release/ 内最新的）")
    parser.add_argument("--version", help="覆盖版本号（默认读后端 app.core.config）")
    args = parser.parse_args()
    build(args.zip, args.version)


if __name__ == "__main__":
    main()
