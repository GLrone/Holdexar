"""轻量级文档与构建一致性检查。

用途：
    在 README 或发布脚本变更时，快速核验版本号、打包资产说明、种子描述、
    调度配置、安全建议合规性及许可证文件存在性。
    纯标准库实现，运行耗时 < 0.2 秒，无需运行全量 CI。

用法：
    python scripts/check_docs_consistency.py

退出码：
    0 全部一致 / 1 存在冲突或缺失
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧/GBK 环境防乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
README_FILE = ROOT / "README.md"
BUILD_RELEASE_FILE = ROOT / "scripts" / "build_release.py"
SCHEDULER_FILE = SERVER / "app" / "core" / "scheduler.py"
APP_INFO_FILE = SERVER / "app" / "core" / "app_info.py"


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def check_version_consistency() -> tuple[bool, str]:
    app_info_text = _read_text(APP_INFO_FILE)
    m_ver = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', app_info_text)
    if not m_ver:
        return False, f"无法在 {APP_INFO_FILE.name} 中解析 APP_VERSION"
    version = m_ver.group(1)

    readme_text = _read_text(README_FILE)
    m_badge = re.search(r"version-v([^-\)]+)(?:--([a-zA-Z0-9.]+))?", readme_text)
    if not m_badge:
        return False, "README.md 中未找到 Version 徽章"

    badge_ver = m_badge.group(1)
    if m_badge.group(2):
        badge_ver = f"{badge_ver}-{m_badge.group(2)}"

    if badge_ver != version:
        return False, f"版本号不一致：代码定义为 {version}，README 徽章为 {badge_ver}"

    return True, f"版本号一致：v{version}"


def check_scheduler_jobs_consistency() -> tuple[bool, str]:
    sched_text = _read_text(SCHEDULER_FILE)
    actual_jobs = len(re.findall(r"scheduler\.add_job\(", sched_text))
    if actual_jobs == 0:
        return False, "未能从 scheduler.py 统计到定时任务"

    readme_text = _read_text(README_FILE)
    m_jobs = re.search(r"APScheduler\((\d+)\s*个定时任务\)", readme_text)
    if not m_jobs:
        return False, "README.md 技术架构表中未找到 APScheduler 定时任务数量描述"

    claimed_jobs = int(m_jobs.group(1))
    if claimed_jobs != actual_jobs:
        return False, f"调度任务数不一致：代码实际注册 {actual_jobs} 个，README 声称 {claimed_jobs} 个"

    return True, f"调度定时任务数一致：{actual_jobs} 个"


def check_license_files_presence() -> tuple[bool, str]:
    required_files = [
        ROOT / "LICENSE",
        ROOT / "packaging" / "licenses" / "LICENSE-mihomo.txt",
    ]
    missing = [f.relative_to(ROOT).as_posix() for f in required_files if not f.is_file() or f.stat().st_size == 0]
    if missing:
        return False, f"关键许可证文件缺失/为空：{', '.join(missing)}"

    readme_text = _read_text(README_FILE)
    if "mihomo" not in readme_text or "GPL-3.0" not in readme_text:
        return False, "README.md 缺少对外部 mihomo 内核的开源致谢与 GPL-3.0 声明"

    return True, "关键许可证文件完整有效，README 致谢已对齐"


def check_release_assets_consistency() -> tuple[bool, str]:
    readme_text = _read_text(README_FILE)

    # 检查是否存在同一事实的相反描述
    if "发布包已内置" in readme_text and "种子" in readme_text:
        return False, "README 存在矛盾描述：发布包为轻量包，不内置种子，应通过 holdexar_seed.db.gz 按需下载"

    if "holdexar_seed.db.gz" not in readme_text:
        return False, "README 缺少对种子分发压缩包 holdexar_seed.db.gz 的正确描述"

    build_text = _read_text(BUILD_RELEASE_FILE)
    if "holdexar_seed.db.gz" not in build_text or "latest.json" not in build_text:
        return False, "build_release.py 产物定义中缺少种子或清单资产"

    return True, "打包资产与种子内容描述一致（轻量包模式 + holdexar_seed.db.gz 独立资产）"


def check_safety_guidance() -> tuple[bool, str]:
    readme_text = _read_text(README_FILE)

    dangerous_phrases = [
        "关闭该功能",
        "关闭此功能",
        "把整个程序目录加入杀软白名单",
        "把整个程序目录加入白名单",
    ]
    hits = [p for p in dangerous_phrases if p in readme_text]
    if hits:
        return False, f"README 包含不合规的安全防护建议（违背安全最佳实践）：{', '.join(hits)}"

    if "Get-FileHash" not in readme_text or "SHA256" not in readme_text:
        return False, "README 安全建议中缺少基于 SHA256 哈希核验的核心指引"

    return True, "安全防护建议专业合规（推荐哈希核验与源码运行，无关闭防护指引）"


def main() -> int:
    checks = [
        ("版本号一致性", check_version_consistency),
        ("调度任务数量一致性", check_scheduler_jobs_consistency),
        ("许可证文件存在性", check_license_files_presence),
        ("打包资产与种子描述一致性", check_release_assets_consistency),
        ("安全防护建议合规性", check_safety_guidance),
    ]

    all_passed = True
    print("[文档与构建一致性检查]")
    for title, func in checks:
        ok, msg = func()
        status = "[OK]  " if ok else "[FAIL]"
        print(f"  {status} {title}: {msg}")
        if not ok:
            all_passed = False

    if all_passed:
        print("[完成] 所有技术承诺均与源码及构建行为保持完全一致。")
        return 0

    print("[错误] 存在不一致项，请根据上述提示修正。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
