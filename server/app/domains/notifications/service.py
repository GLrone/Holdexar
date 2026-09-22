"""通知投递：价格事件 → 策略 → 候选 → 聚合摘要 → 邮件。

触发点只有一个：Price Refresh Cycle 的 finalizing（`core/scheduler` 在事件检测
之后调用 `dispatch`）。**没有也不允许有通知 scheduler**——通知是 Cycle 的下游
副作用，不是第二套调度系统。

投递失败只影响候选状态：`send_mail` 返回 False 时候选落 `failed`，事件 / Cycle /
抓取结果一律不回滚；候选也绝不回写 `price_events`。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.domains.alerts.notify import send_mail_ex, smtp_config
from app.domains.crawl import events as events_service
from app.domains.crawl import service as crawl_service
from app.domains.games.models import Game
from app.domains.notifications import policy as policy_mod
from app.domains.notifications.mail import (
    MAX_DETAIL_ROWS,
    describe_event,
    digest_mail_html,
    event_label,
)
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.policy import (
    CATEGORIES,
    CATEGORY_LABELS,
    FAILED,
    MAX_ATTEMPTS,
    UNDELIVERED,
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
    if not error:
        return False
    text = error.lower()
    if any(hint in text for hint in _PERMANENT_HINTS):
        return False
    return any(hint in text for hint in _RETRYABLE_HINTS)

# 单轮事件读取上限：一轮候选数量级是对象 × 地区，足够且不炸内存
EVENT_LIMIT = 1000

_TYPE_TO_CATEGORY = {
    event_type: category
    for category in CATEGORIES
    for event_type in policy_mod.CATEGORY_EVENT_TYPES[category]
}


def _category_of(event_type: str) -> str:
    return _TYPE_TO_CATEGORY.get(event_type, "price")


async def _watched_appids() -> set[int]:
    """关注范围 = 监控池（与爬取同一口径）。目录层只是价格库维护，不通知。"""
    try:
        return set(await crawl_service.plan_scope_appids("pool"))
    except Exception:  # noqa: BLE001 —— 范围解析失败按「无关注」处理，不产生候选
        logger.exception("[通知] 监控池范围解析失败，本轮不产生通知候选")
        return set()


async def _create_candidates(
    cycle_id: int, events: list[dict], prefs: dict, watched: set[int], recipient: str
) -> int:
    """事件 → 候选（幂等：同一事实对同一收件人只有一条，重复处理不新增）。"""
    now = datetime.now()
    created = 0
    async with get_session_factory()() as session:
        for event in events:
            decision = decide(
                event_type=event["eventType"],
                appid=int(event["appid"]),
                prefs=prefs,
                watched=watched,
                recipient=recipient,
                now=now,
            )
            if not decision.notify:
                continue
            stmt = (
                sqlite_insert(NotificationCandidate)
                .values(
                    event_id=int(event["id"]),
                    cycle_id=cycle_id,
                    appid=int(event["appid"]),
                    region_code=event.get("region"),
                    event_type=event["eventType"],
                    category=decision.category or "price",
                    recipient=recipient,
                    status=decision.status,
                    reason=decision.reason,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["event_id", "recipient"])
            )
            cursor = await session.execute(stmt)
            created += cursor.rowcount or 0
        await session.commit()
    return created


async def _undelivered() -> list[NotificationCandidate]:
    """待投递候选 = 新产生的（pending / 静默顺延 suppressed）+ 可重试的失败候选。

    失败候选只在「临时故障 且 未超过次数上限」时才回到队列；授权码错、配置错
    这类一次就永久失败。**以候选状态为准，不用"最近一次 Cycle ID"猜。**
    `sending` 不在其中：进程在投递途中被杀的候选不重发（宁丢一封不重发）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(NotificationCandidate).order_by(NotificationCandidate.id)
            )
        ).scalars().all()
    return [
        row
        for row in rows
        if row.status in UNDELIVERED
        or (row.status == FAILED and row.attempts < MAX_ATTEMPTS and retryable(row.last_error))
    ]


async def _claim(candidates: list[NotificationCandidate]) -> None:
    """投递前占位：sending + 计数。单写者串行，无需分布式锁。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        for candidate in candidates:
            row = await session.get(NotificationCandidate, candidate.id)
            if row is None:
                continue
            row.status = "sending"
            row.attempts = int(row.attempts or 0) + 1
            row.updated_at = now
        await session.commit()


async def _settle(
    candidates: list[NotificationCandidate],
    status: str,
    reason: str | None,
    error: str | None = None,
) -> None:
    now = datetime.now()
    async with get_session_factory()() as session:
        for candidate in candidates:
            row = await session.get(NotificationCandidate, candidate.id)
            if row is None:
                continue
            row.status = status
            row.reason = reason
            row.last_error = error
            row.updated_at = now
            if status == "delivered":
                row.delivered_at = now
        await session.commit()


async def _region_names(codes: set[str]) -> dict[str, str]:
    """区码 → 展示名；拿不到就用区码，不让邮件因此发不出。"""
    from app.domains.regions import service as regions_service

    try:
        rows = await regions_service.list_regions()
    except Exception:  # noqa: BLE001
        logger.exception("[通知] 区服名称读取失败，摘要退回区码")
        return {}
    return {r["code"].upper(): r["name"] for r in rows if r.get("code")}


async def _digest_rows(candidates: list[NotificationCandidate]) -> tuple[list[str], int]:
    """候选 → 明细行 + 涉及游戏数（明细超出 MAX_DETAIL_ROWS 的不逐条列）。"""
    appids = {c.appid for c in candidates}
    event_ids = [c.event_id for c in candidates]
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
                        events_service.PriceEvent.id.in_(event_ids)
                    )
                )
            ).scalars().all()
        }

    region_codes = {
        (c.region_code or "").upper() for c in candidates if c.region_code
    }
    region_names = await _region_names(region_codes)

    details = [
        describe_event(
            event_type=candidate.event_type,
            region_code=candidate.region_code,
            previous=events[candidate.event_id].previous_json
            if candidate.event_id in events
            else None,
            current=events[candidate.event_id].current_json
            if candidate.event_id in events
            else None,
            region_names=region_names,
            game_name=names.get(candidate.appid),
        )
        for candidate in candidates[:MAX_DETAIL_ROWS]
    ]
    return details, len({c.appid for c in candidates})


async def dispatch(cycle_id: int) -> dict:
    """本轮事件 → 候选 → 聚合摘要 → 邮件。返回计数（供日志与测试断言）。

    任何失败都不上抛：通知是下游副作用，出错只能让候选落 `failed`，
    不能让 Cycle 变 failed，更不能让用户少一轮价格。
    """
    result = {
        "created": 0, "sent": 0, "deferred": 0, "failed": 0,
        "retried": 0, "retryable": False,
    }
    try:
        prefs = await load_prefs()
        recipient = (await smtp_config()).get("to_addr") or ""
        events = await events_service.list_events(cycle_id=cycle_id, limit=EVENT_LIMIT)
        if not prefs.get("enabled") or not recipient:
            # 总开关关闭 / 出口未配置：不产生候选也不留积压（以后开启不补发旧账）
            logger.info(
                "[通知] 本轮 %d 条事件不进入通知（enabled=%s，出口=%s）",
                len(events), prefs.get("enabled"), bool(recipient),
            )
            return result

        watched = await _watched_appids()
        result["created"] = await _create_candidates(
            cycle_id, events, prefs, watched, recipient
        )

        # 静默期 = 本轮不投递：候选已顺延（suppressed），等下一个非静默批次一并送，
        # 而不是丢掉。不判断这条就等于静默期形同虚设——候选刚建完就被自己投出去。
        if policy_mod.in_quiet_window(datetime.now(), prefs):
            result["deferred"] = len(await _undelivered())
            logger.info("[通知] Cycle %d：处于静默期，%d 条候选顺延",
                        cycle_id, result["deferred"])
            return result

        candidates = await _undelivered()
        if not candidates:
            return result
        result["retried"] = sum(1 for c in candidates if c.status == "failed")

        counts: dict[str, int] = {}
        for candidate in candidates:
            label = CATEGORY_LABELS.get(
                candidate.category, event_label(candidate.event_type)
            )
            counts[label] = counts.get(label, 0) + 1
        result["deferred"] = sum(1 for c in candidates if c.cycle_id != cycle_id)
        details, games = await _digest_rows(candidates)

        html = digest_mail_html(
            counts=sorted(counts.items(), key=lambda kv: -kv[1]),
            details=details if prefs.get("includeDetails", True) else [],
            total=len(candidates),
            games=games,
            deferred=result["deferred"],
            when=datetime.now(),
        )
        subject = f"Holdexar 本轮价格更新：{len(candidates)} 条变化"
        await _claim(candidates)
        ok, error = await send_mail_ex(subject, html)
        if ok:
            await _settle(candidates, "delivered", None, None)
            result["sent"] = len(candidates)
        else:
            await _settle(candidates, "failed", f"smtp: {error}", error)
            result["failed"] = len(candidates)
            result["retryable"] = retryable(error)
        logger.info(
            "[通知] Cycle %d：候选 %d（新增 %d / 重试 %d），投递成功 %d / 失败 %d"
            + ("（临时故障，下次可重试）" if result["retryable"] else ""),
            cycle_id, len(candidates), result["created"], result["retried"],
            result["sent"], result["failed"],
        )
        return result
    except Exception:  # noqa: BLE001 —— 通知失败绝不影响 Cycle 收敛
        logger.exception("[通知] Cycle %d 通知投递失败（不影响本轮结果）", cycle_id)
        return result
