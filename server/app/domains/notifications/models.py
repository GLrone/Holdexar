"""通知域模型：价格事件的通知候选 + 内容链事实通知。

`price_events` 是事实（只增不改、没有渠道状态）；候选表达「这个事实经过用户
策略判断后具备通知资格」。**通知状态只落在候选上，绝不回写事件**——同一个
事实可以不通知、可以进邮件、可以进摘要，事实本身不被渠道状态污染。

`fact_notices` 是内容链（HB 当月包 / Epic 喜加一）的事实变化记录：变更判定在
数据链路里做一次（metadata 域两链的记账/快照替换点），灵动岛与后续渠道各自
消费同一行事实，不再各判一遍。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FactNotice(Base):
    """一条内容链事实变化。只增不改：没有确认/删除/状态流转。

    身份 = `fact_key`（如 ``hb_choice:september_2026_choice``）：同一事实重复
    进入（调度重跑 / 手动触发 / 快照重拉）落不进第二行。`data` 存消费方参数
    （标签、数量、条目名等），文案由消费端按 `source`/`kind` 翻译。
    """

    __tablename__ = "fact_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24))
    fact_key: Mapped[str] = mapped_column(Text, unique=True)
    data: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime)


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
