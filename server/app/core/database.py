"""SQLAlchemy async 引擎与会话（SQLite，WAL + foreign_keys）。

schema 演进双层机制：
- **零登记层**（日常）：新表走模型 + create_all；新列/新索引登记
  `_TABLE_EXTRA_COLUMNS` / `_TABLE_EXTRA_INDEXES`，启动幂等执行——
  老库开新版本自动补齐，无需版本号参与。
- **迁移链层**（后门）：数据回填 / 列拆并 / 表重建这类 create_all 与
  ALTER 都覆盖不了的结构性变更，登记 `_MIGRATIONS`（按 SCHEMA_VERSION
  升序）。`PRAGMA user_version` 记录库结构版本号，逐个执行差额迁移，
  每步成功即落版本号——中断可续跑，天然幂等。

版本号约定：从 1 起；迁移链新增一步 → SCHEMA_VERSION +1；已发布的
迁移步骤**永不变更/删除**（用户库可能停在任意版本，只能追加）。
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """全部域模型的声明基类。"""


# ── 轻量 schema 迁移（统一标准）─────────────────────────────
# create_all 不会给已有表加列/索引；这里按表声明增量 DDL，启动幂等执行。
# 新列/新索引一律在此登记，保持单一来源。
_TABLE_EXTRA_COLUMNS: dict[str, dict[str, str]] = {    "games": {
        "xgp_tier": "VARCHAR(50)",
        "is_epic": "BOOLEAN DEFAULT 0",
        "epic_date": "VARCHAR(100)",
        "is_hb": "BOOLEAN DEFAULT 0",
        "hb_data": "VARCHAR(100)",
        # 第三方渠道 bundle 计数（NULL=未拉取）
        "bundle_count": "INTEGER",
        "series_id": "VARCHAR(100)",
        "hl_flag": "INTEGER DEFAULT 0",
        "pp_flag": "INTEGER DEFAULT 0",
        # 最近一次原价跳变时刻（永降/永涨徽章 14 天时效判据）
        "pp_changed_at": "DATETIME",
        "is_adult": "BOOLEAN DEFAULT 0",
        "is_visual_novel": "BOOLEAN DEFAULT 0",
        # 排序缓存预计算列（对齐 mv_game_sort_cache）
        "min_cny_fen": "BIGINT",
        "diff_fen": "INTEGER DEFAULT 0",
        # smart 排序评分（refresh_sort_cache 维护；NULL=未计算）
        "smart_score": "REAL",
        # 商店移除监控：下架判定时间戳 + 连续全 404 轮数
        "removed_at": "DATETIME",
        "removed_strikes": "INTEGER DEFAULT 0",
        # 免费态：NULL=付费 / f2p=永久免费 / promo=限时赠送中；promo_end_at=结束 Unix 秒
        "free_kind": "VARCHAR(10)",
        "promo_end_at": "BIGINT",
    },
    "wishlist_items": {
        "owned": "BOOLEAN DEFAULT 0",
        # 星标关注标记：同步停用核对免疫；与愿望单同属爬取第一优先级
        "manual": "BOOLEAN DEFAULT 0",
        # 愿望单成员标记：爬取第一优先级；反向核对时随成员资格清零
        "wishlisted": "BOOLEAN DEFAULT 0",
        # 手动加入监控池（池页添加 / 导入）：同步反向核对免疫
        "manual_pool": "BOOLEAN DEFAULT 0",
        # 手动移出监控池：同步复活挡标（重加时清标）
        "excluded": "BOOLEAN DEFAULT 0",
        # 榜单发现源入池标记（topsellers/popularnew/comingsoon 轮询落池；
        # 同步反向核对免疫——榜单游戏不在 Steam 名单里）
        "board_pool": "BOOLEAN DEFAULT 0",
    },
    # 补抓账本：missing 状态的补抓尝试计数
    # + 促销截止（browse active_discounts 下发，Unix 秒；每轮 UPSERT 跟随最新抓取）
    "game_current_prices": {
        "fail_count": "INTEGER DEFAULT 0",
        "discount_end_ts": "INTEGER",
    },
    # bundle-as-sub 识别标记（版本显示修复）
    # + browse 促销元数据四列（与 browse_store.GPH_EXTRA_COLUMNS 一一对应：
    #   attach_browse_extras 回贴促销截止/促销类型/bundle 归属）
    "game_price_history": {
        "is_bundle": "BOOLEAN DEFAULT 0",
        "discount_end_ts": "INTEGER",
        "discount_desc": "VARCHAR(60)",
        "bundle_id": "INTEGER",
        "bundle_discount_pct": "INTEGER",
    },
    # 捆绑包形态列：链接/CDN 用（与购买语义 mps 解耦）
    # + 排序快照预计算列（对齐 games：min_cny_fen/diff_fen/is_lowest；
    # is_lowest 是旧库可能缺列的存量列，一并登记保证补齐）
    "bundles": {
        "item_kind": "INTEGER DEFAULT -1",
        "min_cny_fen": "BIGINT",
        "diff_fen": "INTEGER DEFAULT 0",
        "is_lowest": "BOOLEAN DEFAULT 0",
        # smart 排序评分（refresh_bundle_sort_cache 维护；NULL=未计算）
        "smart_score": "REAL",
    },
    # bills 域新导出字段（Steam 消费历史分类器对齐）
    "bill_game_txs": {
        "wallet_balance": "VARCHAR(60) DEFAULT ''",
        "base_price": "VARCHAR(60) DEFAULT ''",
        "payment_parts_json": "TEXT",
        "gift_to_json": "TEXT",
    },
    # 许可证 appid（名称列商店链接提取；家庭库存精确关联用）
    "bill_cdk_games": {
        "appid": "BIGINT",
    },
    # 订阅废弃终态（节点状态存储：>95% 不可用 → deprecated）
    "proxy_subscriptions": {
        "deprecated": "BOOLEAN DEFAULT 0",
        "deprecated_at": "DATETIME",
        "deprecated_reason": "VARCHAR(200)",
    },
    # account 域多账号在线状态（GetPlayerSummaries/miniprofile 双通道）
    "steam_accounts": {
        "is_online": "BOOLEAN DEFAULT 0",
        "in_game": "VARCHAR(200) DEFAULT ''",
        # 钱包轮转递增退避级别（0=正常；每次失败 +1，成功清零；
        # 映射 _WALLET_BACKOFF_MINUTES：2→5→15→30 封顶）
        "wallet_backoff_level": "INTEGER DEFAULT 0",
        # 钱包熔断：连续失败计数（成功清零）与冻结终态（手动刷新/换绑解锁）
        "wallet_fail_streak": "INTEGER DEFAULT 0",
        "wallet_frozen": "BOOLEAN DEFAULT 0",
    },
    # 愿望单账户行展示：Steam 昵称/头像（miniprofile 免 Key 通道）
    "tracked_accounts": {
        "persona_name": "VARCHAR(100)",
        "avatar_url": "VARCHAR(500)",
    },
    # family_groups 游玩明细快照列（游玩动态的快照兜底数据源）
    "family_groups": {
        "play_json": "JSON",
    },
    # 成就域游戏行来源（owned=本号已购 / shared=家庭共享等库外来源 / manual=手动补录）
    "achievement_games": {
        "source": "VARCHAR(12) NOT NULL DEFAULT 'owned'",
    },
    # 成就定义的游戏内 API 名（Web API 明细通道按它直连解锁态；爬虫通道为空）
    "achievement_defs": {
        "apiname": "VARCHAR(160)",
    },
    # 触发事件的人民币分快照（触发时刻 cny_fen 同源落库，历史行回看免再折算）
    "alert_events": {
        "price_cny": "BIGINT",
    },
}

# 增量索引（CREATE INDEX IF NOT EXISTS 幂等）
_TABLE_EXTRA_INDEXES: dict[str, list[str]] = {
    "game_current_prices": [
        "CREATE INDEX IF NOT EXISTS ix_gcp_appid_status_cny "
        "ON game_current_prices(appid, price_status, cny_fen)",
    ],
    "games": [
        "CREATE INDEX IF NOT EXISTS ix_games_diff_fen ON games(diff_fen DESC)",
    ],
    # 捆绑包列表查询：WHERE 有区域价 + ORDER BY 差价降序（排序快照列）
    "bundles": [
        "CREATE INDEX IF NOT EXISTS ix_bundles_diff_fen ON bundles(diff_fen DESC)",
    ],
    # 汇率历史按币种取尾段（rate_history WHERE currency ORDER BY id DESC），
    # 导入 16 年档案（约 9 万行）后走此复合索引
    "fx_rate_history": [
        "CREATE INDEX IF NOT EXISTS ix_frh_currency_id ON fx_rate_history(currency_code, id)",
    ],
    # 节点状态账本按订阅取全量（test_clash_nodes UPSERT / 体检门槛查询）
    "clash_nodes": [
        "CREATE INDEX IF NOT EXISTS ix_clash_nodes_sub_name ON clash_nodes(subscription_id, name)",
    ],
}


def _ensure_schema(sync_conn) -> None:
    from sqlalchemy import text

    for table, columns in _TABLE_EXTRA_COLUMNS.items():
        existing = {
            row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        }
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    for table, statements in _TABLE_EXTRA_INDEXES.items():
        has_table = sync_conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
        ).fetchone()
        if not has_table:
            continue
        for ddl in statements:
            sync_conn.execute(text(ddl))


# ── 迁移链（后门）：结构性 schema 变更登记处 ──────────────────────────
# 使用法（唯一通道，见模块 docstring 的双层机制说明）：
#   1. SCHEMA_VERSION += 1
#   2. _MIGRATIONS 追加 (版本号, 描述, SQL 列表)；SQL 须幂等（中断续跑 +
#      用户库版本乱序防御），复杂逻辑可登记 async fn(engine) 同位元素

SCHEMA_VERSION = 7

# 零小数货币（Steam 以整数计价）：旧捆绑包链路的除数表按 1 处理，与「统一存分」
# 的新约定差 100 倍——v2 归一的目标集合
_BUNDLE_DECIMAL_FREE = ("IDR", "JPY", "KRW", "VND", "CLP", "COP", "PYG", "HUF")


async def _migrate_bundle_price_units(conn) -> None:
    """v2：bundle_region_prices 零小数货币单位归一（「元」→「分」+ cny_fen 去虚高）。

    旧抓取层对零小数货币（除数表按 1 处理）在**同一列**里混进了多套写法，且按行分布
    （生产库中三种形态并存）：

    - 「元价 + fen=price×rate×100」：Bundle 轨解析 formatted_final_price 字符串落「元」，
      cny_fen 本就正确（元值×rate×100 = 分口径 CNY）→ 只需 price ×100；
    - 「分价 +  fen=price×rate×100」：Package(Sub) 轨落 packagedetails 的「分」，
      cny_fen 虚高 100 倍 → 只需按 price×rate 纠正 cny_fen；
    - 「元价 +  fen=price×rate」：更早版本按分口径折算却又落「元」→ price 与 fen 同时小 100 倍。

    前两种的 fen 形状完全相同（`price×rate×100`），单看一行无法区分「元价+正确 fen」与
    「分价+虚高 fen」；第三种又与「分价+正确 fen」同形。**同一轨道内两类并存**，故轨道
    （must_purchase_as_set）也不足以判定。可靠判据是本包其他区：小数货币行不存在这种
    单位混写（除数 100，两轨同落分），其 cny_fen 量级可信；两套解读算出的 CNY 相差
    100 倍，而区域定价差异远小于此，取更接近者即可定单位。包内无小数货币参考行时退回
    fen 形状 + 轨道判定；两者都判不了的行跳过并计数，不猜。

    归一后所有行都是「分价 + fen≈price×rate」：重放（中断续跑）时判据必然落到同一侧
    且目标状态与现状一致 → 不写库，故幂等。
    """
    from sqlalchemy import text

    rates = {
        code: rate
        for code, rate in (
            await conn.execute(text("SELECT currency_code, rate_to_cny FROM fx_rates"))
        ).all()
        if rate
    }
    marks = ", ".join(f"'{c}'" for c in _BUNDLE_DECIMAL_FREE)
    # 每包参考价：小数货币区行（约定无歧义）的 cny_fen 中位数
    ref_rows = (
        await conn.execute(
            text(
                f"""
                SELECT bundle_id, cny_fen FROM bundle_region_prices
                 WHERE currency NOT IN ({marks})
                   AND cny_fen IS NOT NULL AND cny_fen > 0
                """
            )
        )
    ).all()
    refs: dict[int, list[float]] = {}
    for bid, fen in ref_rows:
        refs.setdefault(int(bid), []).append(float(fen))
    ref_median = {bid: sorted(v)[len(v) // 2] for bid, v in refs.items()}

    rows = (
        await conn.execute(
            text(
                f"""
                SELECT p.id, p.price, p.original_price, p.cny_fen, p.currency, p.bundle_id,
                       b.must_purchase_as_set
                  FROM bundle_region_prices p
                  LEFT JOIN bundles b ON b.bundle_id = p.bundle_id
                 WHERE p.currency IN ({marks})
                   AND p.price IS NOT NULL AND p.price > 0
                   AND p.cny_fen IS NOT NULL
                """
            )
        )
    ).all()

    to_cents = fen_fixed = skipped = undecided = 0
    for rid, price, original, fen, currency, bundle_id, mps in rows:
        rate = rates.get(currency)
        if not rate:
            skipped += 1
            continue
        fen_cents = round(price * rate)  # price 当「分」时的正确 CNY 分
        fen_units = round(price * rate * 100)  # price 当「元」时的正确 CNY 分
        ref = ref_median.get(int(bundle_id))
        if ref and fen_cents > 0:
            # 先与参考行比量级（两套解读差 100 倍，区域定价差异远小于此）
            is_units = abs(math.log(fen_units / ref)) < abs(math.log(fen_cents / ref))
        elif abs(fen - fen_cents) <= abs(fen - fen_units):
            is_units = False  # fen 已与「分」口径自洽
        elif mps == 1:
            is_units = False  # 歧义区退回轨道：Sub 轨落分
        elif mps == 0:
            is_units = True  # Bundle 轨落元
        else:
            undecided += 1
            continue
        if is_units:
            new_price = price * 100
            new_original = original * 100 if original else original
            # fen 在「元价」解读下本就正确；若它其实按分口径写的（第三种写法），一并归位
            new_fen = fen if abs(fen - fen_units) <= abs(fen - fen_cents) else fen_units
        else:
            new_price, new_original = price, original
            new_fen = fen if abs(fen - fen_cents) <= abs(fen - fen_units) else fen_cents
        if (new_price, new_original, new_fen) == (price, original, fen):
            continue
        await conn.execute(
            text(
                "UPDATE bundle_region_prices "
                "SET price = :price, original_price = :original, cny_fen = :fen "
                "WHERE id = :rid"
            ),
            {"price": new_price, "original": new_original, "fen": new_fen, "rid": rid},
        )
        to_cents += 1 if is_units else 0
        fen_fixed += 1
    if rows:
        logger.info(
            "[迁移] 捆绑包零小数货币归一：扫描 %d 行，元→分 %d 行，cny_fen 纠正 %d 行，"
            "无汇率跳过 %d 行，无法判定跳过 %d 行",
            len(rows), to_cents, fen_fixed, skipped, undecided,
        )


async def _migrate_alert_targets_to_cny(conn) -> None:
    """v7：price_alerts 的 price 类阈值口径归一——该区货币最小单位 → 人民币分。

    旧口径下阈值与 GameCurrentPrice.price（该区货币分）直比，而前端与邮件
    一律按 ¥ 展示——外区规则「设的 $10、显示 ¥10、比较的也是 $10」，语义
    三方分裂。归一后阈值 = 人民币分，与 crawl 落库的 cny_fen 同口径，触发
    比较、展示、邮件一致；前端按元输入，悬停换算该区现价。

    换算率取 fx_rates 现值，缺币种回退 DEFAULT_EXCHANGE_RATES，再缺的行
    跳过计数（不猜）；CNY 区 rate=1.0 数值不动。pct / historic_low 不涉及
    货币，不碰。事务内整体提交（迁移链每步一事务 + user_version 同事务落
    账），重放只发生在整步回滚后，无需行级幂等判据。
    """
    from sqlalchemy import text

    from app.domains.games.pricing import DEFAULT_EXCHANGE_RATES, REGION_TO_CURRENCY

    rates = {
        code: rate
        for code, rate in (
            await conn.execute(text("SELECT currency_code, rate_to_cny FROM fx_rates"))
        ).all()
        if rate
    }
    rows = (
        await conn.execute(
            text(
                "SELECT id, region, target_value FROM price_alerts"
                " WHERE target_type = 'price' AND target_value IS NOT NULL"
            )
        )
    ).all()

    converted = skipped = 0
    for aid, region, value in rows:
        currency = REGION_TO_CURRENCY.get(str(region or "").upper())
        rate = rates.get(currency) if currency else None
        if rate is None and currency:
            rate = DEFAULT_EXCHANGE_RATES.get(currency)
        if rate is None:
            skipped += 1  # 区码/币种认不出：留原值留痕，不猜
            continue
        await conn.execute(
            text("UPDATE price_alerts SET target_value = :v WHERE id = :id"),
            {"v": round(float(value) * rate), "id": int(aid)},
        )
        converted += 1
    if converted or skipped:
        logger.info(
            "[迁移:v7] 价格阈值口径归一（外币分→人民币分）：%d 条换算，%d 条缺汇率跳过",
            converted, skipped,
        )


async def _migrate_gph_snapshot_unique(conn) -> None:
    """补建 game_price_history 的幂等唯一索引。

    写入侧（crawler/db_writer、历史导入脚本）用 `INSERT OR REPLACE` 去重，
    靠的就是这个唯一索引。缺了它**新装的库**表建出来没有约束，OR REPLACE
    退化成普通 INSERT：同一天同一 sub 反复堆积，表现是走势图出现同日重复点、
    史低次数虚高、`count` 与库内行数对不上（「数据对不上」的一类）。

    表达式索引而非列索引：sub_id / price / is_gold 都可为空，而 SQLite 的唯一索引
    把每个 NULL 视为互不相等，直接索引这三列等于不约束——必须 COALESCE 归一。
    索引定义与库内既有形态逐字一致，否则同名不同义会造成两库行为分叉。

    **非破坏性**：库内已有重复行时只告警、不建索引、更不删行。无索引的库本就在
    无约束地写入，静默删用户数据不可接受；重复行留待人工核对后再跑一次迁移收口。
    """
    from sqlalchemy import text

    dup_groups = (
        await conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "  SELECT 1 FROM game_price_history"
                "  GROUP BY appid, region_code, snapshot_at, COALESCE(sub_id, -1),"
                "           COALESCE(price, 0), COALESCE(is_gold, 0)"
                "  HAVING COUNT(*) > 1)"
            )
        )
    ).scalar_one()
    if dup_groups:
        logger.warning(
            "[迁移] game_price_history 存在 %d 组重复行（同 appid/区/时刻/sub/价/gold），"
            "跳过 ux_gph_snapshot 建索引：请先人工核对清理，重启后本步骤会自动重试",
            dup_groups,
        )
        return
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_gph_snapshot ON game_price_history ("
            "appid, region_code, snapshot_at, COALESCE(sub_id, -1),"
            " COALESCE(price, 0), COALESCE(is_gold, 0))"
        )
    )
    logger.info("[迁移] game_price_history 幂等唯一索引 ux_gph_snapshot 已就绪")


# (目标版本, 说明, 迁移体)：迁移体 = SQL 语句列表，或 async callable(engine)
_MIGRATIONS: list[tuple[int, str, object]] = [
    (2, "bundle_region_prices 零小数货币单位归一（元→分 + cny_fen 去虚高）",
     _migrate_bundle_price_units),
    (3, "bundles.item_kind 形态列（0=bundle/1=sub）初始回填（沿用 mps 旧值，"
        "形态本就是 mps 的旧语义之一；mps 本身的语义纠正交给刷新层带权威值完成，"
        "不在此按行内线索猜）",
     [
         "UPDATE bundles SET item_kind = must_purchase_as_set"
         " WHERE item_kind IS NULL OR item_kind = -1",
     ]),
    (4, "game_price_history 幂等唯一索引 ux_gph_snapshot 补建"
        "（写入侧靠 INSERT OR REPLACE 去重，缺该索引时新库会退化成无约束插入）",
     _migrate_gph_snapshot_unique),
    (5, "wishlist_items 愿望单成员标记回填（监控池三模块语义："
        "愿望单与星标关注识别为爬取队列第一优先级）",
     [
         # 活跃条目中 owned=0 且非星标关注的行均为愿望单同步来源；回填后
         # 未覆盖的行（manual=1）已属第一优先级，其愿望单成员资格由下一次
         # 账户同步按真实愿望单覆写补正（15min 一轮，自愈）。脱池行
         # （active=0）不回填：成员资格随下一次同步恢复入池时写入。
         "UPDATE wishlist_items SET wishlisted = 1"
         " WHERE owned = 0 AND active = 1 AND manual = 0",
     ]),
    (6, "成就域明细表重建（采集通道改为公开社区页：行标识从 apiname 换为"
        "图标资产名，旧两表仅在未发布版本中存在过，直接清掉由 create_all 重建）",
     [
         "DROP TABLE IF EXISTS game_achievements",
         "DROP TABLE IF EXISTS player_achievements",
     ]),
    (7, "price_alerts 价格阈值口径归一（该区货币分 → 人民币分；触发比较改用"
        " cny_fen，外区规则与 ¥ 展示语义对齐）",
     _migrate_alert_targets_to_cny),
]


# 迁移前快照后缀：与生产库同在数据目录，**不进 backups/**
_PRE_MIGRATION_SUFFIX = ".pre-migration.bak"


def _db_file_path() -> Path | None:
    """当前引擎指向的库文件路径；内存库 / 非文件库返回 None。

    刻意从 `engine.url` 取而不是 `get_settings().data_dir`：要快照的是
    **这个引擎正在迁移的那个库**。两者不一致时（测试夹具换临时库、
    `HOLDEXAR_DATA_DIR` 覆盖、打包态数据目录切换）以引擎为准——否则
    临时库迁移会去快照真实库，而真正要迁移的那个反倒没有回滚点。
    """
    db = get_engine().url.database
    if not db or db.startswith(":"):
        return None
    return Path(db)


def sqlite_file_path() -> Path | None:
    """配置指向的库文件路径（不经引擎，纯 URL 解析）；内存库 / 非文件库返回 None。

    与 `_db_file_path` 的区别：不创建引擎、不依赖「哪个引擎正在被使用」，
    供需要在启动链路里**直接以 sqlite3 打开库文件**的场景使用（如种子合并的
    ATTACH 写法——大批量集合式 SQL 走原生连接，绕开 ORM 逐对象化的开销）。
    """
    from sqlalchemy.engine import make_url

    db = make_url(get_settings().db_url).database
    if not db or db.startswith(":"):
        return None
    return Path(db)


async def _snapshot_before_migration(current: int, target: int) -> None:
    """迁移前落一份快照到 `<db>.pre-migration.bak`（每次覆盖，只留最近一份）。

    **必须有**：结构性迁移（数据回填 / 列拆并 / 表重建）不可逆。单位归一那类
    步骤若跑错方向，用户整段价格史就变成错值，而此刻线上唯一副本就是它自己；
    `BACKUP_KEEP` 轮转里的日备最多只能把损失缩到一天前。幂等建索引这类步骤
    风险低，但迁移链是追加式的，下一条是什么无从预判——按统一规则兜底，
    不为「这一步看起来安全」开例外。

    **落在库文件旁边**：迁移前快照是「本次升级的回滚点」，一次性、用完即弃，
    不进 `backups/` 轮转（那是「用户可恢复的历史点」，`BACKUP_KEEP=5`）。

    **失败不阻断启动**：快照失败（库被独占、磁盘满）时迁移本身大概率也会失败
    并留全栈日志；在启动路径上因快照失败直接拒绝启动，比原问题更难自查。此处
    记 error 级日志，把「正在无快照迁移」这件事显式留痕。
    """
    src = _db_file_path()
    if src is None or not src.is_file() or src.stat().st_size == 0:
        return
    dest = src.with_name(src.name + _PRE_MIGRATION_SUFFIX)
    from .backup import snapshot_to

    try:
        # VACUUM INTO 的目标文件已存在会直接报错，先清
        dest.unlink(missing_ok=True)
        await asyncio.to_thread(snapshot_to, src, dest)
        logger.info(
            "[迁移] 迁移前快照已落：%s（v%d → v%d，%.1f MB）",
            dest.name, current, target, dest.stat().st_size / 1024 / 1024,
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "[迁移] 迁移前快照失败（v%d → v%d）：本次迁移**没有回滚点**。"
            "迁移若中断或结果异常，请从 backups/ 里最近一份备份恢复",
            current, target, exc_info=True,
        )


async def _run_schema_migrations() -> None:
    """按 user_version 差额执行迁移链；每步成功即落版本号。

    - 存量库 user_version=0（从未用过版本账本）→ 视为 v1 之前，直接登 v1
      （v1 本身无数据变更，纯账本启用；真变更从 v2 起）
    - 每步一个事务（engine.begin），成功提交时同事务内 UPDATE user_version
      ——中断在任意步，重启后从断点续跑，重放已成功步骤由幂等 SQL 吸收
    - 迁移抛错：向上抛给 init_db 调用方（启动日志留全栈），不静默吞——
      结构性变更失败意味着新旧 schema 混存，静默会让后续写入路径更炸
    - **有差额要跑时，先落一份迁移前快照**（见 `_snapshot_before_migration`）
    """
    engine = get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        current = (
            await conn.execute(text("PRAGMA user_version"))
        ).scalar_one()

    if current == 0:
        # 首登版本账本：v1 = 账本启用（无数据变更）
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA user_version = 1"))
        current = 1

    latest = max((v for v, _, _ in _MIGRATIONS), default=1)
    if current > latest:
        # 高于本代码链 = 步骤被移除，或账本被外部写脏（如测试夹具把临时步骤
        # 泄漏进真实链后在生产库上跑过 init_db）——此时差额迁移会**全部静默
        # 跳过**，必须留痕
        logger.warning(
            "[迁移] 库结构版本 v%d 高于本代码迁移链最新 v%d："
            "差额迁移将全部跳过，请核对版本账本",
            current, latest,
        )

    pending = [m for m in _MIGRATIONS if m[0] > current]
    if pending:
        await _snapshot_before_migration(current, pending[-1][0])

    for target_version, description, body in _MIGRATIONS:
        if target_version <= current:
            continue
        logger.info("[迁移] schema v%d：%s", target_version, description)
        async with engine.begin() as conn:
            if callable(body):
                await body(conn)
            else:
                for ddl in body:
                    await conn.execute(text(ddl))
            await conn.execute(text(f"PRAGMA user_version = {target_version}"))
        current = target_version


async def init_db() -> None:
    """建表 + 基础种子数据。M1 用 create_all，首次 schema 变更前引入 Alembic。"""
    # 导入各域模型模块，确保表注册到 Base.metadata
    from app.domains.achievements import models as _achievements_models  # noqa: F401
    from app.domains.alerts import models as _alerts_models  # noqa: F401
    from app.domains.bills import models as _bills_models  # noqa: F401
    from app.domains.crawl import models as _crawl_models  # noqa: F401
    from app.domains.games import models as _games_models  # noqa: F401
    from app.domains.proxies import models as _proxies_models  # noqa: F401
    from app.domains.rates import models as _rates_models  # noqa: F401
    from app.domains.regions import models as _regions_models  # noqa: F401
    from app.domains.settings import models as _settings_models  # noqa: F401
    from app.domains.wishlist import models as _wishlist_models  # noqa: F401
    from app.domains.family import models as _family_models  # noqa: F401
    from app.domains.account import models as _account_models  # noqa: F401

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_schema)

    # 结构性迁移链（user_version 账本；v1 起步）
    await _run_schema_migrations()

    # 区服配置种子：CC_LIST → crawl_regions（含旧 crawl.enabled_regions 键迁移）
    from app.domains.regions import service as regions_service

    await regions_service.ensure_seeded()

    # 汇率种子：CNY 恒为 1.0（其余币种 M5 由汇率域回填）
    from sqlalchemy import select

    from app.domains.rates.models import FxRate

    async with get_session_factory()() as session:
        existing = await session.scalar(select(FxRate).where(FxRate.currency_code == "CNY"))
        if existing is None:
            session.add(FxRate(currency_code="CNY", rate_to_cny=1.0))
            await session.commit()


@lru_cache
def get_engine():
    engine = create_async_engine(get_settings().db_url, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # WAL 物理收缩：autocheckpoint 只把逻辑尾推回头部复用，文件物理大小
        # 会停在增长过的峰值（可达与主库同量级）。超限即截，
        # 让每个连接做完 checkpoint 都把 WAL 收回本限内。
        cursor.execute("PRAGMA journal_size_limit=67108864")
        cursor.execute("PRAGMA foreign_keys=ON")
        # 60s：种子历史合并单事务 BEGIN IMMEDIATE 持锁可达十几秒，引擎侧
        # 连接等锁要扛过这个窗口（与 _merge_history_sync 自己的 60s 同宽）；
        # 普通请求的锁竞争远短于此，不会白等
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

    return engine


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话。"""
    async with get_session_factory()() as session:
        yield session
