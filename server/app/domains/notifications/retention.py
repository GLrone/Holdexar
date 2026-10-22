"""通知结果保留：保留可继续投递的候选，清理过期的最终结果。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.database import WritePriority, write_session
from app.core.logging import log_event
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.policy import DELIVERED, FAILED, MAX_ATTEMPTS, UNKNOWN
from app.domains.notifications.service import _recover_stuck_sending, retryable

logger = logging.getLogger(__name__)

AUDIT_DAYS = 365
_CHUNK = 500
_MAX_CHUNKS = 20


async def prune_notification_candidates(
    now: datetime | None = None,
    *,
    retention_days: int = AUDIT_DAYS,
    chunk: int = _CHUNK,
    max_chunks: int = _MAX_CHUNKS,
) -> dict[str, int | bool]:
    """回收一年以前的已结束通知结果，保留待投递与可重试候选。"""
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    recovered = await _recover_stuck_sending(now)
    created_at = func.coalesce(
        NotificationCandidate.updated_at,
        NotificationCandidate.created_at,
    )
    cursor = 0
    deleted = 0
    truncated = False

    for index in range(max_chunks):
        async with write_session(WritePriority.BACKGROUND, "notification_retention") as session:
            rows = (
                await session.execute(
                    select(
                        NotificationCandidate.id,
                        NotificationCandidate.status,
                        NotificationCandidate.attempts,
                        NotificationCandidate.last_error,
                    )
                    .where(
                        NotificationCandidate.id > cursor,
                        created_at < cutoff,
                        NotificationCandidate.status.in_((DELIVERED, UNKNOWN, FAILED)),
                    )
                    .order_by(NotificationCandidate.id)
                    .limit(chunk)
                )
            ).all()
            if not rows:
                break
            cursor = int(rows[-1].id)
            terminal_ids = [
                int(row.id)
                for row in rows
                if row.status in (DELIVERED, UNKNOWN)
                or (
                    row.status == FAILED
                    and (
                        int(row.attempts or 0) >= MAX_ATTEMPTS
                        or not retryable(row.last_error)
                    )
                )
            ]
            if terminal_ids:
                result = await session.execute(
                    delete(NotificationCandidate).where(
                        NotificationCandidate.id.in_(terminal_ids),
                        created_at < cutoff,
                        NotificationCandidate.status.in_((DELIVERED, UNKNOWN, FAILED)),
                    )
                )
                await session.commit()
                deleted += int(result.rowcount or 0)
        if len(rows) < chunk:
            break
        if index == max_chunks - 1:
            truncated = True

    if recovered or deleted or truncated:
        log_event(
            logger,
            "通知记录保留清理完成",
            detail={
                "超时结果未知": recovered,
                "过期结果删除": deleted,
                "达到单轮上限": truncated,
            },
        )
    return {"recovered": recovered, "deleted": deleted, "truncated": truncated}
