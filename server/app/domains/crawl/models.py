"""crawl 域模型：爬取任务执行记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20))  # manual | scheduled | wishlist_sync
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|failed|stopped
    mode: Mapped[str | None] = mapped_column(String(10))  # app | batch
    regions_json: Mapped[list | None] = mapped_column(JSON)
    stats_json: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
