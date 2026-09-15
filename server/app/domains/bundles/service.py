"""捆绑包域服务：列表聚合 + 详情（补齐计算数据源）+ 排序快照维护。

聚合语义：按 bundle_id 分组区域价 → 双产品隔离 → 追踪区过滤 → 以 app_ids
最多的区为基准推算各区锁区游戏数；最低价/差价一律读 bundles 排序快照
（min_cny_fen/diff_fen/is_lowest，由 refresh_bundle_sort_cache 在写时维护，
与 games 的 refresh_sort_cache 同构）。

分层约束（排序快照 ≠ 请求期计算）：
- 数据采集层：bundle_region_prices 原始价 + cny_fen（爬取/导入落库）；
- 派生计算层：refresh_bundle_sort_cache → bundles.min_cny_fen/diff_fen/is_lowest；
- 查询层：WHERE + ORDER BY diff_fen + LIMIT（SQL，见 _load_all_locked）。
**任何 GET 不得现算排序所依赖的派生值**（GET → calculate cny / lowest /
diff、Python sort 都视为架构倒退）；展示层的 lowestRegion/lowestCnyFen
只是对快照的查表（全区最低 = min(国区价, 非国区最低快照)）。

数据形态约定：
- 存 minor units + cny_fen，展示字符串按展示时汇率现算。
- pk 爬虫端即"南亚"双区，无独立 BD 区，不需要镜像补 BD 逻辑。
- 归属推演只做"全部 appid 已拥有/家庭共享"条件（前端 ownership store）。
"""
from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.domains.games.models import Bundle, BundleRegionPrice, Game, GameCurrentPrice
from app.domains.games.pricing import (
    build_steam_header_url,
    format_minor_units,
)
from app.domains.games.service import get_rates

# Steam 图片 CDN 三域同库：akamai 域国内直连可达（fastly 被墙/不稳定，
# queniuqe 为历史镜像域）。入库原样保留，展示期统一归一到 akamai。
_IMG_CDN_HOSTS = re.compile(
    r"^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com", re.IGNORECASE
)
_IMG_CDN_REPLACEMENT = "https://shared.akamai.steamstatic.com"

# ── 进程内缓存（列表页每次进/每次刷新都全量重算：装载 25.8k 包 + 14.1 万行
# 区域价、逐包聚合、再序列化 12MB；数据只在刷新/导入时变化，失效式缓存比
# 按 TTL 更准）──────────────────────────────────────────────────────────
# 分层（见模块 docstring）：排序快照（min_cny_fen/diff_fen/is_lowest）由
# refresh_bundle_sort_cache 在写侧维护，聚合只读快照 + 组装展示区表——
# 请求期不现算最低/差价、不排序。
# 缓存有效性 = 写入失效（invalidate_bundles_cache，挂在刷新链尾/导入/追踪区
# 变更/汇率原子刷新四个写侧出口）+ 输入指纹（汇率表 / 追踪区集合任一变化
# 自动重算——两者都会改变聚合结果，且都是运行时可变的用户可见状态，不能只
# 靠写失效）。
# _LIST_JSON_CACHE 是列表的**预序列化 bytes**：FastAPI 的 dict 返回路径每次
# 请求都要重跑 jsonable_encoder + JSON 编码（15k 条实测 2s 级），而两次请求
# 之间内容并不变——编码一次、按指纹复用。三份缓存同源同失效、指纹一致。
_DATA_CACHE: tuple[tuple, tuple[list, dict[int, list], dict[str, float]]] | None = None
_LIST_CACHE: tuple[tuple, list[dict]] | None = None
_LIST_JSON_CACHE: tuple[tuple, bytes] | None = None

# 并发互斥：启动预热与用户请求会在「缓存未建」时同时进来（页面开得比预热快
# 是常态），没有锁就各自全量重算一遍（装载秒级 + 聚合秒级 ×2）。双检锁
# 保证只跑一次，其余请求等锁后直接吃缓存。两把锁分开拿、不嵌套（先 _load_all
# 后聚合），asyncio.Lock 不可重入，嵌套即死锁。
_DATA_LOCK = asyncio.Lock()
_LIST_LOCK = asyncio.Lock()


def _rates_fp(rates: dict[str, float]) -> tuple:
    """汇率指纹：币种→汇率的稳定排序元组（round 防 float 尾噪）。"""
    return tuple(sorted((k, round(v, 8)) for k, v in rates.items()))


def _list_fp(tracked: set[str] | None, rates: dict[str, float]) -> tuple:
    """列表缓存指纹：追踪区集合（None=全量语义，按空集归一）+ 汇率表。"""
    return (tuple(sorted(tracked)) if tracked else (), _rates_fp(rates))


def invalidate_bundles_cache() -> None:
    """捆绑包数据变更后调用（refresh / import / 爬虫补包共用写入口已挂）。"""
    global _DATA_CACHE, _LIST_CACHE, _LIST_JSON_CACHE
    _DATA_CACHE = None
    _LIST_CACHE = None
    _LIST_JSON_CACHE = None


def _normalize_image(url: str | None) -> str | None:
    """图片 CDN 域归一化：fastly/queniuqe → akamai（路径不变，实测同图可达）。"""
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
    逐行只解析一次——14 万行各跑两遍 json.loads 曾是聚合耗时的大头之一。
    memo 挂在行主键上且只活在本次调用里（局部字典，不占常驻内存）。

    返回 (region_prices, baseline_appids)；条目字段：
    priceMinor/currency/discountPercent/baseDiscount/cnyFen/appIds。
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
    }


# ── 排序快照（bundles.min_cny_fen/diff_fen/is_lowest）────────────────────
# 铁律：排序所依赖的派生值一律在写时算好（本函数组），任何 GET 只读快照。
_SNAPSHOT_TOLERANCE_FEN = 500  # 「国区 ≈ 追踪区最低」容差（±5 元，对齐 games isLowest）
_SNAPSHOT_BATCH = 400  # 回写批量（SQLite 绑定变量上限内）


def _snapshot_values(
    bundle: Bundle, price_rows: list, tracked: set[str] | None
) -> tuple[int | None, int, bool]:
    """单包排序快照：min_cny_fen / diff_fen / is_lowest。

    与列表展示严格同源（_family_region_prices：双产品隔离 + 追踪区过滤）：
    - min_cny_fen = 非 CN 各区有价行最低 CNY 分（原始值，无则 NULL）；
    - diff_fen = MAX(国区价 − COALESCE(最低, 国区价), 0)（无国区价 = 0）；
    - is_lowest = 国区价 ≈ 追踪区最低（±5 元）——国区买即（近似）全球最低，
      判据对齐 games 的 isLowest 筛选语义；无国区价（锁区/无数据）恒 False。
    """
    region_prices, _ = _family_region_prices(bundle, price_rows, tracked)
    others = [
        rp["cnyFen"]
        for code, rp in region_prices.items()
        if code != "CN" and rp["cnyFen"] is not None and rp["cnyFen"] > 0
    ]
    min_cny_fen = min(others) if others else None
    cn_cny_fen = region_prices.get("CN", {}).get("cnyFen")
    if cn_cny_fen is None:
        return min_cny_fen, 0, False
    reference = min_cny_fen if min_cny_fen is not None else cn_cny_fen
    diff_fen = max(cn_cny_fen - reference, 0)
    is_lowest = reference >= cn_cny_fen - _SNAPSHOT_TOLERANCE_FEN
    return min_cny_fen, diff_fen, is_lowest


async def refresh_bundle_sort_cache(
    bundle_ids: list[int] | None = None, *, session: AsyncSession | None = None
) -> int:
    """重算 bundles.min_cny_fen / diff_fen / is_lowest（列表排序快照）。

    bundle_ids=None 全库（启动 / 汇率变更 / 追踪区变更）；列表 = 增量
    （刷新链尾 / 单包导入）。口径同列表聚合（双产品隔离 + 追踪区过滤）——
    追踪区集合变化会改变最低价所在区，故追踪区变更必须整库重建。

    session 注入时不自行提交：汇率原子刷新用它把「汇率 → cny_fen →
    games sort → bundles sort」串进同一事务，GET 只可能读到旧快照或新快照。
    返回写入行数。
    """
    if session is not None:
        return await _refresh_bundle_sort_cache(bundle_ids, session)
    async with get_session_factory()() as own:
        written = await _refresh_bundle_sort_cache(bundle_ids, own)
        await own.commit()
    return written


async def _refresh_bundle_sort_cache(
    bundle_ids: list[int] | None, db: AsyncSession
) -> int:
    bundle_ids = [int(b) for b in bundle_ids] if bundle_ids is not None else None
    if bundle_ids is not None and not bundle_ids:
        return 0

    stmt = select(Bundle)
    if bundle_ids is not None:
        stmt = stmt.where(Bundle.bundle_id.in_(bundle_ids))
    bundles = (await db.execute(stmt)).scalars().all()
    if not bundles:
        return 0

    ids = [b.bundle_id for b in bundles]
    price_rows = (
        await db.execute(
            select(
                BundleRegionPrice.id,
                BundleRegionPrice.bundle_id,
                BundleRegionPrice.region_code,
                BundleRegionPrice.price,
                BundleRegionPrice.currency,
                BundleRegionPrice.discount_percent,
                BundleRegionPrice.bundle_base_discount,
                BundleRegionPrice.cny_fen,
                BundleRegionPrice.app_ids,
            ).where(BundleRegionPrice.bundle_id.in_(ids))
        )
    ).all()
    by_bundle: dict[int, list] = {}
    for p in price_rows:
        by_bundle.setdefault(p.bundle_id, []).append(p)

    tracked = await _tracked_region_codes()
    updates = []
    for b in bundles:
        min_fen, diff_fen, is_lowest = _snapshot_values(
            b, by_bundle.get(b.bundle_id, []), tracked
        )
        updates.append(
            {"b": b.bundle_id, "m": min_fen, "d": diff_fen, "l": 1 if is_lowest else 0}
        )

    sql = text(
        "UPDATE bundles SET min_cny_fen = :m, diff_fen = :d, is_lowest = :l "
        "WHERE bundle_id = :b"
    )
    for i in range(0, len(updates), _SNAPSHOT_BATCH):
        await db.execute(sql, updates[i : i + _SNAPSHOT_BATCH])
    return len(updates)


async def _load_all() -> tuple[list, dict[int, list], dict[str, float]]:
    global _DATA_CACHE
    rates = await get_rates()
    fp = _rates_fp(rates)
    if _DATA_CACHE is not None and _DATA_CACHE[0] == fp:
        return _DATA_CACHE[1]
    async with _DATA_LOCK:
        # 双检：等锁期间别的协程可能已经建好
        if _DATA_CACHE is not None and _DATA_CACHE[0] == fp:
            return _DATA_CACHE[1]
        return await _load_all_locked(fp, rates)


async def _load_all_locked(
    fp: tuple, rates: dict[str, float]
) -> tuple[list, dict[int, list], dict[str, float]]:
    global _DATA_CACHE
    # Core 列查询（不构造 ORM 实体）：14.1 万行价格装载从 2.2s 降到亚秒级——
    # identity map 与实体构造对只读行是纯开销（这些行从不回写）。
    # 排序在 SQL 层完成（ORDER BY 排序快照列 diff_fen DESC）——GET 不做
    # Python sort；等价 id 序（bundle_id）保证同差价时分页/缓存稳定。
    async with get_session_factory()() as session:
        bundles = (
            await session.execute(
                select(
                    Bundle.bundle_id,
                    Bundle.name,
                    Bundle.header_image,
                    Bundle.must_purchase_as_set,
                    Bundle.item_kind,
                    Bundle.url,
                    Bundle.min_cny_fen,
                    Bundle.diff_fen,
                ).order_by(Bundle.diff_fen.desc(), Bundle.bundle_id.asc())
            )
        ).all()
        price_rows = (
            await session.execute(
                select(
                    BundleRegionPrice.id,
                    BundleRegionPrice.bundle_id,
                    BundleRegionPrice.region_code,
                    BundleRegionPrice.price,
                    BundleRegionPrice.currency,
                    BundleRegionPrice.discount_percent,
                    BundleRegionPrice.bundle_base_discount,
                    BundleRegionPrice.cny_fen,
                    BundleRegionPrice.app_ids,
                )
            )
        ).all()
    prices_by_bundle: dict[int, list] = {}
    for p in price_rows:
        prices_by_bundle.setdefault(p.bundle_id, []).append(p)
    _DATA_CACHE = (fp, (bundles, prices_by_bundle, rates))
    return _DATA_CACHE[1]


def _build_list_items(bundles, prices_by_bundle, tracked) -> list[dict]:
    """全量聚合 + 剔除追踪区内无价包（列表与其 JSON 出口共用）。

    顺序 = 加载查询的 SQL ORDER BY（排序快照 diff_fen DESC）——不做
    Python sort（GET → Python sort 是架构倒退）。
    """
    items = [
        _aggregate(b, prices_by_bundle.get(b.bundle_id, []), tracked)
        for b in bundles
    ]
    return [i for i in items if i["regionPrices"]]


async def list_bundles() -> list[dict]:
    """全量捆绑包列表（差价降序）。

    区域价/最低价/差价按追踪区（crawl_regions 启用集）过滤——
    未启用任何区时全量展示（对齐游戏卡 GPW 语义）。
    聚合结果缓存（失效见 invalidate_bundles_cache + 汇率/追踪区指纹）；
    返回的是缓存对象，调用方只读（无改写方）。
    """
    global _LIST_CACHE
    tracked = await _tracked_region_codes()
    bundles, prices_by_bundle, rates = await _load_all()
    # tracked=None 是合法语义（未启用任何区 = 全量展示），指纹按空集归一
    fp = _list_fp(tracked, rates)
    if _LIST_CACHE is not None and _LIST_CACHE[0] == fp:
        return _LIST_CACHE[1]
    async with _LIST_LOCK:
        # 双检：等锁期间（预热/并发请求）可能已经建好
        if _LIST_CACHE is not None and _LIST_CACHE[0] == fp:
            return _LIST_CACHE[1]
        items = _build_list_items(bundles, prices_by_bundle, tracked)
        _LIST_CACHE = (fp, items)
        return items


async def list_bundles_json() -> bytes:
    """列表的预序列化 UTF-8 JSON（路由出口用），内容与 list_bundles() 等值。

    差别只在**只编码一次**：FastAPI 对 dict 返回值每次请求都要重跑
    jsonable_encoder + JSON 编码（15k 条聚合结果实测 2s 级），而列表内容
    只在数据/汇率/追踪区变化时变——聚合与编码都按同一指纹缓存。
    """
    global _LIST_JSON_CACHE
    items = await list_bundles()
    fp = _LIST_CACHE[0] if _LIST_CACHE is not None else ()
    if _LIST_JSON_CACHE is not None and _LIST_JSON_CACHE[0] == fp:
        return _LIST_JSON_CACHE[1]
    payload = json.dumps(
        {"bundles": items}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    _LIST_JSON_CACHE = (fp, payload)
    return payload


async def warmup() -> int:
    """启动链预热（后台）：把首次全量聚合 + 序列化的秒级成本挪到开门之后。

    返回预序列化载荷字节数（日志用）。调用方兜异常——预热失败只会让
    用户首次进捆绑包页重新等一次聚合，不影响功能。
    """
    return len(await list_bundles_json())


async def get_bundle_detail(bundle_id: int) -> dict | None:
    """捆绑包详情：列表字段 + 包内游戏（名称/封面 + 各区现价）。

    包内游戏现价是补齐计算器的求和数据源：区键大写，值含 minor 原价与
    cny_fen（price_status=ok）。游戏未入库（games 表无行）时 name 为 None，
    前端回退 AppID 展示，且计算器按既定规则将无数据项自动排除。
    """
    bundles, prices_by_bundle, _rates = await _load_all()
    bundle = next((b for b in bundles if b.bundle_id == bundle_id), None)
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
