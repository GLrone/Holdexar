"""wishlist 域模型：跟踪账户 + 监控条目（监控池任务队列）。

监控条目是监控池（wishlist_items 的活跃行）的唯一承载：愿望单同步、
已购同步、星标关注、监控池页手动添加四条来源都落在这里，全部必爬；
其中愿望单成员（wishlisted）与星标关注（manual）是爬取队列的第一优先级。
"""
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
    # 该条目来自账户已购库（True=已拥有 / False=非已购）；同步时按已购列表覆写
    owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动关注标记（游戏卡星标）：同步反向核对「愿望单已移除 → 停用」
    # 时免疫——手动关注的存续不取决于 Steam 真实愿望单（账户同步 15min
    # 高频后，无此标记的手动条目 15 分钟内即被洗掉）。
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    # 愿望单成员标记（Steam 愿望单同步来源）：爬取队列第一优先级（与关注
    # 并列）；愿望单反向核对（Steam 侧已移除）时随成员资格清零。
    wishlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动加入监控池（监控池页添加 / 任务页导入）：属普通监控条目——
    # 不进愿望单成员标记、不产生关注；同步反向核对免疫（存续不取决于
    # Steam 侧名单，否则手动条目入池 15 分钟内即被同步洗掉）。
    manual_pool: Mapped[bool] = mapped_column(Boolean, default=False)
    # 手动移出监控池（监控池页删除）：同步复活（愿望单/已购仍在 Steam
    # 名单里）一律被此标记挡住——否则用户删掉的条目 15 分钟内即被洗回来。
    # 重新添加（add_pool_items / follow）时清标复活。
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
