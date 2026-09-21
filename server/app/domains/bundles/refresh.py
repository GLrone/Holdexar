"""bundles 刷新服务：按监控区抓取捆绑包价格 → 落 bundles/bundle_region_prices。

抓取层为 IStoreBrowseService/GetItems/v1（与 app 链路 app/crawler/browse_store 同源）：
bundleid / packageid 与 appid 一样进 `ids` 数组，单区一发 ≤400 条。与旧链路
（逐区 ajaxresolvebundles → packagedetails 降级）的关键差异：

- **抓取区 = 监控启用区**（「我」页勾选，与游戏侧同一口径），不再全区全发；
  南亚（CC_LIST 的 pk 聚合位）按 Steam 实际拆巴基斯坦（pk）/ 孟加拉（bd）
  两个独立区各发一次、各落一行（展示层按追踪位合并放行、取两区更低价）。
- **请求数**：整表每区一发（监控区数发，URL ≈3.4KB），而非每包每区一发。
- **身份直给**：`item_type`（1=Sub / 2=Bundle）与 `store_url_path`（sub/… vs
  bundle/…）即 Steam 权威身份，无需「us 区基准判定整包身份 → 全区沿用」的
  双轨探测（同号 sub/bundle 混写即该探测的失真场景）。
- **价格单位统一为分**：直接取 `final_price_in_cents`，与 game_current_prices /
  pricing.format_minor_units 的 cents 约定一致。解析 formatted_final_price
  字符串会把零小数货币落成「元」，与 Package 轨的「分」同列两套单位——而
  cny_fen 一律按无小数货币除数 1 折算，Sub 轨零小数货币行 cny_fen 遂虚高
  100 倍（存量归一见 core/database 迁移 v2）。
- **锁区判定**：`visible=false` / `unvailable_for_country_restriction` 判 locked
  （packagedetails 对锁区 Sub 仍给价）。
- 原价 `price_before_bundle_discount` 一手落库。

出网直连（browse 接口按 country_code 返回各区数据，出口 IP 不参与判定；
加速器在系统网络层透明生效）+ 全局限流（与 app 爬取链共享 200 发/5 分钟
窗口预算，见 crawler/rate_limit.py）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import aiohttp
from sqlalchemy import delete, func, select

from app.core.database import get_session_factory
from app.crawler import browse_store as bs
from app.crawler.config import CC_LIST
from app.domains.games.models import Bundle, BundleRegionPrice, Game
from app.domains.games.pricing import convert_minor_to_cny_fen
from app.domains.games.service import get_rates

from .service import invalidate_bundles_cache, refresh_bundle_sort_cache

logger = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
# 符合 app 爬取标准的类型（只抓 game/dlc）
_CRAWLABLE_TYPES = {"game", "dlc"}
# 每轮预检上限：appdetails 礼貌限速 + 刷新时长兜底（141 级候选全部逐检会
# 把刷新拖成数分钟长事务）。未预检候选自然顺延下轮——已结案的自动跳过，
# 进度单调递减，不丢不重
_PRECHECK_PER_RUN = 30

# 南亚拆区：CC_LIST 的 pk 是聚合"南亚"追踪位，Steam 侧巴基斯坦（pk）与
# 孟加拉（bd）是两个独立区——启用南亚时两区各发一次、各落一行，展示层
# 按追踪位合并放行（PK/BD 同进同出，最低价取两区更低价）。
# BD 不在 CC_LIST（不是独立追踪位），币种随南亚位（USD）。
_SOUTH_ASIA_EXTRA = {"pk": ("bd",)}
_CC_CURRENCY = {cc: cur for cc, _, cur in CC_LIST}
_CC_CURRENCY.update(
    {extra: _CC_CURRENCY["pk"] for extra in _SOUTH_ASIA_EXTRA["pk"]}
)
# 新接口请求参数（语言固定 english：名称与 app 链路同口径，区域价与语言无关）
BROWSE_LANG = "english"
BROWSE_TIMEOUT = 20


async def _bundle_fetch_ccs() -> list[str]:
    """本次捆绑包抓取的 cc 列表：监控启用区 + 南亚拆区展开。

    与游戏侧同口径——只抓「我」页勾选的区（全禁用由 effective_regions 抛
    ValueError，调用方按跳过语义处理；手动导入转 400）。南亚（pk 聚合位）
    拆成 pk / bd 两次请求见 _SOUTH_ASIA_EXTRA。
    """
    from app.domains.regions.service import effective_regions

    ccs: list[str] = []
    for code in await effective_regions(None):
        ccs.append(code)
        ccs.extend(_SOUTH_ASIA_EXTRA.get(code, ()))
    return ccs


async def _rate_limit_acquire() -> None:
    """出网前取限流名额（与 app 爬取链同一进程级窗口预算）。"""
    from app.crawler.rate_limit import steam_rate_limiter

    await steam_rate_limiter.acquire()


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


# ══════════════════════════════════════════════════════════════
# 抓取层：IStoreBrowseService/GetItems（单区一发 ≤400 条）
# ══════════════════════════════════════════════════════════════


def _browse_kind(bundle_id: int, item_kind: int | None) -> str:
    """形态 → ids 键：item_kind=1（Sub 形态）用 packageid，其余 bundleid。

    注意判据是**形态**不是购买语义——mps（必须整包）在形态为 bundle 的包上
    也会是 1（实例 63575），拿 mps 选键会按 packageid 去问一个 bundle 产品。
    """
    return "packageid" if item_kind == 1 else "bundleid"


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_real_item(item: dict) -> bool:
    """条目是否对应真实商品。

    未收录（伪造 id / 已下架）也回 success=15 + visible=false，与「真实存在但
    本区不售」同形——区分点在 name / store_url_slug：真实商品两者皆有，
    未收录两者全空（store_url_path 仍是 `bundle/<id>/` 空壳）。
    """
    return bool(item.get("name") or item.get("store_url_slug"))


async def _fetch_browse_region(
    session: aiohttp.ClientSession, specs: list[dict], cc: str
) -> list[dict] | None:
    """单区一发。返回 store_items（与 specs 位置对齐）；请求失败返回 None。

    失败（None）与「条目锁区」严格区分：前者不写行（下轮刷新重试），
    后者照写 locked（该区不售，不进补抓账本）。
    """
    url = bs.StoreBrowseAPI.build_ids_url(specs, cc, BROWSE_LANG)
    try:
        await _rate_limit_acquire()
        async with session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=BROWSE_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.debug("[bundles] browse %s 状态 %s", cc, resp.status)
                return None
            data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.debug("[bundles] browse %s 失败：%s", cc, e)
        return None
    return ((data or {}).get("response") or {}).get("store_items") or []


def _browse_option(item: dict) -> tuple[str, dict | None]:
    """条目 → (price_status, 购买选项)。锁区/无选项语义对齐 browse_store.evaluate。"""
    if item.get("success") != 1 or not item.get("visible"):
        return "locked", None
    if item.get("unvailable_for_country_restriction"):
        return "locked", None
    opt = item.get("best_purchase_option")
    if not opt:
        opts = item.get("purchase_options") or []
        if any(o.get("package_group") == "default" for o in opts):
            opts = [o for o in opts if o.get("package_group") == "default"]
        opt = opts[0] if opts else None
    if not opt:
        return "locked", None  # 可见但无购买选项 = 该区不售
    if _to_int(opt.get("final_price_in_cents")) is None:
        return "no_price", opt
    return "ok", opt


def _browse_row(
    item: dict | None, bundle_id: int, cc: str, kind: str
) -> dict | None:
    """browse 条目 → 区域行（价格统一「分」）。未收录/条目缺失 → None（不写行）。"""
    if item is None or not _is_real_item(item):
        return None
    status, opt = _browse_option(item)
    has_opt = opt is not None
    opt = opt or {}
    price = _to_int(opt.get("final_price_in_cents"))
    # 捆绑包原价 = 叠折前价（price_before_bundle_discount）；促销时另有 original
    original = _to_int(opt.get("price_before_bundle_discount")) or _to_int(
        opt.get("original_price_in_cents")
    )
    discount = _to_int(opt.get("discount_pct")) or 0
    if not discount and original and price and original > price:
        discount = round((original - price) * 100 / original)
    item_type = item.get("item_type")
    # 形态（链接/CDN 用）：item_type 1=Sub / 2=Bundle，Steam 权威
    item_kind = 1 if item_type == 1 else (0 if item_type == 2 else None)
    # 购买语义（是否必须整包）：Sub 形态恒不可拆；bundle 形态以选项级
    # must_purchase_as_set 为准（实例 63575：item_type=2 却必须整包）——
    # 形态与语义解耦，不能只看 item_type。锁区行（无真实选项）不投
    # 语义票（mps=None）：逐区选项不一致时由落库层多数决归位，不能让
    # 锁区行伪装成「可补齐」的反对票
    if item_kind == 1:
        mps = 1  # Sub 形态恒不可拆
    elif has_opt and bool(opt.get("must_purchase_as_set")):
        mps = 1
    elif item_kind == 0 and has_opt:
        mps = 0  # 有真实选项且未标必须整包 → 可补齐
    else:
        mps = None  # 锁区/无选项的 bundle：语义未知
    return {
        "bundle_id": bundle_id,
        "region_code": cc,
        "price": price,
        "original_price": original,
        "currency": _CC_CURRENCY.get(cc),
        "discount_percent": discount,
        "bundle_base_discount": _to_int(opt.get("bundle_discount_pct")) or 0,
        "app_ids": [
            int(a) for a in (item.get("included_appids") or []) if str(a).isdigit()
        ],
        "price_status": status,
        "name": item.get("name") or "",
        "header_image": bs.header_image_url(item) or "",
        "mps": mps,
        "item_kind": item_kind,
        "kind": kind,
    }


def _align_items(items: list[dict], ids: list[int]) -> list[tuple[int, dict | None]]:
    """条目 ↔ 请求 id 对齐：条数一致按位置（未收录条目回 id=0，位置是唯一可靠映射）；
    条数不符降级按 id 映射。"""
    if len(items) == len(ids):
        return list(zip(ids, items))
    by_id = {int(it["id"]): it for it in items if it.get("id")}
    return [(i, by_id.get(i)) for i in ids]


async def _fetch_regions_batched(
    session: aiohttp.ClientSession,
    want: list[tuple[int, int | None]],
    ccs: list[str] | None = None,
) -> dict[int, list[dict]]:
    """整表逐区一发：want=[(bundle_id, item_kind)] → {bundle_id: 区域行}。

    抓取区（ccs，缺省取监控启用区 + 南亚拆区展开）每区按形态（bundleid /
    packageid）各发；单发超 400 条或 URL 超长由 plan_id_batches 自动再切。
    全部（区 × 形态 × 批）请求一次 fan-out 并发，不再区内串行。请求失败的区
    不写行（下轮重试），锁区照写 locked，未收录（伪造 id）不写行。
    """
    groups: dict[str, list[int]] = {}
    for bid, item_kind in want:
        groups.setdefault(_browse_kind(bid, item_kind), []).append(int(bid))

    fetch_ccs = list(ccs) if ccs is not None else await _bundle_fetch_ccs()

    plans: list[tuple[str, str, list[dict]]] = []
    for cc in fetch_ccs:
        for kind, ids in groups.items():
            for batch in bs.StoreBrowseAPI.plan_id_batches(
                [{kind: bid} for bid in ids], cc, BROWSE_LANG, True,
                bs.DEFAULT_BATCH_SIZE,
            ):
                plans.append((cc, kind, batch))

    async def one(cc: str, kind: str, batch: list[dict]):
        items = await _fetch_browse_region(session, batch, cc)
        return cc, kind, batch, items

    out: dict[int, list[dict]] = {}
    for cc, kind, batch, items in await asyncio.gather(*(one(*p) for p in plans)):
        if items is None:
            continue
        batch_ids = [int(next(iter(s.values()))) for s in batch]
        for bid, item in _align_items(items, batch_ids):
            row = _browse_row(item, bid, cc, kind)
            if row is not None:
                out.setdefault(bid, []).append(row)
    return out


async def _fetch_bundle_regions(
    session: aiohttp.ClientSession, bundle_id: int,
    *, force_package: bool = False,
) -> list[dict]:
    """整包抓取（单包出口：手动导入共用，抓取区=监控启用区）。

    形态：force_package（/sub/ 链接）→ 直接按 packageid 问；其余先按
    bundleid 问，**一条真实条目都没有**（未收录）再按 packageid 兜一次——数字 ID
    空间 bundle/sub 并存，同号可能两侧都有产品。真实存在但全区锁区仍算成功
    （写 locked 行），与「抓取失败（空表）」严格区分。
    """
    primary = 1 if force_package else 0
    rows = (await _fetch_regions_batched(session, [(int(bundle_id), primary)])).get(
        int(bundle_id), []
    )
    if rows or force_package:
        return rows
    alt = (
        await _fetch_regions_batched(session, [(int(bundle_id), 1 - primary)])
    ).get(int(bundle_id), [])
    return alt if alt else rows


def _row_cny_fen(row: dict, rate_map: dict[str, float]) -> int | None:
    """分 → CNY 分：所有币种统一 cents，直接 × 汇率（与 games/pricing 同约定）。

    无小数货币（JPY/KRW/VND…）不可按「除数 1 再 ×100」折算——`*_in_cents`
    对所有币种都是「分」，那样会把分当元再乘 100，cny_fen 虚高 100 倍
    （存量已由 core/database 迁移 v2 归位）。
    """
    price = row.get("price")
    if not price:
        return None
    return convert_minor_to_cny_fen(
        int(price), row.get("currency") or "", rate_map
    )


def _majority(votes: list[int]) -> int | None:
    """0/1 票多数决；平票/无票 → None（不改写库内既有值）。"""
    if not votes:
        return None
    ones = votes.count(1)
    zeros = votes.count(0)
    if ones == zeros:
        return None
    return 1 if ones > zeros else 0


async def _upsert_bundle_rows(
    bundle_id: int, regions: list[dict], rate_map: dict[str, float], now: datetime,
    *, allow_singleton: bool = False,
) -> bool:
    """整包落库：主档 upsert + 区域价逐区 upsert + 双轨混写脏行清理。

    refresh_bundles 与 import_bundle 共用（导入即单包刷新，同一套写库
    语义：主档取 appids 最多行为基准，Package 轨行带 mps=1 标记，
    非本次写入的区行整行清理）。

    返回是否落库。**单包甄别**（allow_singleton=False 时）：包内 appid
    < 2 的不是捆绑包——游戏页 purchase_options 发现链会把本体 sub /
    单 DLC sub / 单游戏升级 sub 一起暴露出来（Steam 的 included_appids
    是权威鉴别），这类整包删除既有行并跳过写入，防止发现链把单品灌进
    捆绑包表。手动导入（用户明确意图）传 allow_singleton=True 例外。
    """
    # 主档取 appids 最多的行为基准（baseline 语义；锁区/包内游戏依据）
    baseline = max(regions, key=lambda r: len(r.get("app_ids") or []))
    baseline_appids = baseline.get("app_ids") or []
    if not allow_singleton and len(set(baseline_appids)) < 2:
        # 单品：删净既有主档/区域价（发现桩经链尾刷新抓价后到此甄别）
        async with get_session_factory()() as db:
            await db.execute(
                delete(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id == bundle_id)
            )
            await db.execute(
                delete(Bundle).where(Bundle.bundle_id == bundle_id)
            )
            await db.commit()
        return False
    # 形态（链接/CDN 用）：item_type 1=Sub / 2=Bundle 是 Steam 权威答案。
    # 逐区 item_type 理论恒定，但取多数决防单区脏值；无票（历史行/合成行）
    # 时不改写库内既有形态
    kind_votes = [r.get("item_kind") for r in regions if r.get("item_kind") in (0, 1)]
    kind_value = _majority(kind_votes)
    # 购买语义：Sub 形态恒不可拆；bundle 形态以选项级 must_purchase_as_set
    # 为准（形态与语义解耦）。锁区行不投票（_browse_row 已滤），
    # 有选项的区间偶发不一致由多数决归位。
    # 全无票且库内 NULL/-1 时补 0（判据兜底）
    mps_votes = [r.get("mps") for r in regions if r.get("mps") in (0, 1)]
    mps_value = _majority(mps_votes)

    async with get_session_factory()() as db:
        row = (
            await db.execute(select(Bundle).where(Bundle.bundle_id == bundle_id))
        ).scalar_one_or_none()
        if row is None:
            row = Bundle(bundle_id=bundle_id)
            db.add(row)
        if baseline.get("name"):
            row.name = baseline["name"]
        if baseline.get("header_image"):
            row.header_image = baseline["header_image"]
        if baseline.get("app_ids"):
            row.app_ids = baseline["app_ids"]
        if mps_value is not None:
            row.must_purchase_as_set = mps_value
        elif row.must_purchase_as_set is None or row.must_purchase_as_set == -1:
            row.must_purchase_as_set = 0
        if kind_value is not None:
            row.item_kind = kind_value
        elif row.item_kind is None or row.item_kind == -1:
            row.item_kind = 0 if row.must_purchase_as_set in (0, 1) else -1
        row.updated_at = now

        for r in regions:
            existing = (
                await db.execute(
                    select(BundleRegionPrice).where(
                        BundleRegionPrice.bundle_id == bundle_id,
                        BundleRegionPrice.region_code == r["region_code"],
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = BundleRegionPrice(
                    bundle_id=bundle_id, region_code=r["region_code"]
                )
                db.add(existing)
            existing.currency = r.get("currency")
            existing.price = r.get("price")
            existing.original_price = r.get("original_price")
            existing.discount_percent = r.get("discount_percent") or 0
            existing.bundle_base_discount = r.get("bundle_base_discount") or 0
            existing.price_status = r.get("price_status", "ok")
            existing.app_ids = r.get("app_ids") or []
            existing.crawled_at = now
            existing.cny_fen = _row_cny_fen(r, rate_map)

        # 历史脏行清理：非本次写入的区（双轨混写/异种产品/下架区）整行删；
        # 大小写重复行（'JP' 与 'jp' 同区）以本次写入的小写行为权威，旧行删——
        # 否则展示层「有价者优先」会随机挑中未归一的旧值
        fresh_codes = {r["region_code"].lower() for r in regions}
        stale = (
            await db.execute(
                select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == bundle_id)
            )
        ).scalars().all()
        for old in stale:
            code = old.region_code.lower()
            if code not in fresh_codes or old.region_code != code:
                await db.delete(old)
        await db.commit()
        return True


async def refresh_bundles() -> dict:
    """全量刷新捆绑包：库内所有包**监控区整表每区一发**（≤400 条）→ upsert。

    同时承担「无价桩首抓」职责：发现的捆绑包（游戏爬取 purchase_options
    落下的无价桩）就在全量表内，随同一批请求完成首抓——不需要独立的
    逐包播种通道。抓取区 = 监控启用区（南亚 pk/bd 双发），全禁用时由
    effective_regions 抛 ValueError（链尾按跳过语义、手动导入转 400）。
    """
    rates = await get_rates()  # {currency: rate_to_cny}
    rate_map = dict(rates) if isinstance(rates, dict) else {}
    rate_map.setdefault("CNY", 1.0)
    ccs = await _bundle_fetch_ccs()

    async with get_session_factory()() as session:
        want = [
            (int(bid), kind)
            for bid, kind in (
                await session.execute(
                    select(
                        Bundle.bundle_id,
                        # 形态选键：item_kind 权威；v3 之前的行兜底沿用 mps 旧值
                        func.coalesce(Bundle.item_kind, Bundle.must_purchase_as_set),
                    ).order_by(Bundle.bundle_id)
                )
            ).all()
        ]
        priced = {
            int(b)
            for b in (
                await session.execute(
                    select(BundleRegionPrice.bundle_id).distinct()
                )
            ).scalars()
        }
    if not want:
        return {"ok": False, "detail": "库内无捆绑包（先从原项目导入或添加）"}
    pending_ids = {bid for bid, _kind in want if bid not in priced}

    now = datetime.utcnow()
    updated_bundles = 0
    updated_prices = 0

    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        rows_by_id = await _fetch_regions_batched(session, want, ccs=ccs)
        # 形态兜底：按库内形态问不到真实条目的包，换另一形态再问一次
        # （历史形态缺失/曾按错形态导入的包自愈；单区一发，代价可忽略）
        missing = [(bid, kind) for bid, kind in want if not rows_by_id.get(bid)]
        if missing:
            flipped = [
                (bid, 1 if _browse_kind(bid, kind) == "bundleid" else 0)
                for bid, kind in missing
            ]
            logger.info("[bundles] %d 个包按库内形态未命中，换形态兜底探测", len(flipped))
            alt = await _fetch_regions_batched(session, flipped, ccs=ccs)
            for bid, _kind in missing:
                if alt.get(bid):
                    rows_by_id[bid] = alt[bid]

    failed: list[int] = [bid for bid, _kind in want if not rows_by_id.get(bid)]
    dropped_singletons = 0
    seeded = 0
    touched: list[int] = []  # 本轮成功落库的包：链尾统一重建排序快照
    for bid, _kind in want:
        regions = rows_by_id.get(bid) or []
        if not regions:
            continue
        if not await _upsert_bundle_rows(bid, regions, rate_map, now):
            dropped_singletons += 1  # 包内 appid<2：非捆绑，甄别删除
            continue
        updated_bundles += 1
        updated_prices += len(regions)
        touched.append(bid)
        if bid in pending_ids:
            seeded += 1

    # ── 排序快照增量重建（bundles.min_cny_fen/diff_fen/is_lowest）──
    # 写时算好、GET 只读：本轮价格变了哪些包就重建哪些包（按批，允许
    # 秒级短窗口——GET 最多看到旧快照，不会看到「价格新 / 差价旧」的半态）
    if touched:
        try:
            await refresh_bundle_sort_cache(touched)
        except Exception:  # noqa: BLE001
            logger.exception("[bundles] 排序快照重建失败（列表沿用上一版快照）")

    logger.info(
        "捆绑包刷新完成：成功 %d/%d，区域价 %d 行，失败 %d 个，单品甄别剔除 %d 个，"
        "无价桩首抓 %d 个（%s 区整表一发）",
        updated_bundles, len(want), updated_prices, len(failed),
        dropped_singletons, seeded, "/".join(ccs),
    )
    result = {
        "ok": not failed,
        "updated": updated_bundles,
        "total": len(want),
        "regionPrices": updated_prices,
        "failed": failed,
        "droppedSingletons": dropped_singletons,
        "seeded": seeded,
    }
    invalidate_bundles_cache()
    # ── 包内 appid 检测 → app 爬取队列联动 ──
    try:
        enqueued, skipped = await _enqueue_new_bundle_apps()
        result["appsEnqueued"] = enqueued
        result["appsSkippedNonGame"] = skipped
    except Exception as e:  # noqa: BLE001
        logger.warning("捆绑包 appid 队列联动失败（不影响刷新结果）: %s", e)
    return result


# Steam 链接形态：/bundle/21478、/sub/61597（商店/steamdb 同段式），
# 裸数字默认按 bundle 探测（双轨判定会自动落到 sub）
_BUNDLE_URL_RE = re.compile(
    r"(?:store\.steampowered\.com|steamdb\.info|steamdb\.in)"
    r"/(?:bundle|sub)/(\d+)",
    re.IGNORECASE,
)


def _parse_bundle_ref(text: str) -> tuple[int, str] | None:
    """Steam 链接/裸 ID → (id, kind)。kind: bundle/sub。

    识别：商店/SteamDB 链接（?query 参数容忍）、裸数字。其余输入返回 None。
    """
    s = (text or "").strip()
    if not s:
        return None
    m = _BUNDLE_URL_RE.search(s)
    if m:
        kind = "sub" if "/sub/" in m.group(0).lower() else "bundle"
        return int(m.group(1)), kind
    if s.isdigit():
        return int(s), "bundle"
    return None


async def import_bundle(text: str) -> dict:
    """导入单个捆绑包/Sub：链接识别 → 双轨整包抓取 → 落库（单包刷新语义）。

    成功后跑一次 _enqueue_new_bundle_apps（包内新 appid 即时进 app 爬取
    队列，与全量刷新同语义）；已在库的包重新导入 = 单包刷新（覆盖更新）。
    """
    ref = _parse_bundle_ref(text)
    if ref is None:
        raise ValueError("无法识别的 Steam 链接或捆绑包 ID")
    bundle_id, kind = ref

    rates = await get_rates()
    rate_map = dict(rates) if isinstance(rates, dict) else {}
    rate_map.setdefault("CNY", 1.0)

    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        regions = await _fetch_bundle_regions(
            session, bundle_id, force_package=(kind == "sub")
        )
    if not regions:
        raise ValueError(
            f"{'Sub' if kind == 'sub' else '捆绑包'} {bundle_id} 抓取失败"
            "（不存在/限速/网络）——稍后重试或检查链接"
        )
    now = datetime.utcnow()
    async with get_session_factory()() as session:
        existed = (
            await session.execute(
                select(Bundle.bundle_id).where(Bundle.bundle_id == bundle_id)
            )
        ).scalar_one_or_none() is not None

    # 手动导入 = 用户明确意图：单 appid 的 sub（Prime 升级类）例外保留
    if not await _upsert_bundle_rows(
        bundle_id, regions, rate_map, now, allow_singleton=True
    ):
        return {"ok": False, "detail": "落库失败"}
    logger.info("捆绑包导入完成：%s %d（%d 区价格）", kind, bundle_id, len(regions))
    result = {
        "ok": True,
        "bundleId": bundle_id,
        "kind": kind,
        "existed": existed,
        "regionPrices": len(regions),
        "name": (max(regions, key=lambda r: len(r.get("app_ids") or []))).get("name", ""),
    }
    # 排序快照重建（单包，写时算好、GET 只读）→ 再失效列表缓存
    try:
        await refresh_bundle_sort_cache([bundle_id])
    except Exception:  # noqa: BLE001
        logger.exception("[bundles] 导入后排序快照重建失败（列表沿用上一版快照）")
    # 包内新 appid 即时进 app 爬取队列——后台跑：预检（每轮限量）+ 小批量
    # 爬取是分钟级长事务，导入交互要求落库后立即回显（与全量刷新的同步
    # 计数语义不同，失败只记日志不影响导入结果）
    invalidate_bundles_cache()

    async def _enqueue_bg() -> None:
        try:
            enqueued, skipped = await _enqueue_new_bundle_apps()
            logger.info(
                "[bundles] 导入后队列联动完成：入队 %d，非游戏标记 %d", enqueued, skipped
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("导入后 appid 队列联动失败（不影响导入结果）: %s", e)

    asyncio.create_task(_enqueue_bg())
    return result


async def _fetch_app_type(
    session: aiohttp.ClientSession, appid: int
) -> tuple[str | None, str]:
    """轻量 appdetails type 预检：返回 (type, name)。失败 (None, '')。

    只取 filters=basic（单 appid 一次小请求）；cc=us（type/name 不随区变）。
    失败原因只在 debug 级逐条记录，汇总告警由调用方统计。
    """
    status: int | None = None
    try:
        await _rate_limit_acquire()
        async with session.get(
            APPDETAILS_URL,
            params={"appids": appid, "cc": "us", "filters": "basic"},
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            status = resp.status
            if status != 200:
                return None, ""
            data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.debug("[bundles] appid %s appdetails 预检异常: %r", appid, e)
        return None, ""
    logger.debug("[bundles] appid %s appdetails HTTP %s", appid, status)
    entry = (data or {}).get(str(appid), {})
    if not entry.get("success"):
        logger.debug("[bundles] appid %s appdetails success=false（下架/限速）", appid)
        return None, ""
    d = entry.get("data", {})
    return (d.get("type") or "").lower() or None, d.get("name") or ""


async def _enqueue_new_bundle_apps() -> tuple[int, int]:
    """捆绑包检测出的 appid 自动进 app 爬取队列。

    流程（对齐"立即抓取对应 APP 信息"需求）：
    1. 汇总全部 bundles.app_ids；
    2. 过滤已结案的：games 表已有 type 的行（game/dlc 已爬过；非 game 类
       型如 hardware/series 已打标，防重检）；
    3. 新 appid 逐一轻量预检 appdetails type（每轮限量 _PRECHECK_PER_RUN，
       余量顺延下轮——刷新时长兜底 + appdetails 礼貌限速）：
       - game/dlc → 先落 games 占位行（updated_at NULL → 回补层/首爬放行通道
         会带全量元数据回补中文名），再把 appid 加入爬取任务；
       - 非 game/dlc（demo/mod/hardware/series…）→ mark_non_game_type 落
         type+updated_at（脱离回补池，下次刷新不会再重复预检——games 表
         已有 type 即结案）；
       - 预检失败（网络/429）→ 不落任何标记，下次刷新重试（宁重试勿漏）。

    爬取任务通过 crawl 域的 backfill 语义触发：直接调用 run_crawl 小批量
    同步跑（几款游戏的量级，低 worker），避免依赖全局单任务槽位。
    返回 (入队爬取数, 非游戏标记数)。
    """
    from app.crawler.runner import CrawlRunConfig, run_crawl

    async with get_session_factory()() as session:
        bundles = (await session.execute(select(Bundle.app_ids))).scalars().all()
        # 已有 type 的行全部结案（含 game/dlc 已爬与非游戏已标——防重检关键）
        typed = (
            await session.execute(
                select(Game.appid).where(Game.type.is_not(None))
            )
        ).scalars().all()

    all_appids: set[int] = set()
    for raw in bundles:
        for a in raw or []:
            all_appids.add(int(a))
    candidates = sorted(all_appids - set(typed))
    if not candidates:
        return 0, 0
    if len(candidates) > _PRECHECK_PER_RUN:
        logger.info(
            "[bundles] 候选 appid %d 个 > 本轮上限 %d，只预检前 %d 个（余量顺延下轮）",
            len(candidates), _PRECHECK_PER_RUN, _PRECHECK_PER_RUN,
        )
        candidates = candidates[:_PRECHECK_PER_RUN]

    crawl_pairs: list[tuple[int, str]] = []
    skipped = 0
    probe_failed = 0
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        from app.crawler.db_writer import DbWriter

        db_writer = DbWriter()
        for appid in candidates:
            app_type, name = await _fetch_app_type(session, appid)
            if app_type is None:
                probe_failed += 1  # 预检失败：不标不记，下次刷新重试
                continue
            if app_type in _CRAWLABLE_TYPES:
                # 占位行（updated_at NULL）→ ensure 语义；中文名由爬取回补
                await db_writer.ensure_game_exists(appid, name)
                crawl_pairs.append((appid, name))
                logger.info("[bundles] appid %s (%s) 为 %s → 进 app 爬取队列", appid, name[:30], app_type)
            else:
                await db_writer.mark_non_game_type(appid, app_type)
                skipped += 1
                logger.info("[bundles] appid %s (%s) 为 %s → 打标跳过", appid, name[:30], app_type)
            # appdetails 限速（封面图三级缓存同为 0.3s 间隔）
            await asyncio.sleep(0.3)
    if probe_failed:
        logger.warning(
            "[bundles] appid 预检失败 %d/%d（限速/无可用代理/下架），未标记，下轮刷新自动重试",
            probe_failed, len(candidates),
        )

    if crawl_pairs:
        # 走「我」页生效区（与手动爬取同口径）；小批量低 worker 直跑
        from app.domains.regions.service import enabled_regions

        regions = await enabled_regions()
        if regions:
            try:
                # 与 crawl 域同一条约束：每次 run 取一次当前 Runtime 地址；拿不到就
                # **不启动**这次 run，绝不退回直连/旧订阅代理（占位行已落库，回补层消化）
                from app.core.config import get_settings as _get_settings
                from app.domains.proxypool.runtime import current_runtime_proxy_url

                proxy_url = current_runtime_proxy_url(_get_settings().data_dir)
                if proxy_url is None:
                    logger.warning(
                        "[bundles] 代理运行时不可用：本次新 appid 爬取未启动（不退回直连）"
                    )
                else:
                    config = CrawlRunConfig(
                        regions=regions, workers=4, proxy_url=proxy_url
                    )
                    stats = await run_crawl(crawl_pairs, config=config)
                    logger.info("[bundles] 新 appid 爬取完成：%s", {k: v for k, v in stats.items() if k != "elapsed_seconds"})
            except Exception as e:  # noqa: BLE001
                logger.warning("[bundles] 新 appid 爬取任务失败（占位行已落库，回补层会消化）: %s", e)
    return len(crawl_pairs), skipped
