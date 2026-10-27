"""Price facts and their query snapshot share the caller's transaction."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text

from app.crawler.utils import get_beijing_time_obj
from .identity import STANDARD_PRODUCT_SQL
from .scoring import smart_score
from .pricing import cny_fen_at_rate


def install_functions(connection) -> None:
    connection.create_function("game_smart_score", 6, smart_score)
    connection.create_function("price_cny_fen", 2, cny_fen_at_rate, deterministic=True)


def price_snapshot_sql(appids: list[int] | None, *, history: bool = True):
    ids = None if appids is None else list(dict.fromkeys(int(a) for a in appids))
    now = get_beijing_time_obj().replace(tzinfo=None)
    target_filter = "" if ids is None else "WHERE g.appid IN (SELECT value FROM json_each(:ids))"
    history_ctes = f"""
    , h AS MATERIALIZED (
        SELECT h.appid,h.id,h.snapshot_at,h.original_price,h.sub_id,
          COALESCE(h.cny_fen,CASE WHEN h.currency='CNY' THEN h.price END) AS cny_fen
        FROM targets t CROSS JOIN game_price_history h ON h.appid=t.appid
        WHERE h.region_code='CN' AND h.price_status='ok'
          AND {STANDARD_PRODUCT_SQL}
    ), lastdiff AS (
        SELECT h.appid, MAX(h.snapshot_at) AS at FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.cny_fen>0 AND CAST(ROUND(h.cny_fen/10.0) AS INTEGER)
          !=CAST(ROUND(t.cn_fen/10.0) AS INTEGER) GROUP BY h.appid
    ), era AS (
        SELECT h.appid, MIN(h.snapshot_at) AS at FROM h
        JOIN targets t ON t.appid=h.appid LEFT JOIN lastdiff d ON d.appid=h.appid
        WHERE h.cny_fen>0 AND CAST(ROUND(h.cny_fen/10.0) AS INTEGER)
          =CAST(ROUND(t.cn_fen/10.0) AS INTEGER)
          AND (d.at IS NULL OR h.snapshot_at>=d.at) GROUP BY h.appid
    ), prior AS (
        SELECT h.appid, MIN(h.cny_fen) AS fen FROM h JOIN era e ON e.appid=h.appid
        WHERE h.cny_fen>0 AND h.snapshot_at<e.at GROUP BY h.appid
    ), original AS (
        SELECT h.*, t.original_price AS current_original,
          ROW_NUMBER() OVER (PARTITION BY h.appid ORDER BY h.snapshot_at DESC,h.id DESC) AS rn
        FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.original_price IS NOT NULL AND t.original_price IS NOT NULL
          AND h.original_price!=t.original_price AND h.snapshot_at<t.observed_at
          AND (COALESCE(t.sub_id,0)=0 OR h.sub_id=t.sub_id)
    ), sequence AS (
        SELECT h.appid,h.original_price,h.snapshot_at,
          LAG(h.original_price) OVER (PARTITION BY h.appid ORDER BY h.snapshot_at,h.id) AS prev
        FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.original_price IS NOT NULL AND t.original_price IS NOT NULL
          AND (COALESCE(t.sub_id,0)=0 OR h.sub_id=t.sub_id)
    ), jumps AS (
        SELECT appid,MAX(snapshot_at) AS at FROM sequence
        WHERE prev IS NOT NULL AND prev!=original_price GROUP BY appid
    )
    """ if history else ""
    flag = """CASE WHEN t.cn_fen IS NULL OR t.cn_fen<=0 THEN 0
        WHEN e.at IS NULL THEN CASE WHEN t.discount>0 THEN 3 ELSE 0 END
        WHEN p.fen IS NULL THEN CASE WHEN t.discount>0 THEN
          CASE WHEN e.at>=:era_cutoff THEN 1 ELSE 2 END ELSE 0 END
        WHEN p.fen-t.cn_fen>=10 THEN 1 WHEN p.fen-t.cn_fen>-10 THEN 2
        ELSE CASE WHEN t.discount>0 THEN 3 ELSE 0 END END""" if history else "t.hl_flag"
    pp = "CASE WHEN o.rn=1 THEN CASE WHEN t.original_price<o.original_price THEN 1 ELSE 2 END ELSE 0 END" if history else "t.pp_flag"
    joins = "LEFT JOIN era e ON e.appid=t.appid LEFT JOIN prior p ON p.appid=t.appid LEFT JOIN original o ON o.appid=t.appid AND o.rn=1 LEFT JOIN jumps j ON j.appid=t.appid" if history else ""
    changed = "j.at" if history else "t.pp_changed_at"
    sql = f"""
    WITH targets AS MATERIALIZED (
        SELECT g.*, c.cny_fen AS cn_fen,c.price AS cn_price,c.original_price,
          COALESCE(c.discount_percent,0) AS discount,c.sub_id,c.updated_at AS observed_at
        FROM games g LEFT JOIN game_current_prices c
          ON c.appid=g.appid AND c.region_code='CN' AND c.price_status='ok'
        {target_filter}
    ) {history_ctes}, lowest AS (
        SELECT c.appid,MIN(c.cny_fen) AS fen FROM targets t
        CROSS JOIN game_current_prices c ON c.appid=t.appid
        WHERE c.region_code!='CN' AND c.price_status='ok' AND c.cny_fen>0
          AND c.attempt_outcome='success' GROUP BY c.appid
    ), result AS MATERIALIZED (
        SELECT t.appid,{flag} AS hl,{pp} AS pp,{changed} AS changed,
          l.fen AS minimum,MAX(COALESCE(t.cn_price-l.fen,0),0) AS difference,
          t.positive_rate,t.review_count,t.release_date,t.discount
        FROM targets t LEFT JOIN lowest l ON l.appid=t.appid {joins}
    )
    UPDATE games AS g SET hl_flag=r.hl,pp_flag=r.pp,pp_changed_at=r.changed,
      min_cny_fen=r.minimum,diff_fen=r.difference,
      smart_score=game_smart_score(r.difference,r.positive_rate,r.review_count,
        r.hl,r.discount,r.release_date)
    FROM result r WHERE g.appid=r.appid
    """
    return sql, {"ids": json.dumps(ids), "era_cutoff": (now-timedelta(days=30)).isoformat(" ")}


async def catalog_inputs(session, appids: list[int]) -> dict:
    rows = await session.execute(text(
        "SELECT appid,name,name_en,type,positive_rate,review_count,release_date,series_id FROM games "
        "WHERE appid IN (SELECT value FROM json_each(:ids))"
    ), {"ids": json.dumps(appids)})
    return {row[0]: tuple(row[1:]) for row in rows}


async def update_price_derived(appids: list[int] | None, *, session=None,
                               history: bool = True, catalog_before: dict | None = None) -> int:
    if appids == []:
        return 0
    if session is None:
        from app.core.database import write_session

        async with write_session(label="price_snapshot") as own:
            count = await update_price_derived(appids, session=own, history=history,
                                               catalog_before=catalog_before)
            await own.commit()
            return count
    from app.core.database import write_gate

    async with write_gate():
        if hasattr(session, "flush"):
            await session.flush()
        sql, params = price_snapshot_sql(appids, history=history)
        await session.execute(text(sql), params)
        count = int((await session.execute(text("SELECT changes()"))).scalar() or 0)
        if catalog_before is not None:
            current = await catalog_inputs(session, appids)
            if any(old[:3] != current.get(aid, ())[:3] for aid, old in catalog_before.items()) or current.keys() != catalog_before.keys():
                from .series import refresh_series

                changed = {aid: (catalog_before.get(aid) or (None,))[-1] or ""
                           for aid in current.keys() | catalog_before.keys()
                           if current.get(aid, ())[:3] != catalog_before.get(aid, ())[:3]}
                await refresh_series(session=session, changed=changed)
            changed = [aid for aid, values in current.items()
                       if values[3:6] != catalog_before.get(aid, ())[3:6]]
            await update_member_bundles(session, changed)
        return count


async def update_member_bundles(session, appids: list[int]) -> None:
    if not appids:
        return
    rows = await session.execute(text(
        "SELECT DISTINCT p.bundle_id FROM bundle_region_prices p,json_each(p.app_ids) a "
        "WHERE CAST(a.value AS INTEGER) IN (SELECT value FROM json_each(:ids))"
    ), {"ids": json.dumps(appids)})
    ids = [row[0] for row in rows]
    if ids:
        from app.domains.bundles.service import refresh_bundle_sort_cache

        await refresh_bundle_sort_cache(ids, session=session)


def input_version() -> dict:
    from .series import load_overrides

    return {"day": get_beijing_time_obj().date().isoformat(), "series": load_overrides()}


async def save_input_version(session, version: dict) -> None:
    from app.core.database import write_gate

    async with write_gate():
        await session.execute(text(
            "INSERT INTO app_settings(key,value_json) VALUES('games.derived_inputs',:value) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"
        ), {"value": json.dumps(version, sort_keys=True)})


async def maintain_derived() -> None:
    from app.core.database import get_session_factory, write_session

    version = input_version()
    query = text("SELECT value_json FROM app_settings WHERE key='games.derived_inputs'")
    async with get_session_factory()() as session:
        saved = (await session.execute(query)).scalar()
    if saved and json.loads(saved) == version:
        return
    async with write_session(label="price_calendar") as session:
        saved = (await session.execute(query)).scalar()
        old = json.loads(saved) if saved else {}
        if old == version:
            return
        if old.get("day") != version["day"]:
            low_ids = list((await session.execute(text("SELECT appid FROM games WHERE hl_flag=1"))).scalars())
            await update_price_derived(low_ids, session=session)
            score_ids = list((await session.execute(text(
                "SELECT appid FROM games WHERE COALESCE(review_count,0)=0"
            ))).scalars())
            await update_price_derived(score_ids, session=session, history=False)
            await update_member_bundles(session, score_ids)
        if old.get("series") != version["series"]:
            from .series import refresh_series

            await refresh_series(session=session)
        await save_input_version(session, version)
        await session.commit()
