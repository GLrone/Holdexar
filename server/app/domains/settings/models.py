"""settings 域模型：本地键值配置（账户 / 区服 / 通知等）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
