"""SQLite 落库（SQLAlchemy async）。

核心语义：UPSERT / 标准版选择（每区取 sub_id 最小）/ cny_fen 计算。
bundle / repair 写入方法随对应功能迁入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import case, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core import seed_assets
from app.core.database import get_session_factory
from app.domains.games.models import (
    Bundle,
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
    GamePriceHistory,
)
from app.domains.rates.models import FxRate

from .config import STALE_HOURS
from .utils import get_beijing_time_obj, is_near_steam_refresh

logger = logging.getLogger(__name__)

_CURRENT_UPDATABLE = (
    "currency",
    "price",
    "original_price",
    "discount_percent",
    "sub_id",
    "price_status",
    "cny_fen",
    "updated_at",
)

# 补抓账本参数：免费游戏 price=0 合法（parse_all_sub_prices is_free 路径），
# 只有 status=ok 且 price=None 才是"成功响应里的坏数据"；连续补抓失败上限，
# 超过即终态化（blocked），防止"该区根本无货"被当成可重试错误死磕。
MISSING_MAX_RETRIES = 5


def _is_ok_price_row(price_cents: int | None) -> bool:
    """ok 行价格合法性：None 才是坏数据（0 = 免费游戏，合法）。"""
    return price_cents is not None


def _missing_ledger_bump() -> dict:
    """missing 计账 SET 片段：fail_count 旧值+1，第 MISSING_MAX_RETRIES 次
    失败即转 blocked 终态（"连续 N 次仍失败"语义，含当次）。

    UPDATE SET 与 UPSERT DO UPDATE SET 中表列均引用旧行值，两处语义一致，
    单一定义避免转移规则漂移。
    """
    return {
        "price_status": case(
            (GameCurrentPrice.fail_count + 1 >= MISSING_MAX_RETRIES, "blocked"),
            else_="missing",
        ),
        "fail_count": GameCurrentPrice.fail_count + 1,
    }


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite DateTime 列统一存 naive（北京本地时间）。"""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class DbWriter:
    """直写 SQLite：games UPSERT + game_current_prices UPSERT + game_price_history INSERT。"""

    def __init__(self) -> None:
        self._fx_rates: dict[str, float] | None = None

    async def connect(self) -> None:
        await self._load_fx_rates()

    async def _load_fx_rates(self) -> None:
        try:
            async with get_session_factory()() as session:
                rows = (await session.execute(select(FxRate))).scalars().all()
            self._fx_rates = {r.currency_code: float(r.rate_to_cny) for r in rows}
            logger.info("[汇率] 已加载 %d 条汇率", len(self._fx_rates))
        except Exception as e:
            logger.warning("[汇率] 加载失败, 将使用 fallback: %s", e)
            self._fx_rates = {}
        self._fx_rates.setdefault("CNY", 1.0)

    def _compute_cny_fen(self, price_cents: int | None, currency: str) -> int | None:
        """cny_fen = ROUND(price_cents * rate_to_cny)。"""
        if price_cents is None or price_cents <= 0:
            return None
        if not self._fx_rates:
            self._fx_rates = {"CNY": 1.0}
        rate = self._fx_rates.get(currency)
        if rate is None:
            return None
        return round(int(price_cents) * rate)

    async def upsert_game_and_prices(self, game_data: dict, prices_data: list[dict] | None) -> bool:
        """写入游戏元数据 + 区域价格。

        - 标准版价格 → game_current_prices (UPSERT)
        - 全版本价格 → game_price_history (INSERT)
        """
        try:
            now_dt = _naive(game_data.get("updated_at") or get_beijing_time_obj())
            game_values = {}
            for k, v in game_data.items():
                # DateTime 列（updated_at/created_at）防串格式：isoformat 字符串
                # （T 分隔，E2E mock 载荷）转 naive datetime——否则与空格分隔行
                # 混排，SQLite 字符串比较会让 T 格式行在 ORDER BY 里恒压顶
                if k in ("updated_at", "created_at") and isinstance(v, str):
                    try:
                        v = datetime.fromisoformat(v)
                    except ValueError:
                        pass
                game_values[k] = _naive(v) if isinstance(v, datetime) else v

            async with get_session_factory()() as session:
                insert_game = sqlite_insert(Game).values(**game_values)
                upsert_game = insert_game.on_conflict_do_update(
                    index_elements=[Game.appid],
                    set_={
                        c: getattr(insert_game.excluded, c)
                        for c in (
                            "name", "name_en", "type", "header_image", "store_url",
                            "chinese_support", "family_sharing", "trading_cards",
                            "release_date", "genres", "is_adult", "is_visual_novel",
                            "developers", "publishers",
                            "positive_rate", "positive_reviews", "review_count",
                            "updated_at",
                        )
                    },
                )
                await session.execute(upsert_game)

                if prices_data:
                    current_batch: list[dict] = []
                    history_batch: list[dict] = []
                    standard_candidates: dict[str, list[dict]] = {}
                    degraded_regions: set[str] = set()  # 本次门禁降级的区（需欠账结转）

                    # ── 历史差量门禁基线：该 appid 每个 (区, sub) 的最新快照。
                    #    全池 6h 化后无门禁 = 价格未变也写快照（实测 ~27 万行/天
                    #    纯冗余，2.9M 存量里绝大多数是这种）；走势图只需要变化点
                    #    ——价格没变就没有新点，线是平的，语义不损。比较键含价
                    #    三件套 + 版本后缀 + gold 标：折扣往返/价格修正/名称修正
                    #    都会正常产生新快照。现价表不受门禁影响，照常每轮刷新
                    #    （updated_at = 最新验证时刻）。
                    latest_snapshots: dict[tuple[str, int], tuple] = {}
                    rows_latest = await session.execute(
                        text(
                            "SELECT region_code, sub_id, price, original_price, "
                            "discount_percent, version_suffix, is_gold FROM ("
                            "SELECT region_code, sub_id, price, original_price, "
                            "discount_percent, version_suffix, is_gold, snapshot_at, "
                            "ROW_NUMBER() OVER (PARTITION BY region_code, sub_id "
                            "ORDER BY snapshot_at DESC, id DESC) AS rn "
                            "FROM game_price_history WHERE appid = :appid"
                            ") WHERE rn = 1"
                        ),
                        {"appid": int(game_data.get("appid") or 0)},
                    )
                    for r in rows_latest:
                        latest_snapshots[(r[0], int(r[1] or 0))] = (
                            r[2], r[3], r[4], r[5], bool(r[6]),
                        )

                    for p in prices_data:
                        region = p.get("region_code", "").upper()
                        price_cents = p.get("price")
                        currency = p.get("currency", "")
                        is_gold = p.get("is_gold", False)
                        version_suffix = p.get("version_suffix")
                        is_bundle = p.get("is_bundle", False)
                        status = p.get("price_status", "ok")
                        # 质量门禁：ok 但无价格 = 成功响应里的坏数据（如 gold 版
                        # 无 sub 价格），降级 missing 进账本走补抓自愈
                        if status == "ok" and not _is_ok_price_row(price_cents):
                            status = "missing"
                            degraded_regions.add(region)
                        cny_fen = (
                            self._compute_cny_fen(price_cents, currency) if price_cents else None
                        )

                        # 所有有价格的版本写入 history（免费游戏 price=0 不进
                        # history）；相对最新快照无变化的行不写（差量门禁）
                        if status == "ok" and price_cents and price_cents > 0:
                            prev = latest_snapshots.get(
                                (region, int(p.get("sub_id") or 0))
                            )
                            if prev == (
                                price_cents,
                                p.get("original_price"),
                                p.get("discount_percent", 0),
                                version_suffix,
                                is_gold,
                            ):
                                pass  # 与最新快照完全一致：跳过，不产生冗余行
                            else:
                                history_batch.append(
                                    {
                                        "appid": p["appid"],
                                        "region_code": region,
                                        "currency": currency,
                                        "price": price_cents,
                                        "original_price": p.get("original_price"),
                                        "discount_percent": p.get("discount_percent", 0),
                                        "sub_id": p.get("sub_id") or 0,
                                        "is_gold": is_gold,
                                        "version_suffix": version_suffix,
                                        "is_bundle": is_bundle,
                                        "price_status": status,
                                        "cny_fen": cny_fen,
                                        "snapshot_at": now_dt,
                                    }
                                )

                        # 标准版候选: 非 gold 且无版本后缀且非捆绑包
                        # （bundle-as-sub 与标准版无法从价格区分，靠 A4 识别标记隔离）
                        is_standard = (
                            (not is_gold)
                            and (not version_suffix or version_suffix == "")
                            and not is_bundle
                        )
                        if is_standard:
                            standard_candidates.setdefault(region, []).append(
                                {
                                    "appid": p["appid"],
                                    "region_code": region,
                                    "currency": currency,
                                    "price": price_cents,
                                    "original_price": p.get("original_price"),
                                    "discount_percent": p.get("discount_percent", 0),
                                    "sub_id": p.get("sub_id") or 0,
                                    "price_status": status,
                                    "cny_fen": cny_fen,
                                    "updated_at": now_dt,
                                }
                            )

                    # 每个区域选标准版写入 current：优先有价（ok+price 有值）行——
                    # 该区任何版本有价就算有数；全部无价才落 missing 状态行
                    seen_regions: set[str] = set()
                    for region, candidates in standard_candidates.items():
                        priced = [c for c in candidates if c["price"] is not None]
                        current_batch.append(
                            min(priced or candidates, key=lambda x: x.get("sub_id") or 0)
                        )
                        seen_regions.add(region)

                    # 无标准版的区域（locked/blocked/missing）也写入 current 作为状态；
                    # 逆序遍历：同区多行降级时以最后一条为准（降级常因版本无价
                    # 整组发生，末行即最新尝试的状态）
                    appid_int = int(game_data.get("appid") or next(iter(prices_data))["appid"])
                    for p in reversed(prices_data):
                        region = p.get("region_code", "").upper()
                        if region in seen_regions:
                            continue
                        seen_regions.add(region)
                        # 门禁降级行的实时状态在主循环里已算过，重算保持独立
                        raw_status = p.get("price_status", "ok")
                        price_cents = p.get("price")
                        status = (
                            "missing"
                            if raw_status == "ok" and not _is_ok_price_row(price_cents)
                            else raw_status
                        )
                        currency = p.get("currency", "")
                        current_batch.append(
                            {
                                "appid": p["appid"],
                                "region_code": region,
                                "currency": currency,
                                "price": price_cents,
                                "original_price": p.get("original_price"),
                                "discount_percent": p.get("discount_percent", 0),
                                "sub_id": p.get("sub_id") or 0,
                                "price_status": status,
                                "cny_fen": self._compute_cny_fen(price_cents, currency)
                                if price_cents
                                else None,
                                "updated_at": now_dt,
                            }
                        )

                    if current_batch:
                        insert_cp = sqlite_insert(GameCurrentPrice)
                        await session.execute(
                            insert_cp.values(current_batch).on_conflict_do_update(
                                index_elements=[
                                    GameCurrentPrice.appid,
                                    GameCurrentPrice.region_code,
                                ],
                                set_={c: getattr(insert_cp.excluded, c) for c in _CURRENT_UPDATABLE},
                            )
                        )

                        # 补抓成功结转清账：本批有 ok 价的区 fail_count 归零
                        # （missing → 补抓成功 → 回 ok 的欠账闭环）
                        ok_regions = {
                            row["region_code"] for row in current_batch if row["price_status"] == "ok"
                        }
                        if ok_regions:
                            await session.execute(
                                update(GameCurrentPrice)
                                .where(
                                    GameCurrentPrice.appid == appid_int,
                                    GameCurrentPrice.region_code.in_(ok_regions),
                                )
                                .values(fail_count=0)
                            )

                        # 欠账结转：门禁降级行计一次失败（递增 + 穷尽转 blocked）。
                        # 常规 upsert 不带 fail_count（避免误清），账本专门走 bump。
                        for region in degraded_regions:
                            await session.execute(
                                update(GameCurrentPrice)
                                .where(
                                    GameCurrentPrice.appid == appid_int,
                                    GameCurrentPrice.region_code == region,
                                )
                                .values(**_missing_ledger_bump())
                            )

                    if history_batch:
                        await session.execute(
                            sqlite_insert(GamePriceHistory).values(history_batch)
                        )

                await session.commit()
                # 复活清标：抓到数据 = 商店健在。removed_at 非空时由榜单
                # 复活通道反哺至此，清除后恢复关注层监控（在 with 外调用，
                # 避免 clear 内层事务与未提交的本事务交叉）
                appid_int = int(game_data.get("appid") or 0)
                if appid_int:
                    await self.clear_removed_mark(appid_int)
                # 种子人工列补挂：新游戏入库即贴上随包维护的
                # xgp/epic/hb/series 标记（种子为空时零开销）
                await seed_assets.apply_curated(appid_int)
                return True

        except Exception as e:
            logger.error("写入失败: %s", e)
            return False

    async def ensure_game_exists(self, appid: int, name: str = "") -> None:
        """确保 games 表有记录（满足外键约束）。"""
        try:
            async with get_session_factory()() as session:
                stmt = sqlite_insert(Game).values(
                    appid=int(appid), name=name,
                    created_at=_naive(get_beijing_time_obj()),
                    updated_at=_naive(get_beijing_time_obj()),
                )
                await session.execute(
                    stmt.on_conflict_do_nothing(index_elements=[Game.appid])
                )
                await session.commit()
            # 回补链路只插行不抓价，人工列在此同步补挂（与主 upsert 同语义）
            await seed_assets.apply_curated(int(appid))
        except Exception as e:
            logger.error("ensure_game_exists 失败: %s", e)

    async def get_known_bundle_ids(self, sub_ids: set[int]) -> set[int]:
        """已入库的 bundle_id（bundle-as-sub 探测的持久缓存，跨重启免重复探测）。"""
        if not sub_ids:
            return set()
        try:
            async with get_session_factory()() as session:
                rows = await session.execute(
                    select(Bundle.bundle_id).where(Bundle.bundle_id.in_(sub_ids))
                )
                return {int(r) for r in rows.scalars()}
        except Exception as e:
            logger.error("查询已知 bundle 失败: %s", e)
            return set()

    async def get_pending_bundle_ids(self, stale_before: datetime) -> list[int]:
        """无区域价的捆绑包（发现桩 / 上次抓取失败），播种给单协程 lane。

        updated_at 早于 stale_before（或 NULL）才播——24h 失败冷却，
        防止下架/锁区的死包每轮空转 42 区超时；成功抓取后有价格行，
        不再入选。
        """
        try:
            async with get_session_factory()() as session:
                has_price = (
                    select(BundleRegionPrice.bundle_id)
                    .where(BundleRegionPrice.bundle_id == Bundle.bundle_id)
                    .exists()
                )
                rows = (
                    await session.execute(
                        select(Bundle.bundle_id).where(
                            ~has_price,
                            or_(
                                Bundle.updated_at.is_(None),
                                Bundle.updated_at < stale_before,
                            ),
                        )
                    )
                ).scalars().all()
                return [int(b) for b in rows]
        except Exception as e:
            logger.error("查询待爬捆绑包失败: %s", e)
            return []

    async def touch_bundle_attempt(self, bundle_id: int) -> None:
        """捆绑包 lane 尝试时间戳（失败也落）：播种冷却判据，防死包每轮空转。"""
        try:
            async with get_session_factory()() as session:
                await session.execute(
                    update(Bundle)
                    .where(Bundle.bundle_id == int(bundle_id))
                    .values(updated_at=_naive(get_beijing_time_obj()))
                )
                await session.commit()
        except Exception as e:
            logger.error("捆绑包尝试时间戳失败 %s: %s", bundle_id, e)

    async def upsert_bundle_candidate(
        self, bundle_id: int, name: str, app_ids: list[int]
    ) -> None:
        """bundle-as-sub 回填 bundles 表（must_purchase_as_set=1 语义）。

        只写 name/app_ids/mps/updated_at 四列——header_image/url 等
        其他来源（PG 导入/页面渠道）的字段不覆盖。
        """
        try:
            async with get_session_factory()() as session:
                stmt = sqlite_insert(Bundle).values(
                    bundle_id=int(bundle_id),
                    name=name or f"Bundle_{bundle_id}",
                    must_purchase_as_set=1,
                    app_ids=[int(a) for a in app_ids],
                    updated_at=_naive(get_beijing_time_obj()),
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[Bundle.bundle_id],
                        set_={
                            "name": stmt.excluded.name,
                            "app_ids": stmt.excluded.app_ids,
                            "must_purchase_as_set": 1,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                )
                await session.commit()
        except Exception as e:
            logger.error("bundle 回填失败 %s: %s", bundle_id, e)

    async def record_bundle_discoveries(self, discoveries: list[dict]) -> int:
        """捆绑包发现桩落库（游戏条目 purchase_options 白送的数据）。

        只 INSERT 库内没有的包（on_conflict_do_nothing）：既有完整主档不
        覆盖——发现渠道只负责「把新包带进门」，价格/封面/appids 由单协程
        lane 播种整区抓取补齐（get_pending_bundle_ids 捞无价包）。
        app_ids 只存本游戏一个 id 当种子，抓取时会被 included_appids 校正。

        返回新插入数（调用方做发现计数）。
        """
        if not discoveries:
            return 0
        try:
            async with get_session_factory()() as session:
                ids = [int(d["bundle_id"]) for d in discoveries]
                known = set(
                    (await session.execute(
                        select(Bundle.bundle_id).where(Bundle.bundle_id.in_(ids))
                    )).scalars()
                )
                fresh = [d for d in discoveries if int(d["bundle_id"]) not in known]
                if not fresh:
                    return 0
                stmt = sqlite_insert(Bundle).values(
                    [
                        {
                            "bundle_id": int(d["bundle_id"]),
                            "name": str(d.get("name") or f"Bundle_{d['bundle_id']}")[:512],
                            "must_purchase_as_set": int(d.get("mps") or 0),
                            "item_kind": int(d.get("item_kind") if d.get("item_kind") is not None else -1),
                            "app_ids": [int(a) for a in (d.get("app_ids") or [])],
                            "updated_at": None,  # 发现桩：播种查询按 NULL 捞
                        }
                        for d in fresh
                    ]
                )
                await session.execute(
                    stmt.on_conflict_do_nothing(index_elements=[Bundle.bundle_id])
                )
                await session.commit()
                return len(fresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("捆绑包发现桩落库失败: %s", e)
            return 0

    async def mark_non_game_type(self, appid: int, app_type: str) -> None:
        """非 game/dlc 短路路径落 type + updated_at（脱离回补池）。

        app_handler 对 type 不在 (game, dlc) 或 coming_soon 无包的
        任务静默丢弃——但挂名孤儿/首爬候选若不落 updated_at 会永留
        回补池（updated_at IS NULL 判据），每天被空转重爬一次。此
        处只写 type/时间戳两列，不碰其他元数据（名字保持挂名值）。
        """
        try:
            async with get_session_factory()() as session:
                stmt = sqlite_insert(Game).values(
                    appid=int(appid),
                    name=f"AppID_{appid}",
                    type=app_type.upper() or None,
                    created_at=_naive(get_beijing_time_obj()),
                    updated_at=_naive(get_beijing_time_obj()),
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[Game.appid],
                        set_={
                            "type": stmt.excluded.type,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                )
                await session.commit()
        except Exception as e:
            logger.error("mark_non_game_type 失败: %s", e)

    async def mark_region_status(self, appid: int, region_code: str, status: str) -> None:
        """标记区域状态（locked/blocked/missing）→ game_current_prices (UPSERT)。

        账本语义（补抓链路）：
        - missing：fail_count 递增（_missing_ledger_bump）；穷尽 MISSING_MAX_RETRIES
          转 blocked（终态）——该区大概率根本无货/无版本，继续重试只是浪费配额
        - locked/blocked：fail_count 清零（非欠账）
        """
        try:
            now_dt = _naive(get_beijing_time_obj())
            async with get_session_factory()() as session:
                if status == "missing":
                    # 首次插入 fail_count=1；已有行则 DO UPDATE 里旧值+1
                    stmt = sqlite_insert(GameCurrentPrice).values(
                        appid=int(appid),
                        region_code=region_code.upper() if region_code else "",
                        currency="",
                        price=None,
                        original_price=None,
                        discount_percent=0,
                        sub_id=None,
                        price_status="missing",
                        fail_count=1,
                        cny_fen=None,
                        updated_at=now_dt,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[GameCurrentPrice.appid, GameCurrentPrice.region_code],
                        set_={
                            **_missing_ledger_bump(),
                            "price": None,
                            "original_price": None,
                            "discount_percent": 0,
                            "cny_fen": None,
                            "updated_at": now_dt,
                        },
                    )
                else:
                    stmt = sqlite_insert(GameCurrentPrice).values(
                        appid=int(appid),
                        region_code=region_code.upper() if region_code else "",
                        currency="",
                        price=None,
                        original_price=None,
                        discount_percent=0,
                        sub_id=None,
                        price_status=status,
                        fail_count=0,
                        cny_fen=None,
                        updated_at=now_dt,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            GameCurrentPrice.appid,
                            GameCurrentPrice.region_code,
                        ],
                        set_={
                            "price_status": stmt.excluded.price_status,
                            "fail_count": 0,
                            "price": None,
                            "original_price": None,
                            "discount_percent": 0,
                            "cny_fen": None,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            logger.error("记录异常状态失败: %s", e)

    async def mark_coming_soon(self, appid: int) -> None:
        """coming_soon 无包（未开放预购/未上线）暂缓语义：type=COMING_SOON 挂名行。

        与 mark_non_game_type 的脱池机制相同（updated_at 落值防回补池空转），
        但 type 单独成类，区别于"永不入库"的 DEMO/MOD 等——重探通道按
        type=COMING_SOON 选候选，开放预购/上线后正常入库覆盖此行。
        只写 type/时间戳，不碰其他元数据（名字保持挂名值）。
        """
        try:
            async with get_session_factory()() as session:
                stmt = sqlite_insert(Game).values(
                    appid=int(appid),
                    name=f"AppID_{appid}",
                    type="COMING_SOON",
                    created_at=_naive(get_beijing_time_obj()),
                    updated_at=_naive(get_beijing_time_obj()),
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[Game.appid],
                        set_={
                            "type": stmt.excluded.type,
                            "updated_at": stmt.excluded.updated_at,
                            # 正常入库路径覆盖过此行（抓到价格）时 removed 可能
                            # 被误标过——暂缓场景一并清复活痕迹
                            "removed_at": None,
                            "removed_strikes": 0,
                        },
                    )
                )
                await session.commit()
        except Exception as e:
            logger.error("mark_coming_soon 失败: %s", e)

    async def bump_removed_strike(self, appid: int) -> bool:
        """全 404 轮记一击；连续 ≥2 击（跨两个价格刷新周期）→ 落 removed_at 终态。

        只对库内已有元数据（updated_at 有值）的行生效——挂名孤儿/COMING_SOON
        无"曾经有数据"事实，不走下架判定（走回补/重探池）。返回是否已转终态。
        """
        try:
            async with get_session_factory()() as session:
                row = await session.get(Game, int(appid))
                if row is None or row.updated_at is None:
                    return False
                row.removed_strikes = (row.removed_strikes or 0) + 1
                if row.removed_strikes >= 2:
                    row.removed_at = _naive(get_beijing_time_obj())
                    logger.info("[下架监控] %s 连续 %d 轮全 404，标记 removed_at",
                                appid, row.removed_strikes)
                await session.commit()
                return row.removed_at is not None
        except Exception as e:
            logger.error("bump_removed_strike 失败: %s", e)
            return False

    async def clear_removed_mark(self, appid: int) -> None:
        """复活清标：upsert_game_and_prices 成功路径调用（抓到数据=商店健在）。

        只清 removed 两列；strikes 归零让下次下架判定重新从零计数。
        """
        try:
            async with get_session_factory()() as session:
                row = await session.get(Game, int(appid))
                if row is not None and (row.removed_at is not None or row.removed_strikes):
                    row.removed_at = None
                    row.removed_strikes = 0
                    await session.commit()
                    logger.info("[下架监控] %s 复活（重新有数据），清除下架标记", appid)
        except Exception as e:
            logger.error("clear_removed_mark 失败: %s", e)

    async def coming_soon_retry_pairs(self, cooldown_days: int, limit: int) -> list[tuple[int, str]]:
        """COMING_SOON 重探候选：updated_at 冷却期满的暂缓行，appid 升序限量。

        判定 type=COMING_SOON 且 updated_at < now - cooldown_days（NULL 视为
        期满——防御历史脏行）。上线/开放预购即由正常入库路径覆盖转正；
        仍未开放则 mark_coming_soon 刷新时间戳重新冷却（低成本空转）。
        """
        try:
            async with get_session_factory()() as session:
                cutoff = _naive(get_beijing_time_obj()) - timedelta(days=cooldown_days)
                rows = (
                    await session.execute(
                        select(Game.appid, Game.name)
                        .where(Game.type == "COMING_SOON")
                        .where(or_(Game.updated_at.is_(None), Game.updated_at < cutoff))
                        .order_by(Game.appid)
                        .limit(limit)
                    )
                ).all()
                return [(int(a), n or "") for a, n in rows]
        except Exception as e:
            logger.error("coming_soon 重探候选查询失败: %s", e)
            return []

    async def generate_missing_tasks(
        self, cooldown_minutes: int = 10, limit_rows: int = 0
    ) -> list[dict]:
        """从欠账账本生成定向补抓任务（按区分组批量：1 任务 = 1 区 × ≤400 appid）。

        选取规则：price_status='missing' 且已过冷却（updated_at < now - cooldown）。
        locked/blocked 不进补抓（终态合理）；missing 连补 MISSING_MAX_RETRIES 次
        仍失败会在 mark_region_status 里转 blocked，不再出现在这里。

        任务形状与主轮一致（type=app，批量 handler 现成消费）：同一区的欠账凑满
        一发再发，请求次数 = 区数 × ⌈行/400⌉，不再逐行一发。limit_rows > 0 时按
        账本年龄从老到新截断——上限挡的是「积压账本一轮吃满挤占关注层」，不是
        请求量（请求量天然被批量压住）。

        Returns:
            [{"type": "app", "id": "{cc}:补{n}", "region": cc,
              "appids": [appid, ...]}, ...]
        """
        cutoff = _naive(get_beijing_time_obj()) - timedelta(minutes=cooldown_minutes)
        tasks: list[dict] = []
        try:
            from .browse_store import DEFAULT_BATCH_SIZE  # 延迟导入：browse_store 反向依赖本模块

            async with get_session_factory()() as session:
                # 下架脱池（宽限期内照常补抓——误判保险期；终态 404 不会
                # 改善，穷尽重试转 blocked 后自然停，无需单独排除逻辑）
                removed = (
                    select(Game.appid).where(Game.removed_at.is_not(None))
                )
                rows = (await session.execute(
                    select(GameCurrentPrice.appid, GameCurrentPrice.region_code)
                    .where(
                        GameCurrentPrice.price_status == "missing",
                        GameCurrentPrice.updated_at < cutoff,
                        ~GameCurrentPrice.appid.in_(removed),
                    )
                    .order_by(GameCurrentPrice.updated_at)
                )).all()
            if limit_rows > 0:
                rows = rows[:limit_rows]
            by_region: dict[str, list[int]] = {}
            for appid, region in rows:
                by_region.setdefault(region.lower(), []).append(int(appid))
            for cc, appids in by_region.items():
                for i, start in enumerate(range(0, len(appids), DEFAULT_BATCH_SIZE), 1):
                    tasks.append(
                        {
                            "type": "app",
                            "id": f"{cc}:补{i}",
                            "region": cc,
                            "appids": appids[start : start + DEFAULT_BATCH_SIZE],
                        }
                    )
        except Exception as e:
            logger.error("生成补抓任务失败: %s", e)
        return tasks

    async def get_discounted_appids(self) -> set[int]:
        """获取所有当前正在打折的 appid。"""
        try:
            async with get_session_factory()() as session:
                rows = await session.execute(
                    select(GameCurrentPrice.appid).where(
                        GameCurrentPrice.discount_percent > 0
                    )
                )
                return {int(r) for r in rows.scalars()}
        except Exception as e:
            logger.error("获取打折 appid 失败: %s", e)
            return set()

    async def get_crawled_appids(self) -> set[int]:
        """已爬过价格的游戏 appid（白名单：ok 且有价的价格行存在）。

        消费方（runner 首爬放行）以"不在白名单"判定未爬——天然覆盖
        games 无行的从未入库形态（TOP100 反哺/手动列表的全新 appid）。
        """
        try:
            async with get_session_factory()() as session:
                rows = await session.execute(
                    select(GameCurrentPrice.appid).where(
                        GameCurrentPrice.price_status == "ok",
                        GameCurrentPrice.price.is_not(None),
                    )
                )
                return {int(r) for r in rows.scalars()}
        except Exception as e:
            logger.error("查询已爬游戏失败: %s", e)
            return set()

    async def get_uncrawled_appids(self) -> set[int]:
        """未爬过价格的游戏 appid（当前库内有行的部分）。

        判定"已爬"= game_current_prices 存在 ok 且有价的价格行；
        两种"无数据"形态都算未爬：
        - games 行 updated_at IS NULL（挂名孤儿：展示层兜底只写
          appid+name，无任何价格/元数据）；
        - games 无行（从未入库）——本集合天然不包含（只查库内行），
          从未入库的 appid 由消费方以"不在已爬集合"兜住。

        预检只能判断"要不要刷新"，不能替代首爬；无价格数据的游戏
        被预检 skip 就是永久漏抓（空库上全新 appid 曾被预检
        "两区均无打折"全 skip，620/570 实证）。
        """
        try:
            async with get_session_factory()() as session:
                rows = await session.execute(
                    select(GameCurrentPrice.appid).where(
                        GameCurrentPrice.price_status == "ok",
                        GameCurrentPrice.price.is_not(None),
                    )
                )
                crawled = {int(r) for r in rows.scalars()}
                # 挂名孤儿（有 games 行但 updated_at NULL）强制算未爬
                orphan_rows = await session.execute(
                    select(Game.appid).where(Game.updated_at.is_(None))
                )
                orphan_ids = {int(r) for r in orphan_rows.scalars()}
                return orphan_ids - crawled
        except Exception as e:
            logger.error("查询未爬游戏失败: %s", e)
            return set()

    async def generate_queue_from_db(self, mode: str = "app") -> list[dict]:
        """按新鲜度/打折状态生成任务队列（当前仅 app 模式；bundle/repair 待迁入）。"""
        if mode != "app":
            raise NotImplementedError(f"mode={mode} 尚未支持（待 bundle/repair 迁入）")

        now_beijing = get_beijing_time_obj()
        cutoff = now_beijing - timedelta(hours=STALE_HOURS)
        tasks: list[dict] = []

        async with get_session_factory()() as session:
            force_all = is_near_steam_refresh(window_minutes=30)
            if force_all:
                logger.info("[队列生成] 检测到 Steam 折扣刷新时间窗口，强制全量更新")
            else:
                logger.info("[队列生成] 查询超过 %d 小时未更新或当前打折的游戏...", STALE_HOURS)

            query = select(Game.appid, Game.name).where(Game.type.in_(["GAME", "DLC"]))
            if not force_all:
                discounted = select(GameCurrentPrice.appid).where(
                    GameCurrentPrice.discount_percent > 0
                )
                query = query.where(
                    or_(
                        Game.updated_at.is_(None),
                        Game.updated_at < cutoff,
                        Game.appid.in_(discounted),
                    )
                )
            query = query.order_by(Game.appid)
            for appid, name in (await session.execute(query)).all():
                tasks.append({"type": "app", "id": int(appid), "name": name or ""})

        logger.info("[队列生成] mode=%s, 共 %d 个任务", mode, len(tasks))
        return tasks
