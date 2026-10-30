"""价格周期与事件按引用保护分块保留。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.database import WritePriority, write_session
from app.core.logging import log_event

logger = logging.getLogger(__name__)

# 整行保留窗：90 天（与 proxypool 遥测保留同值）
ROW_DAYS = 90
_CHUNK = 50
_MAX_CHUNKS = 20


def unfinished_job_units_sql(alias: str = "crawl_jobs") -> str:
    return (f"CASE WHEN json_valid({alias}.stats_json) THEN "
            f"CASE WHEN {alias}.status IN ('failed','interrupted') THEN "
            f"COALESCE(json_array_length({alias}.stats_json, '$.targets.appids'), 0) * "
            f"COALESCE(json_array_length({alias}.stats_json, '$.targets.regions'), 0) + "
            f"COALESCE(json_array_length({alias}.stats_json, '$.targets.tasks'), 0) "
            "ELSE 0 END ELSE 0 END > 0 "
            f"AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id={alias}.cycle_id AND c.status='cancelled')")


async def _chunked_execute(
    sql: str, params: dict, *, chunk: int, max_chunks: int
) -> tuple[int, bool]:
    """分块执行同一形态的 DELETE/UPDATE（IN 子查询按 id 截块）。

    返回 (累计影响行数, 是否被块上限截断)；每块一个事务过写闸。
    """
    affected_total = 0
    for _ in range(max_chunks):
        async with write_session(WritePriority.BACKGROUND, "crawl_prune") as session:
            result = await session.execute(text(sql), {**params, "chunk": chunk})
            await session.commit()
        affected = int(result.rowcount or 0)
        affected_total += affected
        if affected < chunk:
            return affected_total, False
    return affected_total, True


async def prune_price_cycles(
    now: datetime | None = None,
    *,
    row_days: int = ROW_DAYS,
    chunk: int = _CHUNK,
    max_chunks: int = _MAX_CHUNKS,
) -> dict:
    """分块清理无未完成工作或通知引用的过期周期与价格事件。"""
    cutoff = (now or datetime.now()) - timedelta(days=row_days)
    from app.domains.crawl.cycle import pending_cycle_ids

    pending = sorted(await pending_cycle_ids())
    params = {f"pending_{i}": cid for i, cid in enumerate(pending)}
    keep_pending = ""
    if params:
        keep_pending = " AND id NOT IN (" + ",".join(":" + key for key in params) + ")"
    events_deleted, truncated_events = await _chunked_execute(
        "DELETE FROM price_events WHERE id IN ("
        "SELECT id FROM price_events"
        " WHERE occurred_at IS NOT NULL AND occurred_at < :cutoff"
        " AND NOT EXISTS (SELECT 1 FROM notification_candidates n"
        " WHERE n.source_kind='price_event' AND n.event_id=price_events.id)"
        " AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id=price_events.cycle_id"
        " AND json_extract(c.notification_scan_json, '$.state') IN ('pending','scanning'))"
        " ORDER BY id LIMIT :chunk)",
        {"cutoff": cutoff}, chunk=chunk, max_chunks=max_chunks,
    )
    rows_deleted, truncated_rows = await _chunked_execute(
        "DELETE FROM price_cycles WHERE id IN ("
        "SELECT id FROM price_cycles"
        " WHERE started_at IS NOT NULL AND started_at < :cutoff"
        " AND status IN ('completed','partial','failed','cancelled')"
        " AND NOT EXISTS (SELECT 1 FROM price_events e WHERE e.cycle_id=price_cycles.id)"
        " AND NOT EXISTS (SELECT 1 FROM notification_candidates n WHERE n.cycle_id=price_cycles.id)"
        " AND NOT EXISTS (SELECT 1 FROM crawl_jobs j WHERE j.cycle_id=price_cycles.id)"
        " AND COALESCE(json_extract(notification_scan_json, '$.state'),'') NOT IN ('pending','scanning')"
        + keep_pending + " ORDER BY id LIMIT :chunk)",
        {"cutoff": cutoff, **params},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    if rows_deleted or events_deleted:
        log_event(
            logger,
            "价格周期台账保留完成，回收过窗的周期记录与价格事件",
            detail={
                "删除行数": rows_deleted,
                "价格事件": events_deleted,
            },
        )
    return {
        "rows_deleted": rows_deleted,
        "events_deleted": events_deleted,
        "truncated": truncated_rows or truncated_events,
    }
