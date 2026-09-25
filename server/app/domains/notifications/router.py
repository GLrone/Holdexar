"""通知偏好与候选的只读出口，外加投递状态与生产指标。

偏好键是用户面概念（类别 / 静默窗），内部事件类型不出现在任何配置里。
候选只读：它们是投递账本，没有确认 / 删除 / 已读语义（不做未读系统）。
**任何出口都不展示密码或授权码**，只给「是否已配置」与可公开的邮箱账号。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.domains.alerts.notify import get_smtp_config, send_test_mail, smtp_config
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.policy import (
    CATEGORIES,
    CATEGORY_EVENT_TYPES,
    CATEGORY_LABELS,
    MAX_ATTEMPTS,
    load_prefs,
    save_prefs,
)
from app.domains.notifications import service as notification_service

router = APIRouter(tags=["notifications"])


def _mask_account(user: str) -> str:
    """邮箱账号脱敏：保留首尾各 1 位与域名。"""
    if "@" not in user:
        return ""
    name, domain = user.split("@", 1)
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[0]}***{name[-1]}@{domain}"


async def _smtp_view() -> dict:
    """SMTP 配置的可公开部分（不含密码 / 授权码）。"""
    # get_smtp_config 返回前端键形（toAddr / useSsl，密码已掩码）
    cfg = await get_smtp_config()
    return {
        "configured": bool(cfg["toAddr"]) and bool(cfg["host"]),
        "host": cfg["host"],
        "port": cfg["port"],
        "userMasked": _mask_account(cfg["user"] or ""),
        "hasPassword": bool(cfg["hasPassword"]),
        "useSsl": bool(cfg["useSsl"]),
    }


@router.get("/notifications/prefs")
async def get_prefs():
    """用户通知偏好 + 出口状态 + 最近一次投递结果（设置页一屏看全）。"""
    prefs = await load_prefs()
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(NotificationCandidate)
                .where(NotificationCandidate.status.in_(("delivered", "failed")))
                .order_by(NotificationCandidate.updated_at.desc())
            )
        ).scalars().first()
    last = None
    if row is not None:
        last = {
            "status": row.status,
            "at": row.delivered_at.isoformat() if row.delivered_at else None,
            "attempts": row.attempts,
            # 失败原因原文可能含服务端细节，只给用户一句可行动的提示
            "reason": row.reason,
            "retryable": notification_service.retryable(row.last_error),
        }
    return {
        "enabled": prefs["enabled"],
        "quietEnabled": prefs["quietEnabled"],
        "quietStart": prefs["quietStart"],
        "quietEnd": prefs["quietEnd"],
        "includeDetails": prefs["includeDetails"],
        "maxAttempts": MAX_ATTEMPTS,
        "categories": [
            {
                "key": key,
                "label": CATEGORY_LABELS[key],
                "enabled": prefs["categories"].get(key, True),
                "eventTypes": len(CATEGORY_EVENT_TYPES[key]),
            }
            for key in CATEGORIES
        ],
        "smtp": await _smtp_view(),
        "lastDelivery": last,
    }


@router.put("/notifications/prefs")
async def update_prefs(payload: dict):
    """写用户偏好；未知键忽略，类别只认已知类别。"""
    prefs = await save_prefs(payload or {})
    return {"ok": True, "enabled": prefs["enabled"]}


@router.post("/notifications/test")
async def send_test():
    """发一封连通性测试邮件。

    与 Price Event / Candidate / Cycle **完全无关**：不读事件、不建候选、不建
    轮次，只走一次 SMTP——用户点一次测试不能污染业务数据。
    失败（含凭据错误）直接 HTTP 400，消息来自 SMTP 服务端原文。
    """
    cfg = await smtp_config()
    try:
        await send_test_mail(
            host=cfg["host"],
            port=int(cfg["port"]),
            user=cfg["user"],
            password=cfg["password"],
            to_addr=cfg["to_addr"],
            use_ssl=bool(cfg["use_ssl"]),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@router.get("/notifications/candidates")
async def list_candidates(limit: int = Query(50, ge=1, le=500)):
    """最近的通知候选（投递账本，只读）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(NotificationCandidate)
                .order_by(NotificationCandidate.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    return {
        "items": [
            {
                "id": c.id,
                "eventId": c.event_id,
                "cycleId": c.cycle_id,
                "appid": c.appid,
                "region": c.region_code,
                "eventType": c.event_type,
                "category": c.category,
                "recipient": c.recipient,
                "status": c.status,
                "reason": c.reason,
                "attempts": c.attempts,
                "deliveredAt": c.delivered_at.isoformat() if c.delivered_at else None,
            }
            for c in rows
        ]
    }


@router.get("/notifications/stats")
async def stats():
    """投递生产指标：按候选状态计数（不新建统计系统，直接从账本聚合）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    NotificationCandidate.status,
                    func.count(NotificationCandidate.id),
                    func.coalesce(func.sum(NotificationCandidate.attempts), 0),
                ).group_by(NotificationCandidate.status)
            )
        ).all()
        failed_rows = (
            await session.execute(
                select(
                    NotificationCandidate.attempts,
                    NotificationCandidate.last_error,
                ).where(NotificationCandidate.status == "failed")
            )
        ).all()

    counts = {status: {"count": int(n), "attempts": int(a)} for status, n, a in rows}
    failed = counts.get("failed", {"count": 0})["count"]
    can_retry = sum(
        1
        for attempts, error in failed_rows
        if int(attempts or 0) < MAX_ATTEMPTS
        and notification_service.retryable(error)
    )
    return {
        "total": sum(v["count"] for v in counts.values()),
        "delivered": counts.get("delivered", {"count": 0})["count"],
        "pending": counts.get("pending", {"count": 0})["count"],
        "suppressed": counts.get("suppressed", {"count": 0})["count"],
        "sending": counts.get("sending", {"count": 0})["count"],
        "failed": failed,
        # 失败里还能再试的（临时故障且未到次数上限）与已经永久放弃的
        "retryable": can_retry,
        "permanent": failed - can_retry,
        "attempts": sum(v["attempts"] for v in counts.values()),
        "maxAttempts": MAX_ATTEMPTS,
        "byStatus": counts,
    }
