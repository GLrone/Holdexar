"""Steam 榜单发现源：topsellers / popularnew / specials / comingsoon 四板聚合。

发现类增量源（补愿望单覆盖不到的游戏），反哺去向分两路：
- 持久监控（board.pool=True，topsellers / popularnew / comingsoon）：
  本轮榜整批并入监控池（wishlist_service.ensure_board_pool），成为
  随全池轮刷新的监控条目；同时照常补爬差集拓展游戏商店；
- 临时队列（specials）：只把「不在库里」的差集补爬入库（发现面用于
  游戏商店展示），不落监控池——折扣全集量级数千条且定位是展示面
  拓展，不做持久监控。

- topsellers  热销榜 5 页 / 500 条（filter=topsellers；前 100 条兼作
  前端 TOP100 展示序，全量供初始游戏库反哺与预设池登记）
- popularnew  热门新品
  （sort_by=Released_DESC&filter=popularnew，5 批 500 条）
- specials    特惠差集（临时队列）
  （sort_by=Global_Topsellers&specials=1，全量翻页求差集）
- comingsoon  即将推出   ← 商店页 filter=popularcomingsoon&os=win
  （JSON 通道同构直连，item 无价格字段，无包游戏由爬取层 COMING_SOON 暂缓）

特惠源约束：
- 拉榜封顶 5000 条 appid（总数，含已在库的——50 批 × 100）；
- 只对「不在库里」（games 无行）的差集反哺爬取，挂名孤儿留给
  kind=backfill 回补层（防双通道重复吃配额）；
- 连续 3 批零新增自动终止（到末尾后不再空翻页刷请求）。

缓存三级策略：热缓存 1h TTL → miss 实时拉取
→ stale 旧值兜底（24h）；分板独立缓存（key 隔离，互不污染）。
出网走策略引擎 proxy_first，独立轻量会话，不接爬虫主链路 429 熔断。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

SEARCH_URL = "https://store.steampowered.com/search/results"
CACHE_TTL_SECONDS = 3600.0          # 热缓存 1 小时
STALE_TTL_SECONDS = 86400.0         # stale 兜底保留 24 小时
BATCH_SIZE = 100
BATCH_INTERVAL = 0.5                 # 批间隔 500ms 避免限流

# 请求头：成人内容放行 + 中文语言（搜索结果需含 logo 字段）
STEAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cookie": "birthtime=315532800; mature_content=1",
}

_LOGO_APPID_RE = re.compile(r"/apps/(\d+)/")


@dataclass(frozen=True)
class Board:
    """榜单源配置（各榜单的查询参数）。"""

    key: str
    # search/results 附加查询参数（start/count/json 之外）
    params: dict = field(default_factory=dict)
    max_records: int = 100            # 拉榜封顶（总条数，含已在库的）
    max_requests: int = 5             # 最多批数（max_records / BATCH_SIZE 向上取整）
    empty_tolerance: int = 1          # 连续 N 批零新增 → 终止
    # 反哺语义：True=只补 games 无行的（特惠拍板）；False=无行 or 挂名孤儿
    # （updated_at NULL 且无价格行，topsellers 现行语义）
    uncrawled_only: bool = False
    backfill_kind: str = ""           # 爬取 spec kind 标签
    # 落池语义：True=本榜游戏轮询并入持久监控池（ensure_board_pool）；
    # False=临时队列——只补游戏商店差集、不落监控池（specials 专用）
    pool: bool = False

    @property
    def cache_key(self) -> str:
        return self.key


BOARDS: dict[str, Board] = {
        # 热销榜：发现面拉满 5 页 = 500 条（初始游戏库的来源之一，预设池登记
    # 见 games/preset.py）；前端「近期TOP100热榜」展示仍取榜序前 100；
    # 本榜游戏轮询落持久监控池（board.pool）
    "topsellers": Board(
        key="topsellers",
        params={"filter": "topsellers", "hidef2p": 1, "category1": 998},
        max_records=500,
        max_requests=5,
        empty_tolerance=1,
        backfill_kind="top100_backfill",
        pool=True,
    ),
    # 热门新品（对齐 popular_new_supplement.py：filter=popularnew + Released_DESC）
    "popularnew": Board(
        key="popularnew",
        params={"filter": "popularnew", "sort_by": "Released_DESC",
                "hidef2p": 1, "category1": 998},
        max_records=500,
        max_requests=5,
        empty_tolerance=1,
        backfill_kind="popularnew_backfill",
        pool=True,
    ),
    # 特惠差集（对齐 check_steam_specials.py：specials=1 + Global_Topsellers + l=english）。
    # 临时队列（pool 默认 False）：折扣全集只补游戏商店差集，不落监控池
    "specials": Board(
        key="specials",
        params={"specials": 1, "sort_by": "Global_Topsellers",
                "hidef2p": 1, "category1": 998, "l": "english"},
        max_records=5000,             # 拍板：封顶 5000（含在库）
        max_requests=50,
        empty_tolerance=3,            # 拍板：连续 3 批零新增终止
        uncrawled_only=True,          # 拍板：只补不在库的差集
        backfill_kind="specials_backfill",
    ),
    # 即将推出（页面 filter=popularcomingsoon&os=win；与三源同走
    # search/results?json=1 通道，items 结构一致，无需 HTML 解析）。
    # 该榜单 item 无价格字段，是否开放预购由爬取层判定（app_handler COMING_SOON
    # 暂缓语义）：无包游戏落 COMING_SOON 挂名行脱池，开放预购/上线后由
    # coming_soon 重探通道转正。
    "comingsoon": Board(
        key="comingsoon",
        params={"filter": "popularcomingsoon", "os": "win"},
        max_records=500,
        max_requests=5,
        empty_tolerance=1,
        uncrawled_only=True,          # 在库（含 COMING_SOON 挂名行）不重复反哺
        backfill_kind="comingsoon_backfill",  # 转正由重探通道负责
        pool=True,                    # 即将推出同样落持久监控（上线后自动转正续刷）
    ),
}


# ── 进程内缓存：热值 + stale 兜底，分板独立（对齐原型 Redis 双 key × 3 板）──
_board_state: dict[str, dict] = {
    key: {"cache": None, "cache_ts": 0.0, "stale": None, "stale_ts": 0.0}
    for key in BOARDS
}
_board_locks: dict[str, asyncio.Lock] = {key: asyncio.Lock() for key in BOARDS}


async def _strategy_proxy() -> str | None:
    try:
        from app.domains.proxies import service as proxies_service

        return await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        return None


def _board_of(key: str) -> Board:
    board = BOARDS.get(key)
    if board is None:
        raise ValueError(f"未知榜单源: {key}（可选：{'/'.join(BOARDS)}）")
    return board


async def fetch_board(key: str) -> list[int]:
    """分批拉榜单，返回按排名顺序的 appid（≤max_records，失败返回已收集部分）。

    终止条件（满足其一）：凑满 max_records / 批数耗尽 / 连续
    empty_tolerance 批零新增 / 单批网络或 HTTP 错误（用已收集的，不炸整链）。
    """
    board = _board_of(key)
    collected: list[int] = []
    seen: set[int] = set()
    consecutive_empty = 0
    proxy_url = await _strategy_proxy()

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for request_no in range(1, board.max_requests + 1):
            if len(collected) >= board.max_records:
                break
            if consecutive_empty >= board.empty_tolerance:
                logger.info("[%s] 连续 %d 批零新增，终止拉榜（已收集 %d 个）",
                            board.key, consecutive_empty, len(collected))
                break
            params = {
                "start": (request_no - 1) * BATCH_SIZE,
                "count": BATCH_SIZE,
                "json": 1,
                **board.params,
            }
            try:
                async with session.get(
                    SEARCH_URL,
                    params=params,
                    headers=STEAM_HEADERS,
                    proxy=proxy_url,
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[%s] 第 %d 批请求失败：HTTP %d",
                                       board.key, request_no, resp.status)
                        break
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("[%s] 第 %d 批网络错误，停止收集（已收集 %d 个）",
                               board.key, request_no, len(collected))
                break

            items = data.get("items") or [] if isinstance(data, dict) else []
            added = 0
            for item in items:
                if len(collected) >= board.max_records:
                    break
                match = _LOGO_APPID_RE.search(item.get("logo") or "")
                if match:
                    appid = int(match.group(1))
                    if appid not in seen:
                        seen.add(appid)
                        collected.append(appid)
                        added += 1
            consecutive_empty = consecutive_empty + 1 if added == 0 else 0
            if added:
                logger.info("[%s] 第 %d 批 +%d（累计 %d）",
                            board.key, request_no, added, len(collected))
            # 批间隔 500ms 避免限流（凑满即不再请求）
            if len(collected) < board.max_records:
                await asyncio.sleep(BATCH_INTERVAL)

    return collected[:board.max_records]


def _write_cache(key: str, appids: list[int]) -> None:
    now = time.monotonic()
    st = _board_state[key]
    st["cache"], st["cache_ts"] = appids, now
    st["stale"], st["stale_ts"] = appids, now


async def refresh_board(key: str) -> list[int]:
    """强制拉取并写缓存（定时预热入口，对齐 sync_steam_top100.ts）。

    成功 → 写热缓存 + stale 双份；失败/空结果 → 不动缓存，返回当前兜底值。
    """
    appids = await fetch_board(key)
    if appids:
        _write_cache(key, appids)
        logger.info("[%s] 同步完成：%d 个 appid", key, len(appids))
        return appids

    logger.warning("[%s] 同步未获取到数据，保留旧缓存兜底", key)
    cached = _board_state[key]["cache"]
    return cached if cached is not None else []


async def get_board(key: str) -> list[int]:
    """读榜单 appid（API 层入口，对齐 getOrFetchTop100 三级策略）。

    1. 热缓存有效（1h）→ 直接返回
    2. miss → 实时拉取写回（并发 miss 由分板锁合并为单次）
    3. 拉取失败 → stale 旧值兜底（24h 内）；完全失败返回 []
    """
    _board_of(key)
    st = _board_state[key]
    if st["cache"] is not None and time.monotonic() - st["cache_ts"] < CACHE_TTL_SECONDS:
        return st["cache"]

    async with _board_locks[key]:
        # 双检：等锁期间其他协程可能已刷新
        st = _board_state[key]
        if st["cache"] is not None and time.monotonic() - st["cache_ts"] < CACHE_TTL_SECONDS:
            return st["cache"]

        appids = await fetch_board(key)
        if appids:
            _write_cache(key, appids)
            return appids

        if st["stale"] is not None and time.monotonic() - st["stale_ts"] < STALE_TTL_SECONDS:
            logger.warning("[%s] 拉取失败，使用 stale 缓存兜底（%d 个）",
                           key, len(st["stale"]))
            return st["stale"]
        return []


def _reset_cache_for_tests() -> None:
    """测试专用：清空全部榜单缓存状态。"""
    for st in _board_state.values():
        st["cache"], st["cache_ts"] = None, 0.0
        st["stale"], st["stale_ts"] = None, 0.0


async def backfill_specs(key: str, limit: int = 100) -> list[dict]:
    """榜单反哺队列：该榜单 appid 中需要首爬的 → 爬取 spec 列表。

    缺口判定（board.uncrawled_only 决定语义）：
    - False（topsellers/popularnew，现行语义）：games 无行（从未入库）
      **或** 挂名孤儿（updated_at NULL 且无任何价格行）。已有价格行
      （locked/missing 状态）的孤儿进 missing 账本通道，排除防双通道重复。
    - True（specials，拍板约束）：**仅补 games 无行的差集**——挂名孤儿
      一律留给 kind=backfill 回补层，特惠通道不碰在库行。
    - removed 复活语义（所有板统一）：removed_at 非空的在库游戏重新
      上榜 = 免费复活信号，一律反哺重爬（upsert 成功路径自动清标）。

    ⚠️ 实现坑（勿改回 `~has_price` 查询排除 + `known.get(appid, False)`）：
    查询排除会把"有价格行的已爬项"从结果里删掉，而 get 默认 False
    把它们重新当成"从未入库"回炉 pending。必须整表取回（含 has_price
    标量列）在 Python 侧分类。

    返回 [{"scope": "appids", "appids": [...], "kind": board.backfill_kind}]；
    无缺口返回 []。limit 封顶单轮反哺量（首跑特惠差集可达千级，按榜单
    热度序限量消化，每轮 100 → 400/天）。
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.domains.games.models import Game, GameCurrentPrice

    board = _board_of(key)
    appids = await get_board(key)
    if not appids:
        return []

    pending: list[int] = []
    try:
        async with get_session_factory()() as session:
            has_price = (
                select(GameCurrentPrice.appid)
                .where(GameCurrentPrice.appid == Game.appid)
                .exists()
            )
            rows = (
                await session.execute(
                    select(Game.appid, Game.updated_at, has_price, Game.removed_at).where(
                        Game.appid.in_(appids)
                    )
                )
            ).all()
            # 库内行分类：appid → 是否需要反哺
            if board.uncrawled_only:
                # 特惠/comingsoon 语义：有 games 行的一律不碰（含挂名孤儿——
                # 回补层/重探通道负责），唯一例外 removed 复活（重新上榜）
                needs = {
                    int(a): (r is not None) for a, _, _, r in rows
                }
            else:
                # 现行语义：无行(True) 或 挂名孤儿(updated_at NULL 且无价格行)
                needs = {
                    int(a): ((u is None) and (not p)) or (r is not None)
                    for a, u, p, r in rows
                }
            for appid in appids:
                if needs.get(appid, True):  # 库内无行 → True
                    pending.append(appid)
    except Exception:  # noqa: BLE001 —— 库查询失败不阻塞榜单本身
        logger.exception("[%s] 反哺查询失败", board.key)
        return []

    if not pending:
        return []
    pending = pending[:limit]
    logger.info("[%s] 反哺队列：%d/%d 个榜单游戏待首爬（limit=%d）",
                board.key, len(pending), len(appids), limit)
    return [{"scope": "appids", "appids": pending, "kind": board.backfill_kind}]
