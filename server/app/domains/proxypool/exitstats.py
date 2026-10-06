"""出口账本落库：作业收尾把内存里的 (出口 × 端点) 聚合写成 `proxy_run_exits` 行。

fail-soft：观测绝不能变成生产依赖——写不进去只留日志，作业结果不受影响（与
`jobruns.record_start/finish` 同规）。

每行带上「当时绑在该出口上的节点」：出口是容量身份，节点是执行者，两者都要能追。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory, write_gate
from app.core.logging import log_event

from app.domains.proxypool.models import ProxyRunExit

logger = logging.getLogger(__name__)


async def _writeback_production_evidence(
    session: AsyncSession,
    rows: list[dict],
    mapping: dict[str, str],
    now: datetime | None,
) -> None:
    """生产结果回写节点健康账本（统一账本的第三条写入通道）。

    「能不能访问 Steam」= 经该出口是否拿到 Steam 的任何响应：success>0 或
    e429/e5xx>0 都算传输成功（429/5xx 是 Steam 侧响应，不罚节点）；整行零响应
    且全是超时/连不上才是节点失败证据（fail-fast 判死，可被下一轮成功复活）。
    """
    from sqlalchemy import select

    from app.domains.proxypool.models import ProxyNode, ProxyNodeSource
    from app.domains.proxypool.state import evaluate_node_state

    by_node: dict[str, dict] = {}
    for row in rows:
        node_id = mapping.get(str(row.get("exit_ip") or ""))
        if not node_id:
            continue
        agg = by_node.setdefault(node_id, {"ok": 0, "noresp": 0})
        ok = int(row.get("success") or 0) + int(row.get("e429") or 0)             + int(row.get("e5xx") or 0)
        if ok > 0:
            agg["ok"] += 1
        elif int(row.get("timeout") or 0) + int(row.get("connect_error") or 0) > 0:
            agg["noresp"] += 1
    if not by_node:
        return
    node_rows = (await session.execute(
        select(ProxyNode).where(ProxyNode.node_id.in_(by_node))
    )).scalars().all()
    sourced = set((await session.execute(
        select(ProxyNodeSource.node_id).where(ProxyNodeSource.node_id.in_(by_node))
    )).scalars().all())
    stamp = now or datetime.now()
    for node in node_rows:
        agg = by_node[node.node_id]
        if agg["ok"] > 0:
            node.consecutive_failures = 0
            node.state = evaluate_node_state(
                node.state, source_seen=node.node_id in sourced,
                probe_ok=True, consecutive_failures=0,
            )
            node.last_l2_at = stamp
        elif agg["noresp"] > 0:
            failures = (node.consecutive_failures or 0) + 1
            node.consecutive_failures = failures
            node.state = evaluate_node_state(
                node.state, source_seen=node.node_id in sourced,
                probe_ok=False, consecutive_failures=failures,
            )
            node.last_l2_at = stamp


async def record_run_exits(
    session: AsyncSession,
    *,
    run_id: int | None,
    rows: list[dict],
    node_by_exit: dict[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """插入本次作业的出口聚合行，返回插入数。

    `run_id` 为空（作业行没建起来）时直接返回 0：没有作业身份的出口行无法归因。
    """
    if not run_id or not rows:
        return 0
    mapping = node_by_exit or {}
    await _writeback_production_evidence(session, rows, mapping, now)
    for row in rows:
        exit_ip = str(row.get("exit_ip") or "")
        session.add(ProxyRunExit(
            run_id=int(run_id),
            exit_ip=exit_ip,
            endpoint=str(row.get("endpoint") or "other"),
            node=mapping.get(exit_ip),
            requests=int(row.get("requests") or 0),
            success=int(row.get("success") or 0),
            e429=int(row.get("e429") or 0),
            e4xx=int(row.get("e4xx") or 0),
            e5xx=int(row.get("e5xx") or 0),
            timeout=int(row.get("timeout") or 0),
            connect_error=int(row.get("connect_error") or 0),
            other=int(row.get("other") or 0),
            duration_ms=int(row.get("duration_ms") or 0),
            created_at=now or datetime.now(),
        ))
    await session.commit()
    return len(rows)


async def write_run_exits(
    *,
    run_id: int | None,
    collector,
    node_by_exit: dict[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    """从 `ExitStatsCollector` 落库；任何异常只记日志。"""
    try:
        from app.core.database import WritePriority

        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            return await record_run_exits(
                session, run_id=run_id, rows=collector.rows(),
                node_by_exit=node_by_exit, now=now,
            )
    except Exception:  # noqa: BLE001 —— 出口账本写失败不改爬取行为
        log_event(
            logger,
            "出口账本落库失败，不影响本次作业结果",
            level=logging.ERROR,
            exc_info=True,
        )
        return 0


async def run_exit_rows(run_id: int) -> list[dict]:
    """读某次作业的出口账本（按出口聚合，供报表/排障）。"""
    from sqlalchemy import select

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(ProxyRunExit)
                .where(ProxyRunExit.run_id == int(run_id))
                .order_by(ProxyRunExit.exit_ip, ProxyRunExit.endpoint)
            )
        ).scalars().all()
    return [
        {
            "exitIp": r.exit_ip, "endpoint": r.endpoint, "node": r.node,
            "requests": r.requests, "success": r.success, "e429": r.e429,
            "e4xx": r.e4xx, "e5xx": r.e5xx, "timeout": r.timeout,
            "connectError": r.connect_error, "other": r.other,
            "durationMs": r.duration_ms,
        }
        for r in rows
    ]