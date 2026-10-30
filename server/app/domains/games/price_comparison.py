"""同商品、地区与币种的可靠本地价格比较。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import text

from .identity import STANDARD_PRODUCT_SQL


def _standard(alias: str) -> str:
    predicate = STANDARD_PRODUCT_SQL
    for name in ("is_gold", "version_suffix", "is_bundle", "product_unknown"):
        predicate = predicate.replace(name, f"{alias}.{name}")
    return predicate


def _reliable(alias: str) -> str:
    return (
        f"{alias}.price_status = 'ok' AND {alias}.price > 0 "
        f"AND {_standard(alias)} AND ({alias}.source = 'steam' OR "
        f"({alias}.source = 'community' AND EXISTS (SELECT 1 FROM game_price_history k "
        f"WHERE k.appid = {alias}.appid AND k.region_code = {alias}.region_code "
        f"AND k.sub_id = {alias}.sub_id AND k.currency = {alias}.currency "
        f"AND k.source = 'steam' AND k.price_status = 'ok' AND k.price > 0 "
        f"AND k.snapshot_at <= w.b AND {_standard('k')})))"
    )


_HISTORY_SQL = text(
    "WITH wanted AS (SELECT value->>'a' a, value->>'r' r, value->>'s' s, "
    "value->>'c' c, value->>'b' b, value->>'lo' lo, value->>'p' p "
    "FROM json_each(:units)), evidence AS ("
    "SELECT w.*, MIN(h.price) low, COUNT(h.id) points, "
    "MIN(h.snapshot_at) first_at, MAX(h.snapshot_at) last_at, "
    "(SELECT h2.id FROM game_price_history h2 WHERE h2.appid = w.a "
    "AND h2.region_code = w.r AND h2.sub_id = w.s AND h2.currency = w.c "
    "AND h2.snapshot_at < w.b AND (w.lo IS NULL OR h2.snapshot_at >= w.lo) "
    f"AND {_reliable('h2')} ORDER BY h2.snapshot_at DESC, h2.id DESC LIMIT 1) prev_id "
    "FROM wanted w LEFT JOIN game_price_history h ON h.appid = w.a "
    "AND h.region_code = w.r AND h.sub_id = w.s AND h.currency = w.c "
    "AND h.snapshot_at < w.b AND (w.lo IS NULL OR h.snapshot_at >= w.lo) "
    f"AND {_reliable('h')} GROUP BY w.a,w.r,w.s,w.c), latest AS ("
    "SELECT e.*, (SELECT s.id FROM game_price_history s WHERE s.appid=e.a "
    "AND s.region_code=e.r AND s.sub_id=e.s AND s.currency=e.c "
    "AND s.source='steam' AND s.price_status='ok' AND s.price>0 "
    f"AND {_standard('s')} AND s.snapshot_at<=e.b "
    "ORDER BY s.snapshot_at DESC,s.id DESC LIMIT 1) steam_id FROM evidence e) "
    "SELECT e.a,e.r,e.s,e.c,e.low,e.points,e.first_at,e.last_at, "
    "p.price,p.original_price,p.cny_fen,p.snapshot_at,p.source,p.id, "
    "EXISTS (SELECT 1 FROM game_current_prices cp WHERE cp.appid=e.a "
    "AND cp.region_code=e.r AND cp.sub_id=e.s AND cp.currency=e.c AND cp.price=e.p "
    "AND cp.updated_at=e.b AND cp.last_success_at=cp.updated_at "
    "AND cp.price_status='ok' AND cp.attempt_outcome='success' AND cp.steam_answer='ok' "
    "AND cp.price=(SELECT price FROM game_price_history WHERE id=e.steam_id)) current_known, e.steam_id "
    "FROM latest e LEFT JOIN game_price_history p ON p.id=e.prev_id"
)


def _datetime(value) -> datetime:
    return (value if isinstance(value, datetime) else datetime.fromisoformat(value)).replace(tzinfo=None)


def _iso(value) -> str | None:
    return _datetime(value).isoformat() if value else None


async def history_context(session, units: list[dict], *, before: datetime,
                          window_days: int | None = None) -> dict[tuple[int, str, int, str], dict]:
    """批量比较观测之前的历史；observedAt覆盖各单元上界，price用于核验当前观测。"""
    if window_days not in (None, 90, 180, 365):
        raise ValueError("window_days must be 90, 180, 365 or None")
    wanted = {}
    for unit in units:
        appid, sub = int(unit["appid"]), int(unit.get("subId") or 0)
        region = str(unit.get("region") or "").strip().upper()
        currency = str(unit.get("currency") or "").strip().upper()
        if appid <= 0 or sub <= 0 or not region or not currency:
            continue
        at = _datetime(unit.get("observedAt") or before)
        key = (appid, region, sub, currency)
        wanted[key] = {"a": appid, "r": region, "s": sub, "c": currency,
                       "b": at.isoformat(" ", timespec="microseconds"),
                       "lo": (at - timedelta(days=window_days)).isoformat(" ", timespec="microseconds")
                       if window_days else None, "p": unit.get("price")}
    if not wanted:
        return {}
    rows = (await session.execute(_HISTORY_SQL, {"units": json.dumps(list(wanted.values()))})).all()
    return {
        (int(a), r, int(s), c): {
            "previousPrice": price, "baselineLow": low, "firstAt": _iso(first),
            "lastAt": _iso(last), "points": points, "complete": False,
            "previousOriginalPrice": original, "previousCnyFen": cny,
            "previousObservedAt": _iso(at), "previousSource": source,
            "previousRowId": row_id, "currentKnown": bool(current_known),
            "observationId": steam_id if current_known else None,
        }
        for a, r, s, c, low, points, first, last, price, original, cny, at, source, row_id, current_known, steam_id in rows
    }
