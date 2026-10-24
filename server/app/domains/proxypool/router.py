"""proxypool 接口面：生产作业台账与体检台账（只读）。

订阅的晋升/退出不走本路由：生产路径是首检健康自动转正与订阅删除级联退出
（见 `proxies/router.py` 与 `proxies/service.py`）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.proxypool import exitstats, jobruns
from app.domains.proxypool.models import HealthRun

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
    payload["exits"] = await exitstats.run_exit_rows(run_id)
    return payload


@router.get("/health-runs")
async def health_runs(limit: int = Query(20, ge=1, le=50)):
    """体检台账：最近 N 轮（started_at 降序）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(HealthRun)
                .order_by(HealthRun.started_at.desc(), HealthRun.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    return {"runs": [_health_run_payload(row) for row in rows]}


def _health_run_payload(row: HealthRun) -> dict:
    return {
        "id": row.id,
        "channel": row.channel,
        "subscriptionId": row.subscription_id,
        "startedAt": row.started_at.isoformat() if row.started_at else None,
        "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        "durationMs": row.duration_ms,
        "total": row.total,
        "steamOk": row.steam_ok,
        "ipKnown": row.ip_known,
        "failed": row.failed,
        "endReason": row.end_reason,
    }
