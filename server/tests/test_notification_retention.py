from datetime import datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.retention import prune_notification_candidates

_EVENT_IDS = count(1000)


async def _candidate(
    *, status: str, age_days: int, attempts: int = 0, last_error: str | None = None
) -> int:
    created = datetime.now() - timedelta(days=age_days)
    async with get_session_factory()() as session:
        row = NotificationCandidate(
            event_id=next(_EVENT_IDS),
            cycle_id=-1,
            appid=123,
            event_type="PRICE_DROP",
            category="price",
            recipient=f"{status}-{age_days}-{attempts}@example.com",
            status=status,
            attempts=attempts,
            last_error=last_error,
            created_at=created,
            updated_at=created,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_retention_keeps_live_queue_and_retryable_failures_but_prunes_old_outcomes():
    delivered = await _candidate(status="delivered", age_days=400)
    unknown = await _candidate(status="unknown", age_days=400)
    permanent = await _candidate(
        status="failed", age_days=400, attempts=1,
        last_error="SMTPAuthenticationError: credentials rejected",
    )
    retryable = await _candidate(
        status="failed", age_days=400, attempts=1,
        last_error="ConnectionResetError: temporary disconnect",
    )
    pending = await _candidate(status="pending", age_days=400)
    suppressed = await _candidate(status="suppressed", age_days=400)
    stale_sending = await _candidate(status="sending", age_days=400)

    result = await prune_notification_candidates(
        now=datetime.now(), retention_days=365, chunk=20, max_chunks=5
    )

    async with get_session_factory()() as session:
        remaining = {
            row.id: row.status
            for row in (
                await session.execute(
                    select(NotificationCandidate)
                )
            ).scalars().all()
        }
    assert result["deleted"] == 3
    assert result["recovered"] == 1
    assert delivered not in remaining
    assert unknown not in remaining
    assert permanent not in remaining
    assert remaining[retryable] == "failed"
    assert remaining[pending] == "pending"
    assert remaining[suppressed] == "suppressed"
    assert remaining[stale_sending] == "unknown"
