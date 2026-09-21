"""价格数据新鲜度（Freshness）：当前数据距离现在有多新。

与 Coverage 是**不同维度**，不可互相推导——Coverage 说「这一轮刷得全不全」，
Freshness 说「手上的数据离现在多久」。两者可以任意组合（100% 覆盖 + stale、
60% 覆盖 + fresh 都成立），因此不合并成任何综合评分。

第一版只按年龄分档，观察时间来源是 `game_current_prices.updated_at`（当前最
可用的观察时间来源）：

    fresh    age < 6h       一个刷新网格内
    lagging  6h ≤ age < 12h
    stale    age ≥ 12h      与 crawler/config.STALE_HOURS 同口径

不使用「属于最近一个 Cycle 就算 fresh」——价格行没有 `cycle_id`，那种归属
关系不存在。也不新增 last_checked / freshness_score 等字段。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.crawler.config import STALE_HOURS
from app.domains.games.models import GameCurrentPrice

logger = logging.getLogger(__name__)

# 刷新网格步长（小时）；与 6h 锚点网格同口径
FRESH_HOURS = 6.0

# 陈旧阈值沿用 crawler/config（12h = 两个网格）
LAGGING_HOURS = float(STALE_HOURS)

# appid 分批宽度：避开 SQLite 变量上限，一条 IN 不装整池
_CHUNK = 500


def freshness_bucket(age_hours: float) -> str:
    """年龄 → 档位。边界归下一档：6h 起 lagging，12h 起 stale。"""
    if age_hours < FRESH_HOURS:
        return "fresh"
    if age_hours < LAGGING_HOURS:
        return "lagging"
    return "stale"


def freshness_of(observed_at: datetime | None, *, now: datetime | None = None) -> dict:
    """观察时刻 → {observedAt, ageHours, freshness}。

    `observed_at` 为空（该对象一行价格记录都没有）时两者都是 null——「没有
    数据」不是「数据很旧」，不塞进 stale。
    """
    if observed_at is None:
        return {"observedAt": None, "ageHours": None, "freshness": None}
    reference = now or datetime.now()
    age = (reference - observed_at).total_seconds() / 3600.0
    return {
        "observedAt": observed_at.isoformat(),
        "ageHours": round(age, 3),
        "freshness": freshness_bucket(age),
    }


async def appid_freshness(appids: list[int]) -> list[dict]:
    """每个对象价格记录的最后观察时刻与新鲜度档位。

    观察时刻 = 该对象全部区服价格行的 `MAX(updated_at)`：失败记录同样算「被
    观察过」（观察动作发生了），故不按 `price_status` 过滤。列表里没查到任何
    行的对象照样返回，新鲜度为 null。
    """
    ids = [int(a) for a in appids]
    observed: dict[int, datetime] = {}
    if ids:
        async with get_session_factory()() as session:
            for start in range(0, len(ids), _CHUNK):
                rows = (
                    await session.execute(
                        select(
                            GameCurrentPrice.appid,
                            func.max(GameCurrentPrice.updated_at),
                        )
                        .where(GameCurrentPrice.appid.in_(ids[start : start + _CHUNK]))
                        .group_by(GameCurrentPrice.appid)
                    )
                ).all()
                for appid, last_seen in rows:
                    if last_seen is not None:
                        observed[int(appid)] = last_seen
    return [{"appid": a, **freshness_of(observed.get(a))} for a in ids]
