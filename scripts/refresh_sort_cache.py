"""刷新 games 排序缓存预计算列（min_cny_fen / diff_fen）。

排序缓存是 games 表上的预计算列，本脚本是它的手动维护入口
（服务层实现见 app.domains.games.service.refresh_sort_cache）。

用法（项目根，用 server 虚拟环境）：
    server/.venv/Scripts/python.exe scripts/refresh_sort_cache.py              # 全库
    server/.venv/Scripts/python.exe scripts/refresh_sort_cache.py 620,570     # 增量
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.core.database import init_db  # noqa: E402
from app.domains.games.service import refresh_sort_cache  # noqa: E402


async def main() -> None:
    appids = [int(a) for a in sys.argv[1].split(",")] if len(sys.argv) > 1 else None
    await init_db()
    t0 = time.perf_counter()
    n = await refresh_sort_cache(appids)
    print(f"排序缓存已刷新 {n} 行（{'增量 ' + str(len(appids)) + ' 款' if appids else '全库'}）"
          f"耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
