"""proxypool 接口面：生产作业台账 + 订阅级生产准入（晋升）。

台账是只读的；**唯一**的写入口是「订阅晋升」——把 CANDIDATE 订阅的最近一次成功
快照 apply 进 Registry，并在同一事务里把它置为 ACTIVE（见 `admission.py`）。
阈值、自动晋升/降级、候选隔离内核都还没有事实基础（定稿见
`docs/PROXYPOOL_HANDOVER_P1.7_NEXT.md` §15 与 Active/Candidate 分层调查结论）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
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


@router.post("/subscriptions/{subscription_id}/promote")
async def promote_subscription(subscription_id: int):
    """Candidate → Active：**同一事务**内「apply 最近一次成功快照 + 置 ACTIVE」。

    没有成功快照即拒绝（409），不重新抓取、不用失败快照或旧缓存——避免出现
    「库说 ACTIVE、Registry 里没有来源」的中间事实。已是 ACTIVE 时为幂等空转。
    """
    from app.domains.proxypool.admission import PromotionError, promote_to_active

    async with get_session_factory()() as session:
        try:
            result = await promote_to_active(
                session,
                subscription_id=subscription_id,
                data_dir=Path(get_settings().data_dir),
                now=datetime.now(),
            )
            await session.commit()
        except PromotionError as e:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "subscriptionId": result.subscription_id,
        "promoted": result.promoted,
        "detail": result.detail,
        "snapshotSha256": result.snapshot_sha256,
        "appliedNodes": result.applied_nodes,
        "admissionStatus": "ACTIVE",
    }


@router.post("/subscriptions/{subscription_id}/exit")
async def exit_subscription(subscription_id: int):
    """Active → 退出生产池：**同一事务**内置回 CANDIDATE 并移除该订阅的全部来源行。

    不物理删除订阅行（以后可再 Promote）。`ProxyNode` 身份账本保留：没有任何当前
    来源的节点按合格集口径自然退出池，仍被别的订阅提供的节点不受影响。
    已是 CANDIDATE 时为幂等空转。
    """
    from app.domains.proxypool.admission import PromotionError, exit_from_production

    async with get_session_factory()() as session:
        try:
            result = await exit_from_production(session, subscription_id=subscription_id)
            await session.commit()
        except PromotionError as e:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "subscriptionId": result.subscription_id,
        "exited": result.exited,
        "detail": result.detail,
        "removedSources": result.removed_sources,
        "admissionStatus": "CANDIDATE",
    }