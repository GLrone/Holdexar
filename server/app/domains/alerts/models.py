"""alerts 域模型：降价提醒规则 + 触发事件。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appid: Mapped[int] = mapped_column(BigInteger)
    region: Mapped[str] = mapped_column(String(10), default="CN")
    # price: 当前价 <= target_value（分）| pct: 折扣率 >= target_value（%）| historic_low: 创新低
    target_type: Mapped[str] = mapped_column(String(20), default="price")
    target_value: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(Integer)
    appid: Mapped[int] = mapped_column(BigInteger)
    region: Mapped[str] = mapped_column(String(10))
    price: Mapped[int | None] = mapped_column(BigInteger)  # 分
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
