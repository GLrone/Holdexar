"""games 域服务：列表 / 详情 / 历史价格。

三层兜底架构（SQLite 版）：
- 第一层（MV）→ games 表预计算列 min_cny_fen / diff_fen（refresh_sort_cache 维护，
  启动全库 + 爬取落库增量刷新，恒新鲜 → 无需第二层降级路径）；
- 第二层（动态 CTE）→ 由预计算列吸收（COALESCE(g.min_cny_fen, cn.price) 表达 lowest_prices CTE）；
- SQL 级分页（ORDER BY + LIMIT/OFFSET）+ COUNT(*) 分离 + limit+1 探测 hasMore；
- 价格明细只按页内 appid 批量拉取；
- 排序规则统一在 sorting.py；
- 汇率进程内 TTL 缓存。
响应结构（priceMatrix 为区服键控对象）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, asc, desc, func, or_, select, update
from sqlalchemy.orm import aliased

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from app.domains.games.models import Game, Bundle, BundleRegionPrice, GameCurrentPrice, GamePriceHistory
from app.domains.games.sorting import build_order_by
from app.domains.games.pricing import (
    DEFAULT_EXCHANGE_RATES,
    REGION_TO_CURRENCY,
    build_steam_header_url,
    build_steam_store_url,
    convert_minor_to_cny_fen,
    format_minor_units,
)
from app.domains.rates.models import FxRate

from app.crawler.config import CC_LIST

TOLERANCE_FEN = 500  # 5 元容差（分）

# 游戏商店默认隐藏 DLC；白名单豁免个别确需常驻的 DLC（黄金树幽影 / 艾尔登法环）
DLC_EXEMPT_APPIDS = frozenset({2778580})

# ── 汇率进程内缓存（对齐原型 Redis 300s TTL）──
_RATES_TTL_SECONDS = 300.0
_rates_cache: tuple[float, dict[str, float]] | None = None


async def get_rates() -> dict[str, float]:
    global _rates_cache
    if _rates_cache is not None and time.monotonic() - _rates_cache[0] < _RATES_TTL_SECONDS:
        return _rates_cache[1]
    async with get_session_factory()() as session:
        rows = (await session.execute(select(FxRate))).scalars().all()
    rates = dict(DEFAULT_EXCHANGE_RATES)
    for r in rows:
        try:
            rates[r.currency_code.upper()] = float(r.rate_to_cny)
        except (TypeError, ValueError):
            continue
    rates["CNY"] = 1.0
    _rates_cache = (time.monotonic(), rates)
    return rates


def invalidate_rates_cache() -> None:
    """汇率刷新后调用，立即使缓存失效。"""
    global _rates_cache
    _rates_cache = None


def _decode_cursor(after: str | None) -> int:
    """after = base64({"offset": N})；兼容旧系统游标格式，解析失败从 0 开始。"""
    if not after:
        return 0
    import base64

    try:
        data = json.loads(base64.urlsafe_b64decode(after.encode()).decode())
        return max(int(data.get("offset", 0)), 0)
    except Exception:
        return 0


def _encode_cursor(offset: int) -> str:
    import base64

    return base64.urlsafe_b64encode(
        json.dumps({"offset": offset}).encode()
    ).decode()


async def _primary_steamid() -> str:
    """主账户 steamid（账号表优先，回退设置页手填）；读取失败返回空。"""
    try:
        from app.domains.account import service as account_service

        primary = await account_service.get_primary_steam_id()
        if primary:
            return primary
    except Exception:  # noqa: BLE001 —— 账号表读不到按未配置处理
        pass
    from app.domains.settings.service import get_value

    return (await get_value("account.steam_id", "")) or ""


async def _followed_appids() -> list[int]:
    """关注集（游戏卡星标 = 追踪池 manual 条目）；读不到按空集处理。"""
    try:
        from app.domains.wishlist.follows import followed_appids

        return await followed_appids()
    except Exception:  # noqa: BLE001 —— 关注层故障不阻断列表
        return []


def _build_filter_conditions(
    *,
    g, cn, sr,
    region_code: str,
    filter_mode: str,
    is_locked: bool,
    is_lowest: bool,
    q: str | None,
    only_discounted: bool,
    min_rating: int,
    max_rating: int | None,
    min_reviews: int,
    max_reviews: int | None,
    min_price: int | None,
    max_price: int | None,
    only_hb: bool,
    only_epic: bool,
    only_xgp: bool,
    sort: str,
    flag: str = "",
    top100_appids: list[int] | None = None,
    hide_owned: bool = False,
    hide_family_sharing: bool = False,
    primary_steamid: str = "",
    diff_min_fen: int | None = None,
    diff_max_fen: int | None = None,
    diff_type: str = "absolute",
    tolerance_fen: int | None = None,
    strict_lowest: bool = False,
    exclude_dlc: bool = False,
) -> list:
    """WHERE 条件构建（list_games 与 top100 分支共用同一套筛选语义）。

    top100_appids 非空时注入 `appid IN (...)`（top100 的 WHERE 叠加——
    其他筛选/地区模式照常生效）。
    flag：hl=新史低+平史低（hl_flag 1/2）、pp=永降（pp_flag 1）、
    any=两者并集——读全库预计算标记，与提醒规则无关（降价动态 feed）。
    hide_owned：排除「已拥有」徽章同款集合——主账户 owned 行（未配置
    主账户时任一追踪账户 owned 行），与游戏卡归属徽章口径一致。
    hide_family_sharing：排除「家庭共享」徽章同款集合——非主账户的追踪
    账户 owned 行；未配置主账户时不生效（无 family 归属可排除）。
    diff_min_fen/diff_max_fen：与国区差价区间（分）。已选地区时差值 =
    cn.price - sr.cny_fen；未选时回退预计算列 g.diff_fen（= CN 价 - 全区
    最低，下限 0）。diff_type=percent 时区间值按百分比解释（0-100）。
    tolerance_fen：三模式"近似全区最低"容差（分）；None 用默认 5 元。
    strict_lowest：绝对低价——最低区价 < 国区价 − tolerance（差价容错的
    反向语义：最低区必须实质低于国区，而非"近似相等也放行"）。
    """
    tolerance = TOLERANCE_FEN if tolerance_fen is None else tolerance_fen
    conditions: list = [g.name.is_not(None), g.name != ""]

    if is_locked:
        # 锁国区：无 ok 状态的国区行（LEFT JOIN 后 appid 为空即不存在，对齐原型 cnJoinType=LEFT）
        conditions.append(cn.appid.is_(None))
    else:
        conditions += [cn.price.is_not(None), cn.price > 0]
        # 近似全区最低（±5 元容差）= COALESCE(非CN最低, 国区价) >= 国区价 - 容差
        # （等价旧版 NOT EXISTS 相关子查询；由预计算列表达 lowest_prices CTE）
        if region_code == "CN":
            conditions.append(func.coalesce(g.min_cny_fen, cn.price) >= cn.price - tolerance)
        if is_lowest:
            conditions += [
                func.coalesce(g.min_cny_fen, cn.price) >= cn.price - tolerance,
                cn.cny_fen.is_not(None),
            ]

    # 绝对低价：最低区价实质低于国区（diff_fen = MAX(CN - 最低, 0)，差价 > tolerance；
    # tolerance 缺省 0 = 严格任何分差都算——与"近似最低"容差的默认 500 语义不同）
    if strict_lowest:
        conditions.append(g.diff_fen > (tolerance_fen if tolerance_fen is not None else 0))
    if sr is not None:
        conditions.append(cn.cny_fen.is_not(None))
        if filter_mode == "cheaper":
            conditions.append(sr.cny_fen < cn.price - 100)
        elif filter_mode == "highdiff":
            conditions += [
                or_(cn.discount_percent == 0, cn.discount_percent.is_(None)),
                cn.price - sr.cny_fen >= 5000,
                func.coalesce(g.min_cny_fen, cn.price) >= cn.price - tolerance,
            ]
        else:  # global
            conditions += [
                sr.cny_fen < cn.price - 100,
                func.coalesce(g.min_cny_fen, cn.price) >= cn.price - tolerance,
            ]

    if q:
        like = f"%{q}%"
        conditions.append(or_(g.name.like(like), g.name_en.like(like)))
    if only_discounted:
        conditions.append(cn.discount_percent > 0)
    if min_rating > 0:
        conditions.append(g.positive_rate >= min_rating * 100)
    if max_rating is not None and max_rating < 100:
        conditions.append(g.positive_rate <= max_rating * 100)
    if min_reviews > 0:
        conditions.append(g.review_count >= min_reviews)
    if max_reviews is not None and max_reviews > 0:
        conditions.append(g.review_count <= max_reviews)
    if min_price is not None:
        conditions.append(cn.price >= min_price)
    if max_price is not None:
        conditions.append(cn.price <= max_price)

    # 与国区差价区间（absolute=分；percent=0-100 百分比，差值以国区价为基数）
    if diff_min_fen is not None or diff_max_fen is not None:
        if sr is not None:
            diff_expr = cn.price - sr.cny_fen
        else:
            diff_expr = g.diff_fen
        if diff_type == "percent":
            # 交叉相乘避免 SQLite 整数除截断：diff/min/max 均为分，百分值为 0-100
            if diff_min_fen is not None:
                conditions.append(diff_expr * 100 >= diff_min_fen * cn.price)
            if diff_max_fen is not None:
                conditions.append(diff_expr * 100 <= diff_max_fen * cn.price)
        else:
            if diff_min_fen is not None:
                conditions.append(diff_expr >= diff_min_fen)
            if diff_max_fen is not None:
                conditions.append(diff_expr <= diff_max_fen)
    if sort == "new2026":
        conditions.append(g.release_date.like("2026%"))
    # [模块三] 元数据筛选（onlyHb / onlyEpic / onlyXgp）
    if only_hb:
        conditions.append(g.is_hb.is_(True))
    if only_epic:
        conditions.append(g.is_epic.is_(True))
    if only_xgp:
        conditions.append(g.xgp_tier.is_not(None))
    # [flag] 史低/永降标记过滤（降价动态 feed；refresh_hl_flags/refresh_pp_flags 维护）
    if flag == "hl":
        conditions.append(g.hl_flag.in_((1, 2)))
    elif flag == "pp":
        conditions.append(g.pp_flag == 1)
    elif flag == "any":
        conditions.append(or_(g.hl_flag.in_((1, 2)), g.pp_flag == 1))
    if top100_appids is not None:
        # [Top100] 榜内过滤（appid 集合注入）
        conditions.append(g.appid.in_(top100_appids))
    if hide_owned:
        from app.domains.wishlist.models import WishlistItem

        owned_sq = select(WishlistItem.appid).where(
            WishlistItem.owned.is_(True), WishlistItem.active.is_(True)
        )
        if primary_steamid:
            owned_sq = owned_sq.where(WishlistItem.steamid == primary_steamid)
        conditions.append(g.appid.not_in(owned_sq))

    # 屏蔽家庭共享：排除「非主账户的追踪账户已拥有」的 appid——与游戏卡紫色
    # 「家庭共享」归属徽章同口径（ownership() 的 family 分支：owned 行且账户
    # 非主账户）。与 hide_owned 互为补集：两者同开 = 除愿望单外所有已获取渠道
    # 都隐藏。未配置主账户时全部 owned 行归属「已拥有」，没有 family 归属可
    # 排除（同 ownership() 的判定），条件不生效。
    if hide_family_sharing and primary_steamid:
        from app.domains.wishlist.models import WishlistItem

        family_sq = select(WishlistItem.appid).where(
            WishlistItem.owned.is_(True),
            WishlistItem.active.is_(True),
            WishlistItem.steamid != primary_steamid,
        )
        conditions.append(g.appid.not_in(family_sq))

    # 游戏商店默认隐藏 DLC（type='DLC'）；type 为 NULL 的游戏（未归类）按非 DLC 处理，
    # 白名单豁免个别确需常驻的 DLC（黄金树幽影）。exclude_dlc 缺省 False，仅商店页显式开启。
    if exclude_dlc:
        conditions.append(
            or_(
                g.type.is_(None),
                g.type != "DLC",
                g.appid.in_(DLC_EXEMPT_APPIDS),
            )
        )

    return conditions


def _build_base_stmt(
    *,
    g, cn, sr,
    conditions: list,
    region_code: str,
    is_locked: bool,
):
    """基础查询组装（join 形态对齐原型：LOCKED 走 LEFT JOIN，其余 INNER）。"""
    cn_join = and_(
        cn.appid == g.appid,
        cn.region_code == "CN",
        cn.price_status == "ok",
    )
    stmt = select(g, cn).join(cn, cn_join, isouter=is_locked)
    if sr is not None:
        stmt = stmt.join(
            sr,
            and_(
                sr.appid == g.appid,
                sr.region_code == region_code,
                sr.price_status == "ok",
                sr.cny_fen.is_not(None),
            ),
        )
    return stmt.where(and_(*conditions))


async def list_games(
    *,
    sort: str = "default",
    limit: int = 40,
    after: str | None = None,
    q: str | None = None,
    region: str = "",
    filter_mode: str = "global",
    only_discounted: bool = False,
    is_lowest: bool = False,
    flag: str = "",
    min_rating: int = 0,
    max_rating: int | None = None,
    min_reviews: int = 0,
    max_reviews: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    only_hb: bool = False,
    only_epic: bool = False,
    only_xgp: bool = False,
    hide_owned: bool = False,
    hide_family_sharing: bool = False,
    diff_min_fen: int | None = None,
    diff_max_fen: int | None = None,
    diff_type: str = "absolute",
    tolerance_fen: int | None = None,
    strict_lowest: bool = False,
    exclude_dlc: bool = False,
) -> dict:
    """游戏列表。返回 {items, total, hasMore, nextCursor}。

    翻译自 route.ts 的 MV/CTE 查询（预计算列版本）：
    - filterMode（仅在选择了具体非国区时生效）：
      global: 该区价 < 国区-1元 且 近似全区最低（默认±5元，tolerance_fen 可调）
      cheaper: 该区价 < 国区-1元
      highdiff: 国区未打折 且 国区-该区 >= 50元 且 近似全区最低
    - 全部筛选/排序/分页在 SQL 完成；价格明细仅按页内 appid 拉取；
    - sort=top100 例外：热榜集 ≤100 条，SQL 全拉后 Python 按榜序
      重排 + 切片分页（SQLite 无 array_position 的等价实现，其余
      筛选条件照常叠加，与 top100 WHERE 注入语义一致）。
    """
    if sort == "top100":
        return await _list_games_top100(
            limit=limit,
            after=after,
            q=q,
            region=region,
            filter_mode=filter_mode,
            only_discounted=only_discounted,
            is_lowest=is_lowest,
            min_rating=min_rating,
            max_rating=max_rating,
            min_reviews=min_reviews,
            max_reviews=max_reviews,
            min_price=min_price,
            max_price=max_price,
        only_hb=only_hb,
        only_epic=only_epic,
        only_xgp=only_xgp,
        flag=flag,
        diff_min_fen=diff_min_fen,
        diff_max_fen=diff_max_fen,
        diff_type=diff_type,
        tolerance_fen=tolerance_fen,
        strict_lowest=strict_lowest,
        exclude_dlc=exclude_dlc,
    )

    rates = await get_rates()
    offset = _decode_cursor(after)
    region_code = (region or "").strip().upper()
    is_locked = region_code == "LOCKED"
    has_region = bool(region_code) and region_code not in ("CN", "LOCKED")

    g = Game
    cn = aliased(GameCurrentPrice)
    sr = aliased(GameCurrentPrice) if has_region else None

    conditions = _build_filter_conditions(
        g=g, cn=cn, sr=sr, region_code=region_code, filter_mode=filter_mode,
        is_locked=is_locked, is_lowest=is_lowest, q=q,
        only_discounted=only_discounted, min_rating=min_rating, max_rating=max_rating,
        min_reviews=min_reviews, max_reviews=max_reviews,
        min_price=min_price, max_price=max_price,
        only_hb=only_hb, only_epic=only_epic, only_xgp=only_xgp,
        flag=flag,
        sort=sort,
        hide_owned=hide_owned,
        hide_family_sharing=hide_family_sharing,
        primary_steamid=(
            await _primary_steamid() if (hide_owned or hide_family_sharing) else ""
        ),
        diff_min_fen=diff_min_fen,
        diff_max_fen=diff_max_fen,
        diff_type=diff_type,
        tolerance_fen=tolerance_fen,
        strict_lowest=strict_lowest,
        exclude_dlc=exclude_dlc,
    )

    # ── 基础查询（join 形态对齐原型：LOCKED 走 LEFT JOIN，其余 INNER）──
    base = _build_base_stmt(g=g, cn=cn, sr=sr, conditions=conditions,
                           region_code=region_code, is_locked=is_locked)

    # ── 排序（统一在 sorting.py）──
    # 关注置顶前缀（通用排序的第一优先级「收藏游戏置顶」）：关注集是
    # 用户手工策展的小集合，IN 布尔降序即置顶；地区模式里它压过 3-group
    # （priorityGroup「关注极致优先」的同位语义——愿望单优先开关本端
    # 不存在，关注恒优先）。空集不注入，SQL 保持原样。
    followed = await _followed_appids()
    fav_order = [desc(g.appid.in_(followed))] if followed else []
    if is_locked:
        order = fav_order + [asc(func.coalesce(g.min_cny_fen, 999999)), desc(g.appid)]
    else:
        order = fav_order + build_order_by(g, cn, sort=sort, region_mode=has_region, sr=sr)

    async with get_session_factory()() as session:
        # total 分离（对齐原型 COUNT(*) 独立查询）
        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar() or 0

        # limit+1 探测 hasMore（对齐原型）
        rows = (
            await session.execute(base.order_by(*order).limit(limit + 1).offset(offset))
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]

        # ── 页级取价：只拉当前页的全区价格（对齐 getLatestPricesForGames）──
        appid_list = [row[0].appid for row in rows]
        price_rows = await _load_page_prices(session, appid_list)

    items = [
        _build_list_item(game, cn_row, price_rows.get(game.appid, []), rates)
        for game, cn_row in rows
    ]

    return {
        "items": items,
        "total": total,
        "hasMore": has_more,
        "nextCursor": _encode_cursor(offset + limit) if has_more else None,
    }


async def _load_page_prices(session, appid_list: list[int]) -> dict[int, list[GameCurrentPrice]]:
    """页级取价：只拉给定 appid 的全区价格行（含非 ok 状态行）。

    ok/价格行由 _build_list_item 分拣进 priceMatrix；missing/blocked 行
    供 unavailableRegions 信号（前端黄框区分「未抓取成功」与真锁区）。
    """
    price_rows: dict[int, list[GameCurrentPrice]] = {}
    if appid_list:
        all_prices = (
            await session.execute(
                select(GameCurrentPrice).where(GameCurrentPrice.appid.in_(appid_list))
            )
        ).scalars()
        for p in all_prices:
            price_rows.setdefault(p.appid, []).append(p)
    return price_rows


async def _list_games_top100(
    *,
    limit: int = 40,
    after: str | None = None,
    q: str | None = None,
    region: str = "",
    filter_mode: str = "global",
    only_discounted: bool = False,
    is_lowest: bool = False,
    flag: str = "",
    min_rating: int = 0,
    max_rating: int | None = None,
    min_reviews: int = 0,
    max_reviews: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    only_hb: bool = False,
    only_epic: bool = False,
    only_xgp: bool = False,
    diff_min_fen: int | None = None,
    diff_max_fen: int | None = None,
    diff_type: str = "absolute",
    tolerance_fen: int | None = None,
    strict_lowest: bool = False,
    exclude_dlc: bool = False,
) -> dict:
    """TOP100 热销榜分支（sort=top100 的榜内过滤 + 榜序重排）。

    设计：`appid IN (...)` 过滤 + 榜序排序 + SQL 分页。本端 SQLite 无
    array_position → 等价实现：榜集全量拉回后 Python 按榜序重排 + 切片
    分页；筛选条件（地区/价格/元数据）照常叠加。拉取失败（空榜）返回
    空集而非随机游戏。

    展示取榜序前 100：榜单抓取侧已扩到 5 页（初始游戏库的发现面），
    但本排序项语义仍是「近期TOP100热榜」——多出的名次只服务反哺，
    不进排序集。
    """
    from app.domains.games import boards as boards_mod

    appids = (await boards_mod.get_board("topsellers"))[:100]
    if not appids:
        return {"items": [], "total": 0, "hasMore": False, "nextCursor": None}

    rates = await get_rates()
    offset = _decode_cursor(after)
    region_code = (region or "").strip().upper()
    is_locked = region_code == "LOCKED"
    has_region = bool(region_code) and region_code not in ("CN", "LOCKED")

    g = Game
    cn = aliased(GameCurrentPrice)
    sr = aliased(GameCurrentPrice) if has_region else None

    conditions = _build_filter_conditions(
        g=g, cn=cn, sr=sr, region_code=region_code, filter_mode=filter_mode,
        is_locked=is_locked, is_lowest=is_lowest, q=q,
        only_discounted=only_discounted, min_rating=min_rating, max_rating=max_rating,
        min_reviews=min_reviews, max_reviews=max_reviews,
        min_price=min_price, max_price=max_price,
        only_hb=only_hb, only_epic=only_epic, only_xgp=only_xgp,
        flag=flag,
        sort="top100", top100_appids=appids,
        diff_min_fen=diff_min_fen,
        diff_max_fen=diff_max_fen,
        diff_type=diff_type,
        tolerance_fen=tolerance_fen,
        strict_lowest=strict_lowest,
        exclude_dlc=exclude_dlc,
    )
    base = _build_base_stmt(g=g, cn=cn, sr=sr, conditions=conditions,
                            region_code=region_code, is_locked=is_locked)

    async with get_session_factory()() as session:
        rows = (await session.execute(base)).all()

    # ── 榜序重排（等价 array_position；榜外 appid 不会出现——IN 过滤保证）──
    # 排序优先级对齐 buildCteOrderBy，关注置顶压过榜序（top100 分支的
    # 收藏第一优先级）：锁国区最低价 ASC（早退分支，压过榜序）
    # > 地区模式 3-group 前缀 + 组内榜序 > 纯榜序。
    followed = set(await _followed_appids())
    rank = {appid: i for i, appid in enumerate(appids)}
    fav_key = lambda appid: appid not in followed  # noqa: E731 —— False(0)=关注在前
    if is_locked:
        rows.sort(key=lambda r: (fav_key(r[0].appid), int(r[0].min_cny_fen) if r[0].min_cny_fen is not None else 999999, -r[0].appid))
    elif has_region:
        def _group(row) -> int:
            game, cn_row = row
            if game.hl_flag in (1, 2):
                return 0
            if cn_row is not None and (cn_row.discount_percent or 0) > 0:
                return 1
            return 2

        rows.sort(key=lambda r: (fav_key(r[0].appid), _group(r), rank[r[0].appid]))
    else:
        rows.sort(key=lambda r: (fav_key(r[0].appid), rank[r[0].appid]))

    total = len(rows)
    has_more = offset + limit < total
    page = rows[offset : offset + limit]

    appid_list = [row[0].appid for row in page]
    async with get_session_factory()() as session:
        price_rows = await _load_page_prices(session, appid_list)

    items = [
        _build_list_item(game, cn_row, price_rows.get(game.appid, []), rates)
        for game, cn_row in page
    ]

    return {
        "items": items,
        "total": total,
        "hasMore": has_more,
        "nextCursor": _encode_cursor(offset + limit) if has_more else None,
    }


async def get_game_detail(appid: int) -> dict | None:
    """游戏详情：元数据 + 全区当前价 + 版本列表（捆绑包域未建，返回空列表）。"""
    rates = await get_rates()

    async with get_session_factory()() as session:
        game = await session.get(Game, appid)
        if game is None:
            return None

        price_rows = (
            await session.execute(
                select(GameCurrentPrice).where(GameCurrentPrice.appid == appid)
            )
        ).scalars().all()

        version_rows = (
            await session.execute(
                select(
                    GamePriceHistory.version_suffix,
                    GamePriceHistory.is_gold,
                    GamePriceHistory.sub_id,
                )
                .where(GamePriceHistory.appid == appid)
                .distinct()
            )
        ).all()

    price_map: dict[str, dict] = {}
    unavailable_regions: list[str] = []
    for p in price_rows:
        code = p.region_code.upper()
        if p.price_status in ("missing", "blocked"):
            unavailable_regions.append(code)
            continue
        if p.price is None or int(p.price) <= 0:
            continue
        price_cents = int(p.price)
        cny_fen = int(p.cny_fen) if p.cny_fen is not None else None
        if cny_fen is None:
            currency = p.currency or REGION_TO_CURRENCY.get(code)
            if currency:
                cny_fen = convert_minor_to_cny_fen(price_cents, currency, rates)
        price_map[code] = {
            "cents": price_cents,
            "cnyFen": cny_fen,
            "originalCents": int(p.original_price) if p.original_price is not None else None,
            "discount": p.discount_percent or 0,
            "currency": p.currency or REGION_TO_CURRENCY.get(code, "USD"),
        }

    price_matrix: dict[str, list] = {}
    for code, _, currency in CC_LIST:
        data = price_map.get(code.upper())
        if not data:
            continue
        # 第 4 位 = 该区折扣 pct（详情页价格表格列；0 = 无折扣）
        price_matrix[code.upper()] = [
            format_minor_units(data["cents"], currency),
            data["cnyFen"] or 0,
            data["cents"],
            data["discount"] or 0,
        ]

    cn_data = price_map.get("CN")
    cn_price_cents = cn_data["cents"] if cn_data else None
    cn_cny_fen = cn_data["cnyFen"] if cn_data else None
    cn_discount = cn_data["discount"] if cn_data else 0

    lowest_cny_fen = None
    lowest_region_code = ""
    for code, data in price_map.items():
        if code == "CN" or data["cnyFen"] is None:
            continue
        if lowest_cny_fen is None or data["cnyFen"] < lowest_cny_fen:
            lowest_cny_fen = data["cnyFen"]
            lowest_region_code = code.lower()

    versions = [
        {"suffix": v.version_suffix, "isGold": v.is_gold, "subId": v.sub_id}
        for v in version_rows
        if v.version_suffix and v.version_suffix.strip()
    ]

    return {
        "appid": int(game.appid),
        "name": game.name,
        "nameEn": game.name_en,
        "type": game.type or "game",
        "headerImage": game.header_image or build_steam_header_url(appid),
        "storeUrl": game.store_url or build_steam_store_url(appid),
        "chineseSupport": game.chinese_support,
        "familySharing": game.family_sharing or False,
        "tradingCards": game.trading_cards or False,
        "releaseDate": game.release_date,
        "genres": game.genres,
        "developers": game.developers or [],
        "publishers": game.publishers or [],
        "positiveRate": (game.positive_rate / 100) if game.positive_rate is not None else None,
        "positiveReviews": game.positive_reviews,
        "reviewCount": game.review_count,
        "isAdult": game.is_adult or False,
        "xgpTier": game.xgp_tier,
        "isVisualNovel": game.is_visual_novel or False,
        "hlFlag": game.hl_flag or 0,
        "ppFlag": game.pp_flag or 0,
        # 最近一次原价跳变时刻：永降/永涨徽章 14 天时效判据（前端判定显隐）
        "ppChangedAt": game.pp_changed_at.isoformat() if game.pp_changed_at else None,
        "seriesId": game.series_id,
        "isHb": game.is_hb or False,
        "isEpic": game.is_epic or False,
        "epicDate": game.epic_date,
        "hbData": game.hb_data,
        # 第三方渠道 bundle 计数（Barter.vg 档案；NULL=未拉取）
        "bundleCount": game.bundle_count,
        "viewCount": game.view_count,
        # 下架监控：非空 = 已判定下架（前端角标依据）
        "removedAt": game.removed_at.isoformat() if game.removed_at else None,
        "priceMatrix": price_matrix,
        # 爬过但未抓到价格的区（大写码，missing/blocked）：详情页与列表口径一致
        "unavailableRegions": sorted(unavailable_regions),
        "cnPriceCents": cn_price_cents,
        "cnCnyFen": cn_cny_fen,
        "cnDiscount": cn_discount,
        "lowestCnyFen": lowest_cny_fen,
        "lowestRegionCode": lowest_region_code,
        "savingsFen": (
            max(cn_cny_fen - lowest_cny_fen, 0)
            if cn_cny_fen is not None and lowest_cny_fen is not None
            else 0
        ),
        "versions": versions,
        "linkedBundles": await linked_bundles(appid, rates),
    }


# Steam 图片 CDN 三域同库：akamai 域国内直连可达（fastly 被墙/不稳定，
# queniuqe 为历史镜像域）。与 bundles.service._normalize_image 同一规则。
_BUNDLE_IMG_HOSTS = re.compile(
    r"^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com", re.IGNORECASE
)


def _normalize_bundle_image(url: str | None) -> str | None:
    if not url:
        return None
    return _BUNDLE_IMG_HOSTS.sub("https://shared.akamai.steamstatic.com", url)


def _bundle_fallback_image(b: Bundle) -> str:
    kind = "subs" if _bundle_item_kind(b) == 1 else "bundles"
    return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/{kind}/{b.bundle_id}/header.jpg"


def _bundle_item_kind(b: Bundle) -> int:
    """形态（0=bundle/1=sub）：item_kind 权威；未回填（-1/NULL）时沿用 mps 旧值。"""
    kind = b.item_kind if b.item_kind is not None else -1
    if kind not in (0, 1):
        kind = b.must_purchase_as_set if b.must_purchase_as_set is not None else 0
    return kind


async def linked_bundles(appid: int, rates: dict | None = None) -> list[dict]:
    """游戏关联捆绑包（GPW「关联捆绑包」区块 / 详情页 linkedBundles）。

    价格聚合口径：
    priceCny=CN 区 CNY 分，lowestPriceFen/lowestRegion=非 CN 最低，diffFen=省多少。
    本地表仅 19 包，直接全量载入 Python 过滤。

    双产品隔离（bundles.service 同源逻辑）：双轨混写的脏行（同号 sub 的异种
    appids）不参与价格聚合，避免 sub 单品价被当成整包价带偏最低区。
    """
    rates = rates if rates is not None else await get_rates()

    async with get_session_factory()() as session:
        bundles = (await session.execute(select(Bundle))).scalars().all()
        hit = [b for b in bundles if appid in [int(a) for a in (b.app_ids or [])]]
        if not hit:
            return []
        price_rows = (
            await session.execute(
                select(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id.in_([b.bundle_id for b in hit])
                )
            )
        ).scalars().all()

    prices_by_bundle: dict[int, list[BundleRegionPrice]] = {}
    for p in price_rows:
        prices_by_bundle.setdefault(p.bundle_id, []).append(p)

    items = []
    for b in hit:
        rows = prices_by_bundle.get(b.bundle_id, [])
        # 与主档 appids 同族（有交集）的行才计入：剔除双轨混写的异种产品行
        main_appids = {int(a) for a in (b.app_ids or [])}
        if main_appids:
            rows = [
                p for p in rows
                if main_appids & {int(a) for a in (p.app_ids or [])}
            ]
        cn_cny_fen = None
        lowest_cny_fen = None
        lowest_region = ""
        for p in rows:
            code = p.region_code.upper()
            cny_fen = int(p.cny_fen) if p.cny_fen is not None else None
            if cny_fen is None and p.price:
                currency = p.currency or REGION_TO_CURRENCY.get(code, "USD")
                cny_fen = convert_minor_to_cny_fen(int(p.price), currency, rates)
            if code == "CN":
                cn_cny_fen = cny_fen
            elif cny_fen is not None and (lowest_cny_fen is None or cny_fen < lowest_cny_fen):
                lowest_cny_fen = cny_fen
                lowest_region = code.lower()
        if lowest_cny_fen is None:
            lowest_cny_fen = cn_cny_fen
            lowest_region = "cn"
        diff = (
            max(cn_cny_fen - lowest_cny_fen, 0)
            if cn_cny_fen is not None and lowest_cny_fen is not None
            else 0
        )
        items.append(
            {
                "bundleId": b.bundle_id,
                "name": b.name,
                "headerImage": _normalize_bundle_image(b.header_image)
                or _bundle_fallback_image(b),
                "url": b.url
                or f"https://store.steampowered.com/{'sub' if _bundle_item_kind(b) == 1 else 'bundle'}/{b.bundle_id}/",
                "mustPurchaseAsSet": b.must_purchase_as_set if b.must_purchase_as_set is not None else -1,
                "itemKind": _bundle_item_kind(b),
                "priceCny": cn_cny_fen,
                "lowestRegion": lowest_region,
                "lowestPriceFen": lowest_cny_fen,
                "diffFen": diff,
            }
        )

    # 省得多的排前（对齐用户视角的"值得看"）
    items.sort(key=lambda x: x["diffFen"], reverse=True)
    return items


async def get_game_history(
    appid: int, region: str = "cn", days: int = 0, sub_id: int | None = None
) -> dict:
    """历史价格走势（按版本切片）。返回 {region, points, lowest, highest, count, versions}。

    sub_id 缺省 = 标准版；传入 = 指定 sub 包。days 缺省 **0 = 全部时间**——
    时间窗由前端在图表端开窗（导航条/chips），不回源；此处若给 365 之类的
    默认值，任何漏传 days 的调用方都会静默拿到被截断的序列。
    versions 为该 appid+region 库内 distinct 版本（前端下拉数据源）。

    标准版语义：**跨 sub 代际的标准版序列**。Steam 改包内容就换 sub_id，
    只认单个 sub 会把换代之前的历史整段滤掉——实测 872410/CN 只出 2 个点
    而库里 130 行（2021-02-27 起），全库 78 个 (appid, region) 对合计漏
    2747 行，前端表现就是「只有今年的数据 / 时间轴中间断了」。因此：

        标准版 = 该区所有标准版行 ∪ 该区当前在售 sub 的所有行

    标准版判据与写入侧 db_writer.py 的 is_standard 同口径（非 gold ∧ 无版本
    后缀 ∧ 非捆绑包），读取侧不得独立演化。并集右项是必要的兜底：红警 3 这类
    只在捆绑包 sub 下售卖的游戏，其区服唯一在售 sub 会被识别成 bundle-as-sub，
    若一并排除就会得到空图（全库 0 个区服只靠捆绑行活着，故此项不产生回归）。
    """
    rates = await get_rates()
    region = region.lower()

    # 标准版判据（对齐 db_writer.py:187-192 的 is_standard）
    standard = and_(
        or_(GamePriceHistory.is_gold.is_(False), GamePriceHistory.is_gold.is_(None)),
        or_(GamePriceHistory.version_suffix.is_(None), GamePriceHistory.version_suffix == ""),
        or_(GamePriceHistory.is_bundle.is_(False), GamePriceHistory.is_bundle.is_(None)),
    )

    if sub_id is None:
        async with get_session_factory()() as session:
            sale_sub = (
                await session.execute(
                    select(GameCurrentPrice.sub_id)
                    .where(
                        GameCurrentPrice.appid == appid,
                        GameCurrentPrice.region_code == region.upper(),
                        GameCurrentPrice.price_status == "ok",
                        GameCurrentPrice.sub_id.is_not(None),
                    )
                    .order_by(GameCurrentPrice.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        # 注意 sub_id 可能是 0（db_writer 用 `or 0` 把 None 归一成 0），
        # 那不是有效 sub，不能进并集，否则会匹配到一堆 sub_id=0 的杂行。
        version_cond = (
            or_(standard, GamePriceHistory.sub_id == sale_sub)
            if sale_sub
            else standard
        )
    else:
        version_cond = GamePriceHistory.sub_id == sub_id

    point_conditions = [
        GamePriceHistory.appid == appid,
        # 库内 region_code 统一大写存储
        GamePriceHistory.region_code == region.upper(),
        GamePriceHistory.price_status == "ok",
        version_cond,
    ]
    if days > 0:
        # snapshot_at 由 db_writer 以**裸北京时间**写入（_naive(get_beijing_time_obj())），
        # 故这里必须比同一把钟，不能用本机 datetime.now()——服务器不在东八区时
        # 会整段错位。
        point_conditions.append(
            GamePriceHistory.snapshot_at
            >= get_beijing_time_obj().replace(tzinfo=None) - timedelta(days=days)
        )

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(GamePriceHistory)
                .where(and_(*point_conditions))
                .order_by(GamePriceHistory.snapshot_at.asc())
            )
        ).scalars().all()

        version_rows = (
            await session.execute(
                select(
                    GamePriceHistory.sub_id,
                    GamePriceHistory.version_suffix,
                    GamePriceHistory.is_gold,
                )
                .where(
                    GamePriceHistory.appid == appid,
                    GamePriceHistory.region_code == region.upper(),
                    GamePriceHistory.price_status == "ok",
                )
                .distinct()
            )
        ).all()

    def _version_sort_key(v):
        suffix = v.version_suffix or ""
        is_gold = bool(v.is_gold)
        return (0 if (not suffix and not is_gold) else 1, suffix, is_gold)

    versions = [
        {"subId": v.sub_id, "suffix": v.version_suffix, "isGold": bool(v.is_gold)}
        for v in sorted(version_rows, key=_version_sort_key)
    ]

    if not rows:
        return {
            "region": region,
            "points": [],
            "lowest": None,
            "highest": None,
            "count": 0,
            "versions": versions,
        }

    region_info = next((entry for entry in CC_LIST if entry[0] == region), None)

    points = []
    for r in rows:
        price_cents = int(r.price) if r.price is not None else None
        cny_fen = int(r.cny_fen) if r.cny_fen is not None else None
        if cny_fen is None and price_cents is not None and price_cents > 0:
            currency = r.currency or REGION_TO_CURRENCY.get(region.upper())
            if currency:
                cny_fen = convert_minor_to_cny_fen(price_cents, currency, rates)
        formatted = (
            format_minor_units(price_cents, region_info[2])
            if price_cents is not None and region_info
            else ""
        )
        points.append(
            {
                "timestamp": r.snapshot_at.isoformat() if r.snapshot_at else None,
                "cnyFen": cny_fen or 0,
                "cnyYuan": (cny_fen / 100) if cny_fen is not None else None,
                "originalCents": price_cents,
                "formattedPrice": formatted,
                "discount": r.discount_percent or 0,
                "currency": r.currency or REGION_TO_CURRENCY.get(region.upper(), "USD"),
            }
        )

    valid = [p["cnyFen"] for p in points if p["cnyFen"] > 0]
    if not valid:
        return {
            "region": region,
            "points": points,
            "lowest": None,
            "highest": None,
            "versions": versions,
        }

    lowest = min(valid)
    highest = max(valid)
    lowest_point = next(p for p in points if p["cnyFen"] == lowest)

    # 史低次数：价格追平/跌破历史最低值的事件数——同一促销期内的
    # 连续同价位快照只计一次（回升后再次到达 = 新的一次），对齐
    # 外部价格服务「达到史低的次数」口径。
    lowest_hits = 0
    running_low = lowest
    in_low_streak = False
    for p in points:
        v = p["cnyFen"]
        if v <= 0:
            continue
        if v <= running_low:
            running_low = v
            if not in_low_streak:
                lowest_hits += 1
                in_low_streak = True
        else:
            in_low_streak = False

    return {
        "region": region,
        "points": points,
        "lowest": {
            "cnyFen": lowest,
            "cnyYuan": lowest / 100,
            "timestamp": lowest_point["timestamp"],
            "cnyYuanPoint": lowest_point["cnyYuan"],
            "formattedPrice": lowest_point["formattedPrice"],
        },
        "highest": {"cnyFen": highest, "cnyYuan": highest / 100},
        "count": len(points),
        "lowestHits": lowest_hits,
        "versions": versions,
    }


async def get_price_context(appid: int, date: str, region: str = "cn") -> dict:
    """截至 date 的价格上下文（账单许可证命中条数据源）：当时价 + 历史最低。

    与 get_game_history 同一条标准版序列（非 gold ∧ 无版本后缀 ∧ 非捆绑包
    ∪ 当前在售 sub）；窗口 = snapshot_at ≤ date 当日（snapshot_at 是裸北京
    时间，license date 也是东八区日历日，比同一把钟）。当时价 = 窗口内最后
    一个有效快照，史低 = 窗口内 cnyFen 最小的快照；无有效快照（含 date 非
    法）时两值均 null——调用方静默省略价格段，不兜底不报错。
    """
    region = region.lower()
    empty = {"appid": appid, "region": region, "date": date, "at": None, "lowest": None}
    try:
        day_end = datetime.strptime(date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return empty

    # 标准版判据与 get_game_history 逐字同口径（读取侧不得独立演化）
    standard = and_(
        or_(GamePriceHistory.is_gold.is_(False), GamePriceHistory.is_gold.is_(None)),
        or_(GamePriceHistory.version_suffix.is_(None), GamePriceHistory.version_suffix == ""),
        or_(GamePriceHistory.is_bundle.is_(False), GamePriceHistory.is_bundle.is_(None)),
    )
    async with get_session_factory()() as session:
        sale_sub = (
            await session.execute(
                select(GameCurrentPrice.sub_id)
                .where(
                    GameCurrentPrice.appid == appid,
                    GameCurrentPrice.region_code == region.upper(),
                    GameCurrentPrice.price_status == "ok",
                    GameCurrentPrice.sub_id.is_not(None),
                    # sub_id=0 是 db_writer 的 `or 0` 归一产物，不是有效 sub
                    GameCurrentPrice.sub_id > 0,
                )
                .order_by(GameCurrentPrice.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version_cond = (
            or_(standard, GamePriceHistory.sub_id == sale_sub) if sale_sub else standard
        )
        rows = (
            await session.execute(
                select(GamePriceHistory)
                .where(
                    and_(
                        GamePriceHistory.appid == appid,
                        GamePriceHistory.region_code == region.upper(),
                        GamePriceHistory.price_status == "ok",
                        version_cond,
                        GamePriceHistory.snapshot_at.is_not(None),
                        GamePriceHistory.snapshot_at <= day_end,
                    )
                )
                .order_by(GamePriceHistory.snapshot_at.asc())
            )
        ).scalars().all()

    rates = await get_rates()
    points: list[dict] = []
    for r in rows:
        price_cents = int(r.price) if r.price is not None else None
        cny_fen = int(r.cny_fen) if r.cny_fen is not None else None
        if cny_fen is None and price_cents is not None and price_cents > 0:
            currency = r.currency or REGION_TO_CURRENCY.get(region.upper())
            if currency:
                cny_fen = convert_minor_to_cny_fen(price_cents, currency, rates)
        if not cny_fen or cny_fen <= 0:
            continue
        points.append(
            {
                "cnyFen": cny_fen,
                "discount": r.discount_percent or 0,
                "snapshotAt": r.snapshot_at.isoformat() if r.snapshot_at else None,
            }
        )
    if not points:
        return empty
    at = points[-1]
    lowest = min(points, key=lambda p: p["cnyFen"])
    return {"appid": appid, "region": region, "date": date, "at": at, "lowest": lowest}


async def get_price_context_batch(items: list[tuple[int, str]]) -> dict:
    """批量 price-context（账单消费明细命中条数据源）。

    items: (appid, date) 对，≤ 200 对截断；逐项走 get_price_context
    （本地 SQLite 各两次索引查询，量级可承受），appid 非法/日期为空的
    项直接回 null 对，不抛错——调用方按缺价格段渲染。
    """
    results = []
    for appid, date in items[:200]:
        if appid <= 0 or not date:
            results.append({"appid": appid, "date": date, "at": None, "lowest": None})
            continue
        results.append(await get_price_context(appid, date))
    return {"results": results}


async def get_game_versions(appid: int) -> dict:
    """全版本 × 全区最新价（走势抽屉「全部版本」区块数据源，B1 拍板形态）。

    聚合 history 每 (region_code, sub_id) 的最新 ok 行（snapshot_at 升序
    扫描后 dict 覆盖 = 最新胜出）；is_bundle 行剔除（捆绑包由详情页
    「关联捆绑包」承接）。零迁移：版本列 sub_id/is_gold/version_suffix
    既有 schema 早已存在，is_bundle 为本轮新增。
    """
    rates = await get_rates()

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    GamePriceHistory.region_code,
                    GamePriceHistory.sub_id,
                    GamePriceHistory.is_gold,
                    GamePriceHistory.version_suffix,
                    GamePriceHistory.is_bundle,
                    GamePriceHistory.price,
                    GamePriceHistory.original_price,
                    GamePriceHistory.discount_percent,
                    GamePriceHistory.currency,
                    GamePriceHistory.cny_fen,
                )
                .where(
                    GamePriceHistory.appid == appid,
                    GamePriceHistory.price_status == "ok",
                    GamePriceHistory.price.is_not(None),
                    GamePriceHistory.price > 0,
                    GamePriceHistory.sub_id.is_not(None),
                )
                .order_by(GamePriceHistory.snapshot_at.asc(), GamePriceHistory.id.asc())
            )
        ).all()

    if not rows:
        return {"versions": []}

    # 每 (region, sub) 最新行（升序扫描覆盖）；顺带收集 sub 的版本元信息
    latest: dict[tuple[str, int], object] = {}
    sub_meta: dict[int, dict] = {}
    for r in rows:
        latest[(r.region_code, r.sub_id)] = r
        sub_meta.setdefault(
            r.sub_id,
            {
                "suffix": r.version_suffix,
                "isGold": bool(r.is_gold),
                "isBundle": bool(r.is_bundle),
            },
        )
        # 行间版本元信息以非空者为准（理论上同 sub 恒定）
        if r.version_suffix and not sub_meta[r.sub_id]["suffix"]:
            sub_meta[r.sub_id]["suffix"] = r.version_suffix

    versions: list[dict] = []
    for (region_code, sub_id), r in latest.items():
        meta = sub_meta[sub_id]
        if meta["isBundle"]:
            continue  # bundle 行改道「关联捆绑包」区块
        v = next((x for x in versions if x["subId"] == sub_id), None)
        if v is None:
            v = {
                "subId": int(sub_id),
                "suffix": meta["suffix"],
                "isGold": meta["isGold"],
                "regions": {},
            }
            versions.append(v)
        price_cents = int(r.price)
        cny_fen = int(r.cny_fen) if r.cny_fen is not None else None
        if cny_fen is None:
            currency = r.currency or REGION_TO_CURRENCY.get(region_code.upper())
            if currency:
                cny_fen = convert_minor_to_cny_fen(price_cents, currency, rates)
        region_info = next((e for e in CC_LIST if e[0] == region_code.upper()), None)
        v["regions"][region_code.upper()] = {
            "cents": price_cents,
            "formatted": format_minor_units(price_cents, region_info[2])
            if region_info
            else str(price_cents),
            "discount": r.discount_percent or 0,
            "cnyFen": cny_fen,
        }

    # 排序：标准版（无后缀非 gold）→ 有后缀 → gold
    versions.sort(
        key=lambda v: (
            0 if (not v["suffix"] and not v["isGold"]) else 1,
            v["suffix"] or "",
            v["isGold"],
        )
    )
    return {"versions": versions}


async def refresh_hl_flags(appids: list[int] | None = None) -> int:
    """刷新 games.hl_flag（新史低 / 平史低标记）。

    语义：当前 CN 价 vs 既往历史最低（排除当前快照）→ 1=新史低 2=平史低；
    无既往数据时打折记 3；不打折记 0。appids=None 全库刷新（启动时），
    否则增量（爬取落库后调用）。
    """
    from sqlalchemy.orm import aliased

    h = GamePriceHistory
    cn_cur = aliased(GameCurrentPrice)
    async with get_session_factory()() as session:
        prior_q = (
            select(h.appid, func.min(h.cny_fen))
            .select_from(h)
            .join(cn_cur, and_(cn_cur.appid == h.appid, cn_cur.region_code == "CN"))
            .where(
                h.region_code == "CN",
                h.price_status == "ok",
                h.cny_fen.is_not(None),
                h.cny_fen > 0,
                or_(h.is_gold.is_(False), h.is_gold.is_(None)),
                or_(h.version_suffix.is_(None), h.version_suffix == ""),
                # bundle-as-sub（不支持补齐的捆绑包）不参与史低计算——
                # 提取修复前它们 suffix 为空混进标准版，产生假史低
                or_(h.is_bundle.is_(False), h.is_bundle.is_(None)),
                h.snapshot_at < cn_cur.updated_at,
            )
            .group_by(h.appid)
        )
        if appids:
            prior_q = prior_q.where(h.appid.in_(appids))
        prior_rows = (await session.execute(prior_q)).all()
        prior_low = {appid: int(min_fen) for appid, min_fen in prior_rows if min_fen is not None}

        cur_q = select(Game.appid, GameCurrentPrice.discount_percent, GameCurrentPrice.cny_fen).join(
            GameCurrentPrice,
            and_(
                GameCurrentPrice.appid == Game.appid,
                GameCurrentPrice.region_code == "CN",
                GameCurrentPrice.price_status == "ok",
            ),
        )
        if appids:
            cur_q = cur_q.where(Game.appid.in_(appids))
        cur_rows = (await session.execute(cur_q)).all()

        updates: list[tuple[int, int]] = []  # (appid, flag)
        for appid, discount, cn_fen in cur_rows:
            discount = discount or 0
            prior = prior_low.get(int(appid))
            if cn_fen is not None and cn_fen > 0 and prior is not None:
                flag = 1 if cn_fen < prior else (2 if cn_fen == prior else (3 if discount > 0 else 0))
            else:
                flag = 3 if discount > 0 else 0
            updates.append((int(appid), flag))

        if updates:
            from sqlalchemy import case

            # SQLite 绑定变量上限，按批执行 CASE UPDATE
            BATCH = 400
            for i in range(0, len(updates), BATCH):
                batch = updates[i : i + BATCH]
                await session.execute(
                    update(Game)
                    .where(Game.appid.in_([a for a, _ in batch]))
                    .values(hl_flag=case(dict(batch), value=Game.appid))
                )
            await session.commit()
    return len(updates)


async def refresh_pp_flags(appids: list[int] | None = None) -> int:
    """刷新 games.pp_flag + pp_changed_at（永降/永涨标记与跳变时刻）。

    判定：当前国区标准版原价 vs 历史上最近一次「不同的原价」（原价序列
    上的上一个台阶）→ 1=永降 2=永涨；从未变过价 → 0。

    pp_changed_at = 最近一次原价跳变（相邻快照 original 不同）的快照时刻，
    是前端徽章时效判据（变化后 14 天内才展示）。它与 flag 判定独立成查询：
    flag 的比较锚是「当前价行」，必须排除当前刻快照（snapshot_at < updated_at），
    否则本次刚写入的行会自己跟自己比出 0；跳变时刻恰恰**要**看到本次写入——
    不加该条件，变化要等下一轮爬取才计入时效窗（最长偏移一个抓取间隔）。

    - 只比原价（original_price，本地币种）不比折后价：打折不动原价，
      原价变化 = 真调价，与折扣状态无关（打折期间照样可能被永久下调）
    - 历史口径与史低一致：CN 区、ok、非黄金版、无版本后缀、非 bundle-as-sub
    - 当前行挂真实 sub_id 时只比同 sub 的历史（标准版多 sub 择优，
      换 sub 不误报）；sub_id=0 视作未挂不限定
    - 无状态可重算：每次从快照序列重建「最近一次调价方向」，启动全库
      刷新与爬取增量（appids）语义一致，重复执行幂等
    """
    from datetime import datetime as _dt

    from sqlalchemy import case

    h = GamePriceHistory
    c = aliased(GameCurrentPrice)
    inner = (
        select(
            h.appid,
            c.original_price.label("cur_original"),
            h.original_price.label("prior_original"),
            func.row_number()
            .over(partition_by=h.appid, order_by=h.snapshot_at.desc())
            .label("rn"),
        )
        .join(c, and_(c.appid == h.appid, c.region_code == "CN"))
        .where(
            h.region_code == "CN",
            h.price_status == "ok",
            h.original_price.is_not(None),
            h.original_price != c.original_price,
            c.original_price.is_not(None),
            c.price_status == "ok",
            h.snapshot_at < c.updated_at,
            or_(h.is_gold.is_(False), h.is_gold.is_(None)),
            or_(h.version_suffix.is_(None), h.version_suffix == ""),
            or_(c.sub_id.is_(None), c.sub_id == 0, h.sub_id == c.sub_id),
        )
    )
    if appids:
        inner = inner.where(h.appid.in_(appids))
    subq = inner.subquery()

    # 最近一次原价跳变：标准版快照序列按时间排，LAG 出上一条快照的原价，
    # 取「与上一条不同」的最近一条 → 那条快照的时刻即变化发生的观测点
    prev_original = func.lag(h.original_price).over(
        partition_by=h.appid, order_by=h.snapshot_at
    )
    seq = (
        select(
            h.appid,
            h.original_price,
            h.snapshot_at,
            prev_original.label("prev_original"),
        )
        .join(c, and_(c.appid == h.appid, c.region_code == "CN"))
        .where(
            h.region_code == "CN",
            h.price_status == "ok",
            h.original_price.is_not(None),
            c.original_price.is_not(None),
            c.price_status == "ok",
            or_(h.is_gold.is_(False), h.is_gold.is_(None)),
            or_(h.version_suffix.is_(None), h.version_suffix == ""),
            or_(h.is_bundle.is_(False), h.is_bundle.is_(None)),
            or_(c.sub_id.is_(None), c.sub_id == 0, h.sub_id == c.sub_id),
        )
    )
    if appids:
        seq = seq.where(h.appid.in_(appids))
    seq_sub = seq.subquery()
    latest_jump = (
        select(
            seq_sub.c.appid,
            seq_sub.c.snapshot_at.label("changed_at"),
            func.row_number()
            .over(partition_by=seq_sub.c.appid, order_by=seq_sub.c.snapshot_at.desc())
            .label("rn"),
        ).where(
            seq_sub.c.prev_original.is_not(None),
            seq_sub.c.prev_original != seq_sub.c.original_price,
        )
    ).subquery()

    def _as_dt(value) -> _dt | None:
        if isinstance(value, _dt):
            return value
        try:
            return _dt.fromisoformat(str(value))
        except ValueError:
            return None

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(subq.c.appid, subq.c.cur_original, subq.c.prior_original).where(
                    subq.c.rn == 1
                )
            )
        ).all()
        flags = {int(appid): (1 if cur < prior else 2) for appid, cur, prior in rows}

        jump_rows = (
            await session.execute(
                select(latest_jump.c.appid, latest_jump.c.changed_at).where(latest_jump.c.rn == 1)
            )
        ).all()
        changed = {int(appid): _as_dt(ts) for appid, ts in jump_rows}

        if appids:
            targets = [int(a) for a in appids]
        else:
            targets = [int(a) for (a,) in (await session.execute(select(Game.appid))).all()]
        updates = [(a, flags.get(a, 0), changed.get(a)) for a in targets]

        if updates:
            # SQLite 绑定变量上限，按批执行 CASE UPDATE（与 hl_flag 同款）。
            # pp_changed_at 的映射必须覆盖全量 targets：空 case 会生成非法 SQL，
            # 且无跳变的 appid 要显式写 NULL（幂等重算要求清掉旧值）。
            from sqlalchemy import null

            BATCH = 400
            for i in range(0, len(updates), BATCH):
                batch = updates[i : i + BATCH]
                await session.execute(
                    update(Game)
                    .where(Game.appid.in_([a for a, _, _ in batch]))
                    .values(
                        pp_flag=case({a: f for a, f, _ in batch}, value=Game.appid),
                        pp_changed_at=case(
                            {a: (t if t is not None else null()) for a, _, t in batch},
                            value=Game.appid,
                        ),
                    )
                )
            await session.commit()
    return len(updates)


async def refresh_sort_cache(appids: list[int] | None = None) -> int:
    """刷新 games.min_cny_fen / diff_fen 预计算列（对齐 mv_game_sort_cache 构建 SQL）。

    appids=None 全库刷新（启动时）；否则增量（爬取落库后调用，lowest 限定目标集合）。
    语义：min_cny_fen = 非 CN 各区 ok 价最低（原始值，无则 NULL）；
    diff_fen = MAX(CN 价 - COALESCE(最低, CN 价), 0)。
    需要 SQLite ≥ 3.33（UPDATE ... FROM）。
    """
    from sqlalchemy import bindparam, text

    lowest_filter = "AND appid IN :appids" if appids else ""
    sql = text(
        f"""
        WITH lowest AS (
            SELECT appid, MIN(cny_fen) AS min_fen
            FROM game_current_prices
            WHERE region_code != 'CN' AND price_status = 'ok'
              AND cny_fen IS NOT NULL AND cny_fen > 0
              {lowest_filter}
            GROUP BY appid
        )
        UPDATE games AS g SET
            min_cny_fen = l.min_fen,
            diff_fen = COALESCE(MAX(cn.price - COALESCE(l.min_fen, cn.price), 0), 0)
        FROM lowest l
        LEFT JOIN game_current_prices cn
          ON cn.appid = l.appid AND cn.region_code = 'CN'
         AND cn.price_status = 'ok' AND cn.price IS NOT NULL AND cn.price > 0
        WHERE l.appid = g.appid
        """
    )
    params: dict = {"appids": list(appids)} if appids else {}
    if appids:
        sql = sql.bindparams(bindparam("appids", expanding=True))

    async with get_session_factory()() as session:
        result = await session.execute(sql, params)
        await session.commit()
    return result.rowcount or 0


def _build_list_item(game: Game, cn_row: GameCurrentPrice | None, price_rows: list, rates: dict) -> dict:
    """对齐 route.ts buildGameResponse。cn_row=None 为锁区（无国区行，LEFT JOIN）。"""
    base_cn_price = int(cn_row.price) if cn_row is not None and cn_row.price is not None else None

    price_map: dict[str, dict] = {}
    unavailable_regions: list[str] = []
    for p in price_rows:
        code = p.region_code.upper()
        if p.price_status in ("missing", "blocked"):
            # 爬过但未成功（missing=欠账待补抓；blocked=连败终态，大概率无货）
            unavailable_regions.append(code)
            continue
        if p.price is None or int(p.price) <= 0:
            continue
        price_cents = int(p.price)
        cny_fen = int(p.cny_fen) if p.cny_fen is not None else None
        if cny_fen is None:
            currency = p.currency or REGION_TO_CURRENCY.get(code)
            if currency:
                cny_fen = convert_minor_to_cny_fen(price_cents, currency, rates)
        price_map[code] = {"cents": price_cents, "cnyFen": cny_fen}

    all_prices: dict[str, list] = {}
    lowest_cny_fen = base_cn_price
    for code, _, currency in CC_LIST:
        data = price_map.get(code.upper())
        if not data:
            continue
        all_prices[code.upper()] = [
            format_minor_units(data["cents"], currency), data["cnyFen"] or 0, data["cents"], None
        ]
        if (
            data["cnyFen"] is not None
            and code.upper() != "CN"
            and (lowest_cny_fen is None or data["cnyFen"] < lowest_cny_fen)
        ):
            lowest_cny_fen = data["cnyFen"]

    if lowest_cny_fen is None:
        lowest_cny_fen = base_cn_price

    diff = (
        max(base_cn_price - lowest_cny_fen, 0)
        if base_cn_price is not None and lowest_cny_fen is not None
        else 0
    )
    discount = cn_row.discount_percent or 0 if cn_row is not None else 0
    cn_original_fen = (
        int(cn_row.original_price)
        if cn_row is not None and cn_row.original_price is not None
        else None
    )

    return {
        "appid": int(game.appid),
        "name": game.name,
        "nameEn": game.name_en,
        "discount": discount,
        "discountLabel": f"-{discount}%" if discount > 0 else "",
        "positiveRate": (game.positive_rate / 100) if game.positive_rate is not None else None,
        "reviewCount": game.review_count,
        "isAdult": game.is_adult or False,
        "xgpTier": game.xgp_tier,
        "isVisualNovel": game.is_visual_novel or False,
        "releaseDate": game.release_date or "",
        "basePriceFen": base_cn_price,
        # 国区原价（未折价分，original_price）；划线原价展示用——
        # basePriceFen 是折后现价，不能当原价画删除线
        "cnOriginalFen": cn_original_fen,
        "lowestPriceFen": lowest_cny_fen,
        "savingsFen": diff,
        "headerImage": game.header_image or build_steam_header_url(int(game.appid)),
        # 区服键控价格矩阵：{"CN": [formatted, cnyFen, cents, null], ...}，只含有价区
        "priceMatrix": all_prices,
        # 爬过但未抓到价格的区（大写码，missing/blocked）：前端黄框区分「待更新」与锁区
        "unavailableRegions": sorted(unavailable_regions),
        "hlFlag": game.hl_flag or 0,
        "ppFlag": game.pp_flag or 0,
        # 最近一次原价跳变时刻：永降/永涨徽章 14 天时效判据（前端判定显隐）
        "ppChangedAt": game.pp_changed_at.isoformat() if game.pp_changed_at else None,
        "updatedAt": game.updated_at.isoformat() if game.updated_at else None,
        "familySharing": game.family_sharing or False,
        "tradingCards": game.trading_cards or False,
        "seriesId": game.series_id,
        "isHb": game.is_hb or False,
        "isEpic": game.is_epic or False,
        "epicDate": game.epic_date,
        "hbData": game.hb_data,
        # 第三方渠道 bundle 计数（Barter.vg 档案；NULL=未拉取）
        "bundleCount": game.bundle_count,
        # 下架监控：非空 = 已判定下架（前端角标依据）
        "removedAt": game.removed_at.isoformat() if game.removed_at else None,
    }


async def retry_removed_game(appid: int) -> dict:
    """手动复探下架游戏：清标 + 立即后台重爬（复活通道之一）。

    下架判定可能误判（Steam 抖动/临时封禁），重新上架也存在——详情页
    「重新探测」入口调这里。清标让游戏立刻回到关注层池子（宽限期判据
    removed_at 已空，天然放行）；后台 run 异步跑，不阻塞请求。
    返回任务启动摘要；已有任务运行时只清标不启动（下一轮价格刷新
    自然会带上它——脱池判据已解除）。
    """
    from datetime import datetime

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        row = await session.get(Game, int(appid))
        if row is None:
            raise KeyError(f"游戏 {appid} 不存在")
        row.removed_at = None
        row.removed_strikes = 0
        await session.commit()

    from app.domains.crawl import service as crawl_service

    try:
        result = await crawl_service.start_job(
            scope="appids", appids=[int(appid)], kind="removed_retry",
        )
        return {"ok": True, "jobId": result["id"], "requeued": True}
    except (RuntimeError, ValueError) as e:
        # 已有任务在跑 / 空列表：清标已生效，下一轮关注层刷新自然带上
        return {"ok": True, "jobId": None, "requeued": False, "note": str(e)}
