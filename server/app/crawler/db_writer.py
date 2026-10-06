"""SQLite 落库（SQLAlchemy async）。

核心语义：UPSERT / 标准版选择（每区取 sub_id 最小）/ cny_fen 计算。
bundle / repair 写入方法随对应功能迁入。
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import case, delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core import seed_assets
from app.core.database import (
    WritePriority,
    get_session_factory,
    write_gate,
)
from app.core.logging import log_event
from app.domains.games.models import (
    Bundle,
    Game,
    GameCurrentPrice,
    GamePriceHistory,
)
from app.domains.rates.models import FxRate

from .config import STALE_HOURS
from .utils import get_beijing_time_obj, is_near_steam_refresh

logger = logging.getLogger(__name__)

# 一段事务最多写入的款数：写者位按段交还，交互写可在段间插入
_BATCH_WRITE_CHUNK = 100


@asynccontextmanager
async def _session_scope(session):
    """批量写传入共享 session：不提交、不关闭；未传则开自有 session 并在退出时关闭。"""
    if session is not None:
        yield session
    else:
        async with get_session_factory()() as own:
            yield own


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _ms_since(t0: float) -> int:
    """写账计时：perf_counter 起点至今的毫秒数。"""
    return round((time.perf_counter() - t0) * 1000)


_CURRENT_UPDATABLE = (
    "currency",
    "price",
    "original_price",
    "discount_percent",
    "sub_id",
    "price_status",
    "cny_fen",
    "discount_end_ts",
    "updated_at",
    # 尝试观察三元组：批量成功观察随行盖章（失败路径走 mark_region_status /
    # 账本 bump，不在此列——它们不得触碰 last_success_at）
    "attempt_outcome",
    "steam_answer",
    "last_success_at",
)

# 补抓账本参数：免费游戏 price=0 合法（parse_all_sub_prices is_free 路径），
# 只有 status=ok 且 price=None 才是"成功响应里的坏数据"；连续补抓失败上限，
# 超过即终态化（blocked），防止"该区根本无货"被当成可重试错误死磕。
MISSING_MAX_RETRIES = 5

# 批量 INSERT 的行数分块：SQLite 默认绑定变量上限 32766（15 列史行 ≈ 2184 行），
# 400 款 × 多区多版本的全量史行一次 values() 会越限、整批炸到慢路径
_INSERT_CHUNK_ROWS = 2000


def _is_ok_price_row(price_cents: int | None) -> bool:
    """ok 行价格合法性：None 才是坏数据（0 = 免费游戏，合法）。"""
    return price_cents is not None


# 显式命名的标准版（"Standard Edition"/"Standard Digital Edition"）是本体
# 自身的官方命名，不算版本款；"Standard Edition Four Pack" 等带附加词的
# 礼包 SKU 不在此列，仍按版本款处理
_PLAIN_STANDARD_SUFFIX_RE = re.compile(r"standard( digital)? edition$", re.IGNORECASE)


def _is_plain_standard_suffix(version_suffix: str | None) -> bool:
    if not version_suffix:
        return False
    return bool(_PLAIN_STANDARD_SUFFIX_RE.match(version_suffix.strip()))


# 测试入口命名（测试版/测试服/公测/Playtest 等）→ 不进监控池的判定词形。
# 只认锚定的「测试入口」组合词——裸 beta/测试会误伤标题自带裸词的正经游戏。
_TEST_ENTRY_RE = re.compile(
    r"测试版|测试服|技术测试|封闭测试|公开测试|压力测试|内测|公测|封测|体验版"
    r"|play[\s_-]?test|beta[\s_-]?test|closed[\s_-]?beta|open[\s_-]?beta"
    r"|stress[\s_-]?test|tech[\s_-]?test",
    re.IGNORECASE,
)


def _is_test_entry(name: str | None) -> bool:
    """游戏名命中测试入口词形 → True（打 free_kind='beta'，随免费态一起脱池停爬）。"""
    return bool(name) and bool(_TEST_ENTRY_RE.search(name))


def _classify_free_kind(
    prices_data: list[dict] | None,
) -> tuple[str, int | None] | None:
    """单区价格行 → (free_kind, promo_end_at)。

    - probe = CN ok 行（锚区）；CN 无 ok 行时回退任意区 ok 行。
    - probe price=0：original>0 → promo（限时赠送，结束时间取各行
      promo_end_ts 最大值）；original=0 → f2p（永久免费）。
    - probe price>0（付费）→ (None, None) 显式清标记——**仅当 CN 行在场**：
      各区任务独立分类，区服促销不同步（CN 赠送中、他区正价）时，非 CN
      任务不得抹掉 CN 刚打的 promo 标；CN 行缺席且 probe 付费 → None
      （无信号，调用方不动标记）。
    - 一条 ok 行都没有（纯 locked/missing 响应）→ None：下架/断网不该
      抹掉免费态。
    """
    ok_rows = [
        p
        for p in (prices_data or [])
        if p.get("price_status", "ok") == "ok" and p.get("price") is not None
    ]
    if not ok_rows:
        return None

    def _kind(row: dict) -> str | None:
        if int(row["price"]) != 0:
            return None
        return "promo" if int(row.get("original_price") or 0) > 0 else "f2p"

    cn_rows = [p for p in ok_rows if str(p.get("region_code", "")).upper() == "CN"]
    probe = cn_rows[0] if cn_rows else ok_rows[0]
    kind = _kind(probe)
    if kind is None:
        return (None, None) if cn_rows else None
    if kind != "promo":
        return ("f2p", None)
    ends = [
        int(p["promo_end_ts"])
        for p in ok_rows
        if int(p["price"]) == 0
        and int(p.get("original_price") or 0) > 0
        and p.get("promo_end_ts")
    ]
    return ("promo", max(ends) if ends else None)


def _missing_ledger_bump() -> dict:
    """missing 计账 SET 片段：fail_count 旧值+1，第 MISSING_MAX_RETRIES 次
    失败即转 blocked 终态（"连续 N 次仍失败"语义，含当次）；同时盖尝试
    失败章（attempt_outcome=failed——两条调用方都是失败路径，价格与
    last_success_at 不在此列，由调用方语义决定保留）。

    UPDATE SET 与 UPSERT DO UPDATE SET 中表列均引用旧行值，两处语义一致，
    单一定义避免转移规则漂移。
    """
    return {
        "price_status": case(
            (GameCurrentPrice.fail_count + 1 >= MISSING_MAX_RETRIES, "blocked"),
            else_="missing",
        ),
        "fail_count": GameCurrentPrice.fail_count + 1,
        "attempt_outcome": "failed",
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
            log_event(
                logger,
                f"已加载 {len(self._fx_rates)} 条汇率",
                detail={"数量": len(self._fx_rates)},
            )
        except Exception as e:
            log_event(
                logger,
                "加载汇率失败，将使用兜底汇率",
                tag="降级",
                level=logging.WARNING,
                detail={"原因": e},
            )
            self._fx_rates = {}
        self._fx_rates.setdefault("CNY", 1.0)

    def _compute_cny_fen(self, price_cents: int | None, currency: str) -> int | None:
        """cny_fen = ROUND(price_cents * rate_to_cny)；0 = 免费游戏，合法值。"""
        if price_cents is None:
            return None
        if int(price_cents) == 0:
            return 0
        if not self._fx_rates:
            self._fx_rates = {"CNY": 1.0}
        rate = self._fx_rates.get(currency)
        if rate is None:
            return None
        return round(int(price_cents) * rate)

    @staticmethod
    def _normalize_game_row(game_data: dict, prices_data: list[dict] | None) -> tuple[dict, tuple | None]:
        """games 行归一化 + 免费态/Beta 分类（单款写与批量写共用）。

        返回 (game_values, free_state)：free_state 非空时两键进 upsert set_
        （executemany 下以 excluded 引用逐行携带）；None 时不进——纯
        locked/missing 响应绝不洗掉库内免费态。
        """
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

        free_state = _classify_free_kind(prices_data)
        # 测试入口优先于价格分类：Beta/测试页常挂正价（页面透传本体价），
        # 价格分类判不出免费态，只有命名能定性；'beta' 与 f2p/promo 同走
        # free_kind 非空脱池机制
        if _is_test_entry(str(game_values.get("name") or "")):
            free_state = ("beta", None)
        if free_state is not None:
            game_values["free_kind"], game_values["promo_end_at"] = free_state
        game_values["updated_at"] = now_dt
        return game_values, free_state

    def _plan_price_rows(
        self,
        prices_data: list[dict],
        baseline: tuple[dict, dict],
        now_dt,
        event_key: str | None,
    ) -> tuple[list[dict], list[dict], set[str], set[str]]:
        """价格决策（纯函数，无 IO）：差量门禁 / 版本名回填 / 标准版选择。

        单款写与批量写共用这一份决策——历史差量、欠账结转、现价行形状
        只有这一处定义。返回 (current_rows, history_rows, ok_regions,
        degraded_regions)。活动快照标签由调用方按批求值传入（同一观测
        时刻本就该是同一个 key）。
        """
        known_suffix, latest_snapshots = baseline
        history_batch: list[dict] = []
        standard_candidates: dict[str, list[dict]] = {}
        degraded_regions: set[str] = set()  # 本次门禁降级的区（需欠账结转）

        # ── 历史差量门禁基线：该 appid 每个 (区, sub) 的最新快照。
        #    价格未变不写快照（走势图只需要变化点——线是平的，语义不损）。
        #    比较键含价三件套 + 版本后缀 + gold 标：折扣往返/价格修正/名称
        #    修正都会正常产生新快照。现价表不受门禁影响，照常每轮刷新
        #    （updated_at = 最新验证时刻）。
        # ── 版本名防丢锚（同 sub_id 历史名）：browse 响应的 option name
        #    偶发缺失、档案导入行天生无名——空名行会被「标准版」判据误收：
        #    历史图混入版本价，current 的 min(sub_id) 选择还会让编号更小的
        #    豪华版顶替本体。sub_id 是恒定 SKU，版本名不随时间变：取该
        #    appid 每个 sub 的最新非空名，本次为空的行沿用之。
        for p in prices_data:
            region = p.get("region_code", "").upper()
            price_cents = p.get("price")
            currency = p.get("currency", "")
            is_gold = p.get("is_gold", False)
            version_suffix = p.get("version_suffix")
            # 丢名行沿用同 sub_id 的历史名（known_suffix），与
            # prev 基线同口径——标准版判定 / 差量比较全部一致
            if not version_suffix:
                version_suffix = known_suffix.get(
                    int(p.get("sub_id") or 0)
                ) or None
            is_bundle = p.get("is_bundle", False)
            status = p.get("price_status", "ok")
            # 质量门禁：ok 但无价格 = 成功响应里的坏数据（如 gold 版
            # 无 sub 价格），降级 missing 进账本走补抓自愈
            if status == "ok" and not _is_ok_price_row(price_cents):
                status = "missing"
                degraded_regions.add(region)
            cny_fen = (
                self._compute_cny_fen(price_cents, currency)
                if price_cents is not None
                else None
            )

            # 所有有价格的版本写入 history；永久免费 price=0 不进
            # history（无价格事件），限时赠送 price=0+original>0 是
            # 真实价格事件照写——史低/排序缓存的 cny_fen>0 读侧守卫
            # 天然排除 0 价；相对最新快照无变化的行不写（差量门禁）
            if status == "ok" and price_cents is not None and (
                price_cents > 0 or (p.get("original_price") or 0) > 0
            ):
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
                            "discount_end_ts": p.get("discount_end_ts"),
                            "steam_event_key": event_key,
                            "snapshot_at": now_dt,
                        }
                    )

            # 标准版候选: 非 gold 且非捆绑包，且名字无版本后缀——显式命名的
            # Standard Edition 是本体的官方命名，归一化为标准版候选（否则
            # 真本体被当版本款排除，某区全后缀时只能走兜底）。bundle-as-sub
            # 与标准版无法从价格区分，靠 A4 识别标记隔离
            is_standard = (
                (not is_gold)
                and not is_bundle
                and (
                    not version_suffix
                    or _is_plain_standard_suffix(version_suffix)
                )
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
                        "discount_end_ts": p.get("discount_end_ts"),
                        "updated_at": now_dt,
                        "steam_answer": p.get("steam_answer"),
                    }
                )
        # 每个区域选标准版写入 current：优先有价（ok+price 有值）行——
        # 该区任何版本有价就算有数；全部无价才落 missing 状态行
        current_batch: list[dict] = []
        seen_regions: set[str] = set()
        for region, candidates in standard_candidates.items():
            priced = [c for c in candidates if c["price"] is not None]
            current_batch.append(
                min(priced or candidates, key=lambda x: x.get("sub_id") or 0)
            )
            seen_regions.add(region)

        # 无标准版候选的区域也写入 current：该区有价（非捆绑包）行里取
        # sub_id 最小的确定性兜底——最老 SKU 即本体；曾经按响应顺序取末行，
        # 同一 appid 各区会混装不同版本（如显式命名 Standard Edition +
        # Starter Edition 全后缀场景，本体被当版本款出局后落哪个版本全看
        # 响应序）。全部无价才落状态行，同区多行降级时以最后一条为准
        fallback_rows: dict[str, list[dict]] = {}
        fallback_order: list[str] = []
        for p in prices_data:
            region = p.get("region_code", "").upper()
            if region in seen_regions:
                continue
            if region not in fallback_rows:
                fallback_rows[region] = []
                fallback_order.append(region)
            fallback_rows[region].append(p)
        for region in fallback_order:
            rows = fallback_rows[region]
            priced = [
                p
                for p in rows
                if p.get("price_status", "ok") == "ok"
                and _is_ok_price_row(p.get("price"))
                and not p.get("is_bundle", False)
            ]
            p = (
                min(priced, key=lambda x: x.get("sub_id") or 0)
                if priced
                else rows[-1]
            )
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
                    if price_cents is not None
                    else None,
                    "discount_end_ts": p.get("discount_end_ts"),
                    "updated_at": now_dt,
                    "steam_answer": p.get("steam_answer"),
                }
            )

        # 尝试观察盖章：批量写 = 成功观察（拿到了 Steam 对该区的明确答复，
        # 含 locked / 无购买选项）。answer 缺省时按状态推导（导入 / 旧调用
        # 方路径不带显式 answer）。
        for row in current_batch:
            row["attempt_outcome"] = "success"
            row["last_success_at"] = row["updated_at"]
            if not row.get("steam_answer"):
                status = row["price_status"]
                row["steam_answer"] = (
                    "locked"
                    if status == "locked"
                    else "no_options"
                    if status in ("missing", "blocked")
                    else "free" if row.get("price") == 0 else "ok"
                )

        ok_regions = {
            row["region_code"] for row in current_batch if row["price_status"] == "ok"
        }
        return current_batch, history_batch, ok_regions, degraded_regions

    async def upsert_game_and_prices(
        self,
        game_data: dict,
        prices_data: list[dict] | None,
        *,
        baseline: tuple[dict, dict] | None = None,
        session=None,
        commit: bool = True,
    ) -> bool:
        """写入游戏元数据 + 区域价格（默认单款独占事务）。

        传入共享 session 与已取好的基线时（批量写慢路径的用法）本方法不
        提交、不关闭 session，提交与收尾由批量入口统一负责。价格决策走
        `_plan_price_rows`（与批量快路径同一份）。
        """
        try:
            game_values, free_state = self._normalize_game_row(game_data, prices_data)
            now_dt = game_values["updated_at"]

            # 单款独占事务过写调度器；批量慢路径传入共享 session 时按重入放行
            async with (
                write_gate(WritePriority.BACKGROUND),
                _session_scope(session) as session,
            ):
                if baseline is None:
                    baseline = await self._load_baseline(
                        session, int(game_data.get("appid") or 0)
                    )
                insert_game = sqlite_insert(Game).values(**game_values)
                upsert_set = {
                    c: getattr(insert_game.excluded, c)
                    for c in (
                        "name", "name_en", "type", "header_image", "store_url",
                        "chinese_support", "family_sharing", "trading_cards",
                        "release_date", "is_adult", "is_visual_novel",
                        "developers", "publishers",
                        "positive_rate", "positive_reviews", "review_count",
                        "updated_at",
                    )
                }
                if free_state is not None:
                    # 字面量而非 excluded：无 ok 行信号时两键不进 set_——
                    # 纯 locked/missing 响应绝不洗掉库内免费态
                    upsert_set["free_kind"] = free_state[0]
                    upsert_set["promo_end_at"] = free_state[1]
                upsert_game = insert_game.on_conflict_do_update(
                    index_elements=[Game.appid],
                    set_=upsert_set,
                )
                await session.execute(upsert_game)

                if prices_data:
                    from app.domains.steam_events import (
                        service as steam_events_service,
                    )

                    current_batch, history_batch, ok_regions, degraded_regions = (
                        self._plan_price_rows(
                            prices_data,
                            baseline,
                            now_dt,
                            await steam_events_service.active_event_key_at(now_dt),
                        )
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
                        appid_int = int(game_data.get("appid") or 0)
                        if ok_regions and appid_int:
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

                if not commit:
                    return True
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
            log_event(logger, "游戏价格写入失败", level=logging.ERROR, detail={"原因": e})
            return False

    async def _load_baseline(self, session, appid: int):
        """单 appid 的最新快照基线（版本名回填 + 差量门禁 prev）。

        读必须与写同一 session：新开连接在测试的库上读不到未提交/另一端写入。
        """
        known, latest = await self._query_baselines(session, [appid])
        return known.get(appid, {}), latest.get(appid, {})

    async def _query_baselines(self, session, appids: list[int]):
        """批量取基线：每个 (appid, region) 只取最新一趟快照。

        按 ux_gph_snapshot 的 (appid, region_code, snapshot_at) 前缀做组内
        MAX 直取顶趟快照，免掉窗口函数对整段历史的排序——这条查询每批写库
        前各跑一次，是写路径上最大的固定开销。一趟快照的行同时派生版本名
        基线（known）：子版本名与地区无关，顶趟快照里出现的 (appid, sub_id)
        后缀即当前基线；已从最新一趟消失的 sub 不再留旧值当 prev。
        """
        known: dict[int, dict[int, str]] = {}
        latest: dict[int, dict[tuple[str, int], tuple]] = {}
        ids = [int(a) for a in dict.fromkeys(appids) if a]
        for chunk in _chunks(ids, 400):
            params = {f"a{i}": v for i, v in enumerate(chunk)}
            placeholders = ",".join(f":a{i}" for i in range(len(chunk)))
            rows = await session.execute(
                text(
                    "SELECT h.appid, h.region_code, h.sub_id, h.price, "
                    "h.original_price, h.discount_percent, h.version_suffix, "
                    "h.is_gold FROM game_price_history h JOIN ("
                    "  SELECT appid, region_code, MAX(snapshot_at) AS ms"
                    "  FROM game_price_history"
                    f"  WHERE appid IN ({placeholders}) GROUP BY appid, region_code"
                    ") t ON h.appid = t.appid AND h.region_code = t.region_code"
                    " AND h.snapshot_at = t.ms"
                ),
                params,
            )
            for r in rows:
                aid = int(r[0])
                sub_id_int = int(r[2] or 0)
                suffix = r[6] or None
                latest.setdefault(aid, {})[(r[1], sub_id_int)] = (
                    r[3], r[4], r[5], suffix, bool(r[7]),
                )
                if suffix:
                    known.setdefault(aid, {})[sub_id_int] = str(suffix)
        return known, latest

    async def upsert_task_batch(
        self, entries: list[tuple[dict, list[dict] | None]]
    ) -> list[bool]:
        """一区一批（≤400 款）写完：拆 ~100 款的段逐段过写调度器提交。

        每段独立事务（基线一次 + 决策纯函数化 + 四条批量语句 games / current /
        欠账结转 / history），段与段之间写者位空出——交互写（设置/钱包/账号）
        无需等整批写完。决策逻辑与单款写共用 `_plan_price_rows`；段内任意语句
        失败整段回退 `_upsert_task_batch_slow` 逐款隔离写（慢 ~10 倍，只兜坏段）。
        """
        results = [True] * len(entries)
        if not entries:
            return results
        for start in range(0, len(entries), _BATCH_WRITE_CHUNK):
            chunk = entries[start : start + _BATCH_WRITE_CHUNK]
            results[start : start + len(chunk)] = await self._write_task_chunk(chunk)
        return results

    async def _write_task_chunk(
        self, entries: list[tuple[dict, list[dict] | None]]
    ) -> list[bool]:
        """单段写入（≤_BATCH_WRITE_CHUNK 款）一次事务写完。"""
        results = [True] * len(entries)
        if not entries:
            return results
        _t0 = time.perf_counter()
        # 写调度器在取连接之前：排队等写者位的段不占连接池，读请求不被写侧挤占
        async with write_gate(WritePriority.BACKGROUND, label="price_batch"):
            try:
                async with get_session_factory()() as session:
                    _tc = time.perf_counter()
                    await session.connection()
                    pool_ms = _ms_since(_tc)
                    _tb = time.perf_counter()
                    known, latest = await self._query_baselines(
                        session, [int(g.get("appid") or 0) for g, _ in entries]
                    )
                    baseline_ms = _ms_since(_tb)
                    from app.domains.steam_events import (
                        service as steam_events_service,
                    )

                    event_keys: dict = {}
                    game_groups: dict[bool, list[dict]] = {True: [], False: []}
                    plans: list[tuple[int, dict, list, list, set, set]] = []
                    for i, (game_data, prices_data) in enumerate(entries):
                        aid = int(game_data.get("appid") or 0)
                        game_values, _ = self._normalize_game_row(game_data, prices_data)
                        # free_state 键是否存在（而非值真假）决定 upsert set_ 键集：
                        # (None, None) 是「付费显式清免费标记」信号，键在值空——
                        # 两类行混进同一 executemany 会因键集不均触发编译错误
                        game_groups["free_kind" in game_values].append(game_values)
                        now_dt = game_values["updated_at"]
                        if now_dt not in event_keys:
                            event_keys[now_dt] = (
                                await steam_events_service.active_event_key_at(now_dt)
                            )
                        plans.append(
                            (i, aid, prices_data, now_dt, event_keys[now_dt], (known.get(aid, {}), latest.get(aid, {})))
                        )

                    insert_game = sqlite_insert(Game)
                    game_ms = lockwait_ms = current_ms = ledger_ms = history_ms = 0
                    commit_ms = 0
                    base_set = {
                        c: getattr(insert_game.excluded, c)
                        for c in (
                            "name", "name_en", "type", "header_image", "store_url",
                            "chinese_support", "family_sharing", "trading_cards",
                            "release_date", "is_adult", "is_visual_novel",
                            "developers", "publishers",
                            "positive_rate", "positive_reviews", "review_count",
                            "updated_at",
                        )
                    }
                    for has_free, rows in game_groups.items():
                        if not rows:
                            continue
                        # excluded 必须取自带 values 的同一语句对象：跨对象引用
                        # 会把 set_ 渲染成裸绑定参数（编译期即拒）
                        stmt = insert_game.values(rows)
                        upsert_set = {
                            c: getattr(stmt.excluded, c)
                            for c in (
                                "name", "name_en", "type", "header_image", "store_url",
                                "chinese_support", "family_sharing", "trading_cards",
                                "release_date", "is_adult", "is_visual_novel",
                                "developers", "publishers",
                                "positive_rate", "positive_reviews", "review_count",
                                "updated_at",
                            )
                        }
                        if has_free:
                            upsert_set["free_kind"] = stmt.excluded.free_kind
                            upsert_set["promo_end_at"] = stmt.excluded.promo_end_at
                        _tg = time.perf_counter()
                        await session.execute(
                            stmt.on_conflict_do_update(
                                index_elements=[Game.appid],
                                set_=upsert_set,
                            )
                        )
                        _gd = _ms_since(_tg)
                        game_ms += _gd
                        # SQLite 写锁在事务首条写语句处获取：首写耗时含等锁时长，
                        # 即 lockwait 的观测口径；commit 耗时含 WAL EXCLUSIVE 段
                        if not lockwait_ms:
                            lockwait_ms = _gd

                    all_current: list[dict] = []
                    all_history: list[dict] = []
                    ok_groups: dict[frozenset, list[int]] = {}
                    degraded_groups: dict[frozenset, list[int]] = {}
                    for i, aid, prices_data, now_dt, event_key, baseline in plans:
                        if not prices_data:
                            continue
                        current_rows, history_rows, ok_regions, degraded_regions = (
                            self._plan_price_rows(
                                prices_data, baseline, now_dt, event_key
                            )
                        )
                        all_current.extend(current_rows)
                        all_history.extend(history_rows)
                        if ok_regions:
                            ok_groups.setdefault(frozenset(ok_regions), []).append(aid)
                        if degraded_regions:
                            degraded_groups.setdefault(frozenset(degraded_regions), []).append(aid)

                    if all_current:
                        insert_cp = sqlite_insert(GameCurrentPrice)
                        _tp = time.perf_counter()
                        for chunk in _chunks(all_current, _INSERT_CHUNK_ROWS):
                            await session.execute(
                                insert_cp.values(chunk).on_conflict_do_update(
                                    index_elements=[
                                        GameCurrentPrice.appid,
                                        GameCurrentPrice.region_code,
                                    ],
                                    set_={c: getattr(insert_cp.excluded, c) for c in _CURRENT_UPDATABLE},
                                )
                            )
                        current_ms = _ms_since(_tp)
                        # 补抓成功结转清账：本批有 ok 价的区 fail_count 归零
                        # （missing → 补抓成功 → 回 ok 的欠账闭环）；按「区集合相同
                        # 的款」分组成 IN 更新，避免 appid × 区的笛卡尔积误清
                        for regions, aids in ok_groups.items():
                            _tl = time.perf_counter()
                            await session.execute(
                                update(GameCurrentPrice)
                                .where(
                                    GameCurrentPrice.appid.in_(aids),
                                    GameCurrentPrice.region_code.in_(regions),
                                )
                                .values(fail_count=0)
                            )
                            ledger_ms += _ms_since(_tl)
                        # 欠账结转：门禁降级行计一次失败（递增 + 穷尽转 blocked）。
                        # 常规 upsert 不带 fail_count（避免误清），账本专门走 bump。
                        for regions, aids in degraded_groups.items():
                            _tl = time.perf_counter()
                            await session.execute(
                                update(GameCurrentPrice)
                                .where(
                                    GameCurrentPrice.appid.in_(aids),
                                    GameCurrentPrice.region_code.in_(regions),
                                )
                                .values(**_missing_ledger_bump())
                            )
                            ledger_ms += _ms_since(_tl)

                    if all_history:
                        _th = time.perf_counter()
                        for chunk in _chunks(all_history, _INSERT_CHUNK_ROWS):
                            await session.execute(
                                sqlite_insert(GamePriceHistory).values(chunk)
                            )
                        history_ms = _ms_since(_th)
                    _tm = time.perf_counter()
                    await session.commit()
                    commit_ms = _ms_since(_tm)
            except Exception:
                log_event(
                    logger,
                    f"批量写快路径失败，整段回退为逐款隔离写（{len(entries)} 款）",
                    tag="降级",
                    level=logging.ERROR,
                    exc_info=True,
                    detail={"款数": len(entries)},
                )
                return await self._upsert_task_batch_slow(entries)
        # 收尾（复活清标 / 种子补挂）在写者位之外执行：各自过调度器的小事务，
        # 交互写可在段间插入；收尾不叠在未提交事务里
        ok_ids = [
            int(game_data.get("appid") or 0)
            for i, (game_data, _) in enumerate(entries)
            if results[i] and int(game_data.get("appid") or 0)
        ]
        _tp = time.perf_counter()
        await self.clear_removed_marks_batch(ok_ids)
        # 种子人工列补挂：单闸分块 executemany 覆盖整段 ok_ids
        try:
            await seed_assets.apply_curated_batch(ok_ids)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "批量写入收尾失败：种子标记批量补挂异常",
                level=logging.WARNING,
                detail={"原因": e},
            )
        post_ms = _ms_since(_tp)
        log_event(
            logger,
            f"写库分段完成：{len(entries)} 款，共耗时 {_ms_since(_t0)} 毫秒",
            detail={
                "款数": len(entries),
                "基线查询": f"{baseline_ms}毫秒",
                "取连接": f"{pool_ms}毫秒",
                "游戏元数据": f"{game_ms}毫秒",
                "现价": f"{current_ms}毫秒",
                "欠账结转": f"{ledger_ms}毫秒",
                "历史": f"{history_ms}毫秒",
                "提交": f"{commit_ms}毫秒",
                "收尾": f"{post_ms}毫秒",
                "等写锁": f"{lockwait_ms}毫秒",
                "总计": f"{_ms_since(_t0)}毫秒",
            },
        )
        return results

    async def _upsert_task_batch_slow(
        self, entries: list[tuple[dict, list[dict] | None]]
    ) -> list[bool]:
        """逐款隔离写（回退路径）：按 _BATCH_WRITE_CHUNK 分段，每段一闸
        一会话——段内一整批基线一次 + 单款 SAVEPOINT，单款失败不拖垮整段；
        **段间交还写者位**（兜底路径慢 ~10 倍，整批持闸会把交互写饿到
        超时）。快路径的任何段级异常都退到这里；段级异常只作废本段。"""
        results = [False] * len(entries)
        if not entries:
            return results
        _t0 = time.perf_counter()
        baseline_ms = pool_ms = savepoint_ms = apps_ms = commit_ms = 0
        for start in range(0, len(entries), _BATCH_WRITE_CHUNK):
            chunk = entries[start : start + _BATCH_WRITE_CHUNK]
            async with write_gate(
                WritePriority.BACKGROUND, label="price_batch_slow"
            ):
                try:
                    async with get_session_factory()() as session:
                        _tc = time.perf_counter()
                        await session.connection()
                        pool_ms += _ms_since(_tc)
                        _tb = time.perf_counter()
                        known, latest = await self._query_baselines(
                            session, [int(g.get("appid") or 0) for g, _ in chunk]
                        )
                        baseline_ms += _ms_since(_tb)
                        for i_in_chunk, (game_data, prices_data) in enumerate(chunk):
                            i = start + i_in_chunk
                            aid = int(game_data.get("appid") or 0)
                            _ts = time.perf_counter()
                            sp = await session.begin_nested()
                            savepoint_ms += _ms_since(_ts)
                            _ta = time.perf_counter()
                            try:
                                ok = await self.upsert_game_and_prices(
                                    game_data,
                                    prices_data,
                                    baseline=(known.get(aid, {}), latest.get(aid, {})),
                                    session=session,
                                    commit=False,
                                )
                            except Exception as e:  # noqa: BLE001 —— 单款失败不拖垮整段
                                log_event(
                                    logger,
                                    f"批量写入时单款失败：游戏 {aid}",
                                    level=logging.ERROR,
                                    detail={"游戏": aid, "原因": e},
                                )
                                ok = False
                            apps_ms += _ms_since(_ta)
                            if ok:
                                await sp.commit()
                            else:
                                await sp.rollback()
                            results[i] = ok
                        _tm = time.perf_counter()
                        await session.commit()
                        commit_ms += _ms_since(_tm)
                except Exception as e:
                    # 段级失败只作废本段（其余段照常），与主路径段语义对齐
                    log_event(
                        logger,
                        "逐款隔离写段失败",
                        level=logging.ERROR,
                        detail={"原因": e, "起始款序": start},
                    )
        # 收尾（复活清标 / 种子补挂）在写者位之外执行，各自过调度器
        ok_ids = [
            int(game_data.get("appid") or 0)
            for i, (game_data, _) in enumerate(entries)
            if results[i] and int(game_data.get("appid") or 0)
        ]
        _tp = time.perf_counter()
        await self.clear_removed_marks_batch(ok_ids)
        # 种子人工列补挂：单闸分块 executemany 覆盖整段 ok_ids
        try:
            await seed_assets.apply_curated_batch(ok_ids)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "批量写入收尾失败：种子标记批量补挂异常",
                level=logging.WARNING,
                detail={"原因": e},
            )
        post_ms = _ms_since(_tp)
        log_event(
            logger,
            f"逐款隔离写完成：{len(entries)} 款，共耗时 {_ms_since(_t0)} 毫秒",
            detail={
                "款数": len(entries),
                "基线查询": f"{baseline_ms}毫秒",
                "取连接": f"{pool_ms}毫秒",
                "保存点": f"{savepoint_ms}毫秒",
                "逐款写入": f"{apps_ms}毫秒",
                "提交": f"{commit_ms}毫秒",
                "收尾": f"{post_ms}毫秒",
                "总计": f"{_ms_since(_t0)}毫秒",
            },
        )
        return results

    async def ensure_game_exists(self, appid: int, name: str = "") -> None:
        """确保 games 表有记录（满足外键约束）。"""
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            log_event(logger, "补建游戏占位行失败", level=logging.ERROR, detail={"原因": e})

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
            log_event(logger, "查询已知捆绑包失败", level=logging.ERROR, detail={"原因": e})
            return set()

    async def upsert_bundle_candidate(
        self, bundle_id: int, name: str, app_ids: list[int]
    ) -> None:
        """bundle-as-sub 回填 bundles 表（must_purchase_as_set=1 语义）。

        只写 name/app_ids/mps/updated_at 四列——header_image/url 等
        其他来源（PG 导入/页面渠道）的字段不覆盖。
        """
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            log_event(
                logger,
                f"回填捆绑包 {bundle_id} 失败",
                level=logging.ERROR,
                detail={"捆绑包": bundle_id, "原因": e},
            )

    async def record_bundle_discoveries(self, discoveries: list[dict]) -> int:
        """捆绑包发现桩落库（游戏条目 purchase_options 白送的数据）。

        只 INSERT 库内没有的包（on_conflict_do_nothing）：既有完整主档不
        覆盖——发现渠道只负责「把新包带进门」，无价桩不随轮刷新，用户
        关注/导入该包时才整区抓取补价（bundles.refresh_bundles 只刷
        监控层在册的包）。
        app_ids 只存本游戏一个 id 当种子，抓取时会被 included_appids 校正。

        返回新插入数（调用方做发现计数）。
        """
        if not discoveries:
            return 0
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            log_event(
                logger,
                "捆绑包发现桩落库失败",
                level=logging.WARNING,
                detail={"原因": e},
            )
            return 0

    async def mark_non_game_type(self, appid: int, app_type: str) -> None:
        """非 game/dlc 短路路径落 type + updated_at（脱离回补池）。

        app_handler 对 type 不在 (game, dlc) 或 coming_soon 无包的
        任务静默丢弃——但挂名孤儿/首爬候选若不落 updated_at 会永留
        回补池（updated_at IS NULL 判据），每天被空转重爬一次。此
        处只写 type/时间戳两列，不碰其他元数据（名字保持挂名值）。
        """
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            log_event(logger, "标记非游戏类型失败", level=logging.ERROR, detail={"原因": e})

    async def mark_region_status(self, appid: int, region_code: str, status: str) -> None:
        """标记区域状态（locked/blocked/missing）→ game_current_prices (UPSERT)。

        尝试观察语义：missing = 本次尝试失败（传输/写库类），行上保留上一次
        成功的价格与 last_success_at——「这次没拿到」不推翻「曾经拿到」；
        locked = 成功观察（Steam 明确答复该区不售）。price_status 仍按旧口径
        投影（missing/blocked 计欠账、locked 终态），供过渡期读取方兼容。
        账本语义（补抓链路）：
        - missing：fail_count 递增（_missing_ledger_bump）；穷尽 MISSING_MAX_RETRIES
          转 blocked（终态）——该区大概率根本无货/无版本，继续重试只是浪费配额
        - locked/blocked：fail_count 清零（非欠账）
        """
        try:
            now_dt = _naive(get_beijing_time_obj())
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
                if status == "missing":
                    # 首次插入 fail_count=1；已有行则 DO UPDATE 里旧值+1。
                    # 价格三件套不在 SET 里：失败保留 last good price
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
                        attempt_outcome="failed",
                        last_success_at=None,
                        updated_at=now_dt,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[GameCurrentPrice.appid, GameCurrentPrice.region_code],
                        set_={
                            **_missing_ledger_bump(),
                            "attempt_outcome": "failed",
                            "steam_answer": None,
                            "updated_at": now_dt,
                        },
                    )
                else:
                    outcome = "success" if status == "locked" else "failed"
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
                        attempt_outcome=outcome,
                        steam_answer="locked" if status == "locked" else None,
                        last_success_at=now_dt if status == "locked" else None,
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
                            "attempt_outcome": outcome,
                            "steam_answer": stmt.excluded.steam_answer,
                            "last_success_at": stmt.excluded.last_success_at,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            log_event(logger, "记录区域异常状态失败", level=logging.ERROR, detail={"原因": e})

    async def clear_missing_regions(self, pairs: list[tuple[int, str]]) -> int:
        """批量清除误标的 missing 现价行（只删 missing 状态行，历史不动）。

        处理器对目标「看见了但不落价」的判定（非游戏类型、未发售、元数据
        与库内原值双缺）意味着该区当前不存在可写的价格事实——批量失败整批
        记 missing 时误标的行若不清，会成为补抓通道每拍重拾、永不收敛的
        死账——每拍重拾、永不收敛。删除后回到「未观测」态：黄框消失、补抓不再
        拾取，主轮/重探在事实变化后（发售/上线）自然写出真实状态行。
        """
        if not pairs:
            return 0
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
                result = await session.execute(
                    delete(GameCurrentPrice).where(
                        or_(
                            *[
                                (GameCurrentPrice.appid == int(appid))
                                & (GameCurrentPrice.region_code == cc.upper())
                                for appid, cc in pairs
                            ]
                        ),
                        GameCurrentPrice.price_status == "missing",
                    )
                )
                await session.commit()
                return int(result.rowcount or 0)
        except Exception as e:
            log_event(
                logger,
                "清除误标的缺失数据行失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
            return 0

    async def mark_coming_soon(self, appid: int) -> None:
        """coming_soon 无包（未开放预购/未上线）暂缓语义：type=COMING_SOON 挂名行。

        与 mark_non_game_type 的脱池机制相同（updated_at 落值防回补池空转），
        但 type 单独成类，区别于"永不入库"的 DEMO/MOD 等——重探通道按
        type=COMING_SOON 选候选，开放预购/上线后正常入库覆盖此行。
        只写 type/时间戳，不碰其他元数据（名字保持挂名值）。
        """
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            log_event(logger, "标记即将推出暂缓失败", level=logging.ERROR, detail={"原因": e})

    async def bump_removed_strike(self, appid: int) -> bool:
        """全 404 轮记一击；连续 ≥2 击（跨两个价格刷新周期）→ 落 removed_at 终态。

        只对库内已有元数据（updated_at 有值）的行生效——挂名孤儿/COMING_SOON
        无"曾经有数据"事实，不走下架判定（走回补/重探池）。返回是否已转终态。
        """
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
                row = await session.get(Game, int(appid))
                if row is None or row.updated_at is None:
                    return False
                row.removed_strikes = (row.removed_strikes or 0) + 1
                if row.removed_strikes >= 2:
                    row.removed_at = _naive(get_beijing_time_obj())
                    log_event(
                        logger,
                        f"游戏 {appid} 连续 {row.removed_strikes} 轮全部 404，已标记为下架",
                        detail={"游戏": appid, "连续轮数": row.removed_strikes},
                    )
                await session.commit()
                return row.removed_at is not None
        except Exception as e:
            log_event(
                logger,
                "记录下架判定击数失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
            return False

    async def clear_removed_mark(self, appid: int) -> None:
        """复活清标：upsert_game_and_prices 成功路径调用（抓到数据=商店健在）。

        只清 removed 两列；strikes 归零让下次下架判定重新从零计数。
        """
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
                row = await session.get(Game, int(appid))
                if row is not None and (row.removed_at is not None or row.removed_strikes):
                    row.removed_at = None
                    row.removed_strikes = 0
                    await session.commit()
                    log_event(
                        logger,
                        f"游戏 {appid} 重新有数据，已清除下架标记",
                        tag="已修复",
                        detail={"游戏": appid},
                    )
        except Exception as e:
            log_event(
                logger,
                "清除下架标记失败",
                level=logging.ERROR,
                detail={"原因": e},
            )

    async def clear_removed_marks_batch(self, appids: list[int]) -> int:
        """批量复活清标（爬虫批量路径）：整批一次 SELECT + 一次 UPDATE。

        单款版每批要开 400 个会话逐款主键读；此处只碰真正带标记的行，
        返回清标数。
        """
        ids = [int(a) for a in dict.fromkeys(appids) if a]
        if not ids:
            return 0
        try:
            async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
                rows = (
                    await session.execute(
                        select(Game.appid).where(
                            Game.appid.in_(ids),
                            or_(
                                Game.removed_at.is_not(None),
                                Game.removed_strikes != 0,
                            ),
                        )
                    )
                ).scalars().all()
                if not rows:
                    return 0
                await session.execute(
                    update(Game)
                    .where(Game.appid.in_(rows))
                    .values(removed_at=None, removed_strikes=0)
                )
                await session.commit()
                log_event(
                    logger,
                    f"{len(rows)} 款游戏重新有数据，已清除下架标记",
                    tag="已修复",
                    detail={"数量": len(rows)},
                )
                return len(rows)
        except Exception as e:
            log_event(
                logger,
                "批量清除下架标记失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
            return 0

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
            log_event(
                logger,
                "查询即将推出重探候选失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
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
                # 不按业务状态排除欠账：下架/免费态的 missing 行若被排除，
                # 而它们同样不在 pool/catalog 的爬取集合里——账本行将永远
                # 无消费路径的死账。可补抓性交给补抓本身：真不可
                # 得的区由逐项判定重新记账并走 fail_count 穷尽转 blocked。
                rows = (await session.execute(
                    select(GameCurrentPrice.appid, GameCurrentPrice.region_code)
                    .where(
                        GameCurrentPrice.price_status == "missing",
                        GameCurrentPrice.updated_at < cutoff,
                    )
                    .order_by(GameCurrentPrice.updated_at)
                )).all()
                # 空区行是「全球不可见」的尝试痕迹（无对应真实区服），发给
                # Steam 的 country_code 会是空串，一发必 400，且永远没有
                # bump fail_count 的路径——留在账本只会永久占位，即拾即清。
                stale_empty = [int(appid) for appid, region in rows if not region]
                if stale_empty:
                    async with write_gate(WritePriority.BACKGROUND):
                        await session.execute(
                            delete(GameCurrentPrice).where(
                                GameCurrentPrice.appid.in_(stale_empty),
                                GameCurrentPrice.region_code == "",
                                GameCurrentPrice.price_status == "missing",
                            )
                        )
                        await session.commit()
                    log_event(
                        logger,
                        f"已清除 {len(stale_empty)} 行空区的补抓尝试痕迹",
                        detail={"行数": len(stale_empty)},
                    )
            if limit_rows > 0:
                rows = rows[:limit_rows]
            by_region: dict[str, list[int]] = {}
            for appid, region in rows:
                if not region:
                    continue
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
            log_event(
                logger,
                "生成缺失数据补抓任务失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
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
            log_event(logger, "查询正在打折的游戏失败", level=logging.ERROR, detail={"原因": e})
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
            log_event(
                logger,
                "查询已爬取价格的游戏失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
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
        被预检 skip 就是永久漏抓（空库上全新 appid 会被预检
        "两区均无打折"全 skip，如 620/570）。
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
            log_event(
                logger,
                "查询未爬取价格的游戏失败",
                level=logging.ERROR,
                detail={"原因": e},
            )
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
                log_event(logger, "检测到 Steam 折扣刷新时间窗口，本轮改为全量更新")
            else:
                log_event(
                    logger,
                    f"按超过 {STALE_HOURS} 小时未更新或当前正在打折的条件生成爬取队列",
                    detail={"过期小时": STALE_HOURS},
                )

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

        log_event(
            logger,
            f"爬取队列生成完成，共 {len(tasks)} 个任务",
            detail={"模式": mode, "任务数": len(tasks)},
        )
        return tasks
