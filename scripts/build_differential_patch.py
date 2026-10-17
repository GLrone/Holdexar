"""命令行工具：基于版本构建产物生成文件级差量包。

用法：
    python scripts/build_differential_patch.py --base <旧版本目录> --target <新版本目录> --from-ver 0.3.4-beta --to-ver 0.3.5-beta
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.app_info import APP_NAME
from app.core.differential_patch import create_differential_patch, verify_patch_archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 文件级差量更新包制作工具")
    parser.add_argument("--base", type=Path, required=True, help="基准旧版本目录")
    parser.add_argument("--target", type=Path, required=True, help="目标新版本目录")
    parser.add_argument("--from-ver", type=str, required=True, help="源版本号，例如 0.3.4-beta")
    parser.add_argument("--to-ver", type=str, required=True, help="目标版本号，例如 0.3.5-beta")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出差量 zip 路径（默认写入 release/Holdexar-patch-v{from}-to-v{to}.zip）",
    )

    args = parser.parse_args()

    out_zip = args.output
    if not out_zip:
        out_zip = ROOT / "release" / f"{APP_NAME}-patch-v{args.from_ver}-to-v{args.to_ver}.zip"

    print(f"[差量] 正在对比：\n  基准版本: {args.from_ver} ({args.base})\n  目标版本: {args.to_ver} ({args.target})")
    zip_path, manifest = create_differential_patch(
        base_dir=args.base,
        target_dir=args.target,
        from_version=args.from_ver,
        to_version=args.to_ver,
        output_zip=out_zip,
    )

    summary = manifest["summary"]
    patch_size = summary["patchFileBytes"]
    total_target = summary["totalTargetBytes"]
    ratio = (1 - (patch_size / total_target)) * 100 if total_target else 0

    print(
        f"[差量] 制作完成：{zip_path.name}\n"
        f"  目标文件总数: {summary['totalTargetFiles']} 个 ({total_target / 1048576:.2f} MB)\n"
        f"  新增文件: {summary['addedCount']} 个\n"
        f"  修改文件: {summary['modifiedCount']} 个\n"
        f"  删除文件: {summary['removedCount']} 个\n"
        f"  未变文件: {summary['unchangedCount']} 个\n"
        f"  差量包大小: {patch_size / 1048576:.2f} MB (较完整包节省 {ratio:.1f}% 体积)\n"
        f"  差量包 SHA-256: {manifest['patchSha256']}"
    )

    # 验证自洽性
    verify_patch_archive(zip_path)
    print("  ✓ 差量包内部完整性自检通过")


if __name__ == "__main__":
    main()
