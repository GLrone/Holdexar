"""games 域模型：游戏主档 / 实时价格 / 历史切片。

developers/publishers 数组列按 SQLite 习惯转 JSON 文本。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Game(Base):
    __tablename__ = "games"

    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(512))
    name_en: Mapped[str | None] = mapped_column(String(512))
    type: Mapped[str | None] = mapped_column(String(20))  # GAME / DLC / DEMO / MOD ...
    header_image: Mapped[str | None] = mapped_column(String(1024))
    store_url: Mapped[str | None] = mapped_column(String(1024))
    chinese_support: Mapped[str | None] = mapped_column(String(50))
    family_sharing: Mapped[bool] = mapped_column(Boolean, default=False)
    trading_cards: Mapped[bool] = mapped_column(Boolean, default=False)
    xgp_tier: Mapped[str | None] = mapped_column(String(50))  # Xbox Game Pass 档位
    is_epic: Mapped[bool] = mapped_column(Boolean, default=False)  # Epic 免费送过
    epic_date: Mapped[str | None] = mapped_column(String(100))  # 赠送日期（如 "2023-12-25 ~ 2024-01-01"）
    is_hb: Mapped[bool] = mapped_column(Boolean, default=False)  # 进过 HB 慈善包
    hb_data: Mapped[str | None] = mapped_column(String(100))  # HB 信息（如 HB慈善包24年1月包）
    # 第三方渠道 bundle 计数（站点预聚合全量档案按 appid 查表；NULL=未拉取），
    # refresh_bundle_counts 维护——只写有计数的行，无记录者保持 NULL
    bundle_count: Mapped[int | None] = mapped_column(Integer)
    series_id: Mapped[str | None] = mapped_column(String(100))  # 游戏系列（如 GTA / Resident Evil）
    hl_flag: Mapped[int] = mapped_column(Integer, default=0)  # 0=无 1=新史低 2=平史低 3=打折非史低（refresh_hl_flags 维护）
    pp_flag: Mapped[int] = mapped_column(Integer, default=0)  # 0=无 1=永降 2=永涨（refresh_pp_flags 维护：国区原价最近一次调价方向）
    # 最近一次原价跳变（相邻快照原价不同）的时刻；徽章时效判据——
    # 前端只在变化后 14 天内展示永降/永涨，无值或过期即隐藏
    pp_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ── 排序缓存预计算列（refresh_sort_cache 维护）──
    # min_cny_fen = 非 CN 各区 ok 价的最低 CNY 分（原始值，无其他区价为 NULL；
    # 查询侧用 COALESCE(min_cny_fen, cn.price) 取最低价）
    min_cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    # diff_fen = CN 价 - COALESCE(最低, CN 价)（下限 0；无 CN 价时为 0）
    diff_fen: Mapped[int] = mapped_column(Integer, default=0)
    # smart 排序评分预计算列（refresh_sort_cache 维护，公式见 scoring.py；
    # 0~1 浮点，NULL=尚未计算——DESC 排序天然沉底）
    smart_score: Mapped[float | None] = mapped_column(Float)
    is_adult: Mapped[bool] = mapped_column(Boolean, default=False)  # 成人内容
    is_visual_novel: Mapped[bool] = mapped_column(Boolean, default=False)  # 视觉小说
    release_date: Mapped[str | None] = mapped_column(String(50))
    genres: Mapped[str | None] = mapped_column(String(512))
    # 商店移除监控：NULL=在售；非空=判定下架的北京时间。
    # 判定条件（mark_removed，app_handler 终端路径）：Phase 1 五区回退元数据
    # 全部 404 + 库内已有元数据行（之前抓到过）——持续两轮全 404 才落值，
    # 单轮全 404 只递增 removed_strikes（防 Steam 抖动误判）。
    # 语义：关注层/missing 账本脱池（省每日空转配额）；历史价格/史低标记保留；
    # 重新上榜 = 免费复活信号（backfill_specs 反哺，upsert 成功自动清标）。
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_strikes: Mapped[int] = mapped_column(Integer, default=0)
    # 免费态标记（db_writer 每轮爬取按价格行维护）：NULL=付费正常；
    # 'f2p'=永久免费（is_free，无赠送包原价）；'promo'=限时赠送中（100% off，
    # Steam free_to_keep）。两类都不进游戏商店列表/搜索（主门 price>0），
    # f2p 由爬取收尾自动脱池，promo 上仪表盘「Steam 喜加一」模块
    free_kind: Mapped[str | None] = mapped_column(String(10))
    # 赠送结束 Unix 秒（Steam free_to_keep_ends）；仅 promo 态有值
    promo_end_at: Mapped[int | None] = mapped_column(BigInteger)
    developers: Mapped[list | None] = mapped_column(JSON, default=list)
    publishers: Mapped[list | None] = mapped_column(JSON, default=list)
    positive_rate: Mapped[int | None] = mapped_column(Integer)  # 万分比 0-10000
    positive_reviews: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_games_type", "type"),
        Index("ix_games_updated", "updated_at"),
    )


class GameCurrentPrice(Base):
    """实时价格（UPSERT）：每游戏每区域一条标准版价格，锁区/缺失也作为状态写入。"""

    __tablename__ = "game_current_prices"

    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    region_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    currency: Mapped[str | None] = mapped_column(String(10))
    price: Mapped[int | None] = mapped_column(BigInteger)  # 当前售价（分为单位）
    original_price: Mapped[int | None] = mapped_column(BigInteger)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    sub_id: Mapped[int | None] = mapped_column(Integer)
    price_status: Mapped[str] = mapped_column(String(20), default="ok")
    # 补抓账本：该区进入 missing 后的补抓尝试次数（0=未欠账；≥MISSING_MAX_RETRIES
    # 仍失败则转 blocked 终态，由 mark_missing/补抓链路维护）
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    # 促销截止（browse active_discounts[0].discount_end_date，Unix 秒）：
    # 现价表每轮 UPSERT，始终跟随最新一轮抓取；NULL=无折扣/未带促销元数据
    discount_end_ts: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_gcp_region_cny", "region_code", "cny_fen"),
        Index("ix_gcp_status", "price_status"),
        Index("ix_gcp_updated", "updated_at"),
        # isLowest/地区筛选 CTE/增量刷新的核心索引：appid 锚定 + 状态过滤 + 价格范围
        Index("ix_gcp_appid_status_cny", "appid", "price_status", "cny_fen"),
    )


class GamePriceHistory(Base):
    """历史价格切片（INSERT only）：全版本快照，用于走势图。"""

    __tablename__ = "game_price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appid: Mapped[int] = mapped_column(BigInteger)
    region_code: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str | None] = mapped_column(String(10))
    price: Mapped[int | None] = mapped_column(BigInteger)
    original_price: Mapped[int | None] = mapped_column(BigInteger)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    sub_id: Mapped[int | None] = mapped_column(Integer)
    is_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    version_suffix: Mapped[str | None] = mapped_column(String(100))
    # bundle-as-sub 识别：多 app 且名称未命中版本关键词的 sub
    # （如 Gourmet Edition）= 不支持补齐的捆绑包，从标准版候选/史低计算/
    # 版本 chips 三处排除，并回填 bundles 表
    is_bundle: Mapped[bool] = mapped_column(Boolean, default=False)
    price_status: Mapped[str] = mapped_column(String(20), default="ok")
    cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    # 促销截止（browse active_discounts[0].discount_end_date，Unix 秒）；
    # 同族扩展列 discount_desc/bundle_id/bundle_discount_pct 不经 ORM，
    # 由 attach_browse_extras 以裸 SQL 回贴
    discount_end_ts: Mapped[int | None] = mapped_column(Integer)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_gph_appid_region_snapshot", "appid", "region_code", "snapshot_at"),
        Index("ix_gph_sub_id", "sub_id"),
    )


class Bundle(Base):
    """捆绑包主档。app_ids 数组列按 SQLite 习惯转 JSON 文本。"""

    __tablename__ = "bundles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(512))
    # 0=可补齐 1=必须整包 -1=未知（购买语义，非链接形态）
    must_purchase_as_set: Mapped[int] = mapped_column(Integer, default=-1)
    # 链接/CDN 形态：0=bundle / 1=sub / -1=未知。与购买语义解耦——
    # 形态为 bundle 但必须整包的包真实存在（同号 sub/bundle 数字空间并存），
    # 旧代码拿 mps 兼形态会把这类包拼出错误 /sub/ 链接
    item_kind: Mapped[int] = mapped_column(Integer, default=-1)
    header_image: Mapped[str | None] = mapped_column(String(1024))
    # 国区买是否即（近似）追踪区最低：国区价 ≈ 追踪区最低价（±5 元容差）
    is_lowest: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── 排序快照预计算列（refresh_bundle_sort_cache 维护，与 games 同构）──
    # min_cny_fen = 追踪区（crawl_regions 启用集）内非 CN 各区 ok 价最低 CNY 分
    # （原始值；无双区价则 NULL）
    min_cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    # diff_fen = CN 价 - COALESCE(最低, CN 价)（下限 0；无 CN 价时为 0）
    # server_default：与 _TABLE_EXTRA_COLUMNS 的存量库 ALTER 口径一致，
    # 原始 SQL 插行（历史导入/迁移脚本）省略该列时也拿得到默认值
    diff_fen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # smart 排序评分（0~1，refresh_bundle_sort_cache 维护；NULL=未计算）。
    # 公式对齐 games/scoring.py 四因子：省钱(包差价)+质量(成员游戏评测)+
    # 时机(限时叠加促销)+熟悉度(成员游戏评测规模)
    smart_score: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(String(1024))
    app_ids: Mapped[list | None] = mapped_column(JSON, default=list)  # 包含的 Steam AppID 列表
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class BundleRegionPrice(Base):
    """捆绑包区域价格快照；app_ids = 该区实际包含项（锁区检测依据）。"""

    __tablename__ = "bundle_region_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[int] = mapped_column(Integer)
    region_code: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str | None] = mapped_column(String(10))
    price: Mapped[int | None] = mapped_column(BigInteger)
    original_price: Mapped[int | None] = mapped_column(BigInteger)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    bundle_base_discount: Mapped[int] = mapped_column(Integer, default=0)
    price_status: Mapped[str] = mapped_column(String(20), default="ok")
    cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    app_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    crawled_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_brp_bundle_region", "bundle_id", "region_code"),
    )


class PresetGame(Base):
    """预设游戏池清单（appid 账本）：导入文件（关注数据）与 TOP 榜两条来源。

    用途：随包分发的「出品游戏集」——导出资产种子（scripts/export_seed.py）
    把它写进 holdexar_seed.db，客户端并入（seed_assets.merge_preset_seed）
    时缺行的 appid 落成 games 行，全池主轮随即接管价格刷新（预设监控）。

    语义：本表只登记，不触发爬取（入池/首爬由调用方既有通道负责）；
    导入来源只增不改（重复导入保留首次来源与落档时间），榜单来源整批
    替换（TOP 部分 = 最近一次成功抓取的榜）。name 为登记时从 games 主档
    带回的展示名（未爬过的新条目为空），导出侧再以库内名字兜底。
    """

    __tablename__ = "preset_games"

    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(80))  # 文件名 / "topsellers"
    added_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_preset_source", "source"),
    )
