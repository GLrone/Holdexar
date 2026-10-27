"""地区价格新鲜度来自成功观察；对象摘要保留最旧地区的年龄。"""
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
    """批量读取各地区的成功观察与更新状态。"""
    from app.domains.regions.service import enabled_regions

    ids = [int(a) for a in appids]
    by_appid: dict[int, list] = {}
    regions = await enabled_regions()
    if ids:
        async with get_session_factory()() as session:
            for start in range(0, len(ids), _CHUNK):
                rows = (
                    await session.execute(
                        select(GameCurrentPrice)
                        .where(GameCurrentPrice.appid.in_(ids[start : start + _CHUNK]))
                    )
                ).scalars().all()
                for row in rows:
                    by_appid.setdefault(row.appid, []).append(row)
    from app.domains.crawl.service import _unobserved_job_targets

    pending = await _unobserved_job_targets(0, limit_rows=-1, appids=ids) if ids else set()
    return [{"appid": a, **regional_freshness(by_appid.get(a, []), regions, pending)} for a in ids]


def regional_freshness(rows: list, regions: list[str] | None = None, pending_units: set | None = None) -> dict:
    """同一份价格行生成地区明细和对象新鲜度，不重新取价。"""
    indexed = {p.region_code.upper(): p for p in rows if p.region_code}
    codes = [cc.upper() for cc in regions] if regions else list(indexed)
    units = {}
    successes = []
    now = datetime.now()
    for code in codes:
        row = indexed.get(code)
        success_at = row.last_success_at if row is not None else None
        if success_at is not None:
            successes.append(success_at)
        age = freshness_of(success_at, now=now)
        outcome = (row.attempt_outcome or "failed") if row is not None else "notAttempted"
        pending = bool(row is not None and pending_units and (row.appid, code) in pending_units)
        units[code] = {
            "status": row.price_status if row is not None else "notAttempted",
            "outcome": outcome, "answer": row.steam_answer if row is not None else None,
            "lastSuccessAt": age["observedAt"],
            "lastAttemptAt": row.updated_at.isoformat() if row is not None and row.updated_at else None,
            "ageHours": age["ageHours"], "freshness": age["freshness"],
            "pending": pending or (outcome != "success" and (row is None or row.price_status != "blocked")),
            "needsUpdate": pending or outcome != "success" or age["ageHours"] is None or age["ageHours"] >= FRESH_HOURS,
        }
    return {**freshness_of(min(successes) if successes else None, now=now), "regions": units}


async def latest_price_observation() -> datetime | None:
    """全库最近一次成功价格观察时刻（灵动岛「数据有多旧」的事实源）。

    只认 `last_success_at`：成功观察（含 locked / 无购买选项）推进、传输失败
    不回退，且自动轮 / 手动 / 补抓 / 回填共用同一写面——任何触发方的成功
    写入都推进时钟，不读 price_cycles（不挂轮的作业不再对时钟不可见）。
    从未成功观察返回 None：「没有数据」不是「数据很旧」，由调用侧表达。
    """
    async with get_session_factory()() as session:
        value = (
            await session.execute(select(func.max(GameCurrentPrice.last_success_at)))
        ).scalar()
    return value
