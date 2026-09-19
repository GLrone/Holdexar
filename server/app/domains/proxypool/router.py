"""proxypool 只读面：生产作业台账（前端 `/proxies` 的「生产作业」分节）。

只读是**刻意**的：这一版没有任何「操作健康/池」的入口。阈值、判死、切换都还没有
事实基础（定稿见 `docs/PROXYPOOL_HANDOVER_P1.7_NEXT.md` §13）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.core.database import get_session_factory
from app.domains.proxypool import jobruns

router = APIRouter(prefix="/proxypool", tags=["proxypool"])


@router.get("/job-runs")
async def job_runs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """列表 + 今日概览一次返回（省一次往返）。"""
    async with get_session_factory()() as session:
        return await jobruns.list_runs(
            session, limit=limit, offset=offset, now=datetime.now()
        )


@router.get("/job-runs/{run_id}")
async def job_run_detail(run_id: int):
    async with get_session_factory()() as session:
        payload = await jobruns.get_run(session, run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="作业记录不存在")
    return payload