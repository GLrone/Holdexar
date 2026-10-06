"""优惠与行情类工具：HB 月包 / Epic·Steam 喜加一 / 价格事件 / 汇率 / 日历。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.crawl import events as crawl_events_service
from app.domains.games import service as games_service
from app.domains.metadata import service as metadata_service
from app.domains.rates import service as rates_service
from app.domains.steam_events import service as steam_events_service


async def hb_monthly() -> dict:
    """当月 HB 慈善包 contents：1:1 metadata.hb_choice_offers（库内已爬快照）。

    每行带现价与史低（min_cny_fen）；包未入库返回空态原因。"""
    payload = await metadata_service.hb_choice_offers()
    games = payload.get("games") or []
    rows = [
        _shared.offer_row(
            g.get("name") or f"AppID {g.get('appid')}",
            price_fen=g.get("priceFen"),
            low_fen=g.get("lowestCnyFen"),
            extra_key="offerDiscount" if g.get("discount") else None,
            extra_val=f"-{g.get('discount')}%" if g.get("discount") else None,
        )
        for g in games
    ]
    return _shared.rows_result("hb", rows, total=len(rows))


async def epic_free() -> dict:
    """Epic 当期 + 预告白送（库快照/缓存，不触发外网）。"""
    payload = await metadata_service.epic_free_offers()
    offers = payload.get("offers") or []
    rows = [
        {
            "k": o.get("titleCn") or o.get("title") or (f"AppID {o['appid']}" if o.get("appid") else "—"),
            "vKey": "epicUpcoming" if o.get("upcoming") else "epicFree",
            "v": str(o.get("end") or o.get("start") or ""),
            "tone": "ok" if not o.get("upcoming") else "warn",
        }
        for o in offers
    ]
    return _shared.rows_result("epic", rows, total=len(rows))


async def steam_free() -> dict:
    """Steam 限时免费清单（库内 promo 快照）。"""
    payload = await metadata_service.steam_free_offers()
    games = payload.get("games") if isinstance(payload, dict) else None
    rows = [_shared.offer_row(g.get("name") or f"AppID {g.get('appid')}") for g in (games or [])]
    return _shared.rows_result("steamFree", rows, total=len(rows))


async def price_drops() -> dict:
    """最近价格事件（新低/降价/锁区等，price_events 账本只读投影）。"""
    events = await crawl_events_service.list_events(limit=20)
    ids = [int(e.get("appid") or 0) for e in events if e.get("appid")]
    briefs = await games_service.briefs_for(ids) if ids else {}
    items = [
        _shared.brief_item(
            int(e["appid"]), briefs.get(int(e["appid"])) if e.get("appid") else None,
            # 事件类型入库为大写枚举，词条键为小写——统一在此对齐，缺词条时前端会漏原始键
            note={"key": f"ev_{str(e.get('eventType') or '').lower()}", "v": str(e.get("region") or ""),
                  "at": e.get("occurredAt")},
        )
        for e in events if e.get("appid")
    ]
    return _shared.games_card("drops", items, total=len(items))


async def rates_overview() -> dict:
    """汇率快照（fx_rates 本地库，零外网）。"""
    payload = await rates_service.list_rates()
    rows = [
        {"k": r.get("currency") or "", "vKey": "rateLine", "data": {"rate": r.get("rateToCny")}}
        for r in (payload.get("rates") or [])
    ]
    return _shared.rows_result("rates", rows, total=len(rows))


async def calendar_events() -> dict:
    """Steam 官方活动日历（库内已爬，进行中在前）。"""
    payload = await steam_events_service.list_events()
    live = payload.get("live") or []
    upcoming = (payload.get("upcoming") or [])[:_shared._READ_ROWS_LIMIT]
    rows = [
        {"k": e.get("nameZh") or e.get("nameEn") or e.get("key") or "—",
         "vKey": "eventLive", "v": f"{e.get('start')}~{e.get('end')}", "tone": "ok"}
        for e in live
    ] + [
        {"k": e.get("nameZh") or e.get("nameEn") or e.get("key") or "—",
         "vKey": "eventUpcoming", "v": str(e.get("start") or "")}
        for e in upcoming
    ]
    return _shared.rows_result("calendar", rows, total=len(rows))


async def _hb_monthly(args: dict, sid: str | None = None) -> dict:
    return await hb_monthly()


async def _epic_free(args: dict, sid: str | None = None) -> dict:
    return await epic_free()


async def _steam_free(args: dict, sid: str | None = None) -> dict:
    return await steam_free()


async def _price_drops(args: dict, sid: str | None = None) -> dict:
    return await price_drops()


async def _rates_overview(args: dict, sid: str | None = None) -> dict:
    return await rates_overview()


async def _calendar_events(args: dict, sid: str | None = None) -> dict:
    return await calendar_events()


SPECS = [
    ToolSpec(
        name="hb_monthly", group="read", risk="low",
        description="列出当月 HB 慈善包（Humble Choice）的游戏清单，含国区现价与史低。"
                    "用户问『HB 月包/本月慈善包有哪些游戏』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_hb_monthly, step_label="hbMonthly",
    ),
    ToolSpec(
        name="epic_free", group="read", risk="low",
        description="列出 Epic 当期正在送和预告即将送的游戏。用户问『Epic 喜加一/免费游戏』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_epic_free, step_label="epicFree",
    ),
    ToolSpec(
        name="steam_free", group="read", risk="low",
        description="列出 Steam 正在限时免费的游戏。用户问『Steam 有什么免费领』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_steam_free, step_label="steamFree",
    ),
    ToolSpec(
        name="price_drops", group="read", risk="low",
        description="列出最近的价格事件（新史低、降价、锁区、重新上架等）。"
                    "用户问『最近有什么降价』『哪些游戏创新低』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_price_drops, step_label="priceDrops",
    ),
    ToolSpec(
        name="rates_overview", group="read", risk="low",
        description="查看各币种对人民币的当前汇率。用户问『现在汇率多少』『某区折合人民币怎么算』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_rates_overview, step_label="rates",
    ),
    ToolSpec(
        name="calendar_events", group="read", risk="low",
        description="查看 Steam 官方活动日历（进行中的促销与即将开始的活动）。用户问『最近有什么促销/打折活动』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_calendar_events, step_label="calendar",
    ),
]
