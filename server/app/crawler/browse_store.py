"""IStoreBrowseService 价格抓取层（替代 appdetails 只换抓取层版）。

与旧 appdetails 链路的关系：
- 并发调度**原封不动**：仍用 CrawlerScheduler（worker 池）+ SteamHttpClient
  （全局限流 / 429 熔断 / 指数退避 / Connection: close）。
- 抓取层换成 IStoreBrowseService/GetItems/v1：单区 × ≤400 appid 一发批量，
  元数据（名称/开发商/好评率/发售日/头图/家庭共享/卡牌/成人标记）全部由
  同一发响应白送，替掉 appdetails + appreviews 两次请求。**价格按请求参数
  country_code 判定**——出口 IP 不参与数据判定，直连与代理拿到同一份数据，
  这是直连成为标准形态、限流取代换 IP 规避风控的根因。
- 原价直接取 original_price_in_cents（一手），不再用 `现价×100÷(100−折扣)` 反推
  （Steam 先定价后折后取整，反推回不去——实测 699→698 / 22900→22833 这类偏差）。
- 错误语义与旧链路一致：run 内退避重推 MAX_PARTIAL_RETRIES 次 → 仍失败写入
  missing 状态进补抓账本 → 由 generate_missing_tasks() 生成的按区批量任务
  在下个周期只补欠账区（穷尽转 blocked）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from yarl import URL

from app.core.database import get_session_factory
from app.crawler.config import CIS_REGIONS, CC_LIST
from app.crawler.db_writer import DbWriter
from app.crawler.http_client import SteamRateLimitError
from app.crawler.network_check import network_checker
from app.crawler.utils import (
    extract_version_suffix,
    get_beijing_time_obj,
    is_gold_edition,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# 接口常量（实测定版）
# ══════════════════════════════════════════════════════════════

BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
# 必须 GET（POST → 405）；input_json 只能放查询参数。
# 最小转义：保留 `{}",:` 与数字/小写字母（全转义会让单发条数掉到 ~260）
_URL_SAFE = '{}",:0123456789abcdefghijklmnopqrstuvwxyz-_.'
# Steam 侧按 URL ~8KB 截断（实测 400 条 × 全字段 = 7.5KB 仍 200）——防御性再切
MAX_URL_LEN = 8000
DEFAULT_BATCH_SIZE = 400

DATA_REQUEST_BASE = {
    "include_basic_info": True,
    "include_all_purchase_options": True,
}
# 可选抓取项：全部白送，不额外请求
DATA_REQUEST_EXTRAS = {
    "include_release": True,      # → release_date / coming_soon
    "include_assets": True,       # → header_image（asset_url_format）
    "include_reviews": True,      # → 好评率/评论数（替掉 appreviews 接口）
    "include_platforms": True,    # → 平台支持
    "include_coming_soon": True,  # → 即将推出标记
}

# browse 的 item.type 实测标定（对照生产 games.type 反查）：
#   0=游戏 1=Demo 2=Mod 3=Tool 4=DLC 6=应用(如 Wallpaper Engine，旧链路按 GAME 收)
#   14=游戏变体(如 CoD BO 多人在线包，旧链路按 GAME 收)；None+success=15 = 未收录
_TYPE_TO_GAMES = {0: "GAME", 1: "DEMO", 2: "MOD", 3: "TOOL", 4: "DLC", 6: "GAME"}
# 明确非游戏；未标定的编码一律按游戏收（宁可多收一个工具应用，不能漏游戏）
GAME_TYPES = ("GAME", "DLC")
ADULT_DESCRIPTOR_IDS = frozenset((3, 4))
# descriptor 3 = Adult Only Sexual Content，4 = Frequent Nudity or Sexual Content
# ——只认这两个为成人。实测反例：GTA5/Dota2/HuniePop 只有 5（General Mature
# Content），赛博朋克 2077 是 [1,2,5]，旧链路（genres 含 Sexual Content/Nudity）
# 对它们全部判 0；1/5 是主流游戏也带的普通成熟标记，算进去会误杀。
CAT_FAMILY_SHARING = 62          # categories.feature_categoryids 语义位
CAT_TRADING_CARDS = 29

CDN_PREFIX = "https://shared.akamai.steamstatic.com/store_item_assets/"
META_FALLBACK_REGIONS = ("cn", "us", "ua", "id", "pk")
MAX_PARTIAL_RETRIES = 3

CURRENCY_BY_CC = {cc: cur for cc, _, cur in CC_LIST}

# ══════════════════════════════════════════════════════════════
# 运行态（预取 → 任务处理器；每轮 crawl 前 reset_run_state()）
# ══════════════════════════════════════════════════════════════

META: dict[int, dict] = {}          # appid → browse 元数据
CIS_SUBS: dict[int, set[int]] = {}  # appid → kz/ua 在售 subid 集（RU 基准过滤）
PRESERVED: dict[int, dict] = {}     # appid → 库内原值（browse 拿不到的列）
FAILED_TASKS: list[str] = []
NO_OPTIONS_COUNT = 0                # 「可见但无购买选项」计数（判 locked）
PARENT_FOLLOWED = 0                 # 子 app 跟父补齐计数
BUNDLES_DISCOVERED = 0             # purchase_options 捆绑包发现计数（链尾刷新自动抓价）
FOLLOW_PARENT = True
EXTRAS_ENABLED = True
DRY_RUN = False

# 已进入落库循环的批次（兜底路径据此判断「本批是否一行都没写」）
_WRITE_STARTED: set[str] = set()


def reset_run_state() -> None:
    """每轮 crawl 开始前清空运行态（防上一轮残留串味）。"""
    global NO_OPTIONS_COUNT, PARENT_FOLLOWED
    META.clear()
    CIS_SUBS.clear()
    PRESERVED.clear()
    FAILED_TASKS.clear()
    _WRITE_STARTED.clear()
    NO_OPTIONS_COUNT = 0
    PARENT_FOLLOWED = 0


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════


def _to_int(v) -> int | None:
    """browse 的价格/折扣字段是**字符串**（"74000"），一律显式转 int。"""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _naive(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fmt_release_ts(ts) -> str:
    ts = _to_int(ts)
    if not ts or ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _header_image(item: dict | None) -> str | None:
    assets = (item or {}).get("assets") or {}
    fmt = assets.get("asset_url_format") or ""
    filename = assets.get("header") or ""
    if not fmt or not filename:
        return None
    return CDN_PREFIX + fmt.replace("${FILENAME}", filename)


def header_image_url(item: dict | None) -> str | None:
    """条目资产 → 头图 URL（捆绑包链路复用同一资产约定）。"""
    return _header_image(item)


def _discount_meta(opt: dict) -> dict:
    """active_discounts[0] → 促销截止 + 促销类型（browse 白送）。"""
    ads = opt.get("active_discounts") or []
    first = ads[0] if ads else {}
    desc = first.get("discount_description")
    return {
        "discount_end_ts": _to_int(first.get("discount_end_date")),
        "discount_desc": (str(desc)[:60] if desc else None),
        "bundle_id": _to_int(opt.get("bundleid")),
        "bundle_discount_pct": _to_int(opt.get("bundle_discount_pct")),
    }


async def _wait_network_online(context) -> bool:
    """断网守候（stop 感知）：与旧 app_handler 同语义。"""
    return await network_checker.wait_until_online(getattr(context, "stop_event", None))


def _visible_without_options(item: dict | None) -> bool:
    """可见但一发购买选项都没有 = 该区不售（判 locked，仅用于计数）。"""
    if not item or item.get("success") != 1 or not item.get("visible") or item.get("is_free"):
        return False
    return not (item.get("purchase_options") or item.get("best_purchase_option"))


async def _mark_missing_async(db_writer, appids: list[int], cc: str) -> int:
    """同上，异步版：逐 appid 写 missing 进账本。返回写入条数。"""
    if DRY_RUN:
        return 0
    wrote = 0
    for appid in appids:
        try:
            await db_writer.mark_region_status(int(appid), cc, "missing")
            wrote += 1
        except Exception as e:  # noqa: BLE001 —— 账本写失败不阻断主流程
            logger.error("[browse] missing 记账失败 %s/%s: %s", appid, cc, e)
    return wrote


# ══════════════════════════════════════════════════════════════
# 抓取层：StoreBrowseAPI
# ══════════════════════════════════════════════════════════════


class StoreBrowseAPI:
    """IStoreBrowseService/GetItems 适配器：批量（≤400）× 单区。"""

    @staticmethod
    def build_ids_url(
        id_specs: list[dict], cc: str, lang: str, extras: bool = True
    ) -> URL:
        """通用 ids 构造：appid / bundleid / packageid 可混批（编码约定同源）。"""
        body = {
            # 对象形式；纯 int 会返回 success 但拿不到价格
            "ids": [dict(s) for s in id_specs],
            "context": {"language": lang, "country_code": cc.upper(), "steam_realm": 1},
            "data_request": {
                **DATA_REQUEST_BASE,
                **(DATA_REQUEST_EXTRAS if extras else {}),
            },
        }
        qs = urllib.parse.urlencode(
            {"input_json": json.dumps(body, separators=(",", ":"))}, safe=_URL_SAFE
        )
        # ⚠ 坑（实测）：aiohttp 用 yarl 建 URL，会把 `{ } "` 再百分号编码一遍，
        # 7.5KB 的 400 条 URL 被撑到 ~9.2KB → Steam 直接 400。encoded=True 声明
        # 「已编码、别再动」，保住 400 条/发的硬限制。
        return URL(f"{BROWSE_URL}?{qs}", encoded=True)

    @staticmethod
    def build_url(appids: list[int], cc: str, lang: str, extras: bool = True) -> URL:
        return StoreBrowseAPI.build_ids_url(
            [{"appid": int(a)} for a in appids], cc, lang, extras
        )

    @staticmethod
    def plan_batches(
        appids: list[int], cc: str, lang: str, extras: bool, batch_size: int
    ) -> list[list[int]]:
        """按条数分批 + 按 URL 长度兜底再切（7 位 appid 变多会自动收紧）。"""
        specs = [{"appid": int(a)} for a in dict.fromkeys(appids)]
        return [
            [int(next(iter(s.values()))) for s in batch]
            for batch in StoreBrowseAPI.plan_id_batches(
                specs, cc, lang, extras, batch_size
            )
        ]

    @staticmethod
    def plan_id_batches(
        id_specs: list[dict], cc: str, lang: str, extras: bool, batch_size: int
    ) -> list[list[dict]]:
        """plan_batches 的通用版：id 规格（bundleid/packageid）分批。"""
        seen: set[str] = set()
        uniq: list[dict] = []
        for spec in id_specs:
            key = json.dumps(spec, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(dict(spec))
        out: list[list[dict]] = []
        for chunk in chunks(uniq, batch_size):
            out.extend(StoreBrowseAPI._split_ids_by_url(chunk, cc, lang, extras))
        return out

    @staticmethod
    def _split_ids_by_url(
        id_specs: list[dict], cc: str, lang: str, extras: bool
    ) -> list[list[dict]]:
        if len(id_specs) <= 1:
            return [id_specs]
        url_len = len(
            str(StoreBrowseAPI.build_ids_url(id_specs, cc, lang, extras))
        )
        if url_len <= MAX_URL_LEN:
            return [id_specs]
        mid = len(id_specs) // 2
        return StoreBrowseAPI._split_ids_by_url(
            id_specs[:mid], cc, lang, extras
        ) + StoreBrowseAPI._split_ids_by_url(id_specs[mid:], cc, lang, extras)

    @staticmethod
    async def fetch_batch(
        context, appids: list[int], cc: str, lang: str, extras: bool = True
    ) -> tuple[dict[int, dict | None], int]:
        """一发批量。返回 ({appid: item|None}, 响应体字节估算)。"""
        url = StoreBrowseAPI.build_url(appids, cc, lang, extras)
        data = await context.http_client.get_json(context.session, url)
        store_items = ((data or {}).get("response") or {}).get("store_items") or []
        size = len(json.dumps(data, ensure_ascii=False)) if data else 0
        return StoreBrowseAPI.map_items(store_items, appids), size

    @staticmethod
    def map_items(items: list[dict], appids: list[int]) -> dict[int, dict | None]:
        """条目 → {appid: item}。

        坑：服务端对「未收录」条目回 appid=0（如 228980 Steamworks），按 appid
        映射会互相覆盖；发送前已去重，位置 1:1 才是唯一可靠映射，返回的 appid
        只做校验（数量不符时降级为按 appid 映射）。
        """
        out: dict[int, dict | None] = {}
        if len(items) == len(appids):
            for sent, it in zip(appids, items):
                if not it.get("appid"):
                    it = {**it, "appid": int(sent)}
                elif int(it["appid"]) != int(sent):
                    logger.warning("[browse] 位置错位：请求 %s 返回 %s", sent, it["appid"])
                out[int(sent)] = it
        else:
            logger.warning(
                "[browse] 条目数不符：请求 %d 返回 %d，降级按 appid 映射",
                len(appids), len(items),
            )
            for it in items:
                if it.get("appid"):
                    out[int(it["appid"])] = it
        for a in appids:
            out.setdefault(int(a), None)
        return out

    # ── 解析 ──

    @staticmethod
    def evaluate(item: dict | None, name_en: str = "") -> tuple[str, list[dict] | None]:
        """item → (price_status, options)。

        实测编码：
        - 未收录/锁区 → success=15 + visible=false
        - 免费游戏   → is_free=true，**没有** purchase_options 键（不是空数组）
        - 付费       → purchase_options[]（bundle 选项只有 bundleid、无 packageid）
        - 可见但无购买选项 → 该区不售。**判 locked 而非 missing**：missing 在库内
          语义是「抓取欠账」（进补抓账本、连失 5 次转 blocked），把「该区不卖」
          记成欠账会让补抓层空转。
        """
        if not item or item.get("success") != 1 or not item.get("visible"):
            return "locked", None
        if item.get("unvailable_for_country_restriction"):
            return "locked", None
        if item.get("is_free"):
            return "free", None
        opts = StoreBrowseAPI.parse_options(item, name_en)
        if not opts:
            return "locked", None
        return "ok", opts

    @staticmethod
    def parse_options(item: dict, name_en: str = "") -> list[dict]:
        """purchase_options[] → 旧链路 parse_all_sub_prices 的同款 dict 形状。

        与旧实现的关键差异：原价直接取 original_price_in_cents（一手数据），
        不再用 `price×100÷(100-discount)` 反推（Steam 先定价后折后取整，回不去）。
        """
        # 版本后缀提取的基准名：调用方没给（补抓轮无预取、META 全空）时
        # 用条目自身名字兜底——价格批固定 english 语境，item.name 即英文名。
        # 缺了这层，豪华版等选项的后缀提取全部失效 → 全员伪装成标准版候选
        # → min(sub_id) 会把 sub 编号更小的豪华版选成现价
        # （案例：女神异闻录４ 黄金版 deluxe 376686 < 标准 447601）
        if not name_en:
            name_en = item.get("name") or ""
        raw = item.get("purchase_options") or []
        if not raw:
            best = item.get("best_purchase_option")
            raw = [best] if best else []
        # 分组语义对齐旧链路 package_groups["default"]：有 default 就只取 default
        if any(o.get("package_group") == "default" for o in raw):
            raw = [o for o in raw if o.get("package_group") == "default"]

        results: list[dict] = []
        seen: set[int] = set()
        for opt in raw:
            packageid = _to_int(opt.get("packageid"))
            bundleid = _to_int(opt.get("bundleid"))
            if not packageid and not bundleid:
                continue
            key = packageid or -bundleid
            if key in seen:
                continue
            seen.add(key)

            price = _to_int(opt.get("final_price_in_cents"))
            original = _to_int(opt.get("original_price_in_cents"))
            if original is None:
                original = price  # 无促销 → 原价 = 现价
            discount = _to_int(opt.get("discount_pct")) or 0
            if discount <= 0 and price and original and original > price:
                discount = round((original - price) / original * 100)
            name = opt.get("purchase_option_name") or ""
            results.append(
                {
                    "price_cents": price if price else None,
                    "original_cents": original if price else None,
                    "is_gold": is_gold_edition(name),
                    "discount_pct": discount,
                    "sub_id": packageid if packageid else bundleid,
                    # bundle 选项（packageid 缺席、只有 bundleid，如 Gori 慈善包）
                    # → 送进 history 留档，但标记 is_bundle 让 db_writer 排除出
                    # 「标准版」候选（绝不参与 current 落库）
                    "is_bundle": bool(bundleid and not packageid),
                    "version_suffix": extract_version_suffix(name, name_en),
                    "option_name": name,
                    **_discount_meta(opt),
                }
            )
        return results

    @staticmethod
    def build_meta(item_zh: dict | None, item_en: dict | None) -> dict:
        """browse 响应 → games 表元数据。"""
        src = item_zh or item_en or {}
        basic = src.get("basic_info") or {}
        reviews = ((src.get("reviews") or {}).get("summary_filtered")) or {}
        features = set((src.get("categories") or {}).get("feature_categoryids") or [])
        descriptors = set(src.get("content_descriptorids") or [])
        release = src.get("release") or {}
        raw_type = src.get("type")
        review_count = _to_int(reviews.get("review_count")) or 0
        pct = _to_int(reviews.get("percent_positive")) or 0
        games_type = _TYPE_TO_GAMES.get(raw_type) or "GAME"

        return {
            "name_zh": (item_zh or {}).get("name") or "",
            "name_en": (item_en or src).get("name") or "",
            "type_raw": raw_type,
            "type": games_type,
            "type_label": games_type.lower(),
            "visible": bool(src.get("visible")),
            "coming_soon": bool(src.get("is_coming_soon") or release.get("is_coming_soon")),
            "release_ts": release.get("steam_release_date"),
            "header_image": _header_image(src),
            "developers": [d.get("name") for d in (basic.get("developers") or []) if d.get("name")],
            "publishers": [p.get("name") for p in (basic.get("publishers") or []) if p.get("name")],
            "short_description": basic.get("short_description") or "",
            "family_sharing": CAT_FAMILY_SHARING in features,
            "trading_cards": CAT_TRADING_CARDS in features,
            "is_adult": bool(ADULT_DESCRIPTOR_IDS & descriptors),
            # 评测摘要（0-100 口径，写库层再 ×100）。此前 computed-but-dropped：
            # 算了没进返回值，写库层永远拿 None 回落库内旧值——browse 首爬
            # 建的新行全库评测量成 0
            "review_count": review_count,
            "positive_rate": pct,
        }


# ══════════════════════════════════════════════════════════════
# games 表元数据装配
# ══════════════════════════════════════════════════════════════

# games 表：browse 拿不到的列，保留库内原值（不保留就会被写 NULL）
_PRESERVED_COLUMNS = [
    "name", "name_en", "type", "header_image", "chinese_support", "family_sharing",
    "trading_cards", "release_date", "genres", "developers", "publishers",
    "positive_rate", "positive_reviews", "review_count", "is_adult", "is_visual_novel",
]


def load_preserved_rows(db_path: Path) -> dict[int, dict]:
    """库内原值 → PRESERVED（browse 拿不到的列不能被抹成 NULL）。

    旧链路的 chinese_support 来自 appdetails 语言表、genres 来自 genres 分类，
    browse 均不返回——不做保留就会把已有数据抹成 NULL。
    """
    cols = ",".join(_PRESERVED_COLUMNS)
    conn = sqlite3.connect(Path(db_path).as_posix())
    try:
        rows = conn.execute(f"SELECT appid,{cols} FROM games").fetchall()
    finally:
        conn.close()
    out: dict[int, dict] = {}
    for row in rows:
        appid = int(row[0])
        out[appid] = {col: row[i + 1] for i, col in enumerate(_PRESERVED_COLUMNS)}
        out[appid]["family_sharing"] = bool(out[appid]["family_sharing"])
        out[appid]["trading_cards"] = bool(out[appid]["trading_cards"])
        out[appid]["is_adult"] = bool(out[appid]["is_adult"])
        out[appid]["is_visual_novel"] = bool(out[appid]["is_visual_novel"])
        out[appid]["developers"] = json.loads(out[appid]["developers"] or "[]")
        out[appid]["publishers"] = json.loads(out[appid]["publishers"] or "[]")
    return out


def build_game_data(appid: int, meta: dict | None, now_dt) -> dict | None:
    """browse 元数据 + 库内原值 → DbWriter.upsert_game_and_prices 的 game_data。

    meta 为 None（五个回退区全部不可见）时退化为纯原值——等于不动该行；
    连原值都没有（空库）则返回 None，调用方跳过写入。
    """
    keep = PRESERVED.get(appid)
    if meta is None and not keep:
        return None
    has_browse_meta = meta is not None
    meta = meta or {}
    name_en = (meta.get("name_en") or (keep or {}).get("name_en") or "").strip()
    name = (meta.get("name_zh") or "").strip() or (keep or {}).get("name") or name_en
    # 评测三件套：meta 携带真摘要（review_count>0）才覆盖，否则整体保留库内
    # 原值——API 对评测数不足的游戏不给 summary（拿到的 0 ≠ 真没有），
    # 不许拿 0 清掉老数据
    meta_reviews = int(meta.get("review_count") or 0)
    meta_pct = int(meta.get("positive_rate") or 0)
    if meta_reviews > 0 and meta_pct > 0:
        review_count = meta_reviews
        positive_rate = meta_pct * 100
        positive_reviews = round(meta_reviews * meta_pct / 100)
    else:
        review_count = (keep or {}).get("review_count")
        positive_rate = (keep or {}).get("positive_rate")
        positive_reviews = (keep or {}).get("positive_reviews") or 0

    return {
        "appid": int(appid),
        # ↓ browse 可提供（meta 为 None 时保留库内原值）
        "name": name if has_browse_meta else (keep or {}).get("name"),
        "name_en": name_en if has_browse_meta else (keep or {}).get("name_en"),
        "type": (meta.get("type") or (keep or {}).get("type") or "GAME").upper()
        if has_browse_meta else (keep or {}).get("type"),
        "header_image": meta.get("header_image") or (keep or {}).get("header_image"),
        # ↓ browse 拿不到，一律保留库内原值；is_visual_novel 建模 NOT NULL，
        #   空库首爬（无原值）必须给 bool 缺省，透传 None 会让 INSERT 违约束
        "chinese_support": (keep or {}).get("chinese_support"),
        "is_visual_novel": bool((keep or {}).get("is_visual_novel")),
        "genres": (keep or {}).get("genres"),
        # ↓ browse 可提供
        "family_sharing": bool(meta.get("family_sharing")) if has_browse_meta
        else bool((keep or {}).get("family_sharing")),
        "trading_cards": bool(meta.get("trading_cards")) if has_browse_meta
        else bool((keep or {}).get("trading_cards")),
        "release_date": _fmt_release_ts(meta.get("release_ts")) or (keep or {}).get("release_date"),
        "is_adult": bool(meta.get("is_adult")) if has_browse_meta
        else bool((keep or {}).get("is_adult")),
        "developers": meta.get("developers") or (keep or {}).get("developers") or [],
        "publishers": meta.get("publishers") or (keep or {}).get("publishers") or [],
        "positive_rate": positive_rate,
        "positive_reviews": positive_reviews,
        "review_count": review_count if review_count is not None else 0,
        "created_at": now_dt,
        "updated_at": now_dt,
    }


# ══════════════════════════════════════════════════════════════
# DbWriter 扩展：browse 独有字段落库口
# ══════════════════════════════════════════════════════════════

GPH_EXTRA_COLUMNS = {
    "discount_end_ts": "INTEGER",      # 促销截止 Unix 时间戳
    "discount_desc": "VARCHAR(60)",    # 促销类型（#discount_desc_preset_special）
    "bundle_id": "INTEGER",            # bundle 选项身份
    "bundle_discount_pct": "INTEGER",  # bundle 叠折
}


class BrowseDbWriter(DbWriter):
    """沿用生产写入语义，另加 browse 独有字段的落库口。"""

    _extras_ready: bool | None = None

    @classmethod
    async def _extras_columns_present(cls) -> bool:
        """game_price_history 是否已有 browse 扩展列（生产库未迁移前自动跳过）。"""
        if cls._extras_ready is not None:
            return cls._extras_ready
        try:
            async with get_session_factory()() as session:
                res = await session.execute(
                    text("PRAGMA table_info(game_price_history)")
                )
                cols = {row[1] for row in res.fetchall()}
            cls._extras_ready = set(GPH_EXTRA_COLUMNS) <= cols
            if not cls._extras_ready:
                logger.warning(
                    "[browse] game_price_history 缺扩展列 %s，扩展字段落库跳过"
                    "（需迁移后才有促销截止/促销类型/bundle 归属）",
                    sorted(set(GPH_EXTRA_COLUMNS) - cols),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[browse] 扩展列探测失败：%s", e)
            cls._extras_ready = False
        return cls._extras_ready

    async def ensure_extra_columns(self) -> None:
        """自建 browse 独有列（测试隔离库用；生产库请走正式迁移）。"""
        import sqlite3 as _sq

        from app.core.config import get_settings

        db_path = Path(get_settings().data_dir) / "holdexar.db"
        conn = _sq.connect(db_path.as_posix())
        try:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(game_price_history)")}
            for name, ddl in GPH_EXTRA_COLUMNS.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE game_price_history ADD COLUMN {name} {ddl}"
                    )
                    logger.info("[browse] game_price_history += %s", name)
            conn.commit()
        finally:
            conn.close()
        type(self)._extras_ready = None

    async def attach_browse_extras(
        self, appid: int, cc: str, now_dt, extras_by_sub: dict[int, dict]
    ) -> None:
        """把 browse 独有字段贴回本批刚写的 history 行（键 = appid+区+sub+快照）。"""
        if not extras_by_sub:
            return
        if not await self._extras_columns_present():
            return
        params = [
            {
                "e": opt.get("discount_end_ts"),
                "d": opt.get("discount_desc"),
                "b": opt.get("bundle_id"),
                "bd": opt.get("bundle_discount_pct"),
                "a": int(appid),
                "r": cc.upper(),
                "s": int(sub_id),
                "t": _naive(now_dt),
            }
            for sub_id, opt in extras_by_sub.items()
        ]
        try:
            async with get_session_factory()() as session:
                await session.execute(
                    text(
                        "UPDATE game_price_history SET discount_end_ts=:e, "
                        "discount_desc=:d, bundle_id=:b, bundle_discount_pct=:bd "
                        "WHERE appid=:a AND region_code=:r AND sub_id=:s AND snapshot_at=:t"
                    ),
                    params,
                )
                await session.commit()
        except Exception as e:  # noqa: BLE001 —— 扩展字段不阻断主链路
            logger.warning("[browse] 扩展字段落库失败 %s/%s: %s", appid, cc, e)


# ══════════════════════════════════════════════════════════════
# 任务处理器
# ══════════════════════════════════════════════════════════════


async def _follow_parent_apps(context, items: dict, appids: list[int], cc: str) -> int:
    """把「子 app」的购买选项从其父 app 补齐（原地改写 items）。返回补齐条数。

    判据：success=1 + visible + 无 purchase_options + 非免费 + 带
    related_items.parent_appid。父 appid 若同批已抓则直接复用，否则合并成一发。
    """
    if not FOLLOW_PARENT:
        return 0
    pairs: dict[int, int] = {}
    for appid in appids:
        it = items.get(appid)
        if not it or it.get("success") != 1 or not it.get("visible"):
            continue
        if it.get("is_free") or it.get("purchase_options"):
            continue
        parent = _to_int((it.get("related_items") or {}).get("parent_appid"))
        if parent and parent != appid:
            pairs[appid] = parent
    if not pairs:
        return 0

    missing = [p for p in dict.fromkeys(pairs.values()) if p not in items]
    if missing:
        pitems, _ = await StoreBrowseAPI.fetch_batch(context, missing, cc, "english", EXTRAS_ENABLED)
        items.update(pitems)

    filled = 0
    for appid, parent in pairs.items():
        pit = items.get(parent)
        if not pit or pit.get("success") != 1 or not pit.get("visible"):
            continue
        if not pit.get("purchase_options") and not pit.get("best_purchase_option"):
            continue
        items[appid] = {
            **items[appid],
            "purchase_options": pit.get("purchase_options") or [],
            "best_purchase_option": pit.get("best_purchase_option"),
            "parent_appid": parent,
        }
        filled += 1
    return filled


async def handle_browse_price_task(context) -> None:
    """任务入口：**任何**未预期异常都在这里兜住并重推，绝不让它逃到调度器。

    调度器 `_worker` 对 handler 抛出的异常只做 `fail_count += 1` 然后丢弃，
    **不重推**（重推一律由 handler 自己 `context.queue.put` 完成，旧 app_handler
    的各处重推点就是这个语义）。原先只有 fetch 那一发被 try 包着，`_follow_parent_apps`
    里补发父 app 的那次请求是裸的——全量跑时 ru:4 就在那里抛了 ClientConnectorError
    逃到调度器，整批 400 个 appid 的 RU 行永久丢失（RU 1848 行 vs 其他区 2248 行）。
    """
    task = context.task
    task_id = task.get("id")
    cc = task.get("region")
    appids: list[int] = task.get("appids") or []
    try:
        await _browse_price_task_inner(context)
        return
    except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都不得逃到调度器
        retries = task.get("retries", 0)
        if network_checker.is_offline:
            if not await _wait_network_online(context):
                return
            await context.queue.put(dict(task))
            return
        if retries < MAX_PARTIAL_RETRIES:
            delay = 5.0 * (2**retries) if isinstance(e, SteamRateLimitError) else 2.0
            logger.warning(
                "[browse] %s 未预期异常（%s），%.0fs 后重推（第 %d 次）",
                task_id, type(e).__name__, delay, retries + 1,
            )
            await asyncio.sleep(delay)
            await context.queue.put({**task, "retries": retries + 1})
            return
        # 穷尽：若本批一行都没写（异常发生在落库循环之前），整批记 missing
        # 进补抓账本，让补抓层下个周期只补这些失败区——绝不能静默丢行。
        if task_id not in _WRITE_STARTED:
            n = await _mark_missing_async(context.db_writer, appids, cc)
            logger.error(
                "[browse] %s 重推耗尽（%s），未落库整批 %d 条记 missing 进账本",
                task_id, e, n,
            )
        else:
            logger.error("[browse] %s 重推耗尽（%s），部分已落库，不覆盖", task_id, e)
        FAILED_TASKS.append(str(task_id))
    finally:
        _WRITE_STARTED.discard(str(task_id))


async def _browse_price_task_inner(context) -> None:
    """browse 价格任务：一发批量（单区 × ≤400 appid）→ 逐 appid 落库。

    与旧 app_handler 的差异只在抓取层（元数据来自预取 META、价格来自本发响应）；
    退避重推 / 断网守卫 / 欠账语义保持一致。
    """
    global PARENT_FOLLOWED, NO_OPTIONS_COUNT
    task = context.task
    cc = task["region"]
    appids: list[int] = task["appids"]
    retries = task.get("retries", 0)
    task_id = task.get("id")

    if network_checker.is_offline and not await _wait_network_online(context):
        logger.warning("[browse] %s 断网等待期间收到停止信号，任务丢弃", task_id)
        return

    started = time.monotonic()
    try:
        items, size = await StoreBrowseAPI.fetch_batch(
            context, appids, cc, "english", EXTRAS_ENABLED
        )
    except Exception as e:  # noqa: BLE001
        rate_limited = isinstance(e, SteamRateLimitError)
        # 断网守卫：网络故障烧掉的预算不算数，等恢复后原样重推
        if network_checker.is_offline:
            if not await _wait_network_online(context):
                return
            await context.queue.put(dict(task))
            return
        if retries < MAX_PARTIAL_RETRIES:
            delay = 5.0 * (2**retries) if rate_limited else 1.0
            logger.warning(
                "[browse] %s 抓取失败（%s），%.0fs 后重推（第 %d 次）",
                task_id, type(e).__name__, delay, retries + 1,
            )
            await asyncio.sleep(delay)
            await context.queue.put({**task, "retries": retries + 1})
            return
        # 穷尽：本批必然一行未写 → 整批记 missing 进账本（补抓层下轮只补失败区）
        n = await _mark_missing_async(context.db_writer, appids, cc)
        logger.error(
            "[browse] %s 抓取失败且已达最大重试（%s），%d 条记 missing 进账本", task_id, e, n
        )
        FAILED_TASKS.append(str(task_id))
        return

    # ── 子 app 跟父：type=14 / related_items.parent_appid 的条目自身没有购买
    #    选项（如 42710 "CoD: BO - Multiplayer" 之于 42700）。旧链路 appdetails
    #    对这类 appid 会重定向到父 app 返回父的价格，为保价格口径一致，这里
    #    显式补一发父 appid 并把父的 purchase_options 挂回子 appid ──
    followed = await _follow_parent_apps(context, items, appids, cc)
    if followed:
        PARENT_FOLLOWED += followed

    logger.info(
        "[browse] %s 返回 %d/%d 条 | %.0fKB | %.1fs",
        task_id, len(items), len(appids), size / 1024, time.monotonic() - started,
    )

    currency = CURRENCY_BY_CC.get(cc, "USD")
    now_dt = get_beijing_time_obj()
    counts = {"ok": 0, "locked": 0, "missing": 0, "skip": 0, "write_fail": 0}
    _WRITE_STARTED.add(str(task_id))

    for appid in appids:
        meta = META.get(appid)

        # 只收 game/dlc；短路前落 type 防止该行永留回补池（与旧链路同语义）
        if meta is not None and meta.get("type") and meta["type"] not in GAME_TYPES:
            await context.db_writer.mark_non_game_type(
                appid, str(meta.get("type_label") or meta["type"])
            )
            counts["skip"] += 1
            continue

        status, opts = StoreBrowseAPI.evaluate(
            items.get(appid), (meta or {}).get("name_en", "")
        )
        if status == "locked" and _visible_without_options(items.get(appid)):
            NO_OPTIONS_COUNT += 1

        # 即将推出且该区无购买选项（未开放预购）→ COMING_SOON 暂缓（同旧链路：
        # 「无价格付费游戏不入库」；有包预购走正常链路），由 coming_soon 重探转正
        if meta and meta.get("coming_soon") and status == "missing":
            if not DRY_RUN:
                await context.db_writer.mark_coming_soon(appid)
            counts["skip"] += 1
            continue

        # ── RU 区 CIS 基准过滤 ──
        # 旧链路走 appdetails 五区探测拿「基准 subid」；browse 直接用 kz/ua 的
        # purchase_options 当基准集：ru 在售 sub 必须落在基准集内，否则视为锁区
        if cc == "ru" and status == "ok" and opts:
            bench = CIS_SUBS.get(appid) or set()
            if bench:
                kept = [o for o in opts if (o.get("sub_id") or 0) in bench]
                status, opts = ("ok", kept) if kept else ("locked", None)

        if status == "ok" and opts:
            # 标准版候选只收「包」，bundle 选项仅在「该区一个包都没有」时才顶上。
            # 这一步必须在新抓取层做，不能指望 db_writer 的 is_bundle 过滤：
            # db_writer 在「该区无标准版候选」时会走降级分支，把该区 prices_data
            # 的**末行**原样当状态行写进 current。旧链路的价格数组只含包，末行
            # 必然是包；browse 的选项数组里混着 bundle，一旦传下去，Arma 3 这类
            # 「Gold/Platinum 版都被 is_gold/版本后缀排除」的条目会把 ¥583.19 的
            # Arma Veteran's Pack 当成现价落库（基线 ¥57.25 / sub 1544395）。
            pkgs_only = [o for o in opts if not o.get("is_bundle")]
            opts = pkgs_only or opts
            prices_arr = [
                {
                    "appid": int(appid),
                    "region_code": cc.upper(),
                    "currency": currency,
                    "price": o["price_cents"],
                    "original_price": o["original_cents"],
                    "discount_percent": o["discount_pct"],
                    "sub_id": o["sub_id"] or 0,
                    "is_gold": o["is_gold"],
                    "version_suffix": o["version_suffix"] or None,
                    "is_bundle": o["is_bundle"],
                    "price_status": "ok",
                    "crawled_at": now_dt,
                }
                for o in opts
            ]
        elif status == "free":
            # 免费：与旧链路同语义（price=0 的 ok 记录进 current，不进 history）
            status = "ok"
            prices_arr = [
                {
                    "appid": int(appid), "region_code": cc.upper(), "currency": currency,
                    "price": 0, "original_price": 0, "discount_percent": 0,
                    "sub_id": 0, "is_gold": False, "version_suffix": None,
                    "is_bundle": False, "price_status": "ok", "crawled_at": now_dt,
                }
            ]
        else:
            prices_arr = [
                {
                    "appid": int(appid), "region_code": cc.upper(), "currency": currency,
                    "price": None, "original_price": None, "discount_percent": 0,
                    "sub_id": 0, "is_gold": False, "version_suffix": None,
                    "is_bundle": False, "price_status": status, "crawled_at": now_dt,
                }
            ]
        counts[status if status in counts else "missing"] += 1

        if DRY_RUN:
            continue

        game_data = build_game_data(appid, meta, now_dt)
        if game_data is None:  # 元数据与库内原值双缺 → 不造空行
            counts["skip"] += 1
            continue
        wrote_ok = await context.db_writer.upsert_game_and_prices(game_data, prices_arr)
        if not wrote_ok:
            counts["write_fail"] += 1
            await context.db_writer.mark_region_status(appid, cc, "missing")
            continue

        if status == "ok" and opts:
            extras = {o["sub_id"]: o for o in opts if o.get("sub_id")}
            await context.db_writer.attach_browse_extras(appid, cc, now_dt, extras)

        # ── 捆绑包发现（purchase_options 白送）：游戏条目的购买选项里每条
        #    捆绑包选项都带 bundleid/packageid + must_purchase_as_set。库里
        #    没有的包写发现桩（无价 + updated_at NULL），随下一次 6h 主轮
        #    链尾的全量刷新整区抓价——跨厂 bundle（旧 appdetails 包列表
        #    探测覆盖不到）由此入账。已入库的包不动（不覆盖完整主档）。
        #    失败只计数不抛：发现是副产物，绝不拖垮主价格链路。
        if not DRY_RUN:
            try:
                found = _discover_bundles_from_item(items.get(appid))
                if found:
                    await context.db_writer.record_bundle_discoveries(found)
                    BUNDLES_DISCOVERED += len(found)
            except Exception as e:  # noqa: BLE001
                logger.debug("[browse] %s 捆绑包发现落库失败: %s", task_id, e)

    logger.debug("[browse] %s 计数 %s", task_id, counts)


def _discover_bundles_from_item(item: dict | None) -> list[dict]:
    """游戏条目 purchase_options → 捆绑包发现桩列表。

    读**原始**选项（parse_options 的 default 组过滤对 bundle 选项无差别放行，
    但其产物丢掉了 must_purchase_as_set / included_game_count，这里要原始键）。

    收录判据（垃圾实测来源：DLC 页的单 DLC sub / 每游戏页都有的本体 sub）：
    - bundleid 键（真捆绑包）一律收；
    - packageid 键（sub 形态）仅当选项名 ≠ 条目名才收——同名即本体 sub
      （「游戏名 == 游戏名」）或单 DLC sub（DLC 页上「DLC 名 == DLC 名」），
      都不是捆绑包；异名的升级包（Orange Box 469 / Gourmet Edition 1066582）
      是真实整包购买单元，收。

    语义与形态同时落，判据随形态分流（与 _browse_row 同一套）：
    - bundleid 键：选项级 must_purchase_as_set 是权威语义；
    - packageid 键：sub 恒不可拆——游戏页 sub 选项的选项级
      must_purchase_as_set 实测恒 False（131 个 sub 普查无一例外），
      不能照抄，由形态蕴含为 1。
    """
    if not item or item.get("success") != 1 or not item.get("visible"):
        return []
    item_name = (item.get("name") or "").strip().lower()
    out: list[dict] = []
    seen: set[int] = set()
    for opt in item.get("purchase_options") or []:
        is_bundle = bool(_to_int(opt.get("bundleid")))
        bid = _to_int(opt.get("bundleid")) or _to_int(opt.get("packageid"))
        if not bid or bid in seen:
            continue
        name = (opt.get("purchase_option_name") or "").strip()
        if not name:
            continue
        if not is_bundle and name.strip().lower() == item_name:
            continue  # 同名 sub = 本体 sub / 单 DLC sub，不是捆绑包
        seen.add(bid)
        out.append(
            {
                "bundle_id": bid,
                "name": name,
                "mps": (1 if opt.get("must_purchase_as_set") else 0)
                if is_bundle else 1,
                "item_kind": 0 if is_bundle else 1,
                "app_ids": [int(item.get("appid") or 0)] if item.get("appid") else [],
            }
        )
    return out


# ══════════════════════════════════════════════════════════════
# 预取：元数据 + CIS 基准
# ══════════════════════════════════════════════════════════════


class _Ctx:
    """预取用的最小 context（http_client + session）。"""

    def __init__(self, http_client, session):
        self.http_client = http_client
        self.session = session


async def prefetch_lang(
    session, http_client, appids: list[int], lang: str, extras: bool, batch_size: int
) -> dict[int, dict]:
    """按 lang 抓一轮元数据，跨 META_FALLBACK_REGIONS 补齐未可见条目。"""
    out: dict[int, dict] = {}
    pending = list(appids)
    for cc in META_FALLBACK_REGIONS:
        if not pending:
            break
        still: list[int] = []
        for batch in StoreBrowseAPI.plan_batches(pending, cc, lang, extras, batch_size):
            ctx = _Ctx(http_client, session)
            try:
                items, _ = await StoreBrowseAPI.fetch_batch(ctx, batch, cc, lang, extras)
            except Exception as e:  # noqa: BLE001 —— 单发失败不拖垮预取，条目回落库内原值
                logger.error("[预取] %s/%s 失败（%d 条）：%s", lang, cc, len(batch), e)
                still.extend(batch)
                continue
            for appid, item in items.items():
                if item and item.get("success") == 1 and item.get("visible"):
                    out[appid] = item
                else:
                    still.append(appid)
        logger.info(
            "[预取] 语言=%s 区=%s 命中 %d，余 %d", lang, cc, len(out), len(still)
        )
        pending = still
    return out


async def prefetch_cis(session, http_client, appids: list[int], extras: bool, batch_size: int):
    """kz/ua 在售 subid 集 → RU 区基准（替掉旧链路的 appdetails 五区探测）。"""
    for cc in CIS_REGIONS:
        for batch in StoreBrowseAPI.plan_batches(appids, cc, "english", extras, batch_size):
            ctx = _Ctx(http_client, session)
            try:
                items, _ = await StoreBrowseAPI.fetch_batch(ctx, batch, cc, "english", extras)
            except Exception as e:  # noqa: BLE001
                logger.error("[预取] CIS %s 失败（%d 条）：%s", cc, len(batch), e)
                continue
            for appid, item in items.items():
                status, opts = StoreBrowseAPI.evaluate(item)
                if status == "ok" and opts:
                    CIS_SUBS.setdefault(appid, set()).update(
                        o["sub_id"] for o in opts if o.get("sub_id")
                    )
    logger.info("[预取] CIS 基准覆盖 %d 个 appid", len(CIS_SUBS))
