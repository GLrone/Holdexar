"""steam_events 域服务：官方活动日历同步、查询与价格历史周期标签。

数据源：Steamworks 公开的 Upcoming Steam Events 文档页，英文页为主源
（主题 Fest 表只有英文版结构完整），中文页作名称通道与季节大促日期交叉核对。
同步遵循「校验门在前」：英文页解析结果未过校验门即整批拒绝入库，保留旧数据。

活动窗口同时是价格观测行的周期标签来源：``active_event_key_at`` 供爬虫
写入时打标，同步成功后按窗口回贴存量行（INSERT-only 差量门禁跳过的同价行
也由回贴覆盖）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, get_session_factory
from app.core.database import write_gate
from app.core.logging import log_event
from app.crawler.utils import get_beijing_time_obj
from . import parser
from .models import SteamEvent

logger = logging.getLogger(__name__)

UPCOMING_URLS = {
    "en": "https://partner.steamgames.com/doc/marketing/upcoming_events?l=english",
    "zh": "https://partner.steamgames.com/doc/marketing/upcoming_events?l=schinese",
}
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
# 启动兜底阈值：快照龄超过该值才真正抓取（每日 cron 是常规同步拍）
SYNC_STALE_HOURS = 72
# API 响应的 stale 标记阈值：超过则前端提示「数据较旧」
RESPONSE_STALE_HOURS = 24 * 7
# 打标窗口缓存有效期（爬虫高频调用，避免每行观测都查库）
_WINDOW_CACHE_TTL_MINUTES = 360


def _as_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _now() -> datetime:
    return _as_naive(get_beijing_time_obj())


def _event_key(category: str, name_en: str, start: date,
               info_slug: str | None) -> str:
    """稳定 slug，跨语言、跨同步轮次不变；价格历史标签引用它。"""
    if category == "themed_fest" and info_slug:
        return "themed_" + info_slug.rsplit("/", 1)[-1]
    if category == "next_fest":
        return f"nextfest_{start.year}_{start.month:02d}"
    if category == "seasonal_sale":
        lowered = name_en.lower()
        season = next(
            (w for w in ("spring", "summer", "autumn", "winter") if w in lowered),
            None,
        )
        if season is None:
            for ch, key in (("春", "spring"), ("夏", "summer"),
                            ("秋", "autumn"), ("冬", "winter")):
                if name_en.startswith(ch):
                    season = key
                    break
        if season:
            return f"seasonal_{start.year}_{season}"
        normalized = re.sub(r"[^a-z0-9]+", "_", name_en.lower()).strip("_")
        return f"seasonal_{start.year}_{normalized or start.isoformat()}"
    normalized = re.sub(r"[^a-z0-9]+", "_", name_en.lower()).strip("_")
    return f"{category}_{normalized or start.isoformat()}"


_sync_lock = asyncio.Lock()
# 打标窗口缓存：None = 未加载；二元组 (windows, loaded_at)
_windows_cache: tuple[list[tuple[date, date, str]], datetime] | None = None


async def _fetch_page(lang: str) -> str:
    from app.domains.rates.http import open_client

    # open_client 是 async 工厂：先 await 拿客户端，再进入上下文管理
    client = await open_client(timeout=15.0)
    async with client:
        resp = await client.get(UPCOMING_URLS[lang], headers=_REQUEST_HEADERS)
        resp.raise_for_status()
        return resp.text


def _merge_bilingual(
    events_en: list[dict], events_zh: list[dict], zh_nav_names: dict[str, str]
) -> list[dict]:
    """英文页为主源，中文页按 (category, start, end) 叠加本地化名。

    主题 Fest 另有一条导航译名通道（中文页导航按文档子页 slug 给出官方
    译名），表格窗口匹配不上时作兜底。
    """
    zh_by_window: dict[tuple, str] = {}
    for e in events_zh:
        zh_by_window.setdefault((e["category"], e["start"], e["end"]), e["name"])
    merged: list[dict] = []
    for e in events_en:
        name_zh = zh_by_window.get((e["category"], e["start"], e["end"]))
        if not name_zh and e["category"] == "themed_fest" and e.get("info_slug"):
            name_zh = zh_nav_names.get(e["info_slug"].lower())
        merged.append({**e, "name_en": e["name"], "name_zh": name_zh or e["name"]})
    return merged


async def sync() -> dict:
    """抓取并入库官方活动日历。校验门不过则抛 RuntimeError，旧数据原样保留。"""
    global _windows_cache
    async with _sync_lock:
        en_html: str | None = None
        zh_html: str | None = None

        async def _grab(lang: str, sink: list[str | None]) -> None:
            try:
                sink[0] = await _fetch_page(lang)
            except Exception:  # noqa: BLE001 —— 单语言失败由主流程按通道语义处理
                log_event(
                    logger,
                    "Steam 活动文档页抓取失败",
                    level=logging.ERROR,
                    exc_info=True,
                    detail={"语言": lang},
                )

        en_sink: list[str | None] = [None]
        zh_sink: list[str | None] = [None]
        await asyncio.gather(_grab("en", en_sink), _grab("zh", zh_sink))
        en_html, zh_html = en_sink[0], zh_sink[0]
        if en_html is None:
            raise RuntimeError("steam events doc page (en) unreachable")

        events_en = parser.parse_upcoming_events(en_html)
        problems = parser.validate_events(events_en)
        if problems:
            raise RuntimeError("steam events validation gate: " + "; ".join(problems))

        events_zh: list[dict] = []
        zh_nav_names: dict[str, str] = {}
        if zh_html is not None:
            events_zh = parser.parse_upcoming_events(zh_html)
            zh_nav_names = parser.parse_zh_fest_names(zh_html)
            # 交叉核对：两语通道都解析出的季节大促，日期必须一致（EN 为准）
            zh_windows = {(e["category"], e["start"], e["end"]) for e in events_zh}
            for e in events_en:
                if e["category"] != "seasonal_sale":
                    continue
                matched = any(
                    e2["name"] == e["name"] or e2["start"] == e["start"]
                    for e2 in events_zh if e2["category"] == "seasonal_sale"
                )
                if matched and (e["category"], e["start"], e["end"]) not in zh_windows:
                    log_event(
                        logger,
                        "Steam 活动中英文通道日期不一致，以英文页为准",
                        level=logging.WARNING,
                        detail={"活动": e["name"], "开始日期": e["start"]},
                    )

        rows = _merge_bilingual(events_en, events_zh, zh_nav_names)
        now = _now()
        prepared = [
            {
                "event_key": _event_key(e["category"], e["name_en"], e["start"],
                                        e.get("info_slug")),
                "category": e["category"],
                "name_en": e["name_en"],
                "name_zh": e["name_zh"],
                "start_date": e["start"],
                "end_date": e["end"],
                "source_updated_at": now,
            }
            for e in rows
        ]

        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            for row in prepared:
                stmt = sqlite_insert(SteamEvent).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[SteamEvent.event_key],
                    set_={
                        "category": stmt.excluded.category,
                        "name_en": stmt.excluded.name_en,
                        "name_zh": stmt.excluded.name_zh,
                        "start_date": stmt.excluded.start_date,
                        "end_date": stmt.excluded.end_date,
                        "source_updated_at": stmt.excluded.source_updated_at,
                    },
                )
                await session.execute(stmt)
            await session.commit()

        _write_snapshot("en", en_html)
        if zh_html is not None:
            _write_snapshot("zh", zh_html)

        tagged = await _backfill_price_history(prepared)
        _windows_cache = None  # 失效，下次打标查询按新窗口重建
        log_event(
            logger,
            "Steam 活动日历已同步",
            tag="成功",
            detail={"活动数": len(prepared), "回贴价格观测行": tagged},
        )
        return {"count": len(prepared), "backfilled": tagged, "fetchedAt": now.isoformat()}


def _write_snapshot(lang: str, raw: str) -> None:
    try:
        from app.core.config import get_settings

        snap_dir = get_settings().data_dir / "steam_events"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / f"upcoming_{lang}.html").write_text(raw, encoding="utf-8")
    except OSError:
        log_event(
            logger,
            "Steam 活动页快照写入失败",
            level=logging.ERROR,
            exc_info=True,
            detail={"语言": lang},
        )


async def _backfill_price_history(rows: list[dict]) -> int:
    """按活动窗口回贴价格观测行的周期标签（只补 NULL 行，幂等）。"""
    total = 0
    async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
        for row in rows:
            window_start = f"{row['start_date'].isoformat()} 00:00:00"
            window_end = (
                f"{(row['end_date'] + timedelta(days=1)).isoformat()} 00:00:00"
            )
            result = await session.execute(
                text(
                    "UPDATE game_price_history SET steam_event_key = :key "
                    "WHERE steam_event_key IS NULL "
                    "AND snapshot_at >= :ws AND snapshot_at < :we"
                ),
                {"key": row["event_key"], "ws": window_start, "we": window_end},
            )
            total += result.rowcount or 0
        await session.commit()
    return total


async def _load_windows() -> list[tuple[date, date, str]]:
    """季节大促优先的打标窗口（同一天同时落在季节大促与 Fest 时归季节大促）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(SteamEvent).order_by(SteamEvent.start_date)
            )
        ).scalars().all()
    priority = {"seasonal_sale": 0, "next_fest": 1, "themed_fest": 2}
    windows = [
        (r.start_date, r.end_date, r.event_key)
        for r in sorted(rows, key=lambda r: priority.get(r.category, 9))
    ]
    return windows


async def active_event_key_at(ts: datetime) -> str | None:
    """观测时刻所属的活动窗口 key；不在任何窗口内返回 None。

    带进程内缓存（打标发生在逐行观测路径上，不能每行都查库）。
    """
    global _windows_cache
    now = _now()
    if _windows_cache is None:
        _windows_cache = (await _load_windows(), now)
    else:
        windows, loaded_at = _windows_cache
        if (now - loaded_at).total_seconds() / 60 > _WINDOW_CACHE_TTL_MINUTES:
            _windows_cache = (await _load_windows(), now)
    assert _windows_cache is not None
    windows, _ = _windows_cache
    day = ts.date()
    for start, end, key in windows:
        if start <= day <= end:
            return key
    return None


async def refresh_if_stale() -> bool:
    """快照龄超阈值即补同步（启动链兜底）。返回是否触发了同步。"""
    from app.domains.settings.service import get_value

    if not await get_value("fetch.steam_events", True):
        return False
    async with get_session_factory()() as session:
        last = (
            await session.execute(select(func.max(SteamEvent.source_updated_at)))
        ).scalar()
    if last is not None:
        age_hours = (_now() - _as_naive(last)).total_seconds() / 3600
        if age_hours < SYNC_STALE_HOURS:
            return False
    try:
        await sync()
        return True
    except Exception:  # noqa: BLE001 —— 失败留日志等下一拍，旧数据继续展示
        log_event(logger, "Steam 活动日历启动补同步失败", level=logging.ERROR, exc_info=True)
        return False


def _serialize(row: SteamEvent) -> dict:
    return {
        "key": row.event_key,
        "category": row.category,
        "nameEn": row.name_en,
        "nameZh": row.name_zh,
        "start": row.start_date.isoformat(),
        "end": row.end_date.isoformat(),
        "preciseStartTs": row.precise_start_ts,
        "preciseEndTs": row.precise_end_ts,
        "storeUrl": row.store_url,
    }


async def list_events() -> dict:
    """活动全量 + 进行中 + 最近未来 + 新鲜度。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(SteamEvent).order_by(SteamEvent.start_date)
            )
        ).scalars().all()
    today = _now().date()
    events = [_serialize(r) for r in rows]
    live = [e for e in events if e["start"] <= today.isoformat() <= e["end"]]
    upcoming = [e for e in events if e["start"] > today.isoformat()]
    fetched_at = max((r.source_updated_at for r in rows), default=None)
    fetched_iso = fetched_at.isoformat() if fetched_at else None
    stale = fetched_at is None or (
        (_now() - _as_naive(fetched_at)).total_seconds() / 3600 > RESPONSE_STALE_HOURS
    )
    return {
        "events": events,
        "live": live,
        "next": upcoming[0] if upcoming else None,
        "fetchedAt": fetched_iso,
        "stale": stale,
    }
