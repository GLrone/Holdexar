"""周期下游消费价格事实并投递摘要，投递结果只更新通知候选。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, bindparam, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.domains.alerts.notify import delivery_rejection_code, is_delivery_unknown, send_mail_ex, smtp_config
from app.domains.alerts.models import AlertEvent
from app.domains.crawl import events as events_service
from app.domains.crawl import service as crawl_service
from app.domains.crawl.cycle import PriceCycle
from app.domains.games.models import Game
from app.domains.games.pricing import format_minor_units
from app.domains.notifications import policy as policy_mod
from app.domains.notifications.mail import (
    MAX_DETAIL_ROWS,
    describe_event,
    digest_mail_html,
    event_label,
)
from app.domains.notifications.models import FactNotice, NotificationCandidate
from app.domains.settings.models import AppSetting
from app.domains.notifications.policy import (
    CATEGORY_LABELS,
    FAILED,
    MAX_ATTEMPTS,
    SENDING,
    UNDELIVERED,
    UNKNOWN,
    decide,
    load_prefs,
)

logger = logging.getLogger(__name__)

# 临时故障：网络 / 连接 / 超时 / 服务端临时拒收（4xx）。这类才值得重试。
# 判据优先用**异常类型名**（`send_mail_ex` 已带上），其次才是英文措辞；最后再
# 兜一层中文措辞——中文 Windows 会把 socket 错误本地化，只认英文的话重试永远
# 不会生效。
_RETRYABLE_HINTS = (
    "smtpserverdisconnected", "smtpconnecterror", "smtpresponsenotready",
    "connectionrefusederror", "connectionreseterror", "timeouterror", "gaierror",
    "timeout", "timed out", "temporar", "network", "unreachable", "broken pipe",
    "closed", "421", "450", "451", "452",
    "拒绝", "无法连接", "超时", "网络", "重置", "断开",
)
# 永久失败：凭据 / 地址 / 配置。重试只会把同一封邮件反复发出去。
_PERMANENT_HINTS = (
    "smtpauthenticationerror", "authentication", "535", "534", "530",
    "username and password", "login", "未配置",
    "smtpsenderrefused", "smtphelorefused", "smtpdataerror", "smtputf8error",
    "553", "550", "554", "552", "551", "no such",
)


def retryable(error: str | None) -> bool:
    """这次失败是否值得重试。

    只认明确的临时故障；未知错误按不可重试处理——宁可丢一封，也不要在诊断
    不清的情况下循环发送。
    """
    if not error or is_delivery_unknown(error):
        return False
    code = delivery_rejection_code(error)
    if code is not None:
        return code < 500
    text = error.lower()
    if any(hint in text for hint in _PERMANENT_HINTS):
        return False
    return any(hint in text for hint in _RETRYABLE_HINTS)

EVENT_BATCH = 1000
SENDING_TIMEOUT = timedelta(minutes=30)

NOTICE_TYPES = {
    ("hb_choice", "bundle_changed"): "HB_CHOICE",
    ("epic_free", "free_rotation"): "EPIC_FREE",
    ("bundles", "discount_pick"): "BUNDLE_DEAL",
    ("alerts", "proxy_health"): "PROXY_ALERT",
    ("alerts", "system_alert"): "SYSTEM_ALERT",
}


async def _session_prefs(session) -> dict:
    row = await session.get(AppSetting, policy_mod.PREFS_KEY)
    return policy_mod._merge_prefs(row.value_json if row else None)


async def enqueue_facts(source_kind: str, facts: list[dict], *, session=None) -> int:
    """在来源事务中建立候选；资格和不可变来源共享一次提交。"""
    if not facts:
        return 0
    if session is None:
        async with write_session(WritePriority.BACKGROUND, "notification_facts") as owned:
            created = await enqueue_facts(source_kind, facts, session=owned)
            await owned.commit()
            return created
    models = {"price_event": events_service.PriceEvent, "alert_event": AlertEvent,
              "fact_notice": FactNotice}
    if source_kind not in models:
        raise ValueError("不支持的通知事实来源")
    prefs = await _session_prefs(session)
    if not prefs.get("enabled"):
        return 0
    ids = {int(fact["id"]) for fact in facts}
    model = models[source_kind]
    sources = {row.id: row for row in (await session.execute(
        select(model).where(model.id.in_(ids))
    )).scalars().all()}
    existing = set((await session.execute(select(NotificationCandidate.event_id).where(
        NotificationCandidate.source_kind == source_kind, NotificationCandidate.event_id.in_(ids),
    ))).scalars().all())
    recipient = await _saved_recipient(session, "")
    now, payloads = datetime.now(), []
    for fact in facts:
        identity, event_type = int(fact["id"]), fact["eventType"]
        if identity in existing or not policy_mod.type_enabled(prefs, event_type):
            continue
        source = sources.get(identity)
        if source is None:
            raise ValueError("提醒依据不存在")
        if source_kind == "alert_event":
            expected = (int(source.appid), source.region, (source.details_json or {}).get("eventType"))
            occurred = source.triggered_at
        elif source_kind == "fact_notice":
            expected = (int((source.data or {}).get("appid") or 0), None,
                        NOTICE_TYPES.get((source.source, source.kind)))
            occurred = source.occurred_at
        else:
            expected = (int(source.appid), source.region_code, source.event_type)
            occurred = source.occurred_at
        if expected != (int(fact.get("appid") or 0), fact.get("region"), event_type):
            raise ValueError("提醒依据与来源不一致")
        frequency = prefs["types"][event_type]["frequency"]
        payloads.append(dict(
            source_kind=source_kind, event_id=identity, cycle_id=int(fact.get("cycleId") or 0),
            appid=expected[0], region_code=expected[1], event_type=event_type,
            category=policy_mod.category_of(event_type), recipient=recipient,
            status="suppressed" if policy_mod.in_quiet_window(now, prefs) else "pending",
            payload_json=deepcopy(fact.get("payload") or {}),
            due_at=policy_mod.delivery_due(occurred or now, frequency, prefs),
            attempts=0, created_at=now, updated_at=now,
        ))
        existing.add(identity)
    if not payloads:
        return 0
    result = await session.execute(sqlite_insert(NotificationCandidate.__table__)
        .on_conflict_do_nothing(index_elements=["source_kind", "event_id", "recipient"]), payloads)
    return int(result.rowcount or 0)


async def pause_rule_candidates(session, rule_id: int) -> int:
    result = await session.execute(update(NotificationCandidate).where(
        NotificationCandidate.source_kind == "alert_event",
        func.json_extract(NotificationCandidate.payload_json, "$.ruleId") == rule_id,
        NotificationCandidate.status.in_((*UNDELIVERED, FAILED)),
    ).values(status="suppressed", reason="rule_paused", updated_at=datetime.now()))
    return int(result.rowcount or 0)

async def record_fact_notices(source: str, kind: str, notices: list[dict]) -> list[str]:
    """登记内容事实并在同事务建立候选；fact_key 幂等，重复发现不落第二行。

    notices 每条 {factKey, data}，data 须带 appid（0 = 非单一游戏事实）与
    展示字段（title 等）；返回本次新增并已进入候选的 factKey 列表。
    """
    event_type = NOTICE_TYPES.get((source, kind))
    if event_type is None:
        raise ValueError("未登记的通知事实来源")
    if not notices:
        return []
    async with write_session(WritePriority.BACKGROUND, "fact_notice_record") as owned:
        now = datetime.now()
        rows = []
        for offset in range(0, len(notices), 500):
            values = [dict(source=source, kind=kind, fact_key=str(item["factKey"]),
                           data=item.get("data") or {}, occurred_at=now)
                      for item in notices[offset:offset + 500]]
            statement = sqlite_insert(FactNotice.__table__).values(values).on_conflict_do_nothing(
                index_elements=["fact_key"]).returning(*FactNotice.__table__.c)
            rows.extend((await owned.execute(statement)).mappings().all())
        if not rows:
            return []
        facts = [dict(id=int(row["id"]), appid=int((row["data"] or {}).get("appid") or 0),
                      region=None, eventType=event_type, payload=row["data"] or {})
                 for row in rows]
        await enqueue_facts("fact_notice", facts, session=owned)
        await owned.commit()
    return [str(row["fact_key"]) for row in rows]

async def _watched_appids() -> set[int]:
    """关注范围 = 监控池（与爬取同一口径）。目录层只是价格库维护，不通知。"""
    try:
        return set(await crawl_service.plan_scope_appids("pool"))
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "监控池范围解析失败，本轮不产生通知候选",
            level=logging.ERROR,
            exc_info=True,
        )
        raise


async def _create_candidates(
    cycle_id: int, events: list[dict], prefs: dict, watched: set[int], recipient: str,
    *, scan: dict | None = None,
) -> int:
    """事件 → 候选（幂等：同一事实对同一收件人只有一条，重复处理不新增）。"""
    now = datetime.now()
    payloads = []
    for event in events:
        decision = decide(event_type=event["eventType"], appid=int(event["appid"]),
                          prefs=prefs, watched=watched,
                          recipient=recipient or ("pending" if scan else ""), now=now)
        if decision.notify:
            payloads.append(dict(event_id=int(event["id"]), cycle_id=cycle_id,
                                 appid=int(event["appid"]), region_code=event.get("region"),
                                 event_type=event["eventType"], category=decision.category or "price",
                                 recipient=recipient, status=decision.status, reason=decision.reason,
                                 attempts=0, created_at=now, updated_at=now))
    if not payloads and scan is None:
        return 0
    created = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        source = None
        progress = None
        if scan is not None:
            source = await session.get(PriceCycle, cycle_id)
            progress = dict(source.notification_scan_json or {}) if source else {}
            if progress.get("state") != "scanning" or progress.get("upper") != scan["upper"]:
                return 0
            page_end = max(event["id"] for event in events)
            cursor = int(progress.get("cursor") or 0)
            if cursor >= page_end:
                return 0
            payloads = [row for row in payloads if row["event_id"] > cursor]
        recipient = await _saved_recipient(session, recipient)
        if not recipient and payloads and scan is not None:
            raise RuntimeError("提醒收件邮箱已清空，保留未消费事件等待配置恢复")
        if not recipient and scan is None:
            return 0
        for row in payloads:
            row["recipient"] = recipient
        ids = [row["event_id"] for row in payloads]
        facts = {row.id: row for row in (await session.execute(
            select(events_service.PriceEvent).where(events_service.PriceEvent.id.in_(ids))
        )).scalars().all()} if ids else {}
        existing = set((await session.execute(select(NotificationCandidate.event_id).where(
            NotificationCandidate.event_id.in_(ids)
        ))).scalars().all()) if ids else set()
        payloads = [row for row in payloads if row["event_id"] not in existing
                    and (fact := facts.get(row["event_id"])) is not None
                    and (fact.cycle_id, fact.appid, fact.region_code, fact.event_type) ==
                    (cycle_id, row["appid"], row["region_code"], row["event_type"])]
        if payloads:
            result = await session.execute(sqlite_insert(NotificationCandidate.__table__)
                                           .on_conflict_do_nothing(index_elements=["event_id", "recipient"]),
                                           payloads)
            created = int(result.rowcount or 0)
        if source is not None:
            source.notification_scan_json = dict(progress, cursor=page_end)
        await session.commit()
    return created


async def _open_scan(cycle_id: int, prefs: dict, watched: set[int], recipient: str):
    async with write_session(WritePriority.BACKGROUND, "notification_scan") as session:
        source = await session.get(PriceCycle, cycle_id)
        if source is None:
            return False, None
        progress = source.notification_scan_json or {}
        if progress.get("state") == "scanning":
            return True, deepcopy(progress)
        if progress.get("state") == "done":
            return True, None
        cursor = int(progress.get("cursor") or 0)
        upper = int((await session.execute(select(func.max(events_service.PriceEvent.id)).where(
            events_service.PriceEvent.cycle_id == cycle_id
        ))).scalar_one() or cursor)
        if not prefs.get("enabled") or not recipient:
            source.notification_scan_json = {"state": "done", "cursor": upper}
            await session.commit()
            return True, None
        appids = set((await session.execute(select(events_service.PriceEvent.appid).where(
            events_service.PriceEvent.cycle_id == cycle_id,
            events_service.PriceEvent.id > cursor, events_service.PriceEvent.id <= upper,
        ).distinct())).scalars().all())
        progress = {"state": "scanning", "cursor": cursor, "upper": upper,
                    "prefs": deepcopy(prefs), "appids": sorted(watched & appids)}
        source.notification_scan_json = progress
        await session.commit()
        return True, deepcopy(progress)


async def _complete_scan(cycle_id: int, scan: dict) -> None:
    async with write_session(WritePriority.BACKGROUND, "notification_scan") as session:
        source = await session.get(PriceCycle, cycle_id)
        current = source.notification_scan_json or {} if source else {}
        if current.get("state") != "scanning" or current.get("upper") != scan["upper"]:
            return
        latest = int((await session.execute(select(func.max(events_service.PriceEvent.id)).where(
            events_service.PriceEvent.cycle_id == cycle_id
        ))).scalar_one() or scan["upper"])
        source.notification_scan_json = {
            "state": "pending" if latest > scan["upper"] else "done", "cursor": scan["upper"],
        }
        await session.commit()


async def resume_event_scans(cycle_id: int | None = None, *, prefs: dict | None = None,
                             recipient: str | None = None) -> int:
    async with get_session_factory()() as session:
        ids = list((await session.execute(select(PriceCycle.id).where(
            func.json_extract(PriceCycle.notification_scan_json, "$.state").in_(("pending", "scanning"))
        ).order_by(PriceCycle.id))).scalars().all())
    if cycle_id is not None and cycle_id not in ids:
        ids.append(cycle_id)
    if not ids:
        return 0
    prefs = prefs if prefs is not None else await load_prefs()
    recipient = recipient if recipient is not None else (await smtp_config()).get("to_addr") or ""
    watched = await _watched_appids() if prefs.get("enabled") and recipient else set()
    created = 0
    for identity in ids:
        owned, scan = await _open_scan(identity, prefs, watched, recipient)
        if owned and scan is None:
            continue
        scan_prefs = scan["prefs"] if scan else prefs
        scope = set(scan["appids"]) if scan else watched
        async for page in events_service.notification_batches(
            identity, scope, batch=EVENT_BATCH,
            after=int(scan["cursor"]) if scan else 0, upper=scan["upper"] if scan else None,
        ):
            created += await _create_candidates(identity, page, scan_prefs, scope, recipient, scan=scan)
        if scan is not None:
            await _complete_scan(identity, scan)
    return created


def _deliverable_statement():
    return select(NotificationCandidate).where(or_(
        NotificationCandidate.status.in_(UNDELIVERED),
        and_(NotificationCandidate.status == FAILED, NotificationCandidate.attempts < MAX_ATTEMPTS),
    )).order_by(NotificationCandidate.id)


def _eligible(rows, *, prefs: dict | None = None, now: datetime | None = None):
    now = now or datetime.now()
    return [row for row in rows if row.reason != "rule_paused"
            and (row.due_at is None or row.due_at <= now)
            and (prefs is None or policy_mod.type_enabled(prefs, row.event_type))
            and (row.status in UNDELIVERED
                 or (row.status == FAILED and int(row.attempts or 0) < MAX_ATTEMPTS
                     and retryable(row.last_error)))]


async def _saved_recipient(session, fallback: str) -> str:
    from app.domains.settings.models import AppSetting

    setting = await session.get(AppSetting, "smtp.to_addr")
    return str(setting.value_json or "") if setting is not None else fallback


async def retarget_candidates(session, recipient: str) -> int:
    """未发送候选随目的地转移，合并时保留失败预算与路由历史。"""
    if not recipient:
        return 0
    rows = (await session.execute(select(NotificationCandidate).where(
        NotificationCandidate.status.in_((*UNDELIVERED, FAILED)),
    ).order_by(NotificationCandidate.id))).scalars().all()
    if not rows:
        return 0
    event_ids = {row.event_id for row in rows}
    targets = {}
    ids = sorted(event_ids)
    for offset in range(0, len(ids), 500):
        candidates = (await session.execute(select(NotificationCandidate).where(
            NotificationCandidate.event_id.in_(ids[offset:offset + 500])
        ).order_by(NotificationCandidate.id))).scalars().all()
        for row in candidates:
            key = (row.source_kind, row.event_id)
            target = targets.get(key)
            protected = row.status not in (*UNDELIVERED, FAILED)
            if target is None or (protected and target.status in (*UNDELIVERED, FAILED)) \
                    or (target.status in (*UNDELIVERED, FAILED) and row.recipient == recipient):
                targets[key] = row
    now = datetime.now()
    moved = 0
    merged_ids = []
    for row in rows:
        target = targets[(row.source_kind, row.event_id)]
        if target is row and row.recipient == recipient:
            continue
        history = [*(row.delivery_history_json or []), {
            "recipient": row.recipient, "status": row.status,
            "attempts": int(row.attempts or 0), "lastError": row.last_error,
            "reason": row.reason, "at": row.updated_at.isoformat() if row.updated_at else None,
        }]
        moved += 1
        if target is row:
            row.recipient = recipient
            row.delivery_history_json = history
            row.updated_at = now
            continue
        target.delivery_history_json = [*(target.delivery_history_json or []), *history]
        if target.status in (*UNDELIVERED, FAILED):
            target.attempts = int(target.attempts or 0) + int(row.attempts or 0)
            if row.status == FAILED:
                if target.status != FAILED or retryable(target.last_error):
                    target.reason, target.last_error = row.reason, row.last_error
                target.status = FAILED
            target.updated_at = now
        merged_ids.append(row.id)
    for offset in range(0, len(merged_ids), 500):
        await session.execute(delete(NotificationCandidate).where(
            NotificationCandidate.id.in_(merged_ids[offset:offset + 500])
        ))
    return moved


async def _retarget(recipient: str) -> int:
    async with write_session(WritePriority.BACKGROUND, "notification_recipient") as session:
        recipient = await _saved_recipient(session, recipient)
        moved = await retarget_candidates(session, recipient)
        await session.commit()
    return moved


def _matching_fact():
    fact = events_service.PriceEvent
    price = select(fact.id).where(
        fact.id == NotificationCandidate.event_id, fact.cycle_id == NotificationCandidate.cycle_id,
        fact.appid == NotificationCandidate.appid, fact.event_type == NotificationCandidate.event_type,
        fact.region_code.is_not_distinct_from(NotificationCandidate.region_code),
        fact.created_at <= NotificationCandidate.created_at,
    ).exists()
    rule = select(AlertEvent.id).where(
        AlertEvent.id == NotificationCandidate.event_id,
        AlertEvent.appid == NotificationCandidate.appid,
        AlertEvent.region == NotificationCandidate.region_code,
        AlertEvent.triggered_at <= NotificationCandidate.created_at,
        func.json_extract(AlertEvent.details_json, "$.eventType") == NotificationCandidate.event_type,
    ).exists()
    notice = select(FactNotice.id).where(
        FactNotice.id == NotificationCandidate.event_id,
        FactNotice.occurred_at <= NotificationCandidate.created_at,
        func.coalesce(func.json_extract(FactNotice.data, "$.appid"), 0) == NotificationCandidate.appid,
        NotificationCandidate.region_code.is_(None),
        or_(*(and_(FactNotice.source == source, FactNotice.kind == kind,
                   NotificationCandidate.event_type == event_type)
              for (source, kind), event_type in NOTICE_TYPES.items())),
    ).exists()
    return or_(and_(NotificationCandidate.source_kind == "price_event", price),
               and_(NotificationCandidate.source_kind == "alert_event", rule),
               and_(NotificationCandidate.source_kind == "fact_notice", notice))


async def _quarantine_invalid() -> int:
    async with write_session(WritePriority.BACKGROUND, "notification_integrity") as session:
        result = await session.execute(update(NotificationCandidate).where(
            NotificationCandidate.status.in_((*UNDELIVERED, FAILED, SENDING)),
            ~_matching_fact(),
        ).values(status=UNKNOWN, reason="提醒依据缺失或不一致，已停止发送并保留记录",
                 last_error="price event identity mismatch", updated_at=datetime.now()))
        await session.commit()
    return int(result.rowcount or 0)


async def recover_delivery_state() -> dict[str, int]:
    recovered = await _recover_stuck_sending()
    invalid = await _quarantine_invalid()
    if recovered or invalid:
        log_event(logger, "无法确认的提醒已停止自动发送并保留记录",
                  level=logging.WARNING, detail={"超时": recovered, "依据异常": invalid})
    return {"recovered": recovered, "invalid": invalid}


async def _undelivered() -> list[NotificationCandidate]:
    """待投递候选 = 新产生的（pending / 静默顺延 suppressed）+ 可重试的失败候选。

    失败候选只在「临时故障 且 未超过次数上限」时才回到队列；授权码错、配置错
    这类一次就永久失败。**以候选状态为准，不用"最近一次 Cycle ID"猜。**
    `sending` / `unknown` 不在其中：未确认 SMTP 是否已接收时不重发。
    """
    async with get_session_factory()() as session:
        rows = (await session.execute(_deliverable_statement())).scalars().all()
    return _eligible(rows)


async def _recover_stuck_sending(now: datetime | None = None) -> int:
    """把超时的在途候选标为 unknown；外部是否已接收邮件无法安全推断。"""
    now = now or datetime.now()
    cutoff = now - SENDING_TIMEOUT
    async with write_session(WritePriority.BACKGROUND, "notification_recover") as session:
        result = await session.execute(
            update(NotificationCandidate)
            .where(
                NotificationCandidate.status == SENDING,
                or_(
                    NotificationCandidate.updated_at.is_(None),
                    NotificationCandidate.updated_at < cutoff,
                ),
            )
            .values(
                status=UNKNOWN,
                reason="投递中断，外部是否已接收无法确认",
                last_error="sending 超时，投递结果未知",
                updated_at=now,
            )
        )
        await session.commit()
    return int(result.rowcount or 0)


async def _claim(candidates: list[NotificationCandidate] | None = None,
                 *, recipient: str | None = None) -> list[NotificationCandidate]:
    """以状态和版本字段作条件更新；多进程重复扫描时仅一个能占位。"""
    now = datetime.now()
    claimed: list[NotificationCandidate] = []
    async with write_session(WritePriority.BACKGROUND) as session:
        prefs = await _session_prefs(session)
        if recipient is not None:
            recipient = await _saved_recipient(session, recipient)
            if not recipient:
                return []
            await retarget_candidates(session, recipient)
            candidates = _eligible((await session.execute(_deliverable_statement().where(
                NotificationCandidate.recipient == recipient
            ))).scalars().all(), prefs=prefs, now=now)
        candidates = _eligible(candidates or [], prefs=prefs, now=now)
        for offset in range(0, len(candidates), 100):
            batch = candidates[offset:offset + 100]
            predicates = [and_(NotificationCandidate.id == c.id,
                               NotificationCandidate.event_id == c.event_id,
                               NotificationCandidate.recipient == c.recipient,
                               NotificationCandidate.status == c.status,
                               NotificationCandidate.attempts == int(c.attempts or 0),
                               NotificationCandidate.updated_at.is_not_distinct_from(c.updated_at),
                               NotificationCandidate.last_error.is_not_distinct_from(c.last_error))
                          for c in batch]
            ids = set((await session.execute(update(NotificationCandidate).where(
                or_(*predicates), _matching_fact(),
            ).values(status=SENDING, attempts=NotificationCandidate.attempts + 1,
                     updated_at=now).returning(NotificationCandidate.id)
              .execution_options(synchronize_session=False))).scalars().all())
            claimed.extend(c for c in batch if c.id in ids)
        await session.commit()
    for candidate in claimed:
        candidate._delivery_previous = {
            "status": candidate.status, "reason": candidate.reason, "last_error": candidate.last_error,
            "attempts": int(candidate.attempts or 0), "updated_at": candidate.updated_at,
        }
        candidate.attempts = int(candidate.attempts or 0) + 1
        candidate.updated_at = now
    return claimed


async def _release_unstarted(candidates: list[NotificationCandidate]) -> int:
    """仅撤销尚未进入邮件协议的本次认领并归还尝试次数。"""
    table = NotificationCandidate.__table__
    statement = update(table).where(
        table.c.id == bindparam("claim_id"), table.c.event_id == bindparam("claim_event"),
        table.c.recipient == bindparam("claim_recipient"), table.c.status == SENDING,
        table.c.attempts == bindparam("claim_attempts"), table.c.updated_at == bindparam("claim_at"),
    ).values(**{key: bindparam("prior_" + key)
                for key in ("status", "reason", "last_error", "attempts", "updated_at")})
    restored = 0
    async with write_session(WritePriority.BACKGROUND, "notification_prepare") as session:
        for offset in range(0, len(candidates), 100):
            payloads = [{"claim_id": c.id, "claim_event": c.event_id, "claim_recipient": c.recipient,
                         "claim_attempts": c.attempts, "claim_at": c.updated_at,
                         **{"prior_" + key: value for key, value in c._delivery_previous.items()}}
                        for c in candidates[offset:offset + 100]]
            result = await session.execute(statement, payloads)
            restored += int(result.rowcount or 0)
        await session.commit()
    return restored


async def _settle(
    candidates: list[NotificationCandidate],
    status: str,
    reason: str | None,
    error: str | None = None,
) -> int:
    now = datetime.now()
    settled = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        for offset in range(0, len(candidates), 100):
            predicates = [and_(NotificationCandidate.id == c.id,
                               NotificationCandidate.event_id == c.event_id,
                               NotificationCandidate.recipient == c.recipient,
                               NotificationCandidate.attempts == c.attempts,
                               NotificationCandidate.updated_at == c.updated_at)
                          for c in candidates[offset:offset + 100]]
            values = dict(status=status, reason=reason, last_error=error, updated_at=now)
            if status == "delivered":
                values["delivered_at"] = now
            result = await session.execute(update(NotificationCandidate).where(
                NotificationCandidate.status == SENDING, or_(*predicates),
            ).values(**values).execution_options(synchronize_session=False))
            settled += int(result.rowcount or 0)
        await session.commit()
    return settled


async def _region_names(codes: set[str]) -> dict[str, str]:
    """区码 → 展示名；拿不到就用区码，不让邮件因此发不出。"""
    from app.domains.regions import service as regions_service

    try:
        rows = await regions_service.list_regions()
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "区服名称读取失败，摘要退回区码",
            tag="降级",
            level=logging.ERROR,
            exc_info=True,
        )
        return {}
    return {r["code"].upper(): r["name"] for r in rows if r.get("code")}


def _rule_detail(payload: dict, region_names: dict[str, str], game_name: str | None) -> str:
    """规则命中行：候选 payload 冻结的观测身份直接上信，不回查来源表。"""
    parts = [event_label(str(payload.get("eventType") or "RULE_PRICE"))]
    region = str(payload.get("region") or "").upper()
    if region:
        parts.append(region_names.get(region, region))
    amount, currency = payload.get("price"), payload.get("currency")
    if amount is not None and currency:
        parts.append(format_minor_units(amount, str(currency)))
    if game_name:
        parts.append(game_name)
    return " · ".join(str(part) for part in parts)


def _notice_detail(payload: dict, game_name: str | None) -> str:
    """内容变化行：标题与类别标签来自事实快照，缺什么就跳过不编。"""
    parts: list[str] = []
    title = payload.get("title")
    if title:
        parts.append(str(title))
    for label in payload.get("labels") or []:
        text = str(label)
        if text not in parts:
            parts.append(text)
    if game_name and game_name not in parts:
        parts.append(game_name)
    return " · ".join(parts)


async def _digest_rows(candidates: list[NotificationCandidate]) -> tuple[list[str], int]:
    """候选 → 明细行 + 涉及游戏数（明细超出 MAX_DETAIL_ROWS 的不逐条列）。"""
    detail_candidates = candidates[:MAX_DETAIL_ROWS]
    appids = {c.appid for c in detail_candidates if c.appid}
    price_event_ids = [c.event_id for c in detail_candidates if c.source_kind == "price_event"]
    async with get_session_factory()() as session:
        names = {
            int(appid): name
            for appid, name in (
                await session.execute(
                    select(Game.appid, Game.name).where(Game.appid.in_(appids))
                )
            ).all()
        }
        events = {
            int(row.id): row
            for row in (
                await session.execute(
                    select(events_service.PriceEvent).where(
                        events_service.PriceEvent.id.in_(price_event_ids)
                    )
                )
            ).scalars().all()
        } if price_event_ids else {}

    region_codes = {
        (c.region_code or "").upper() for c in candidates if c.region_code
    }
    region_names = await _region_names(region_codes)

    details = []
    for candidate in detail_candidates:
        payload = candidate.payload_json or {}
        game_name = payload.get("gameName") or names.get(candidate.appid)
        if candidate.source_kind == "alert_event":
            details.append(_rule_detail(payload, region_names, game_name))
        elif candidate.source_kind == "fact_notice":
            details.append(_notice_detail(payload, game_name))
        else:
            details.append(describe_event(
                event_type=candidate.event_type,
                region_code=candidate.region_code,
                previous=events[candidate.event_id].previous_json
                if candidate.event_id in events
                else None,
                current=events[candidate.event_id].current_json
                if candidate.event_id in events
                else None,
                region_names=region_names,
                game_name=game_name,
            ))
    return details, len({c.appid for c in candidates})


async def dispatch(cycle_id: int | None = None) -> dict:
    """消费本轮事实并投递已认领摘要，通知失败不回滚价格结果。"""
    result = {
        "created": 0, "sent": 0, "deferred": 0, "failed": 0,
        "retried": 0, "retryable": False, "unknown": 0,
    }
    try:
        recovery = await recover_delivery_state()
        result["unknown"] = recovery["recovered"] + recovery["invalid"]
        prefs = await load_prefs()
        recipient = (await smtp_config()).get("to_addr") or ""
        result["created"] += await resume_event_scans(cycle_id, prefs=prefs, recipient=recipient)
        if not prefs.get("enabled") or not recipient:
            log_event(
                logger,
                "本轮事件不进入通知（通知总开关关闭或收件出口未配置）",
                tag="跳过",
                detail={
                    "通知开关": "开" if prefs.get("enabled") else "关",
                    "收件出口": "已配置" if recipient else "未配置",
                },
            )
            return result

        await _retarget(recipient)

        if policy_mod.in_quiet_window(datetime.now(), prefs):
            result["deferred"] = len(await _undelivered())
            log_event(
                logger,
                "本轮处于静默期，通知候选顺延到下批",
                tag="跳过",
                detail={"周期": cycle_id, "顺延": result["deferred"]},
            )
            return result

        candidates = await _claim(recipient=recipient)
        if not candidates:
            return result
        try:
            result["retried"] = sum(1 for c in candidates if c.status == "failed")
            counts: dict[str, int] = {}
            for candidate in candidates:
                label = CATEGORY_LABELS.get(candidate.category, event_label(candidate.event_type))
                counts[label] = counts.get(label, 0) + 1
            result["deferred"] = sum(1 for c in candidates if c.cycle_id != cycle_id)
            details, games = await _digest_rows(candidates)
            html = digest_mail_html(
                counts=sorted(counts.items(), key=lambda kv: -kv[1]),
                details=details if prefs.get("includeDetails", True) else [],
                total=len(candidates), games=games, deferred=result["deferred"], when=datetime.now(),
            )
            subject = f"Holdexar 本轮价格更新：{len(candidates)} 条变化"
        except (Exception, asyncio.CancelledError):
            await _release_unstarted(candidates)
            raise
        ok, error = await send_mail_ex(subject, html, recipient=candidates[0].recipient)
        if ok:
            result["sent"] = await _settle(candidates, "delivered", None, None)
        elif is_delivery_unknown(error):
            result["unknown"] += await _settle(
                candidates, UNKNOWN, "邮件发送结果无法确认，已停止自动重发", error)
        else:
            result["failed"] = await _settle(candidates, "failed", f"smtp: {error}", error)
            result["retryable"] = retryable(error)
        log_event(
            logger,
            "通知本轮投递完成" + ("，存在临时故障，下轮可重试" if result["retryable"] else ""),
            detail={
                "周期": cycle_id,
                "候选": len(candidates),
                "新增": result["created"],
                "重试": result["retried"],
                "成功": result["sent"],
                "失败": result["failed"],
                "结果未知": result["unknown"],
            },
        )
        return result
    except Exception:  # noqa: BLE001 —— 通知失败绝不影响 Cycle 收敛
        log_event(
            logger,
            "通知投递失败，不影响本轮价格刷新结果",
            detail={"周期": cycle_id},
            level=logging.ERROR,
            exc_info=True,
        )
        return result
