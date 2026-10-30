"""通知策略：一个事实是否值得告诉用户。

`should_notify`（`decide`）是唯一的通知判断入口——渠道只消费决策结果，不各自
判断要不要通知 / 怎么去重 / 是否静默。判定只读两个来源：用户偏好与监控池
（关注范围），**不重新判断价格**——是不是降价、是不是史低是 P5 的事。

用户面只有少数几个类别（价格变化 / 历史低价 / 可购买状态 / 免费与下架），
内部多个事件类型映射到同一类别；内部枚举不出现在任何用户可见配置里。
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timedelta

from app.domains.crawl.events import (
    FREE_PROMO,
    HISTORICAL_LOW_MATCH,
    NEW_HISTORICAL_LOW,
    PERMANENT_PRICE_CHANGE,
    PRICE_DROP,
    PRICE_INCREASE,
    PRICE_RESTORED,
    PRICE_UNAVAILABLE,
    REGION_LOCKED,
    REGION_UNLOCKED,
    REMOVED,
)

# 偏好存 KV 的键（值是 JSON，结构见 DEFAULT_PREFS）
PREFS_KEY = "notifications.prefs"

# 用户类别：内部多个事件类型映射到同一类别，用户不必认识内部枚举
CATEGORIES = ("price", "low", "availability", "lifecycle", "rules", "featured", "content", "system")

CATEGORY_LABELS = {
    "price": "价格变化",
    "low": "历史低价",
    "availability": "可购买状态",
    "lifecycle": "免费与下架",
    "rules": "我的提醒",
    "featured": "价格精选",
    "content": "免费游戏与月包",
    "system": "运行提醒",
}

CATEGORY_EVENT_TYPES: dict[str, tuple[str, ...]] = {
    "price": (PRICE_DROP, PRICE_INCREASE, PERMANENT_PRICE_CHANGE),
    "low": (NEW_HISTORICAL_LOW, HISTORICAL_LOW_MATCH),
    "availability": (PRICE_RESTORED, PRICE_UNAVAILABLE, REGION_LOCKED, REGION_UNLOCKED),
    "lifecycle": (FREE_PROMO, REMOVED),
    "rules": ("RULE_PRICE", "RULE_PCT", "RULE_LOW"),
    "featured": ("FEATURED", "BUNDLE_DEAL"),
    "content": ("EPIC_FREE", "HB_CHOICE"),
    "system": ("PROXY_ALERT", "SYSTEM_ALERT"),
}

TYPE_LABELS = {
    PRICE_DROP: "价格下降", PRICE_INCREASE: "价格上涨", PERMANENT_PRICE_CHANGE: "原价调整",
    NEW_HISTORICAL_LOW: "本地历史新低", HISTORICAL_LOW_MATCH: "追平本地历史低价",
    PRICE_RESTORED: "恢复购买", PRICE_UNAVAILABLE: "暂不售", REGION_LOCKED: "进入锁区",
    REGION_UNLOCKED: "解除锁区", FREE_PROMO: "限时免费", REMOVED: "游戏下架",
    "RULE_PRICE": "指定价格", "RULE_PCT": "指定折扣", "RULE_LOW": "低价规则",
    "FEATURED": "精选与监控池折扣", "BUNDLE_DEAL": "捆绑包精选",
    "EPIC_FREE": "Epic 免费游戏", "HB_CHOICE": "Humble Choice",
    "PROXY_ALERT": "连接通道提醒", "SYSTEM_ALERT": "系统运行提醒",
}

# 候选状态
PENDING = "pending"
SUPPRESSED = "suppressed"
SENDING = "sending"
UNKNOWN = "unknown"
DELIVERED = "delivered"
FAILED = "failed"

# 未投递的基础状态（会进入下一次摘要）
UNDELIVERED = (PENDING, SUPPRESSED)

# 单条候选最多投递次数：超过即永久失败，不再重试（重试是为了扛临时故障，
# 不是无限循环——授权码错、配置错这类必须一次就停）
MAX_ATTEMPTS = 3

DEFAULT_PREFS: dict = {
    # 默认关：开启是用户动作，避免给已有 SMTP 配置的用户凭空多一类邮件
    "enabled": False,
    "categories": {c: True for c in CATEGORIES},
    "quietEnabled": False,
    "quietStart": "23:00",
    "quietEnd": "08:00",
    "includeDetails": True,
    "manualRegion": None,
    "digestTime": "08:00",
    "digestWeekday": 0,
    "types": {key: {"enabled": True, "frequency": "immediate"} for key in TYPE_LABELS},
    "featured": {},
}


@dataclass
class Decision:
    """策略决定：要不要产生候选、落什么状态、为什么。"""

    notify: bool
    status: str | None = None
    reason: str | None = None
    category: str | None = None


def category_of(event_type: str) -> str | None:
    """事件类型 → 用户类别；未知类型不属于任何类别（不通知）。"""
    for category, types in CATEGORY_EVENT_TYPES.items():
        if event_type in types:
            return category
    return None


def _merge_prefs(raw: dict | None) -> dict:
    from app.domains.alerts.rules import DEFAULT_FEATURED

    defaults = dict(DEFAULT_PREFS, featured=DEFAULT_FEATURED)
    return _merge_dict(defaults, raw or {})


def _merge_dict(base: dict, patch: dict) -> dict:
    return {
        key: _merge_dict(value, patch.get(key, {}))
        if isinstance(value, dict) else deepcopy(patch.get(key, value))
        for key, value in base.items()
    }


async def load_prefs() -> dict:
    """读用户偏好（合并默认值；KV 值是 JSON）。"""
    from app.domains.settings.service import get_value

    return _merge_prefs(await get_value(PREFS_KEY, None))


async def save_prefs(raw: dict) -> dict:
    from app.core.database import WritePriority, write_session
    from app.domains.alerts.rules import validate_featured
    from app.domains.settings.models import AppSetting
    from app.crawler.config import CC_LIST

    async with write_session(WritePriority.INTERACTIVE, "notification_prefs") as session:
        row = await session.get(AppSetting, PREFS_KEY)
        prefs = _merge_dict(_merge_prefs(row.value_json if row else None), raw)
        for key in ("quietStart", "quietEnd", "digestTime"):
            parsed = _parse_hhmm(prefs[key])
            if parsed is None or not (0 <= parsed[0] < 24 and 0 <= parsed[1] < 60):
                raise ValueError("通知时间须为有效的小时和分钟")
        if type(prefs["digestWeekday"]) is not int or not 0 <= prefs["digestWeekday"] < 7:
            raise ValueError("每周发送日须为0至6")
        for config in prefs["types"].values():
            if config["frequency"] not in ("immediate", "daily", "weekly", "off"):
                raise ValueError("不支持的通知频率")
        region = prefs["manualRegion"]
        if region is not None:
            region = str(region).strip().upper()
            if region not in {code.upper() for code, _, _ in CC_LIST}:
                raise ValueError("请选择已支持的通知地区")
            prefs["manualRegion"] = region
        prefs["featured"] = validate_featured(prefs["featured"])
        if row is None:
            session.add(AppSetting(key=PREFS_KEY, value_json=prefs, updated_at=datetime.now()))
        else:
            row.value_json, row.updated_at = prefs, datetime.now()
        await session.commit()
    return prefs


def type_enabled(prefs: dict, event_type: str) -> bool:
    category = category_of(event_type)
    config = prefs.get("types", {}).get(event_type, {})
    return bool(prefs.get("enabled") and category
                and prefs.get("categories", {}).get(category, True)
                and config.get("enabled", True) and config.get("frequency", "immediate") != "off")


def delivery_due(now: datetime, frequency: str, prefs: dict) -> datetime:
    if frequency == "immediate":
        return now
    hour, minute = _parse_hhmm(prefs.get("digestTime", "08:00"))
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "weekly":
        due += timedelta(days=(int(prefs.get("digestWeekday", 0)) - now.weekday()) % 7)
    if due <= now:
        due += timedelta(days=7 if frequency == "weekly" else 1)
    return due


def _parse_hhmm(value) -> tuple[int, int] | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = value.split(":", 1)
        return int(hour), int(minute)
    except ValueError:
        return None


def in_quiet_window(now: datetime, prefs: dict) -> bool:
    """是否处于全局静默窗口（支持跨零点，如 23:00–08:00）。

    静默不是丢弃：候选仍会创建，状态标 suppressed，随下一个非静默批次一并投递。
    """
    if not prefs.get("quietEnabled"):
        return False
    start = _parse_hhmm(prefs.get("quietStart"))
    end = _parse_hhmm(prefs.get("quietEnd"))
    if start is None or end is None or start == end:
        return False
    minutes = now.hour * 60 + now.minute
    start_m, end_m = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


def decide(
    *,
    event_type: str,
    appid: int,
    prefs: dict,
    watched: set[int],
    recipient: str,
    now: datetime,
) -> Decision:
    """这个事实要不要产生通知候选。

    顺序即语义：总开关 → 投递出口 → 关注范围 → 类别开关；都通过后，静默期
    只决定「现在发」还是「顺延」，不决定「发不发」。
    """
    if not prefs.get("enabled"):
        return Decision(False, reason="disabled")
    if not recipient:
        return Decision(False, reason="no_recipient")
    if appid not in watched:
        return Decision(False, reason="not_watched")
    category = category_of(event_type)
    if category is None:
        return Decision(False, reason="unknown_type")
    if not prefs["categories"].get(category, True):
        return Decision(False, reason="category_off")
    if not type_enabled(prefs, event_type):
        return Decision(False, reason="type_off")
    if in_quiet_window(now, prefs):
        return Decision(True, status=SUPPRESSED, category=category)
    return Decision(True, status=PENDING, category=category)
