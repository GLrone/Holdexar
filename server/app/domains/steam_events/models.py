"""steam_events 域模型：Steam 官方活动日历（季节大促 / Next Fest / 主题 Fest）。

数据来源是 Steamworks 公开的 Upcoming Steam Events 文档页（日粒度、PT 时区
口径）；活动页上线后可由商店页内嵌元数据补精确 Unix 起止（precise_*_ts）。

event_key 是稳定标识（不随界面语言漂移），价格历史观测行的
``steam_event_key`` 标签与它对齐；界面名按语言取 name_en / name_zh。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SteamEvent(Base):
    __tablename__ = "steam_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 稳定 slug：seasonal_2026_autumn / nextfest_2026_10 / themed_cooking_2026
    event_key: Mapped[str] = mapped_column(String(60), unique=True)
    category: Mapped[str] = mapped_column(String(20))
    name_en: Mapped[str] = mapped_column(String(120))
    name_zh: Mapped[str | None] = mapped_column(String(120))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # 精确 Unix 起止（活动页上线后由商店页元数据补齐；NULL = 暂无，展示退回日粒度）
    precise_start_ts: Mapped[int | None] = mapped_column(BigInteger)
    precise_end_ts: Mapped[int | None] = mapped_column(BigInteger)
    store_url: Mapped[str | None] = mapped_column(String(200))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
