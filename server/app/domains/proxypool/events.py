"""编排事件流水（`orchestration_events`）：生产生命周期的事实留痕。

这个模块只做一件事：把生产路径上**已经发生**的事实写成一行事件。它不判对错、
不聚合、不参与任何决策——事件是给事后还原用的，不是生产依赖。

事件类型是固定枚举，一处定义（本模块常量）：
订阅同步失败、订阅晋升、订阅退出、对账拒绝。新增类型前先确认真实生产路径存在，
不为「看起来完整」预先造类型。

两类写入形态，同一个函数按调用方给不给 session 分流：

- **同事务**（传 `session`）：事件与它描述的业务行一起提交/回滚。适用于业务事实
  本身就写在同一个事务里的场景——两者同生共死，不会出现「事件说发生了、业务行
  却没有」的形态。
- **独立会话**（不传 `session`）：调用方的事务已经提交，或即将因异常回滚。抛异常
  的路径走这一支——否则失败证据会随调用方的回滚一起消失，而这正是最需要留下的
  那一类事实。

写入一律 fail-soft：观测不得成为生产依赖（与 `jobruns` 同一约定），写不进去只记
日志，绝不让调用方失败。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.domains.proxypool.models import OrchestrationEvent

logger = logging.getLogger(__name__)

# ── 事件类型（固定枚举）────────────────────────────────────────────
KIND_SUBSCRIPTION_FAILED = "subscription_failed"
KIND_SUBSCRIPTION_PROMOTED = "subscription_promoted"
KIND_SUBSCRIPTION_EXITED = "subscription_exited"
KIND_RECONCILE_REJECTED = "reconcile_rejected"

# ── 级别 ──────────────────────────────────────────────────────────
LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"

# 单行 message 上限：异常串可能带 URL / 无界长度，截断后再落库
_MESSAGE_MAX = 1000


async def record(
    kind: str,
    message: str,
    *,
    level: str = LEVEL_INFO,
    payload: dict | None = None,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> bool:
    """追加一行事件。返回是否写成功；任何异常都只记日志。

    `payload` 只放**能直接从运行上下文取得**的结构化字段（id、计数、状态），
    不放异常原文——原文进 message，截断后仍保持一行有界。
    """
    row = OrchestrationEvent(
        ts=now or datetime.now(),
        kind=kind,
        level=level,
        message=(message or "")[:_MESSAGE_MAX],
        payload_json=dict(payload) if payload else None,
    )
    try:
        if session is not None:
            # 事务边界归调用方：这里只 add，不 commit
            session.add(row)
            return True
        async with get_session_factory()() as own:
            own.add(row)
            await own.commit()
        return True
    except Exception:  # noqa: BLE001 —— 观测失败不得影响生产
        logger.exception("[编排事件] 写入失败（kind=%s）", kind)
        return False
