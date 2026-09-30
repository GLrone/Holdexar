"""内容链事实通知：变更判定产生的用户可感知事实 → 岛/渠道的单一事实源。

判定只发生在数据链路里（metadata 域 HB 记账点 / Epic 快照替换点），本模块
只负责落行与增量读取——消费端（灵动岛轮询、后续渠道）不重复判断「变没变」。
与候选层（service.py）的关系：候选是价格事件的**通知资格**，这里的事实
**本身就是变化**，不需要策略判断，也没有静默期与重试。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, get_session_factory
from app.core.database import write_gate
from app.domains.notifications.models import FactNotice

logger = logging.getLogger(__name__)

# 单次增量读取上限（轮询节拍 60s 下绰绰有余；防异常积压拖垮响应）
FACTS_LIMIT = 50


async def record_fact(
    source: str, kind: str, fact_key: str, data: dict | None = None
) -> bool:
    """落一条事实（幂等：fact_key 已存在即跳过）。返回是否新落。

    任何失败都不上抛：事实记录是数据链路的下游副作用，落不进只少一条
    岛上消息，不能拖垮调用它的抓取/记账主流程。
    """
    try:
        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            stmt = (
                sqlite_insert(FactNotice)
                .values(
                    source=source,
                    kind=kind,
                    fact_key=fact_key,
                    data=data,
                    occurred_at=datetime.now(),
                )
                .on_conflict_do_nothing(index_elements=["fact_key"])
            )
            cursor = await session.execute(stmt)
            await session.commit()
        return bool(cursor.rowcount)
    except Exception:  # noqa: BLE001 —— 事实落库失败不影响数据链路
        logger.exception("[事实通知] 落行失败（source=%s key=%s）", source, fact_key)
        return False


async def list_facts(after_id: int | None = None, limit: int = FACTS_LIMIT) -> dict:
    """after_id 之后的增量事实（升序）+ 当前最大 id。

    after_id 为 NULL = 只对齐：回空增量与 latestId，消费端记下 latestId
    作为下一次的游标（升级后的第一轮不回放历史事实）。
    """
    async with get_session_factory()() as session:
        latest = (
            await session.execute(select(FactNotice.id).order_by(FactNotice.id.desc()).limit(1))
        ).scalar()
        if after_id is None:
            rows: list[FactNotice] = []
        else:
            rows = list(
                (
                    await session.execute(
                        select(FactNotice)
                        .where(FactNotice.id > int(after_id))
                        .order_by(FactNotice.id)
                        .limit(limit)
                    )
                ).scalars().all()
            )
    return {
        "latestId": int(latest or 0),
        "items": [
            {
                "id": row.id,
                "source": row.source,
                "kind": row.kind,
                "data": row.data,
                "occurredAt": row.occurred_at.isoformat() if row.occurred_at else None,
            }
            for row in rows
        ],
    }
