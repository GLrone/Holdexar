"""捆绑包分页查询、详情与写时排序快照。"""
from __future__ import annotations

import json
import re

from sqlalchemy import literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, get_session_factory, write_session
from app.domains.crawl import freshness as freshness_service
from app.domains.games.models import Bundle, BundleRegionPrice, Game, GameCurrentPrice
from app.domains.games.pricing import (
    build_steam_header_url,
    format_minor_units,
)
from app.domains.games.scoring import (
    W_FAMILIARITY,
    W_QUALITY,
    W_SAVE,
    W_TIMING,
    familiarity_score,
    quality_score,
    save_score,
)

# Steam 图片 CDN 三域同库：akamai 域国内直连可达（fastly 被墙/不稳定，
# queniuqe 为历史镜像域）。入库原样保留，展示期统一归一到 akamai。
_IMG_CDN_HOSTS = re.compile(
    r"^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com", re.IGNORECASE
)
_IMG_CDN_REPLACEMENT = "https://shared.akamai.steamstatic.com"

def _normalize_image(url: str | None) -> str | None:
    """图片 CDN 域归一化：fastly/queniuqe → akamai（路径不变，同图可达）。"""
    if not url:
        return None
    return _IMG_CDN_HOSTS.sub(_IMG_CDN_REPLACEMENT, url)


def _parse_app_ids(raw) -> list[int]:
    """app_ids JSON 列 → int 列表（脏数据容错：空/坏 JSON 回空表）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [int(a) for a in raw]
        except (TypeError, ValueError):
            return []
    try:
        return [int(a) for a in json.loads(raw)]
    except (TypeError, ValueError):
        return []


def _store_url(bundle: Bundle, item_kind: int) -> str:
    """商店链接：item_kind=1 是 Sub 形态，链接用 /sub/（形态与购买语义解耦）。"""
    if bundle.url:
        return bundle.url
    kind = "sub" if item_kind == 1 else "bundle"
    return f"https://store.steampowered.com/{kind}/{bundle.bundle_id}/"


def _fallback_image(bundle: Bundle, item_kind: int) -> str:
    """无封面兜底 URL：bundle 与 sub 的 CDN 路径不同（steam/bundles vs steam/subs）。"""
    kind = "subs" if item_kind == 1 else "bundles"
    return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/{kind}/{bundle.bundle_id}/header.jpg"


async def _tracked_region_codes() -> set[str] | None:
    """追踪区（crawl_regions 表启用集，大写）；空/异常返回 None = 不过滤（全量）。"""
    try:
        from app.domains.regions.service import enabled_regions

        codes = await enabled_regions()
        return {c.upper() for c in codes} if codes else None
    except Exception:  # noqa: BLE001
        return None


def _filter_region_prices(
    region_prices: dict[str, dict], tracked: set[str] | None
) -> dict[str, dict]:
    """按追踪区集过滤区域价表；南亚双区（PK/BD）特殊处理。

    - 未启用任何区 / 后端不可达 → 不过滤（对齐游戏卡 GPW「未启用即全部」语义）；
    - CN 恒保留（国区价是差价基准列）；
    - 南亚：pk（巴基斯坦）与 BD（孟加拉）是两个独立 Steam 区但共享"南亚"
      追踪位。本端爬虫 CC_LIST 只有 pk 位（BD 在 refresh 容灾时以 BD 落行）。
      启用集含 pk 时：PK、BD 两行都放行（有哪个显示哪个，最低价取两者更低价）；
      启用集不含 pk 时：两行都剔除。
    """
    if tracked is None:
        return region_prices
    out: dict[str, dict] = {}
    for code, rp in region_prices.items():
        if code == "CN":
            out[code] = rp
        elif code in ("PK", "BD"):
            if "PK" in tracked:
                out[code] = rp
        elif code in tracked:
            out[code] = rp
    return out


def _family_region_prices(
    bundle: Bundle, price_rows: list, tracked: set[str] | None
) -> tuple[dict[str, dict], list[int]]:
    """双产品隔离 + 同区去重 + 追踪区过滤后的区表 / 基准 appids。

    列表聚合（_aggregate）与排序快照（refresh_bundle_sort_cache）共用——
    「展示的区表」与「排序读的快照」必须同源，不能各算各的。

    ── 双产品隔离 ──
    refresh 双轨降级按区混用 Bundle/Package API 时，同一 bundle_id 会出现两套
    appids（bundle 本体 vs 同号 sub 的 appid 列表）。以 appids 最多的行为"主产品"
    （baseline 语义），其余行若 appids 与主产品无交集（异种产品，见 bundle
    61597 = sub 347440 污染），整行剔除——锁区判定/最低价不再被带偏。
    同一行的 app_ids 在本函数里要用三次以上（挑主行 / 同族过滤 / 区域表），
    逐行只解析一次（同一行要用三次以上，重复 json.loads 是纯开销）。
    memo 挂在行主键上且只活在本次调用里（局部字典，不占常驻内存）。

    返回 (region_prices, baseline_appids)；条目字段：
    priceMinor/currency/discountPercent/baseDiscount/discountEndsAt/cnyFen/appIds。
    """
    parsed_aids: dict[int, list[int]] = {}

    def aids_of(p) -> list[int]:
        key = p.id
        cached = parsed_aids.get(key)
        if cached is None:
            cached = _parse_app_ids(p.app_ids)
            parsed_aids[key] = cached
        return cached

    rows: list = []
    for p in price_rows:
        if not (p.region_code or "").strip():
            continue
        rows.append(p)
    main_row = max(rows, key=lambda p: len(aids_of(p)), default=None)
    baseline_appids: list[int] = aids_of(main_row) if main_row is not None else []
    baseline_set = set(baseline_appids)
    family_rows = [p for p in rows if baseline_set & set(aids_of(p))]
    # 主行都无 appids（全部为空）时不能全剔：退化为原全量行为
    rows = family_rows if (baseline_appids and len(family_rows) > 0) else rows

    region_prices: dict[str, dict] = {}
    for p in rows:
        code = (p.region_code or "").upper()
        aids = aids_of(p)
        if len(aids) > len(baseline_appids):
            baseline_appids = aids
        entry = {
            "priceMinor": int(p.price) if p.price is not None else None,
            "currency": (p.currency or "").upper() or None,
            "discountPercent": p.discount_percent or 0,
            "baseDiscount": p.bundle_base_discount or 0,
            # cny_fen 是当前价快照的完整字段（爬取/导入写库时落值，
            # 汇率变更由 recompute_cny_fen_all 重算）——不在这里现算
            "cnyFen": int(p.cny_fen) if p.cny_fen is not None else None,
            # 促销截止（Unix 秒，browse active_discounts 下发；NULL=无促销）。
            # 已过期行随展示层时间守卫隐藏——抓取间隙促销结束属正常滞留
            "discountEndsAt": int(p.discount_end_ts) if p.discount_end_ts else None,
            "appIds": aids,
        }
        # 源数据存在大小写并存的同区行（如 jp/JP）：有价者优先，其余丢弃，
        # 保证同一区键唯一且取到有效行
        existing = region_prices.get(code)
        if existing is None or (existing["cnyFen"] is None and entry["cnyFen"] is not None):
            region_prices[code] = entry

    # 追踪区过滤（南亚 PK/BD 双区适配）→ 最低价/差价在过滤后的区内取
    return _filter_region_prices(region_prices, tracked), baseline_appids


def _aggregate(
    bundle: Bundle,
    price_rows: list[BundleRegionPrice],
    tracked: set[str] | None = None,
) -> dict:
    """单包聚合：区域价表 / 基准 appids / 锁区数 + 排序快照读取。

    最低价/差价不在这里算：读 bundles.min_cny_fen / diff_fen（写时由
    refresh_bundle_sort_cache 维护）。lowestRegion/lowestCnyFen 是对快照的
    展示级查表——全区最低 = min(国区价, 非国区最低快照)，最低区 = 命中该
    值的有价区（行被新一轮抓取换掉、快照尚未重建的短窗口内可能查不到，
    此时返回空串，由下一轮写侧重建自愈）。

    tracked：追踪区集（大写）。给定后区域价表按追踪区过滤（快照同一口径）；
    None = 不过滤。南亚 PK/BD 双区见 _filter_region_prices。
    """
    mps = bundle.must_purchase_as_set if bundle.must_purchase_as_set is not None else -1
    item_kind = bundle.item_kind if bundle.item_kind is not None else -1
    # v3 之前的行 item_kind 尚未回填（-1）：形态沿用 mps 旧值兜底
    if item_kind not in (0, 1):
        item_kind = mps

    region_prices, baseline_appids = _family_region_prices(bundle, price_rows, tracked)

    baseline_set = set(baseline_appids)
    for rp in region_prices.values():
        rp["lockedCount"] = len(baseline_set - set(rp["appIds"]))
        rp["formatted"] = format_minor_units(rp["priceMinor"], rp["currency"] or "")

    min_cny_fen = int(bundle.min_cny_fen) if bundle.min_cny_fen is not None else None
    cn_cny_fen = region_prices.get("CN", {}).get("cnyFen")
    candidates = [v for v in (cn_cny_fen, min_cny_fen) if v is not None]
    lowest_cny_fen = min(candidates) if candidates else None
    lowest_code = ""
    if lowest_cny_fen is not None:
        if cn_cny_fen is not None and cn_cny_fen <= lowest_cny_fen:
            lowest_code = "cn"
        else:
            lowest_code = next(
                (
                    code.lower()
                    for code, rp in region_prices.items()
                    if code != "CN" and rp["cnyFen"] == lowest_cny_fen
                ),
                "",
            )

    return {
        "bundleId": bundle.bundle_id,
        "name": bundle.name,
        "headerImage": _normalize_image(bundle.header_image) or _fallback_image(bundle, item_kind),
        "url": _store_url(bundle, item_kind),
        "mustPurchaseAsSet": mps,
        "itemKind": item_kind,
        "appIds": baseline_appids,
        "regionPrices": region_prices,
        "cnCnyFen": cn_cny_fen,
        "lowestRegion": lowest_code,
        "lowestCnyFen": lowest_cny_fen,
        "diffFen": int(bundle.diff_fen or 0),
        # smart 评分随载荷下发：前端选区时用「save(该区差价) + 评分余量」做
        # 地区重锚排序（余量 = 评分 − save(快照差价)，与地区无关的三因子和）
        "smartScore": float(bundle.smart_score or 0.0),
        # 价格数据状态（观察时刻/新鲜度分档）：观察时刻 = 该包全部价格行的
        # MAX(crawled_at)，与 games 的 priceData 同口径（被观察过即算，不按
        # 价格有无过滤）；无 coverage——捆绑包不属价格刷新 Cycle 的期望集，
        # 不冒充覆盖率。
        "priceData": freshness_service.freshness_of(
            max(
                (p.crawled_at for p in price_rows if p.crawled_at is not None),
                default=None,
            )
        ),
    }


def register_bundle_sql_functions(connection) -> None:
    for name, count, function in (
        ("bundle_lower", 1, lambda value: (value or "").lower()),
        ("bundle_save_score", 1, save_score),
        ("bundle_quality_score", 3, quality_score),
        ("bundle_familiarity_score", 1, familiarity_score),
    ):
        connection.create_function(name, count, function, deterministic=True)


async def refresh_bundle_sort_cache(
    bundle_ids: list[int] | None = None, *, session: AsyncSession | None = None
) -> int:
    """批量更新指定包排序快照；注入会话时沿用调用方事务。"""
    if bundle_ids == []:
        return 0
    if session is None:
        async with write_session(WritePriority.BACKGROUND) as own:
            written = await refresh_bundle_sort_cache(bundle_ids, session=own)
            await own.commit()
            return written
    from app.core.database import write_gate

    async with write_gate():
        await session.flush()
        connection = await session.connection()
        await connection.run_sync(lambda conn: register_bundle_sql_functions(conn.connection.dbapi_connection))
        sql, params = bundle_sort_sql(bundle_ids, await _tracked_region_codes())
        await session.execute(text(sql), params)
        return int((await session.execute(text("SELECT changes()"))).scalar() or 0)


_LIST_REGION_DROP = ("appIds", "lockedCount", "formatted")


def _json_array_sql(column: str) -> str:
    return (f"CASE WHEN json_valid({column}) THEN CASE WHEN json_type({column}) = 'array' "
            f"THEN {column} ELSE '[]' END ELSE '[]' END")


def _region_rows_sql(target_where: str = ":bundle_ids IS NULL OR b.bundle_id IN (SELECT value FROM json_each(:bundle_ids))") -> str:
    return f"""
WITH targets AS MATERIALIZED (
    SELECT bundle_id, name, must_purchase_as_set, min_cny_fen, diff_fen, smart_score
    FROM bundles b WHERE {target_where}
), raw AS MATERIALIZED (
    SELECT p.id, p.bundle_id, p.cny_fen, p.discount_percent, p.bundle_base_discount,
           UPPER(p.region_code) AS code,
           {_json_array_sql('p.app_ids')} AS aids
    FROM targets t JOIN bundle_region_prices p USING (bundle_id)
    WHERE TRIM(COALESCE(p.region_code, '')) != ''
), ranked AS (
    SELECT bundle_id, aids, ROW_NUMBER() OVER (
        PARTITION BY bundle_id ORDER BY json_array_length(aids) DESC, id
    ) AS n FROM raw
), baseline AS MATERIALIZED (
    SELECT bundle_id, aids FROM ranked WHERE n = 1
), family AS (
    SELECT r.*, ROW_NUMBER() OVER (
        PARTITION BY r.bundle_id, r.code ORDER BY r.cny_fen IS NULL, r.id
    ) AS n
    FROM raw r JOIN baseline b USING (bundle_id)
    WHERE (json_array_length(b.aids) = 0 OR EXISTS (
        SELECT 1 FROM json_each(r.aids) a JOIN json_each(b.aids) m
        ON CAST(a.value AS INTEGER) = CAST(m.value AS INTEGER)
    )) AND (:tracked IS NULL OR r.code = 'CN' OR
        (r.code IN ('PK', 'BD') AND EXISTS (
            SELECT 1 FROM json_each(:tracked) WHERE value = 'PK'
        )) OR (r.code NOT IN ('PK', 'BD') AND EXISTS (
            SELECT 1 FROM json_each(:tracked) WHERE value = r.code
        )))
), regions AS MATERIALIZED (
    SELECT * FROM family WHERE n = 1
), prices AS (
    SELECT bundle_id, MAX(CASE WHEN code = 'CN' THEN cny_fen END) AS cn,
           MAX(CASE WHEN code = :region THEN cny_fen END) AS regional,
           MAX(COALESCE(discount_percent, 0)) AS discount,
           COALESCE(MAX(CASE WHEN code = :region THEN discount_percent END), 0) AS regional_discount,
           MAX(CASE WHEN code != 'CN' AND cny_fen > 0 THEN 1 ELSE 0 END) AS gift,
           MIN(CASE WHEN code != 'CN' AND cny_fen > 0 THEN cny_fen END) AS minimum,
           MAX(CASE WHEN cny_fen IS NOT NULL AND COALESCE(discount_percent, 0)
               > COALESCE(bundle_base_discount, 0) THEN 1 ELSE 0 END) AS timing
    FROM regions GROUP BY bundle_id
)
"""


def bundle_sort_sql(bundle_ids: list[int] | None, tracked: set[str] | None) -> tuple[str, dict]:
    sql = _region_rows_sql() + f"""
    , reviews AS (
        SELECT b.bundle_id,
            AVG(bundle_quality_score(g.positive_rate, g.review_count, g.release_date)) AS quality,
            AVG(bundle_familiarity_score(g.review_count)) AS familiarity
        FROM baseline b JOIN json_each(b.aids) a
        JOIN games g ON g.appid = CAST(a.value AS INTEGER) GROUP BY b.bundle_id
    ), result AS (
        SELECT b.bundle_id, p.minimum,
            MAX(COALESCE(p.cn - p.minimum, 0), 0) AS difference,
            p.cn IS NOT NULL AND COALESCE(p.minimum, p.cn) >= p.cn - 500 AS lowest,
            COALESCE(v.quality, 0.5) AS quality, COALESCE(v.familiarity, 0) AS familiarity,
            COALESCE(p.timing, 0) AS timing
        FROM targets b LEFT JOIN prices p USING (bundle_id) LEFT JOIN reviews v USING (bundle_id)
    )
    UPDATE bundles AS b SET min_cny_fen = r.minimum, diff_fen = r.difference,
        is_lowest = r.lowest, smart_score = {W_SAVE} * bundle_save_score(r.difference)
            + {W_QUALITY} * r.quality + {W_TIMING} * r.timing + {W_FAMILIARITY} * r.familiarity
    FROM result r WHERE b.bundle_id = r.bundle_id
    """
    return sql, {"bundle_ids": json.dumps(bundle_ids) if bundle_ids is not None else None,
                 "tracked": json.dumps(sorted(tracked)) if tracked else None, "region": ""}


def _visible_bundle_sql() -> str:
    basis = (f"(SELECT {_json_array_sql('m.app_ids')} FROM bundle_region_prices m "
             "WHERE m.bundle_id=b.bundle_id AND TRIM(COALESCE(m.region_code,''))!='' "
             f"ORDER BY json_array_length({_json_array_sql('m.app_ids')}) DESC,m.id LIMIT 1)")
    return f"""EXISTS (SELECT 1 FROM bundle_region_prices p
        WHERE p.bundle_id=b.bundle_id AND TRIM(COALESCE(p.region_code,''))!=''
        AND (:tracked IS NULL OR UPPER(p.region_code)='CN' OR
            (UPPER(p.region_code) IN ('PK','BD') AND EXISTS (
                SELECT 1 FROM json_each(:tracked) WHERE value='PK')) OR
            (UPPER(p.region_code) NOT IN ('PK','BD') AND EXISTS (
                SELECT 1 FROM json_each(:tracked) WHERE value=UPPER(p.region_code))))
        AND (EXISTS (SELECT 1 FROM json_each({_json_array_sql('p.app_ids')}) a
            JOIN json_each({basis}) m ON CAST(a.value AS INTEGER)=CAST(m.value AS INTEGER))
            OR json_array_length({basis})=0))"""


_OWNERSHIP_SQL = """
, owned_apps AS MATERIALIZED (
    SELECT w.appid, MAX(:primary = '' OR w.steamid = :primary) AS self_owned,
        MIN(CASE WHEN :primary != '' AND w.steamid != :primary THEN
            COALESCE(NULLIF(a.label, ''), NULLIF(a.persona_name, ''), a.steamid) END) AS family_account
    FROM wishlist_items w JOIN tracked_accounts a ON a.steamid = w.steamid
    WHERE w.active = 1 AND w.owned = 1 GROUP BY w.appid
), ownership AS MATERIALIZED (
    SELECT b.bundle_id,
        CASE WHEN COUNT(*)=COUNT(o.appid) THEN CASE WHEN SUM(COALESCE(o.self_owned,0))>0 THEN 'owned'
            WHEN MIN(o.family_account) IS NOT NULL THEN 'family' ELSE 'owned' END END AS ownership_type,
        MIN(o.family_account) AS family_account
    FROM baseline b JOIN json_each(b.aids) m
    JOIN games g ON g.appid=CAST(m.value AS INTEGER) AND COALESCE(g.name,'')!=''
    LEFT JOIN owned_apps o ON o.appid=g.appid GROUP BY b.bundle_id
)
"""


async def _page_ownership(session: AsyncSession, items: list[dict], primary: str) -> None:
    if not items:
        return
    sql = """WITH baseline AS (
        SELECT json_extract(value,'$.id') AS bundle_id, json_extract(value,'$.aids') AS aids
        FROM json_each(:page)
    )""" + _OWNERSHIP_SQL + " SELECT * FROM ownership"
    rows = (await session.execute(text(sql), {
        "primary": primary, "page": json.dumps([{"id": item["bundleId"], "aids": item["appIds"]} for item in items]),
    })).mappings().all()
    by_id = {row["bundle_id"]: row for row in rows}
    for item in items:
        row = by_id.get(item["bundleId"])
        kind = row["ownership_type"] if row else None
        item["ownership"] = {"type": kind, "account": row["family_account"] if kind == "family" else None}


async def list_bundles(
    sort: str = "diff", *, offset: int = 0, limit: int = 120, removed: bool = False,
    query: str = "", region: str = "", region_mode: str = "all",
    followed_only: bool = False, completable_only: bool = False,
    cn_lowest_only: bool = False, only_discounted: bool = False, gift_only: bool = False,
    hide_owned: bool = False, hide_family: bool = False,
    price_min: float | None = None, price_max: float | None = None,
    diff_min: float | None = None, diff_max: float | None = None, diff_type: str = "absolute",
) -> dict:
    from app.domains.account import service as account_service
    from app.domains.games.derived import maintain_derived
    from app.domains.settings.service import get_value

    await maintain_derived()
    primary = (await account_service.get_primary_steam_id()) or (await get_value("account.steam_id", "")) or ""
    region = region.strip().upper()
    tracked = await _tracked_region_codes()
    params = {"tracked": json.dumps(sorted(tracked)) if tracked else None,
              "region": region.strip().upper(), "offset": max(0, offset), "limit": max(1, min(limit, 200)),
              "removed": int(removed), "query": query.strip().lower(), "primary": primary, "bundle_ids": None,
              "price_min": price_min, "price_max": price_max, "diff_min": diff_min, "diff_max": diff_max}
    from app.domains.monitoring.models import MonitorTarget, NON_CRAWL_STATES

    blocked = select(MonitorTarget.id).where(
        MonitorTarget.target_type == "bundle", MonitorTarget.target_id == literal_column("b.bundle_id"),
        MonitorTarget.state.in_(NON_CRAWL_STATES),
    ).exists().compile(compile_kwargs={"literal_binds": True})
    target_conditions = [f"({blocked})=:removed"]
    conditions = []
    if query.strip():
        target_conditions.append("instr(bundle_lower(b.name), :query)>0")
    if followed_only:
        target_conditions.append("EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type='bundle' "
            "AND s.target_id=b.bundle_id AND s.source='favorite' AND s.active=1)")
    if completable_only:
        target_conditions.append("b.must_purchase_as_set=0")
    if region and region_mode == "cheaper":
        conditions.append("cn > regional")
        target_conditions.append("EXISTS (SELECT 1 FROM bundle_region_prices r WHERE r.bundle_id=b.bundle_id "
            "AND UPPER(r.region_code)=:region AND r.cny_fen IS NOT NULL)")
    elif region and region_mode == "locked":
        conditions.append("regional IS NULL")
    if cn_lowest_only:
        conditions.append("cn IS NOT NULL AND (min_cny_fen IS NULL OR cn <= min_cny_fen)")
    if only_discounted:
        conditions.append("discount > 0")
    if gift_only:
        conditions.append("cn > 0 AND gift = 1")
    if hide_owned:
        conditions.append("COALESCE(ownership_type, '') != 'owned'")
    if hide_family:
        conditions.append("COALESCE(ownership_type, '') != 'family'")
    for name, operator in (("price_min", ">="), ("price_max", "<=")):
        if params[name] is not None:
            conditions.append(f"cn {operator} :{name} * 100")
    diff_value = "diff_value * 100.0 / NULLIF(cn, 0)" if diff_type == "percent" else "diff_value / 100.0"
    if diff_type == "percent" and (diff_min is not None or diff_max is not None):
        conditions.append("cn > 0")
    for name, operator in (("diff_min", ">="), ("diff_max", "<=")):
        if params[name] is not None:
            conditions.append(f"({diff_value}) {operator} :{name}")
    score = "b.smart_score" if sort == "smart" else "b.diff_fen"
    if sort == "discount":
        discount = "regional_discount" if region else "discount"
        order = f"{discount} DESC, diff_fen DESC, followed DESC, bundle_id"
    elif region and sort == "smart":
        order = ("bundle_save_score(COALESCE(cn - regional, 0)) + "
                 "MAX(0, COALESCE(smart_score, 0) - bundle_save_score(COALESCE(diff_fen, 0))) DESC, "
                 "followed DESC, smart_score DESC, bundle_id")
    elif region:
        order = "cn - regional DESC, followed DESC, diff_fen DESC, bundle_id"
    else:
        order = "followed DESC, score DESC, bundle_id"
    ownership_filter = hide_owned or hide_family
    ownership_cte = _OWNERSHIP_SQL if ownership_filter else ""
    ownership_columns = "o.ownership_type, o.family_account" if ownership_filter else "NULL AS ownership_type, NULL AS family_account"
    ownership_join = "LEFT JOIN ownership o USING (bundle_id)" if ownership_filter else ""
    sql = _region_rows_sql(" AND ".join(target_conditions)) + ownership_cte + f"""
    , candidates AS (
        SELECT b.*, p.*, {score} AS score,
               CASE WHEN :region != '' AND p.cn IS NOT NULL AND p.regional IS NOT NULL
                    THEN p.cn - p.regional ELSE COALESCE(b.diff_fen, 0) END AS diff_value,
               EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type = 'bundle'
                   AND s.target_id = b.bundle_id AND s.source = 'favorite' AND s.active = 1) AS followed,
               {ownership_columns}
        FROM targets b JOIN prices p USING (bundle_id)
        {ownership_join}
    ), matched AS MATERIALIZED (
        SELECT * FROM candidates WHERE {" AND ".join(conditions) or "1"}
    ), page AS (
        SELECT * FROM matched ORDER BY {order}
        LIMIT :limit OFFSET :offset
    )
    SELECT COUNT(*) AS total, COALESCE(SUM(must_purchase_as_set = 0), 0) AS completable,
           (SELECT json_group_array(json_object('id', bundle_id, 'type', ownership_type,
                'account', CASE WHEN ownership_type = 'family' THEN family_account END)) FROM page) AS items FROM matched
    """
    fast_sql = f"""WITH matched AS MATERIALIZED (
        SELECT b.bundle_id,b.must_purchase_as_set,{score} AS score,
            EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type='bundle' AND s.target_id=b.bundle_id
                AND s.source='favorite' AND s.active=1) AS followed
        FROM bundles b WHERE {" AND ".join(target_conditions)} AND {_visible_bundle_sql()}
    ), page AS (SELECT * FROM matched ORDER BY followed DESC,score DESC,bundle_id LIMIT :limit OFFSET :offset)
    SELECT COUNT(*) AS total,COALESCE(SUM(must_purchase_as_set=0),0) AS completable,
        (SELECT json_group_array(json_object('id',bundle_id,'type',NULL,'account',NULL)) FROM page) AS items
    FROM matched"""
    fast_eligible = not region and sort != "discount" and not any((
        cn_lowest_only, only_discounted, gift_only, price_min is not None,
        price_max is not None, diff_min is not None, diff_max is not None,
    ))
    async with get_session_factory()() as session:
        connection = await session.connection()
        await connection.run_sync(lambda conn: register_bundle_sql_functions(conn.connection.dbapi_connection))
        await session.execute(text("BEGIN"))
        owned_rows = ownership_filter and bool((await session.execute(text(
            "SELECT EXISTS (SELECT 1 FROM wishlist_items w JOIN tracked_accounts a ON a.steamid=w.steamid "
            "WHERE w.active=1 AND w.owned=1)"
        ))).scalar())
        use_fast = fast_eligible and not owned_rows
        stats = (await session.execute(text(fast_sql if use_fast else sql), params)).mappings().one()
        page = json.loads(stats["items"])
        ids = [row["id"] for row in page]
        bundles, prices_by_bundle = await _load_bundles(session, ids)
        by_id = {b.bundle_id: b for b in bundles}
        items = []
        for row in page:
            bid = row["id"]
            item = _aggregate(by_id[bid], prices_by_bundle.get(bid, []), tracked)
            item["ownership"] = {"type": row["type"], "account": row["account"]}
            for rp in item["regionPrices"].values():
                for field in _LIST_REGION_DROP:
                    rp.pop(field, None)
            items.append(item)
        if not ownership_filter or use_fast:
            await _page_ownership(session, items, primary)
    return {"bundles": items, "total": stats["total"],
            "completableCount": stats["completable"], "offset": params["offset"], "limit": params["limit"]}


async def _load_bundles(session: AsyncSession, ids: list[int]) -> tuple[list, dict[int, list]]:
    if not ids:
        return [], {}
    bundles = (await session.execute(select(Bundle).where(Bundle.bundle_id.in_(ids)))).scalars().all()
    prices = (await session.execute(select(BundleRegionPrice).where(
        BundleRegionPrice.bundle_id.in_(ids)
    ).order_by(BundleRegionPrice.id))).scalars().all()
    by_bundle: dict[int, list] = {}
    for price in prices:
        by_bundle.setdefault(price.bundle_id, []).append(price)
    return bundles, by_bundle


async def get_bundle_detail(bundle_id: int) -> dict | None:
    """捆绑包详情：列表字段 + 包内游戏（名称/封面 + 各区现价）。

    包内游戏现价是补齐计算器的求和数据源：区键大写，值含 minor 原价与
    cny_fen（price_status=ok）。游戏未入库（games 表无行）时 name 为 None，
    前端回退 AppID 展示，且计算器按既定规则将无数据项自动排除。
    """
    from app.domains.games.derived import maintain_derived

    await maintain_derived()
    async with get_session_factory()() as session:
        bundles, prices_by_bundle = await _load_bundles(session, [bundle_id])
    bundle = bundles[0] if bundles else None
    if bundle is None:
        return None
    # 详情同样按追踪区过滤（列表与抽屉同一口径）
    tracked = await _tracked_region_codes()
    summary = _aggregate(bundle, prices_by_bundle.get(bundle_id, []), tracked)

    baseline = summary["appIds"]
    games: list[dict] = []
    if baseline:
        async with get_session_factory()() as session:
            game_rows = (
                await session.execute(select(Game).where(Game.appid.in_(baseline)))
            ).scalars().all()
            price_rows = (
                await session.execute(
                    select(GameCurrentPrice).where(
                        GameCurrentPrice.appid.in_(baseline),
                        GameCurrentPrice.price_status == "ok",
                    )
                )
            ).scalars().all()
        meta_by_appid = {g.appid: g for g in game_rows}
        prices_by_appid: dict[int, dict[str, dict]] = {}
        for p in price_rows:
            code = (p.region_code or "").upper()
            # cny_fen 直接读快照列：GET 不回算（缺失行由 recompute_cny_fen_all
            # 在汇率刷新时补齐；无值即前端计算器自动排除该区）
            prices_by_appid.setdefault(p.appid, {})[code] = {
                "priceMinor": int(p.price) if p.price is not None else None,
                "currency": (p.currency or "").upper() or None,
                "cnyFen": int(p.cny_fen) if p.cny_fen is not None else None,
            }
        for appid in baseline:
            meta = meta_by_appid.get(appid)
            games.append(
                {
                    "appid": appid,
                    "name": meta.name if meta else None,
                    "headerImage": (meta.header_image if meta else None)
                    or build_steam_header_url(appid),
                    "prices": prices_by_appid.get(appid, {}),
                }
            )

    summary["games"] = games
    return summary


# ── 用户面动作：移除 / 恢复 / 关注（复用 monitoring 的 bundle 生命周期）──

# 移除链路挂的排除 reason：恢复时只解除本链路所挂的排除，维护面板里
# 用户显式设置的排除语义不被恢复动作洗掉
REMOVAL_EXCLUSION_REASON = "bundle_removed"



async def remove_bundle(bundle_id: int) -> dict:
    """移除捆绑包：列表隐藏 + 退出刷新（包行与区域价保留）。

    stop 摘掉全部用户来源（含星标），set_exclusion 挂 bundle_removed 排除
    ——排除与释放两态都在刷新候选集之外，整表刷新不会把它洗回来；重新
    导入（refresh.import_bundle 的 track）同样解除排除。
    """
    bid = int(bundle_id)
    async with get_session_factory()() as session:
        row = (
            await session.execute(select(Bundle.bundle_id).where(Bundle.bundle_id == bid))
        ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"捆绑包 {bid} 不存在")

    from app.domains.monitoring import service as monitoring_service

    await monitoring_service.stop("bundle", bid)
    await monitoring_service.set_exclusion("bundle", bid, True, REMOVAL_EXCLUSION_REASON)
    return {"removed": True, "bundleId": bid}


async def restore_bundle(bundle_id: int) -> dict:
    """恢复被移除的包：只对本链路所挂的排除（reason=bundle_removed）生效。

    恢复走与重新导入同一条 track 通道（挂 import 来源 + 解除排除）——
    移除的 stop 已把目标置为 released（无来源态同样不进刷新），只解排除
    回不到刷新候选集。维护面板里用户显式设置的排除/停止监控语义保留。
    """
    bid = int(bundle_id)
    from app.domains.monitoring import service as monitoring_service

    reason = await monitoring_service.exclusion_reason("bundle", bid)
    if reason != REMOVAL_EXCLUSION_REASON:
        return {"restored": False, "bundleId": bid}
    await monitoring_service.track("bundle", bid, "import")
    return {"restored": True, "bundleId": bid}


async def follow_bundle(bundle_id: int) -> dict:
    """关注一个包：挂 favorite 来源（不需要 Steam 账户），列表置顶并进入刷新集。"""
    bid = int(bundle_id)
    from app.domains.monitoring import service as monitoring_service

    # 关注 = 用户显式要求盯住它：解除「别再爬它」后挂来源（与游戏星标同语义）
    await monitoring_service.ensure_source("bundle", bid, "favorite", clear_exclusion=True)
    return {"bundleId": bid, "followed": True}


async def unfollow_bundle(bundle_id: int) -> dict:
    """取消关注：只摘 favorite 来源（导入来源与排除标不动）。

    星标是唯一激活来源时，摘除后回到「无记录」基线（forget）——刷新集
    只收监控层在册的包，无记录即不随轮刷新，价格停留在最后一次快照。
    """
    bid = int(bundle_id)
    from app.domains.monitoring import service as monitoring_service

    sources = await monitoring_service.sources_map("bundle", [bid])
    await monitoring_service.detach_source("bundle", bid, "favorite")
    if sources.get(bid, set()) == {"favorite"}:
        await monitoring_service.forget("bundle", bid)
    return {"bundleId": bid, "followed": False}


async def followed_bundle_ids() -> list[int]:
    """当前关注的包 id（升序）。"""
    from app.domains.monitoring import service as monitoring_service

    return await monitoring_service.ids_with_source("bundle", "favorite")


async def names_for(bundle_ids: list[int]) -> dict[int, str]:
    """批量取捆绑包名（领航台关注清单等只读投影用）。"""
    ids = [int(b) for b in bundle_ids if b]
    if not ids:
        return {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Bundle.id, Bundle.name).where(Bundle.id.in_(ids)))
        ).all()
    return {int(b): (n or f"Bundle {b}") for b, n in rows}
