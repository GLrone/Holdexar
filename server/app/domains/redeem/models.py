"""redeem 域模型：CDK 激活 / 免费领取的事件账本。

分工：本表是「激活事件」账（每次激活尝试的结果留痕）；
许可归属账在 bills 域 bill_cdk_games（账单导入侧），两者不互写。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Steam 回执原文入库上限（超出截断，防单行膨胀；回执主体信息在前列字段）
RAW_MAX = 8000


class RedeemActivation(Base):
    """一次激活 / 免费领取的结果行（成功、已拥有、失败都记）。"""

    __tablename__ = "redeem_activations"
    __table_args__ = (Index("ix_ra_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # cdk=CDK 激活 · free=免费领取
    kind: Mapped[str] = mapped_column(String(10), default="cdk")
    # ok | own | fail
    status: Mapped[str] = mapped_column(String(10))
    product_key: Mapped[str] = mapped_column(String(120), default="")
    subid: Mapped[str] = mapped_column(String(40), default="")
    sub_name: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(String(300), default="")
    raw: Mapped[str] = mapped_column(Text, default="")
    steamid: Mapped[str] = mapped_column(String(32), default="")
