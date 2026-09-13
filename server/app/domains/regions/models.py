"""SQLAlchemy 模型：crawl_regions 区服配置表。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CrawlRegion(Base):
    """爬取区服配置：CC_LIST 的落库投影，启用状态在此管理。

    加新区 = crawler/config.py 的 CC_LIST 加一行，启动种子自动补行。
    """

    __tablename__ = "crawl_regions"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # cc 小写
    name: Mapped[str] = mapped_column(String(50))
    currency: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
