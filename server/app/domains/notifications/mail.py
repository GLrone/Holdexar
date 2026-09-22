"""通知摘要邮件：把已落库的价格事件组装成一封「本轮价格更新」。

只消费 `price_events` 的事实（类型 / 地区 / previous / current / occurred_at），
**不重新判断价格**——是不是降价、是不是史低是事件检测的事。这里的全部工作
是把已判定的事实翻译成人话。

HTML 组件直接复用 `alerts.notify` 的邮件组件库（共享投递出口，§十四：两个入口
可以共享 Delivery）——通知层不另写一套邮件排版。
"""
from __future__ import annotations

from datetime import datetime

from app.crawler.config import CC_LIST
from app.core.app_info import APP_NAME
from app.domains.alerts.notify import (
    _bullet_list,
    _divider,
    _hero,
    _kv_grid,
    _notice,
    _page_html,
    _row,
    _theme_colors,
)
from app.domains.games.pricing import format_minor_units

# 事件类型 → 用户标签（与前端 priceEvent.type.* 同一套措辞）
EVENT_LABELS = {
    "PRICE_DROP": "价格下降",
    "PRICE_INCREASE": "价格上涨",
    "NEW_HISTORICAL_LOW": "历史新低",
    "HISTORICAL_LOW_MATCH": "追平史低",
    "PERMANENT_PRICE_CHANGE": "原价调整",
    "REGION_LOCKED": "进入锁区",
    "REGION_UNLOCKED": "解除锁区",
    "PRICE_UNAVAILABLE": "暂不售",
    "PRICE_RESTORED": "恢复可购买",
    "FREE_PROMO": "限时免费",
    "REMOVED": "游戏下架",
}

# price_status → 用户用词（只出现在后端已判定出的状态跃迁里）
STATE_WORDS = {
    "ok": "可购买",
    "locked": "锁区",
    "missing": "待补抓",
    "blocked": "暂无价格",
}

_CURRENCY_BY_REGION = {code.upper(): currency for code, _, currency in CC_LIST}

# 摘要详情最多列出的行数：超出用一句收口，不为展示造分页
MAX_DETAIL_ROWS = 12


def _num(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def region_currency(region_code: str | None) -> str | None:
    """地区 → 货币代号；游戏级事件 / 未知区返回 None。"""
    if not region_code:
        return None
    return _CURRENCY_BY_REGION.get(region_code.upper())


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, "价格变化")


def describe_event(
    *,
    event_type: str,
    region_code: str | None,
    previous: dict | None,
    current: dict | None,
    region_names: dict[str, str],
    game_name: str | None = None,
) -> str:
    """一条事件 → 一行人话（标签 · 地区 · 之前 → 现在 · 游戏）。

    前后值直接读事件里存的事实快照；拿不到就跳过那一段，不编造。
    """
    prev, cur = previous or {}, current or {}
    parts = [event_label(event_type)]

    region = region_code or _text(cur.get("region"))
    if region:
        parts.append(region_names.get(region.upper(), region.upper()))

    if event_type in ("PRICE_DROP", "PRICE_INCREASE"):
        pair = (_price_text(prev.get("price"), region_code),
                _price_text(cur.get("price"), region_code))
    elif event_type == "NEW_HISTORICAL_LOW":
        pair = (_price_text(prev.get("low"), region_code),
                _price_text(cur.get("price"), region_code))
    elif event_type == "HISTORICAL_LOW_MATCH":
        pair = (_price_text(prev.get("price") or prev.get("low"), region_code),
                _price_text(cur.get("price"), region_code))
    elif event_type == "PERMANENT_PRICE_CHANGE":
        pair = (_price_text(prev.get("originalPrice"), region_code),
                _price_text(cur.get("originalPrice"), region_code))
    elif event_type in ("REGION_LOCKED", "REGION_UNLOCKED", "PRICE_UNAVAILABLE",
                        "PRICE_RESTORED"):
        from_state, to_state = _text(prev.get("status")), _text(cur.get("status"))
        pair = (
            STATE_WORDS.get(from_state, "") if from_state else "",
            STATE_WORDS.get(to_state, "") if to_state else "",
        )
    else:
        pair = ("", "")  # 限时免费 / 下架：标签已说清，没有「之前 → 现在」

    change = " → ".join(p for p in pair if p)
    if change:
        parts.append(change)
    if game_name:
        parts.append(game_name)
    return " · ".join(parts)


def _price_text(amount, region_code: str | None) -> str:
    minor = _num(amount)
    if minor is None:
        return ""
    currency = region_currency(region_code)
    if currency:
        return format_minor_units(minor, currency)
    # 区服表外的地区没有可靠货币：宁可空着，不编一个符号
    return ""


def digest_mail_html(
    *,
    counts: list[tuple[str, int]],
    details: list[str],
    total: int,
    games: int,
    deferred: int,
    when: datetime,
) -> str:
    """本轮价格更新摘要邮件正文。

    counts 是按用户类别聚合后的条数；details 是已格式化的事件行（超出的不逐条
    列）。deferred 是本批里来自更早轮次（静默期顺延）的条数。
    """
    c = _theme_colors()
    subtitle = f"这一轮价格刷新共产生 {total} 条变化，涉及 {games} 款游戏。"
    if deferred:
        subtitle += f"其中 {deferred} 条来自更早的轮次（静默期顺延）。"
    inner = _hero(
        c,
        badge="&#9432;",
        badge_color=c["accent"],
        eyebrow="PRICE REFRESH",
        title="本轮价格更新",
        subtitle=subtitle,
    )
    if counts:
        inner += _row(_kv_grid(c, [(label, f"{n} 条", c["accent"]) for label, n in counts]))
    if details:
        inner += _divider(c)
        inner += _row(
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.4px;'
            f'color:{c["text_sub"]};">变化明细</div>',
            pad="14px 24px 4px",
        )
        inner += _bullet_list(c, details)
    if total > len(details):
        inner += _notice(c, f"其余 {total - len(details)} 条变化不再逐条列出，可在游戏详情页查看。")
    inner += _notice(
        c,
        "同一轮的变化合并成一封邮件发送；静默期内产生的变化随下一轮一并送达。"
        f"（{when.strftime('%Y-%m-%d %H:%M')}）",
    )
    return _page_html(c, inner, preheader=f"{APP_NAME} 本轮价格更新：{total} 条变化")
