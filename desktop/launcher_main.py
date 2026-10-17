"""Holdexar 桌面轻量启动器入口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import argparse

from app.core.launcher import run_launcher
from app.core.update_host import bridge_legacy_update


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 启动器")
    parser.add_argument(
        "--apply-update",
        action="store_true",
        help="执行待安装的更新交接并迁移至新架构（供旧版本更新器调用）",
    )
    parser.add_argument("--staging", help=argparse.SUPPRESS)
    parser.add_argument("--target", help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--attempt-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--current-version", default=None, help=argparse.SUPPRESS)
    args, unknown = parser.parse_known_args()

    if getattr(args, "apply_update", False) and args.staging and args.target:
        ok = bridge_legacy_update(
            staging=Path(args.staging),
            target=Path(args.target),
            wait_pid=args.wait_pid,
            attempt_id=getattr(args, "attempt_id", None),
            data_dir=Path(args.data_dir) if args.data_dir else None,
            current_version=getattr(args, "current_version", None),
        )
        sys.exit(0 if ok else 1)

    code = run_launcher(unknown)
    sys.exit(code)


if __name__ == "__main__":
    main()
