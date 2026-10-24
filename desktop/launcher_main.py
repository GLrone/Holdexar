"""Holdexar 桌面轻量启动器入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.launcher import run_launcher  # noqa: E402
from app.core.update_host import report_data_protocol  # noqa: E402


def main() -> None:
    if report_data_protocol(sys.argv[1:]):
        return
    parser = argparse.ArgumentParser(description="Holdexar 启动器")
    parser.add_argument("--server", action="store_true", help="无窗口模式，仅启动本地服务")
    parser.parse_args()
    sys.exit(run_launcher(sys.argv[1:]))


if __name__ == "__main__":
    main()
