"""rates 域模型：汇率快照 + 历史记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FxRate(Base):
    __tablename__ = "fx_rates"

    currency_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    rate_to_cny: Mapped[float] = mapped_column(Float, default=1.0)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class FxRateHistory(Base):
    __tablename__ = "fx_rate_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    currency_code: Mapped[str] = mapped_column(String(10))
    rate_to_cny: Mapped[float] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(String(30))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
