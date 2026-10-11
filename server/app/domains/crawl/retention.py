"""价格周期台账保留：把调度账本最大无界点（coverage_json 明细 + 老轮整行）
收敛为有界稳态。

两段清理（每日 04:37 一拍）：
- 整行删除 90 天窗外的 price_cycles（对齐同窗遥测先例）；
- 存量 JSON 明细（coverage_json，v11 时代冻结快照）除最近 8 轮外置 NULL——
  新轮不再写明细（读路径走统计列），保留最近 8 轮作为回滚缓冲。

分块删除与置空：每块一个事务过写闸，块间写者位空出；单轮块上限内做不完
留给下一拍。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.database import WritePriority, get_session_factory, write_gate
from app.core.logging import log_event

logger = logging.getLogger(__name__)

# 整行保留窗：90 天（与 proxypool 遥测保留同值）
ROW_DAYS = 90
# 存量明细保留轮数：≈2 天 6h 网格轮 + 手动轮余量（回滚缓冲）
DETAIL_KEEP = 8
_CHUNK = 50
_MAX_CHUNKS = 20


async def _chunked_execute(
    sql: str, params: dict, *, chunk: int, max_chunks: int
) -> tuple[int, bool]:
    """分块执行同一形态的 DELETE/UPDATE（IN 子查询按 id 截块）。

    返回 (累计影响行数, 是否被块上限截断)；每块一个事务过写闸。
    """
    affected_total = 0
    for _ in range(max_chunks):
        async with write_gate(
            WritePriority.BACKGROUND, "crawl_prune"
        ), get_session_factory()() as session:
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
    detail_keep: int = DETAIL_KEEP,
    chunk: int = _CHUNK,
    max_chunks: int = _MAX_CHUNKS,
) -> dict:
    """整行清理窗外老轮 + 置空存量 JSON 明细（保留最近 detail_keep 轮）。

    被置空的行由统计列读路径继续正确服务（聚合精确、拆分合并）；
    返回 {"rows_deleted", "details_trimmed", "truncated"}。
    """
    cutoff = (now or datetime.now()) - timedelta(days=row_days)
    rows_deleted, truncated_rows = await _chunked_execute(
        "DELETE FROM price_cycles WHERE id IN ("
        "SELECT id FROM price_cycles"
        " WHERE started_at IS NOT NULL AND started_at < :cutoff"
        " ORDER BY id LIMIT :chunk)",
        {"cutoff": cutoff},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    details_trimmed, truncated_details = await _chunked_execute(
        "UPDATE price_cycles SET coverage_json = NULL WHERE id IN ("
        "SELECT id FROM price_cycles"
        " WHERE coverage_json IS NOT NULL AND id NOT IN ("
        "  SELECT id FROM price_cycles WHERE coverage_json IS NOT NULL"
        "  ORDER BY id DESC LIMIT :keep)"
        " ORDER BY id LIMIT :chunk)",
        {"keep": detail_keep},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    if rows_deleted or details_trimmed:
        log_event(
            logger,
            "价格周期台账保留完成，回收过窗的周期记录与存量明细",
            detail={
                "删除行数": rows_deleted,
                "置空明细轮数": details_trimmed,
            },
        )
    return {
        "rows_deleted": rows_deleted,
        "details_trimmed": details_trimmed,
        "truncated": truncated_rows or truncated_details,
    }
