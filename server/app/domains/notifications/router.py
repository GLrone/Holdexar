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
from app.domains.notifications.mail import event_label
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.policy import (
    CATEGORIES,
    CATEGORY_EVENT_TYPES,
    CATEGORY_LABELS,
    MAX_ATTEMPTS,
    TYPE_LABELS,
    UNKNOWN,
    category_of,
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


def _prefs_view(prefs: dict, *, extras: dict | None = None) -> dict:
    """设置页与提醒页共用的偏好投影；region 为当前生效地区解析结果。"""
    from app.domains.notifications.region import resolve_region

    view = {
        "enabled": prefs["enabled"],
        "quietEnabled": prefs["quietEnabled"],
        "quietStart": prefs["quietStart"],
        "quietEnd": prefs["quietEnd"],
        "includeDetails": prefs["includeDetails"],
        "maxAttempts": MAX_ATTEMPTS,
        "manualRegion": prefs.get("manualRegion"),
        "digestTime": prefs.get("digestTime", "08:00"),
        "digestWeekday": prefs.get("digestWeekday", 0),
        "featured": prefs.get("featured") or {},
        "region": resolve_region(prefs=prefs),
        "types": [
            {
                "key": key,
                "label": TYPE_LABELS[key],
                "category": category_of(key),
                "enabled": (prefs.get("types", {}).get(key) or {}).get("enabled", True),
                "frequency": (prefs.get("types", {}).get(key) or {}).get("frequency", "immediate"),
            }
            for key in TYPE_LABELS
        ],
        "categories": [
            {
                "key": key,
                "label": CATEGORY_LABELS[key],
                "enabled": prefs["categories"].get(key, True),
                "eventTypes": len(CATEGORY_EVENT_TYPES[key]),
            }
            for key in CATEGORIES
        ],
    }
    if extras:
        view.update(extras)
    return view


@router.get("/notifications/prefs")
async def get_prefs():
    """用户通知偏好 + 出口状态 + 最近一次投递结果（设置页一屏看全）。"""
    prefs = await load_prefs()
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(NotificationCandidate)
                .where(NotificationCandidate.status.in_(("delivered", "failed", UNKNOWN)))
                .order_by(NotificationCandidate.updated_at.desc())
            )
        ).scalars().first()
    last = None
    if row is not None:
        last = {
            "status": row.status,
            "at": (row.delivered_at or row.updated_at).isoformat()
            if row.delivered_at or row.updated_at
            else None,
            "attempts": row.attempts,
            # 失败原因原文可能含服务端细节，只给用户一句可行动的提示
            "reason": row.reason,
            "retryable": notification_service.retryable(row.last_error),
        }
    return _prefs_view(prefs, extras={"smtp": await _smtp_view(), "lastDelivery": last})


@router.put("/notifications/prefs")
async def update_prefs(payload: dict):
    """写用户偏好；未知键忽略，类别只认已知类别。"""
    prefs = await save_prefs(payload or {})
    return {"ok": True, **_prefs_view(prefs)}


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


def _payload_detail(payload: dict, fallback_title: str | None) -> dict:
    """候选冻结的展示摘要：游戏名/价格/币种/触发解释，来自创建时的 payload。"""
    return {
        "title": payload.get("gameName") or payload.get("title") or fallback_title,
        "price": payload.get("price"),
        "currency": payload.get("currency"),
        "originalPrice": payload.get("originalPrice"),
        "discount": payload.get("discount"),
        "observedAt": payload.get("observedAt"),
        "labels": payload.get("labels") or [],
        "summary": payload.get("summary"),
    }


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
    from app.domains.crawl.events import PriceEvent
    from app.domains.games.models import Game

    appids = {c.appid for c in rows if c.appid}
    price_ids = [c.event_id for c in rows if c.source_kind == "price_event"]
    async with get_session_factory()() as session:
        names = {int(a): n for a, n in (await session.execute(
            select(Game.appid, Game.name).where(Game.appid.in_(appids)))).all()} if appids else {}
        events = {int(row.id): row for row in (await session.execute(
            select(PriceEvent).where(PriceEvent.id.in_(price_ids)))).scalars().all()} if price_ids else {}

    items = []
    for c in rows:
        if c.source_kind == "price_event":
            event = events.get(c.event_id)
            current = (event.current_json or {}) if event else {}
            detail = {
                "title": names.get(c.appid),
                "price": current.get("price"),
                "currency": current.get("currency"),
                "originalPrice": current.get("originalPrice"),
                "discount": current.get("discount"),
                "observedAt": current.get("observedAt") or current.get("at"),
                "labels": [],
                "summary": None,
            }
        else:
            detail = _payload_detail(c.payload_json or {}, names.get(c.appid))
        items.append({
            "id": c.id,
            "eventId": c.event_id,
            "cycleId": c.cycle_id,
            "appid": c.appid,
            "region": c.region_code,
            "sourceKind": c.source_kind,
            "eventType": c.event_type,
            "eventLabel": event_label(c.event_type),
            "category": c.category,
            "recipient": c.recipient,
            "status": c.status,
            "reason": c.reason,
            "attempts": c.attempts,
            "deliveredAt": c.delivered_at.isoformat() if c.delivered_at else None,
            "deliveryHistory": c.delivery_history_json or [],
            "payload": c.payload_json or {},
            "createdAt": c.created_at.isoformat() if c.created_at else None,
            "dueAt": c.due_at.isoformat() if c.due_at else None,
            "detail": detail,
        })
    return {"items": items}


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
        "unknown": counts.get(UNKNOWN, {"count": 0})["count"],
        "failed": failed,
        # 失败里还能再试的（临时故障且未到次数上限）与已经永久放弃的
        "retryable": can_retry,
        "permanent": failed - can_retry,
        "attempts": sum(v["attempts"] for v in counts.values()),
        "maxAttempts": MAX_ATTEMPTS,
        "byStatus": counts,
    }


@router.get("/notifications/facts")
async def fact_notices(
    afterId: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    """内容链事实通知的增量出口（灵动岛轮询）。

    afterId 缺省 = 只对齐：回空增量与 latestId，消费端记下游标后从
    后续轮询开始取增量（升级后的第一轮不回放历史事实）。
    """
    from app.domains.notifications import facts as facts_service

    return await facts_service.list_facts(after_id=afterId, limit=limit)
