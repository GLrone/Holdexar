"""刷新 games.series_id（同系列归组：本地名称聚类，零网络开销）。

series_id 是 games 表上的归组列，本脚本是它的手动维护入口
（服务层实现见 app.domains.games.series.refresh_series）。

用法（项目根，用 server 虚拟环境）：
    server/.venv/Scripts/python.exe scripts/refresh_series.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.core.database import init_db  # noqa: E402
from app.domains.games.series import refresh_series  # noqa: E402


async def main() -> None:
    await init_db()
    t0 = time.perf_counter()
    n = await refresh_series()
    print(f"系列归组已刷新 {n} 行，耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
