"""alerts 域服务：规则 CRUD + 爬取后的触发检查 + 邮件通知 + 游戏搜索。"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.app_info import APP_NAME
from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.crawler.config import APPDETAILS_URL
from app.crawler.utils import get_beijing_time_obj
from app.domains.account import service as account_service
from app.domains.games.models import (
    Bundle,
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
)
from app.domains.monitoring.models import MonitorExclusion, MonitorSource
from app.domains.games.pricing import (
    REGION_TO_CURRENCY,
    build_steam_header_url,
    format_cny_fen,
    format_minor_units,
)
from app.domains.regions import service as regions_service
from app.domains.settings import service as settings_service
from app.domains.wishlist.models import WishlistItem
from .models import AlertEvent, PriceAlert
from . import notify
from . import rules

logger = logging.getLogger(__name__)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def region_display_name(code: str) -> str:
    """区服中文名（邮件正文用）；未知 code 回退 code 本身。"""
    try:
        regions = await regions_service.list_regions()
        return next((r["name"] for r in regions if r["code"] == code.lower()), code)
    except Exception:  # noqa: BLE001 — 区服表异常时展示 code 即可
        return code


async def _owner_region_code() -> str:
    """号主所在地区：主账号钱包快照 region_code（小写；钱包从未同步过则未知）。

    新史低速报与监控池折扣速报共用「可直接入手的地区优先」口径，两处必须
    同源，所以出口只有一个。
    """
    try:
        primary = await account_service.get_primary_account()
        wallet = primary.wallet_json if primary and isinstance(primary.wallet_json, dict) else None
        return str((wallet or {}).get("region_code") or "")
    except Exception:  # noqa: BLE001 — 账号域异常不阻断价格检查
        log_event(logger, "读取主账号地区失败，按无号主地区兜底", tag="降级", level=logging.WARNING)
        return ""


def _alert_dict(a: PriceAlert) -> dict:
    return {
        "id": a.id,
        "appid": int(a.appid),
        "region": a.region,
        "targetType": a.target_type,
        "targetValue": a.target_value,
        "config": a.config_json,
        "active": a.active,
        "repeatMode": a.repeat_mode,
        "repeatHours": a.repeat_hours,
        "createdAt": a.created_at.isoformat() if a.created_at else None,
        "lastTriggeredAt": a.last_triggered_at.isoformat() if a.last_triggered_at else None,
    }


async def list_alerts() -> list[dict]:
    """列出全部提醒规则，附带游戏中文名与封面（缺档回退 Steam CDN 拼图）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(PriceAlert).order_by(PriceAlert.id.desc()))
        ).scalars().all()
        # 批量取游戏名 + 封面
        appids = {int(a.appid) for a in rows}
        games: dict[int, tuple[str, str | None]] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name, Game.header_image).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): (g[1], g[2]) for g in game_rows}
    result = []
    for a in rows:
        d = _alert_dict(a)
        name, header = games.get(int(a.appid), ("", None))
        d["gameName"] = name
        d["gameHeader"] = header or build_steam_header_url(int(a.appid))
        result.append(d)
    return result


async def add_alert(
    appid: int,
    region: str,
    target_type: str,
    target_value: float | None,
    *,
    repeat_mode: str = "once",
    repeat_hours: int = 24,
    config: dict | None = None,
) -> dict:
    """保存规则后立即使用已有观测求值。"""
    from app.domains.notifications.policy import load_prefs

    data = rules.validate_rule(dict(appid=appid, region=region, targetType=target_type,
                                    targetValue=target_value, config=config,
                                    repeatMode=repeat_mode, repeatHours=repeat_hours))
    prefs = await load_prefs()
    now = _naive(get_beijing_time_obj())
    async with write_session(WritePriority.INTERACTIVE) as session:
        alert = PriceAlert(
            appid=appid,
            region=data["region"],
            target_type=target_type,
            target_value=data.get("targetValue"),
            config_json=data["config"],
            active=True,
            repeat_mode=repeat_mode,
            repeat_hours=max(1, int(repeat_hours)),
            created_at=now,
        )
        session.add(alert)
        await session.flush()
        identity = alert.id
        triggered = await _check_rules(session, [appid], prefs, now, [identity])
        await session.commit()
    await _dispatch_matches(triggered)
    async with get_session_factory()() as session:
        return _alert_dict(await session.get(PriceAlert, identity))


async def update_alert(
    alert_id: int,
    *,
    active: bool | None = None,
    target_value: float | None = None,
    target_type: str | None = None,
    region: str | None = None,
    repeat_mode: str | None = None,
    repeat_hours: int | None = None,
    config: dict | None = None,
) -> dict:
    """更新条件；停用只暂停未开始的投递资格。"""
    from app.domains.notifications.policy import load_prefs

    prefs = await load_prefs()
    now = _naive(get_beijing_time_obj())
    async with write_session(WritePriority.INTERACTIVE) as session:
        alert = await session.get(PriceAlert, alert_id)
        if alert is None:
            raise ValueError("提醒不存在")
        data = _alert_dict(alert)
        changes = {"targetType": target_type, "targetValue": target_value, "region": region,
                   "repeatMode": repeat_mode, "repeatHours": repeat_hours, "config": config}
        data.update({key: value for key, value in changes.items() if value is not None})
        data = rules.validate_rule(data)
        alert.target_type, alert.target_value = data["targetType"], data.get("targetValue")
        alert.region, alert.config_json = data["region"], data["config"]
        alert.repeat_mode, alert.repeat_hours = data["repeatMode"], data["repeatHours"]
        if active is not None:
            alert.active = active
        if active is False:
            from app.domains.notifications.service import pause_rule_candidates
            await pause_rule_candidates(session, alert_id)
        appid = int(alert.appid)
        await session.flush()
        triggered = await _check_rules(session, [appid], prefs, now, [alert_id]) if active is not False else []
        await session.commit()
    await _dispatch_matches(triggered)
    async with get_session_factory()() as session:
        return _alert_dict(await session.get(PriceAlert, alert_id))


async def delete_alert(alert_id: int) -> bool:
    async with write_session(WritePriority.INTERACTIVE) as session:
        alert = await session.get(PriceAlert, alert_id)
        if alert is None:
            return False
        from app.domains.notifications.service import pause_rule_candidates
        await pause_rule_candidates(session, alert_id)
        await session.delete(alert)
        await session.commit()
        return True


def _event_type(kind: str) -> str:
    return {"price": "RULE_PRICE", "pct": "RULE_PCT", "historic_low": "RULE_LOW", "new_low": "RULE_LOW"}[kind]


async def _inputs(session, appids: list[int] | None, region: str,
                  scope: list[str] | None = None) -> tuple[list[dict], dict[int, dict]]:
    prices = select(GameCurrentPrice, Game).select_from(Game).outerjoin(GameCurrentPrice, and_(
        Game.appid == GameCurrentPrice.appid, GameCurrentPrice.region_code == region))
    if appids is not None:
        prices = prices.where(Game.appid.in_(appids))
    if scope and "catalog" not in scope:
        source_names = {"followed": "favorite", "wishlist": "family_wishlist", "owned": "owned", "manual": "manual"}
        from_monitoring = select(MonitorSource.target_id).where(
            MonitorSource.target_type == "game", MonitorSource.active.is_(True),
            MonitorSource.source.in_([source_names[value] for value in scope]))
        member_flags = []
        if "wishlist" in scope:
            member_flags.append(WishlistItem.wishlisted.is_(True))
        if "owned" in scope:
            member_flags.append(WishlistItem.owned.is_(True))
        filters = [Game.appid.in_(from_monitoring)]
        if member_flags:
            filters.append(Game.appid.in_(select(WishlistItem.appid).where(
                WishlistItem.active.is_(True), or_(*member_flags))))
        prices = prices.where(or_(*filters))
    rows = (await session.execute(prices)).all()
    currents = []
    for price, game in rows:
        observed = {"appid": int(game.appid), "region": region,
                    "positiveRate": game.positive_rate, "reviewCount": game.review_count,
                    "gameName": game.name, "headerImage": game.header_image}
        if price is not None:
            observed.update(currency=str(price.currency or "").upper(), subId=price.sub_id,
                            price=price.price, originalPrice=price.original_price,
                            discount=price.discount_percent, status=price.price_status, cnyFen=price.cny_fen,
                            observedAt=price.updated_at.isoformat() if price.updated_at else None)
        currents.append(observed)
    ids = [row["appid"] for row in currents]
    memberships = {identity: {"sources": [], "owned": False, "wishlisted": False,
                              "ignored": False, "priority": 0} for identity in ids}
    if not ids:
        return currents, memberships
    for identity, source, priority in (await session.execute(select(
            MonitorSource.target_id, MonitorSource.source, MonitorSource.priority).where(
                MonitorSource.target_type == "game", MonitorSource.target_id.in_(ids),
                MonitorSource.active.is_(True)))).all():
        item = memberships[int(identity)]
        item["sources"].append(source)
        item["priority"] = max(item["priority"], priority or 0)
    for identity in (await session.execute(select(MonitorExclusion.target_id).where(
            MonitorExclusion.target_type == "game", MonitorExclusion.target_id.in_(ids),
            MonitorExclusion.active.is_(True)))).scalars().all():
        memberships[int(identity)]["ignored"] = True
    for identity, owned, wished in (await session.execute(select(
            WishlistItem.appid, WishlistItem.owned, WishlistItem.wishlisted).where(
                WishlistItem.appid.in_(ids), WishlistItem.active.is_(True)))).all():
        memberships[int(identity)]["owned"] |= bool(owned)
        memberships[int(identity)]["wishlisted"] |= bool(wished)
    return currents, memberships


async def _contexts(session, currents: list[dict], window: int | None) -> dict:
    from app.domains.games.price_comparison import history_context

    if not currents:
        return {}
    return await history_context(session, currents, before=datetime.now(), window_days=window)


def _comparison(contexts: dict, current: dict) -> dict:
    key = (current["appid"], str(current.get("region") or "").upper(),
           int(current.get("subId") or 0), str(current.get("currency") or "").upper())
    return contexts.get(key, {})


def _observation_key(current: dict, event_type: str = "") -> str:
    import json

    keys = ("appid", "region", "subId", "currency", "observationId") if event_type == "FEATURED" else (
        "appid", "region", "subId", "currency", "observedAt", "price")
    return json.dumps([current.get(key) for key in keys],
                      ensure_ascii=True, separators=(",", ":"))


async def preview_rule(payload: dict) -> dict:
    data = rules.validate_rule(payload)
    async with get_session_factory()() as session:
        currents, _ = await _inputs(session, [data["appid"]], data["region"])
        contexts = await _contexts(session, currents, (data.get("config") or {}).get("windowDays"))
    current = currents[0] if currents else {"appid": data["appid"], "region": data["region"]}
    history = _comparison(contexts, current)
    current["currentKnown"] = history.get("currentKnown", False)
    result = rules.evaluate_rule(data, current, history)
    return {**result, "rule": data}


async def _append_matches(session, matches: list[tuple[int, dict, str, dict]], now: datetime) -> list[dict]:
    if not matches:
        return []
    from app.domains.notifications.policy import category_of
    from app.domains.notifications.service import enqueue_facts
    rule_ids = {identity for identity, _, _, _ in matches}
    keys = {_observation_key(result["observed"], event_type) for _, result, event_type, _ in matches}
    existing = set()
    for identity, details in (await session.execute(select(AlertEvent.alert_id, AlertEvent.details_json).where(
            AlertEvent.alert_id.in_(rule_ids),
            func.json_extract(AlertEvent.details_json, "$.observationKey").in_(keys)))).all():
        if details:
            existing.add((identity, details.get("observationKey")))
    payloads = []
    for identity, result, event_type, metadata in matches:
        current = result["observed"]
        key = _observation_key(current, event_type)
        if (identity, key) in existing:
            continue
        details = {**metadata, "ruleId": identity or None, "observationKey": key,
                   "eventType": event_type, "evaluation": result,
                   **{field: current.get(field) for field in (
                       "price", "currency", "subId", "observedAt", "originalPrice", "discount", "gameName", "headerImage")},
                   "labels": result["labels"]}
        payloads.append(dict(alert_id=identity, appid=current["appid"], region=current["region"],
                             price=current["price"], price_cny=current.get("cnyFen"),
                             triggered_at=now, notified=False, details_json=details))
        existing.add((identity, key))
    events = []
    for offset in range(0, len(payloads), 500):
        statement = sqlite_insert(AlertEvent.__table__).values(payloads[offset:offset + 500]).returning(
            *AlertEvent.__table__.c)
        events.extend((await session.execute(statement)).mappings().all())
    await session.flush()
    facts = [dict(id=event["id"], cycleId=0, appid=int(event["appid"]), region=event["region"],
                  eventType=event["details_json"]["eventType"],
                  category=category_of(event["details_json"]["eventType"]),
                  payload=event["details_json"], occurredAt=now.isoformat()) for event in events]
    await enqueue_facts("alert_event", facts, session=session)
    return [event["details_json"] for event in events]


async def _check_rules(session, appids: list[int], prefs: dict, now: datetime,
                       alert_ids: list[int] | None = None) -> list[dict]:
    from app.domains.notifications.policy import type_enabled

    statement = select(PriceAlert).where(PriceAlert.active.is_(True), PriceAlert.appid.in_(appids))
    if alert_ids is not None:
        statement = statement.where(PriceAlert.id.in_(alert_ids))
    alerts = (await session.execute(statement)).scalars().all()
    eligible = [alert for alert in alerts if type_enabled(prefs, _event_type(alert.target_type))]
    by_region = {}
    for alert in eligible:
        by_region.setdefault(alert.region, []).append(alert)
    matches = []
    matched_alerts = {}
    for region, region_alerts in by_region.items():
        currents, _ = await _inputs(session, [int(alert.appid) for alert in region_alerts], region)
        current_map = {row["appid"]: row for row in currents}
        contexts = {}
        for window in {(alert.config_json or {}).get("windowDays") for alert in region_alerts}:
            contexts[window] = await _contexts(session, currents, window)
        for alert in region_alerts:
            if alert.repeat_mode == "cooldown" and alert.last_triggered_at is not None:
                if (now - alert.last_triggered_at).total_seconds() < (alert.repeat_hours or 24) * 3600:
                    continue
            current = current_map.get(int(alert.appid), {})
            history = _comparison(contexts[(alert.config_json or {}).get("windowDays")], current) if current else {}
            current = dict(current, currentKnown=history.get("currentKnown", False))
            result = rules.evaluate_rule(_alert_dict(alert), current, history)
            if result["matched"]:
                matches.append((alert.id, result, _event_type(alert.target_type), {"rule": _alert_dict(alert)}))
                matched_alerts[alert.id] = alert
    triggered = await _append_matches(session, matches, now)
    for result in triggered:
        alert = matched_alerts[result["ruleId"]]
        alert.last_triggered_at = now
        if alert.repeat_mode == "once":
            alert.active = False
    return triggered


async def _dispatch_matches(triggered: list[dict]) -> None:
    if triggered:
        from app.domains.notifications.service import dispatch

        await dispatch(None)
        log_event(logger, "价格提醒已触发", detail={"条数": len(triggered)})


async def check_appids(appids: list[int], *, alert_ids: list[int] | None = None) -> list[dict]:
    from app.domains.notifications.policy import load_prefs

    if not appids:
        return []
    prefs = await load_prefs()
    now = _naive(get_beijing_time_obj())
    async with write_session(WritePriority.BACKGROUND) as session:
        triggered = await _check_rules(session, appids, prefs, now, alert_ids)
        await session.commit()
    await _dispatch_matches(triggered)
    return triggered



def _featured_sort(result: dict, sort: str):
    cur, membership = result["observed"], result.get("membership", {})
    if sort == "price":
        value = cur.get("price") or 0
    elif sort == "savings":
        value = -((cur.get("originalPrice") or 0) - (cur.get("price") or 0))
    elif sort == "quality":
        value = -(cur.get("positiveRate") or 0)
    elif sort == "followed":
        value = -membership.get("priority", 0)
    elif sort == "wishlist":
        value = -int(membership.get("wishlisted", False) or "family_wishlist" in membership.get("sources", []))
    else:
        value = -(cur.get("discount") or 0)
    return value, cur["appid"]


async def _featured_results(session, config: dict, region: str, appids: list[int] | None = None) -> list[dict]:
    currents, membership = await _inputs(session, appids, region, config["scope"])
    contexts = await _contexts(session, currents, config["windowDays"])
    results = []
    for current in currents:
        history = _comparison(contexts, current)
        current["currentKnown"] = history.get("currentKnown", False)
        current["observationId"] = history.get("observationId")
        results.append(rules.evaluate_featured(config, current, history, membership[current["appid"]]))
    return sorted(results, key=lambda result: (not result["matched"], *_featured_sort(result, config["sort"])))


async def preview_featured(payload: dict) -> dict:
    from app.domains.notifications.policy import load_prefs, type_enabled
    from app.domains.notifications.region import resolve_region

    prefs = await load_prefs()
    config = rules.validate_featured(payload.get("featured", payload.get("config", payload)))
    selected = resolve_region(explicit=payload.get("region"), prefs=prefs)
    if not selected["region"]:
        return {"items": [], "matched": 0, "total": 0, "region": selected, "config": config,
                "exclusions": [{"code": "region_unknown"}]}
    async with get_session_factory()() as session:
        results = await _featured_results(session, config, selected["region"], payload.get("appids"))
    return {"items": results, "matched": sum(result["matched"] for result in results),
            "total": len(results), "region": selected, "config": config}


async def check_featured(appids: list[int] | None = None) -> list[dict]:
    from app.domains.notifications.policy import load_prefs, type_enabled
    from app.domains.notifications.region import resolve_region
    from app.domains.notifications.service import dispatch

    prefs = await load_prefs()
    if not type_enabled(prefs, "FEATURED"):
        return []
    config = rules.validate_featured(prefs.get("featured"))
    selected = resolve_region(prefs=prefs)
    if not selected["region"]:
        return []
    now = _naive(get_beijing_time_obj())
    async with write_session(WritePriority.BACKGROUND) as session:
        results = await _featured_results(session, config, selected["region"], appids)
        matches = [(0, result, "FEATURED", {"featured": config}) for result in results if result["matched"]]
        triggered = await _append_matches(session, matches, now)
        await session.commit()
    if triggered:
        await dispatch(None)
    return triggered


async def check_new_lows(appids: list[int]) -> list[dict]:
    return await check_featured(appids) if appids else []


# ─── 监控范围扩展通知：折扣速报 / 捆绑包 / Epic / HB / 通道告警 ──────
#
# 统一语义：新事实写 FactNotice 进候选管线（与降价提醒同一投递生命周期），
# fact_key 幂等防重复发现；SMTP 未配置不消耗资格，下轮按新事实继续。
# 发现闸挡噪音：折扣类 20h 上限，告警类按 kind 冷却（SYS_ALERT_COOLDOWN_HOURS）。

DEAL_MIN_DISCOUNT = 50          # 折扣深度门槛（%）
DEAL_MIN_INTERVAL_HOURS = 20.0  # 同类速报最小间隔
BUNDLE_MIN_CNY_FEN = 500        # 捆绑包价格下限（¥5，滤掉拆分/占位包）
PROXY_ALERT_RATIO = 0.2         # 可用通道占比低于此值即告警

KEY_BUNDLE_DEAL_AT = "alerts.bundle_deal_at"           # epoch 秒（发现闸）
KEY_SYS_ALERT_AT = "alerts.sys_alert_at"               # {kind: epoch 秒（冷却）}

SYS_ALERT_COOLDOWN_HOURS: dict[str, float] = {
    "backup": 24.0,   # 备份失败
    "proxy": 12.0,    # 代理通道告警
    "crawl": 12.0,    # 价格网格连续失败
}
SYS_ALERT_DEFAULT_COOLDOWN = 24.0


def _hours_since_ts(ts) -> float:
    """距记账时间戳的小时数；无值/坏值回一个大数（= 从未记过账）。"""
    try:
        return (time.time() - float(ts)) / 3600
    except (TypeError, ValueError):
        return 1e9


def _bundle_url(url: str | None, bundle_id: int, item_kind: int, region: str) -> str:
    """包商店直链 + 该区 cc（形态与购买语义解耦：item_kind=1 是 /sub/）。"""
    kind = "sub" if int(item_kind or -1) == 1 else "bundle"
    base = str(url) if url else f"https://store.steampowered.com/{kind}/{int(bundle_id)}/"
    cc = (region or "").strip().lower()
    if not cc:
        return base
    return f"{base}{'&' if '?' in base else '?'}cc={cc}"


def _pick_region_row(prices: list[dict], owner_region: str) -> dict:
    """该区选择口径（与新史低速报同源）：号主区可买优先，否则折扣最深、价最低。"""
    owner_row = next(
        (p for p in prices if owner_region and p["region"] == owner_region.upper()), None
    )
    return owner_row or max(prices, key=lambda p: (p["discount"], -p["cny"]))


async def check_wishlist_deals() -> list[dict]:
    return await check_featured()


async def check_bundle_deals() -> list[dict]:
    """捆绑包精选：全量刷包链尾调用，新折扣按「包 + 深度」事实落账通报。

    20h 发现闸防折扣横跳噪音；同一包同深度 fact_key 幂等，仅更深折扣产生
    新事实进候选。低价垃圾包（<BUNDLE_MIN_CNY_FEN）与无 CNY 快照的包直接排除。
    """
    if _hours_since_ts(await settings_service.get_value(KEY_BUNDLE_DEAL_AT, 0)) < DEAL_MIN_INTERVAL_HOURS:
        return []
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    Bundle.bundle_id,
                    Bundle.name,
                    Bundle.item_kind,
                    Bundle.url,
                    BundleRegionPrice.region_code,
                    BundleRegionPrice.currency,
                    BundleRegionPrice.price,
                    BundleRegionPrice.cny_fen,
                    BundleRegionPrice.discount_percent,
                    BundleRegionPrice.app_ids,
                )
                .join(BundleRegionPrice, BundleRegionPrice.bundle_id == Bundle.bundle_id)
                .where(
                    BundleRegionPrice.price_status == "ok",
                    BundleRegionPrice.discount_percent >= DEAL_MIN_DISCOUNT,
                    BundleRegionPrice.cny_fen.is_not(None),
                    BundleRegionPrice.cny_fen >= BUNDLE_MIN_CNY_FEN,
                )
            )
        ).all()

        owner_region = await _owner_region_code()

        grouped: dict[int, dict] = {}
        for bid, name, kind, url, region, currency, price, cny, discount, app_ids in rows:
            entry = grouped.setdefault(int(bid), {
                "name": name, "kind": int(kind or -1), "url": url, "prices": [],
            })
            entry["prices"].append({
                "region": (region or "").upper(),
                "currency": currency or "CNY",
                "price": int(price) if price is not None else None,
                "cny": int(cny) if cny is not None else 0,
                "discount": int(discount or 0),
                "count": len(app_ids or []),
            })

        picks: list[dict] = []
        for bid, entry in grouped.items():
            best_discount = max(p["discount"] for p in entry["prices"])
            chosen = _pick_region_row(entry["prices"], owner_region)
            cn_row = next((p for p in entry["prices"] if p["region"] == "CN"), None)
            picks.append({
                "bundle_id": bid,
                "name": entry["name"],
                "url": _bundle_url(entry["url"], bid, entry["kind"], chosen["region"]),
                "region_code": chosen["region"],
                "region_name": await region_display_name(chosen["region"]),
                "price_text": format_minor_units(chosen["price"], chosen["currency"]),
                "cn_price_text": format_cny_fen(cn_row["cny"]) if cn_row else "—",
                "max_discount": best_discount,
                "condition": f"包内 {chosen['count']} 项" if chosen["count"] else "",
            })
        picks.sort(key=lambda p: (-p["max_discount"], p["bundle_id"]))

    if not picks:
        return []
    await settings_service.set_value(KEY_BUNDLE_DEAL_AT, time.time())
    from app.domains.notifications.service import dispatch, record_fact_notices

    created = set(await record_fact_notices("bundles", "discount_pick", [
        {"factKey": f"bundle:{p['bundle_id']}:{p['max_discount']}",
         "data": {"appid": 0, "title": f"{p['name']} -{p['max_discount']}%",
                  "labels": ["捆绑包精选"], "bundleId": p["bundle_id"],
                  "priceText": p["price_text"], "cnPriceText": p["cn_price_text"],
                  "regionCode": p["region_code"], "regionName": p["region_name"],
                  "url": p["url"], "condition": p["condition"]}}
        for p in picks
    ]))
    fresh = [p for p in picks if f"bundle:{p['bundle_id']}:{p['max_discount']}" in created]
    if fresh:
        await dispatch(None)
    log_event(
        logger,
        "捆绑包精选新折扣已落账" if fresh else "捆绑包精选无新折扣",
        tag="成功" if fresh else "跳过",
        detail={"合格包": len(grouped), "新增": len(fresh)},
    )
    return fresh


def _epic_key(offer: dict) -> str:
    """当期条目标识：appid 优先，无 appid 用标题小写（游乐条目常无 appid）。"""
    appid = offer.get("appid")
    if appid:
        return f"appid:{int(appid)}"
    return "title:" + str(offer.get("title") or "").strip().lower()


async def check_epic_free() -> list[dict]:
    """Epic 喜加一：当期免费条目逐条落账（fact_key 幂等），预告段不记账。

    取数走展示链（`metadata.epic_free_offers`），与标记链 `refresh_epic_free`
    分离——落账要的是标题与商店页，标记链只落 is_epic/epic_date。同一当期
    条目重跑不重复通报；预告条目转正后才会自己产生新事实。
    """
    from app.domains.metadata import service as metadata_service
    from app.domains.notifications.service import dispatch, record_fact_notices

    payload = await metadata_service.epic_free_offers()
    if not payload.get("ok"):
        log_event(logger, "Epic 喜加一跳过，展示链本轮无数据", tag="跳过")
        return []
    offers = [o for o in (payload.get("offers") or []) if o.get("title")]
    current = [o for o in offers if not o.get("upcoming")]
    if not current:
        return []

    created = await record_fact_notices("epic_free", "free_rotation", [
        {"factKey": f"epic:{_epic_key(o)}",
         "data": {"appid": 0, "title": o.get("titleCn") or o["title"],
                  "labels": ["Epic 免费游戏"], "url": str(o.get("url") or "")}}
        for o in current
    ])
    if not created:
        log_event(logger, "Epic 喜加一无新条目", tag="跳过", detail={"当期": len(current)})
        return []
    await dispatch(None)
    log_event(logger, "Epic 喜加一新条目已落账", tag="成功",
              detail={"新增": len(created), "当期": len(current)})
    fresh_keys = set(created)
    return [o for o in current if f"epic:{_epic_key(o)}" in fresh_keys]


async def check_hb_choice(result: dict) -> list[dict]:
    """Humble Choice 当月包：`refresh_hb_choice` 结案后调用（每天幂等）。

    同月包 fact_key 幂等，新月包落账进候选；marked 里的 appid 都已在
    games 侧落行（占位行也算），中文名随事实记录。
    """
    if not result or not result.get("recorded"):
        return []
    label = str(result.get("label") or result.get("machineName") or "")
    marked = result.get("marked") or []
    appids = [int(m["appid"]) for m in marked if m.get("appid")]
    if not label or not appids:
        return []

    titles = {int(m["appid"]): m.get("title") or "" for m in marked if m.get("appid")}
    async with get_session_factory()() as session:
        for appid, name in (
            await session.execute(
                select(Game.appid, Game.name).where(Game.appid.in_(appids))
            )
        ).all():
            titles[int(appid)] = name or titles.get(int(appid), "")
    game_titles = [titles.get(int(m["appid"])) or str(m["appid"]) for m in marked if m.get("appid")]

    from app.domains.notifications.service import dispatch, record_fact_notices

    created = await record_fact_notices("hb_choice", "bundle_changed", [
        {"factKey": f"hb_choice:{label}",
         "data": {"appid": 0, "title": f"Humble Choice {label}：{len(game_titles)} 款可兑换",
                  "labels": ["Humble Choice"], "games": game_titles}}
    ])
    if not created:
        log_event(logger, "Humble Choice 当月包已记账", tag="跳过", detail={"月包": label})
        return []
    await dispatch(None)
    log_event(logger, "Humble Choice 当月包已落账", tag="成功",
              detail={"月包": label, "款数": len(game_titles)})
    return game_titles


async def _subscription_digest(proxies_service) -> list[dict]:
    """订阅摘要（只取展示字段）：订阅 URL 是凭据，绝不进邮件正文。"""
    try:
        subs = await proxies_service.list_subscriptions()
    except Exception:  # noqa: BLE001 — 摘要失败不影响告警主体
        return []
    out = []
    for s in subs[:5]:
        stats = s.get("lastStats")
        traffic = stats.get("traffic") if isinstance(stats, dict) else None
        out.append({
            "id": s.get("id"),
            "label": s.get("label") or f"订阅 {s.get('id')}",
            "kind": s.get("kind"),
            "traffic": traffic if isinstance(traffic, dict) else None,
        })
    return out


async def check_proxy_health() -> bool:
    """代理通道 / Clash 订阅告警：全灭或可用占比过低时发一封（12h 冷却）。

    未配置任何通道（total=0）是用户选择而不是异常，静默跳过。
    """
    from app.domains.proxies import service as proxies_service

    try:
        stats = await proxies_service.pool_stats()
    except Exception:  # noqa: BLE001 — 统计失败无法判定，不误报
        log_event(
            logger,
            "代理池统计失败，跳过通道告警",
            tag="跳过",
            level=logging.ERROR,
            exc_info=True,
        )
        return False
    total = int(stats.get("total") or 0)
    available = int(stats.get("available") or 0)
    if total <= 0:
        return False
    if available <= 0:
        reason = "全部代理通道不可用"
    elif available / total < PROXY_ALERT_RATIO:
        reason = f"可用通道不足 {int(PROXY_ALERT_RATIO * 100)}%（{available}/{total}）"
    else:
        return False
    if not await _alert_cooldown_ok("proxy"):
        return False

    clash = stats.get("clash") or {}
    pool = stats.get("pool") or {}
    snapshot = {
        "reason": reason,
        "available": available,
        "total": total,
        "pool_total": pool.get("total", 0),
        "pool_ok": pool.get("ok", 0),
        "clash_running": bool(clash.get("running")),
        "clash_nodes": clash.get("nodes", 0),
        "clash_ok_nodes": clash.get("okNodes", 0),
        "clash_exit_ips": clash.get("exitIps", 0),
        "clash_ok_exit_ips": clash.get("okExitIps", 0),
        "subscriptions": await _subscription_digest(proxies_service),
    }
    await _mark_alert("proxy")
    from app.domains.notifications.service import dispatch, record_fact_notices

    created = await record_fact_notices("alerts", "proxy_health", [
        {"factKey": f"proxy_alert:{time.time_ns()}",
         "data": {"appid": 0, "title": f"代理通道告警：{reason}",
                  "labels": ["连接通道提醒"], **snapshot}}
    ])
    if created:
        await dispatch(None)
    log_event(
        logger,
        "代理通道告警已落账" if created else "代理通道告警落账失败",
        tag="成功" if created else "未完成",
        detail={"原因": reason},
    )
    return bool(created)


async def _alert_cooldown_ok(kind: str) -> bool:
    store = await settings_service.get_value(KEY_SYS_ALERT_AT, {})
    last = store.get(kind) if isinstance(store, dict) else None
    cooldown = SYS_ALERT_COOLDOWN_HOURS.get(kind, SYS_ALERT_DEFAULT_COOLDOWN)
    return _hours_since_ts(last) >= cooldown


async def _mark_alert(kind: str) -> None:
    store = await settings_service.get_value(KEY_SYS_ALERT_AT, {})
    store = dict(store) if isinstance(store, dict) else {}
    store[kind] = time.time()
    if len(store) > 20:  # 只留最近 20 类，防 KV 无界增长
        for key, _ in sorted(store.items(), key=lambda kv: kv[1])[: len(store) - 20]:
            store.pop(key, None)
    await settings_service.set_value(KEY_SYS_ALERT_AT, store)


async def send_system_alert(
    *,
    kind: str,
    title: str,
    summary: str,
    rows: list[tuple[str, str]] | None = None,
    level: str = "warning",
    hint: str = "",
) -> bool:
    """系统异常告警（备份失败 / 价格网格连续失败…）：按 kind 冷却后落账通报。"""
    if not await _alert_cooldown_ok(kind):
        return False
    await _mark_alert(kind)
    from app.domains.notifications.service import dispatch, record_fact_notices

    created = await record_fact_notices("alerts", "system_alert", [
        {"factKey": f"system_alert:{kind}:{time.time_ns()}",
         "data": {"appid": 0, "title": title, "labels": ["系统运行提醒"],
                  "summary": summary, "rows": [list(row) for row in (rows or [])],
                  "level": level, "hint": hint}}
    ])
    if created:
        await dispatch(None)
    log_event(
        logger,
        "系统告警已落账" if created else "系统告警落账失败",
        tag="成功" if created else "未完成",
        detail={"标题": title},
    )
    return bool(created)


async def delete_event(event_id: int) -> bool:
    """删除单条触发历史。"""
    from app.domains.notifications.models import NotificationCandidate

    async with write_session(WritePriority.INTERACTIVE) as session:
        event = await session.get(AlertEvent, event_id)
        if event is None:
            return False
        referenced = (await session.execute(select(NotificationCandidate.id).where(
            NotificationCandidate.source_kind == "alert_event", NotificationCandidate.event_id == event_id,
        ).limit(1))).scalar_one_or_none()
        if referenced is not None:
            raise ValueError("该记录是通知结果的依据，需要保留")
        await session.delete(event)
        await session.commit()
        return True


async def clear_events() -> dict:
    """只清理没有通知引用的触发记录。"""
    from app.domains.notifications.models import NotificationCandidate

    async with write_session(WritePriority.INTERACTIVE) as session:
        references = select(NotificationCandidate.event_id).where(NotificationCandidate.source_kind == "alert_event")
        protected = (await session.execute(select(func.count()).select_from(AlertEvent).where(
            AlertEvent.id.in_(references)))).scalar_one()
        result = await session.execute(AlertEvent.__table__.delete().where(AlertEvent.id.not_in(references)))
        await session.commit()
        return {"removed": int(result.rowcount or 0), "protected": protected}


async def list_events(limit: int = 50) -> list[dict]:
    """列出触发历史，附带游戏中文名、封面与本币价文本。

    priceCny 取触发时刻落库的人民币分快照；迁移前的旧事件无快照，
    前端回退展示本币价文本（priceText）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
        appids = {int(e.appid) for e in rows}
        games: dict[int, tuple[str, str | None]] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name, Game.header_image).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): (g[1], g[2]) for g in game_rows}
    return [
        {
            "id": e.id,
            "alertId": e.alert_id,
            "appid": int(e.appid),
            "gameName": games.get(int(e.appid), ("", None))[0],
            "gameHeader": games.get(int(e.appid), ("", None))[1]
            or build_steam_header_url(int(e.appid)),
            "region": e.region,
            "price": int(e.price) if e.price else None,
            "priceCny": int(e.price_cny) if e.price_cny else None,
            "priceText": (
                format_minor_units(int(e.price), REGION_TO_CURRENCY.get(e.region, "CNY"))
                if e.price
                else ""
            ),
            "triggeredAt": e.triggered_at.isoformat() if e.triggered_at else None,
            "notified": e.notified,
            "details": e.details_json,
        }
        for e in rows
    ]


async def search_games(q: str, region: str = "CN") -> dict:
    """搜索游戏：优先本地数据库（中文名/英文名/appid），本地无结果且输入为纯数字 appid 时
    尝试在线查询 Steam appdetails 验证该游戏存在且为非免费游戏。

    返回 { items: [{appid, name, nameEn, isFree}] }
    """
    q = q.strip()
    if not q:
        return {"items": []}

    region_cc = region.lower()
    # 1. 本地搜索（中文名 LIKE / 英文名 LIKE / appid 精确）
    async with get_session_factory()() as session:
        # 纯数字 → 也按 appid 精确匹配
        is_numeric = q.isdigit()
        if is_numeric:
            appid_val = int(q)
            rows = (
                await session.execute(
                    select(Game).where(or_(
                        Game.name.like(f"%{q}%"),
                        Game.name_en.like(f"%{q}%"),
                        Game.appid == appid_val,
                    )).limit(20)
                )
            ).scalars().all()
        else:
            rows = (
                await session.execute(
                    select(Game).where(or_(
                        Game.name.like(f"%{q}%"),
                        Game.name_en.like(f"%{q}%"),
                    )).limit(20)
                )
            ).scalars().all()

    if rows:
        return {
            "items": [
                {
                    "appid": int(g.appid),
                    "name": g.name or "",
                    "nameEn": g.name_en or "",
                    "isFree": False,  # 本地库存的游戏默认非免费
                }
                for g in rows
            ]
        }

    # 2. 本地无结果，如果输入不是纯数字 appid → 返回无效
    if not is_numeric:
        return {"items": [], "error": "输入不是有效的 AppID，且本地无匹配游戏记录"}

    # 3. 输入是 appid → 在线查询 Steam appdetails 验证（代理优先：Steam 域直连基本不通）
    appid_val = int(q)
    try:
        from app.domains.proxies import service as proxies_service

        try:
            proxy_url = await proxies_service.resolve_proxy_url()
        except Exception:  # noqa: BLE001
            proxy_url = None
        async with httpx.AsyncClient(timeout=15, proxy=proxy_url) as client:
            resp = await client.get(
                APPDETAILS_URL,
                params={"appids": appid_val, "cc": region_cc, "l": "schinese"},
            )
        data = resp.json()
        app_data = data.get(str(appid_val), {}).get("data", {})
        if not app_data:
            return {"items": [], "error": f"AppID {appid_val} 在 Steam 不存在或已下架"}
        is_free = app_data.get("is_free", False)
        name = app_data.get("name", "")
        if is_free:
            return {"items": [], "error": f"《{name}》是免费游戏，无需价格提醒"}
        return {
            "items": [{
                "appid": appid_val,
                "name": name,
                "nameEn": "",
                "isFree": False,
            }]
        }
    except Exception as e:
        log_event(
            logger,
            "在线查询 Steam AppID 失败",
            detail={"appid": appid_val, "原因": e},
            level=logging.WARNING,
        )
        return {"items": [], "error": f"在线查询失败: {e}"}
