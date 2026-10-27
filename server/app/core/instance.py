"""同一数据目录的生产进程独占。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from app.core.database import _CrossProcessWriteLock


@asynccontextmanager
async def production_instance(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    lease = _CrossProcessWriteLock(data_dir.resolve() / "production.lock")
    try:
        try:
            async with asyncio.timeout(0.2):
                await lease.acquire()
        except TimeoutError as exc:
            raise RuntimeError("此数据目录已有后台服务，请打开正在运行的应用") from exc
        yield
    finally:
        lease.release()
