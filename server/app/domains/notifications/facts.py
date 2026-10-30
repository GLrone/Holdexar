"""内容变化事实与其通知资格同事务保存，增量读取供用户界面消费。"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.domains.notifications.models import FactNotice

logger = logging.getLogger(__name__)

# 单次增量读取上限（轮询节拍 60s 下绰绰有余；防异常积压拖垮响应）
FACTS_LIMIT = 50
_CONTENT_TYPES = {("hb_choice", "bundle_changed"): "HB_CHOICE",
                  ("epic_free", "free_rotation"): "EPIC_FREE"}


async def record_fact(
    source: str, kind: str, fact_key: str, data: dict | None = None
) -> bool:
    """新事实与通知资格原子落库；重复键跳过，失败回滚并返回False。"""
    try:
        now = datetime.now()
        async with write_session(WritePriority.BACKGROUND) as session:
            stmt = (
                sqlite_insert(FactNotice)
                .values(
                    source=source,
                    kind=kind,
                    fact_key=fact_key,
                    data=data,
                    occurred_at=now,
                )
                .on_conflict_do_nothing(index_elements=["fact_key"])
                .returning(FactNotice.id)
            )
            fact_id = (await session.execute(stmt)).scalar_one_or_none()
            event_type = _CONTENT_TYPES.get((source, kind))
            if fact_id is not None and event_type:
                from . import policy, service

                content = data or {}
                payload = {**content, "eventType": event_type, "source": source, "kind": kind,
                           "title": content.get("title") or content.get("productName")
                           or content.get("label") or policy.TYPE_LABELS[event_type],
                           "items": content.get("items") or [],
                           "labels": content.get("labels") or [policy.TYPE_LABELS[event_type]],
                           "observedAt": now.isoformat()}
                await service.enqueue_facts("fact_notice", [{
                    "id": fact_id, "cycleId": 0, "appid": int(content.get("appid") or 0),
                    "region": None, "eventType": event_type, "category": policy.category_of(event_type),
                    "payload": payload, "occurredAt": now,
                }], session=session)
            await session.commit()
        return fact_id is not None
    except Exception:  # noqa: BLE001 —— 事实落库失败不影响数据链路
        log_event(
            logger,
            "一条变化及其通知资格未能保存",
            level=logging.ERROR,
            exc_info=True,
            detail={"来源": source, "类型": kind, "事实键": fact_key},
        )
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
