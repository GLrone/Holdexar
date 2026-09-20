"""rates 域模型：汇率快照 + 历史记录 + Provider 配额账本。

历史语义（v8 起）：
- `rate_date` 是「这条汇率代表哪一天」，业务查询一律用它，不再用
  `fetched_at`（Holdexar 获取时刻）的日期部分顶替；
- `(currency_code, rate_date)` 唯一——一币种一天一个 canonical 值；
- `source_kind` 三态：`observed`（Provider 实际返回）/ `carried`（历史遗留的
  人工延续值，只可展示不可当真实历史消费）/ `derived`（由合法来源计算）。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FxRate(Base):
    __tablename__ = "fx_rates"

    currency_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    rate_to_cny: Mapped[float] = mapped_column(Float, default=1.0)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class FxRateHistory(Base):
    __tablename__ = "fx_rate_history"
    # canonical 唯一日索引：新库由 create_all 落位；存量库由 v8 迁移链在
    # 「同日合并」之后落位（同名同义，两库不分叉）。写入侧一律按
    # (currency_code, rate_date) UPSERT。
    __table_args__ = (
        Index("ux_frh_currency_date", "currency_code", "rate_date", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    currency_code: Mapped[str] = mapped_column(String(10))
    rate_to_cny: Mapped[float] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(30))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 汇率代表日（canonical 唯一键的一部分；业务查询唯一入口）
    rate_date: Mapped[date | None] = mapped_column(Date)
    # observed / carried / derived
    source_kind: Mapped[str | None] = mapped_column(String(12))
    # Provider 有明确观察时刻时记录（EOD 源为数据日收盘时刻；无则 NULL）
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)


class FxProviderUsage(Base):
    """Provider 配额账本（月度）：换 Key / 换月份 / 换 Provider 各自一行。

    响应 headers 不暴露月度余量（Provider 只暴露 1 req/s 级限速），本地记账是唯一
    手段。`key_fingerprint` 是 Key 的哈希指纹——Key 明文只留在
    `secrets/fx_maintenance.env`，不进库、不进 git。
    """

    __tablename__ = "fx_provider_usage"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_fingerprint: Mapped[str] = mapped_column(String(24), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    request_limit: Mapped[int | None] = mapped_column(Integer)
    last_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_remaining: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
