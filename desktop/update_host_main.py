"""Holdexar 独立更新宿主（UpdateHost）主入口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.update_host import main


if __name__ == "__main__":
    main()
