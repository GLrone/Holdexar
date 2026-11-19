"""wishlist 域模型：Steam 账户来源数据（跟踪账户 + 愿望单/已购成员事实）。

本表是 **Steam 账户来源数据**，不是本地用户身份模型：

- 行身份 `(steamid, appid)`：来自 Steam 愿望单 / 已购同步的账户成员事实；
- 持续监控资格由 monitoring 域的来源表达——项目自加来源（`favorite` 关注、
  `manual` 手动加入、`board` 榜单落池）直挂监控层，不落本表；
- 本表的来源布尔列只作账号对账的输入，不表达「用户是否在关注」。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TrackedAccount(Base):
    __tablename__ = "tracked_accounts"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(100))
    persona_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    kinds_json: Mapped[dict | None] = mapped_column(JSON)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    added_at: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    owned: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    wishlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动加入列（已退役）：加入真相源是 monitoring 的 manual 来源，由
    # add_pool_items 直挂，不落本表。存量列仅作旧库兼容，不再写入。
    manual_pool: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # 榜单发现源落池列（已退役）：落池真相源是 monitoring 的 board 来源，由
    # ensure_board_pool 直挂监控层，不落本表。存量列仅作旧库兼容，不再写入。
    board_pool: Mapped[bool] = mapped_column(Boolean, default=False)
