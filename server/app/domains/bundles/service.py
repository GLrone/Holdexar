"""捆绑包域服务：列表聚合 + 详情（补齐计算数据源）。

聚合语义：按 bundle_id 分组区域价 → 汇率折算 CNY → 以 app_ids 最多的区为基准推算各区
锁区游戏数 → 找最低价区 → 差价（CN−最低，下限 0）→ 按差价降序。

数据形态约定：
- 存 minor units + cny_fen，展示字符串按展示时汇率现算。
- pk 爬虫端即"南亚"双区，无独立 BD 区，不需要镜像补 BD 逻辑。
- 归属推演只做"全部 appid 已拥有/家庭共享"条件（前端 ownership store）。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.games.models import Bundle, BundleRegionPrice, Game, GameCurrentPrice
from app.domains.games.pricing import (
    build_steam_header_url,
    convert_minor_to_cny_fen,
    format_minor_units,
)
from app.domains.games.service import get_rates

# Steam 图片 CDN 三域同库：akamai 域国内直连可达（fastly 被墙/不稳定，
# queniuqe 为历史镜像域）。入库原样保留，展示期统一归一到 akamai。
_IMG_CDN_HOSTS = re.compile(
    r"^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com", re.IGNORECASE
)
_IMG_CDN_REPLACEMENT = "https://shared.akamai.steamstatic.com"

# ── 进程内缓存（列表页每次进/每次刷新都全量重算，实测 3.3s/次 —— 全花在
# 装载 3,386 包 + 77,962 行区域价上；数据只在刷新/导入时变化，失效式缓存
# 比按 TTL 更准）────────────────────────────────────────────────────────
# 缓存有效性 = 写入失效（invalidate_bundles_cache，挂在 _upsert_bundle_rows
# 的三个调用方）+ 输入指纹（汇率表 / 追踪区集合任一变化自动重算——两者都
# 会改变聚合结果，且都是运行时可变的用户可见状态，不能只靠写失效）。
_DATA_CACHE: tuple[tuple, tuple[list[Bundle], dict[int, list[BundleRegionPrice]], dict[str, float]]] | None = None
_LIST_CACHE: tuple[tuple, list[dict]] | None = None


def _rates_fp(rates: dict[str, float]) -> tuple:
    """汇率指纹：币种→汇率的稳定排序元组（round 防 float 尾噪）。"""
    return tuple(sorted((k, round(v, 8)) for k, v in rates.items()))


def invalidate_bundles_cache() -> None:
    """捆绑包数据变更后调用（refresh / import / 爬虫补包共用写入口已挂）。"""
    global _DATA_CACHE, _LIST_CACHE
    _DATA_CACHE = None
    _LIST_CACHE = None


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


def _aggregate(
    bundle: Bundle,
    price_rows: list[BundleRegionPrice],
    rates: dict[str, float],
    tracked: set[str] | None = None,
) -> dict:
    """单包聚合：区域价表 / 基准 appids / 锁区数 / 最低价区 / 差价。

    tracked：追踪区集（大写）。给定后区域价表按追踪区过滤，最低价区/
    差价也只在追踪区内取（对齐游戏卡 GPW 只看启用区的语义）；
    None = 不过滤。南亚 PK/BD 双区见 _filter_region_prices。
    """
    mps = bundle.must_purchase_as_set if bundle.must_purchase_as_set is not None else -1
    item_kind = bundle.item_kind if bundle.item_kind is not None else -1
    # v3 之前的行 item_kind 尚未回填（-1）：形态沿用 mps 旧值兜底
    if item_kind not in (0, 1):
        item_kind = mps

    # ── 双产品隔离 ──
    # refresh 双轨降级按区混用 Bundle/Package API 时，同一 bundle_id 会出现两套
    # appids（bundle 本体 vs 同号 sub 的 appid 列表）。以 appids 最多的行为"主产品"
    # （baseline 语义），其余行若 appids 是主产品 appids 的子集之外的产品
    # （套娃行，见 bundle 61597 = sub 347440 污染），整行剔除——只保留与主产品
    # 同族（appids 与主基准有交集）的行，锁区判定/最低价不再被异种产品带偏。
    rows: list[BundleRegionPrice] = []
    for p in price_rows:
        code = (p.region_code or "").upper()
        if not code:
            continue
        rows.append(p)
    main_row = max(
        rows, key=lambda p: len(_parse_app_ids(p.app_ids)), default=None
    )
    baseline_appids: list[int] = (
        _parse_app_ids(main_row.app_ids) if main_row is not None else []
    )
    baseline_set = set(baseline_appids)
    family_rows = [p for p in rows if baseline_set & set(_parse_app_ids(p.app_ids))]
    # 主行都无 appids（全部为空）时不能全剔：退化为原全量行为
    rows = family_rows if (baseline_appids and len(family_rows) > 0) else rows

    region_prices: dict[str, dict] = {}
    all_region_appids: dict[str, list[int]] = {}
    for p in rows:
        code = (p.region_code or "").upper()
        if not code:
            continue
        aids = _parse_app_ids(p.app_ids)
        all_region_appids[code] = aids
        if len(aids) > len(baseline_appids):
            baseline_appids = aids
        cny_fen = int(p.cny_fen) if p.cny_fen is not None else None
        if cny_fen is None and p.price:
            currency = p.currency or ""
            cny_fen = convert_minor_to_cny_fen(int(p.price), currency, rates)
        entry = {
            "priceMinor": int(p.price) if p.price is not None else None,
            "currency": (p.currency or "").upper() or None,
            "discountPercent": p.discount_percent or 0,
            "baseDiscount": p.bundle_base_discount or 0,
            "cnyFen": cny_fen,
            "appIds": aids,
        }
        # 源数据存在大小写并存的同区行（如 jp/JP）：有价者优先，其余丢弃，
        # 保证同一区键唯一且取到有效行
        existing = region_prices.get(code)
        if existing is None or (existing["cnyFen"] is None and entry["cnyFen"] is not None):
            region_prices[code] = entry

    baseline_set = set(baseline_appids)
    for code, rp in region_prices.items():
        rp["lockedCount"] = len(baseline_set - set(rp["appIds"]))
        rp["formatted"] = format_minor_units(rp["priceMinor"], rp["currency"] or "")

    # 追踪区过滤（南亚 PK/BD 双区适配）→ 最低价/差价在过滤后的区内取
    region_prices = _filter_region_prices(region_prices, tracked)

    cn_cny_fen = region_prices.get("CN", {}).get("cnyFen")
    lowest_code = ""
    lowest_cny_fen = None
    for code, rp in region_prices.items():
        cny_fen = rp["cnyFen"]
        if cny_fen is not None and (lowest_cny_fen is None or cny_fen < lowest_cny_fen):
            lowest_cny_fen = cny_fen
            lowest_code = code.lower()
    diff_fen = (
        max(cn_cny_fen - lowest_cny_fen, 0)
        if cn_cny_fen is not None and lowest_cny_fen is not None
        else 0
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
        "diffFen": diff_fen,
    }


async def _load_all() -> tuple[list[Bundle], dict[int, list[BundleRegionPrice]], dict[str, float]]:
    global _DATA_CACHE
    rates = await get_rates()
    fp = _rates_fp(rates)
    if _DATA_CACHE is not None and _DATA_CACHE[0] == fp:
        return _DATA_CACHE[1]
    async with get_session_factory()() as session:
        bundles = (await session.execute(select(Bundle))).scalars().all()
        price_rows = (await session.execute(select(BundleRegionPrice))).scalars().all()
    prices_by_bundle: dict[int, list[BundleRegionPrice]] = {}
    for p in price_rows:
        prices_by_bundle.setdefault(p.bundle_id, []).append(p)
    _DATA_CACHE = (fp, (bundles, prices_by_bundle, rates))
    return _DATA_CACHE[1]


async def list_bundles() -> list[dict]:
    """全量捆绑包列表（差价降序）。

    区域价/最低价/差价按追踪区（crawl_regions 启用集）过滤——
    未启用任何区时全量展示（对齐游戏卡 GPW 语义）。
    聚合结果缓存（失效见 invalidate_bundles_cache + 汇率/追踪区指纹）；
    返回的是缓存对象，调用方只读（FastAPI 直接序列化，无改写方）。
    """
    global _LIST_CACHE
    tracked = await _tracked_region_codes()
    bundles, prices_by_bundle, rates = await _load_all()
    # tracked=None 是合法语义（未启用任何区 = 全量展示），指纹按空集归一
    fp = (tuple(sorted(tracked)) if tracked else (), _rates_fp(rates))
    if _LIST_CACHE is not None and _LIST_CACHE[0] == fp:
        return _LIST_CACHE[1]
    items = [
        _aggregate(b, prices_by_bundle.get(b.bundle_id, []), rates, tracked)
        for b in bundles
    ]
    items = [i for i in items if i["regionPrices"]]
    items.sort(key=lambda x: x["diffFen"], reverse=True)
    _LIST_CACHE = (fp, items)
    return items


async def get_bundle_detail(bundle_id: int) -> dict | None:
    """捆绑包详情：列表字段 + 包内游戏（名称/封面 + 各区现价）。

    包内游戏现价是补齐计算器的求和数据源：区键大写，值含 minor 原价与
    cny_fen（price_status=ok）。游戏未入库（games 表无行）时 name 为 None，
    前端回退 AppID 展示，且计算器按既定规则将无数据项自动排除。
    """
    bundles, prices_by_bundle, rates = await _load_all()
    bundle = next((b for b in bundles if b.bundle_id == bundle_id), None)
    if bundle is None:
        return None
    # 详情同样按追踪区过滤（列表与抽屉同一口径）
    tracked = await _tracked_region_codes()
    summary = _aggregate(bundle, prices_by_bundle.get(bundle_id, []), rates, tracked)

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
            cny_fen = int(p.cny_fen) if p.cny_fen is not None else None
            if cny_fen is None and p.price:
                cny_fen = convert_minor_to_cny_fen(
                    int(p.price), p.currency or "", rates
                )
            prices_by_appid.setdefault(p.appid, {})[code] = {
                "priceMinor": int(p.price) if p.price is not None else None,
                "currency": (p.currency or "").upper() or None,
                "cnyFen": cny_fen,
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
