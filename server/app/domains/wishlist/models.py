"""wishlist 域模型：跟踪账户 + 愿望单条目（任务队列）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TrackedAccount(Base):
    __tablename__ = "tracked_accounts"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(100))
    # Steam 实名档案（miniprofile 免 Key 通道拉取；愿望单页账户行展示用）
    persona_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    kinds_json: Mapped[dict | None] = mapped_column(JSON)  # {"wishlist":true,"owned":false}
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    added_at: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 该条目来自账户已购库（True=已拥有 / False=纯愿望单）；同步时按已购列表覆写
    owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动关注标记（游戏卡星标）：同步反向核对「愿望单已移除 → 停用」
    # 时免疫——手动关注的存续不取决于 Steam 真实愿望单（账户同步 15min
    # 高频后，无此标记的手动条目 15 分钟内即被洗掉）。批量导入不产生
    # 此标记：导入只爬取入库（crawl.import_appids），不进追踪池。
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
