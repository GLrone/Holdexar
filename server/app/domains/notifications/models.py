"""通知域模型：价格事件的通知候选。

`price_events` 是事实（只增不改、没有渠道状态）；候选表达「这个事实经过用户
策略判断后具备通知资格」。**通知状态只落在候选上，绝不回写事件**——同一个
事实可以不通知、可以进邮件、可以进摘要，事实本身不被渠道状态污染。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NotificationCandidate(Base):
    """一条已通过策略的通知资格。

    身份 = (event_id, recipient)：同一事实对同一收件人只会有一条候选，重复
    处理同一事件 / 重启 / 重复进入 finalizing 都不会产生第二条。加渠道不应让
    同一个事实重新生成一套通知——渠道是候选的下游，不另做判断。

    status 是投递状态，不是事实状态：
    - `pending`    待投递（本轮摘要会带上）
    - `suppressed` 静默期顺延：不是丢掉，等下一个非静默批次一并投递
    - `sending`    投递中（瞬态；进程在 SMTP 期间被杀会停在该态，不重发）
    - `delivered`  已投递
    - `failed`     投递失败（reason 记原因；不自动重试，重试会放大成重复投递）
    """

    __tablename__ = "notification_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # price_events.id：事实身份的锚
    event_id: Mapped[int] = mapped_column(Integer)
    cycle_id: Mapped[int] = mapped_column(Integer)
    appid: Mapped[int] = mapped_column(Integer)
    # NULL = 游戏级事件（促销免费 / 下架），不属于单一地区
    region_code: Mapped[str | None] = mapped_column(String(8))
    # 冗余一份事件类型与类别：摘要组装不必回查事件表
    event_type: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # suppressed / failed 的原因（quiet / smtp:…）；投递成功为 NULL
    reason: Mapped[str | None] = mapped_column(Text)
    # 已尝试投递次数（首次投递记为 1）；只有可重试的失败才会再进队列
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次投递失败的原因原文（`send_mail_ex` 返回），用于区分可重试与永久失败
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index(
            "ux_notification_candidate", "event_id", "recipient", unique=True
        ),
        Index("ix_notification_status", "status"),
    )
