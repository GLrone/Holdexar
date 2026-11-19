"""bills 域模型：Steam 完整账单（导入快照 + 解析后的交易/充值/CDK 行）。

字段语义与解析器输出一一对应；
金额一律**整数分**（CNY 分），原币金额以 TEXT 保留精度（如 960.70 KZT）。
一次导入 = 一份账单（bill_imports），行级数据挂账单 ID，可多账单比对。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BillImport(Base):
    """一次 Steam_Report_*.json 导入（快照）。"""

    __tablename__ = "bill_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    steam_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    nickname: Mapped[str] = mapped_column(String(120), default="")
    avatar_base64: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str] = mapped_column(String(300), default="")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")


class BillGameTx(Base):
    """游戏交易行（购买/礼物购买/内购/退款）。sign: +1 支出 / -1 退款冲回。"""

    __tablename__ = "bill_game_txs"
    __table_args__ = (
        Index("ix_bgt_import_date", "import_id", "date"),
        Index("ix_bgt_import_type", "import_id", "tx_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[str] = mapped_column(String(20))
    tx_type: Mapped[str] = mapped_column(String(20))
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    currency: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Float)
    sign: Mapped[int] = mapped_column(Integer, default=1)
    cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    fx_rate: Mapped[float | None] = mapped_column(Float)
    fx_note: Mapped[str] = mapped_column(String(60), default="")
    payment: Mapped[str] = mapped_column(String(120), default="")
    discount_pct: Mapped[str] = mapped_column(String(20), default="")
    orig_price: Mapped[float | None] = mapped_column(Float)
    wallet_balance: Mapped[str] = mapped_column(String(60), default="")
    base_price: Mapped[str] = mapped_column(String(60), default="")
    payment_parts_json: Mapped[str | None] = mapped_column(Text)  # 混合支付拆分 [{pm, amount}]
    gift_to_json: Mapped[str | None] = mapped_column(Text)
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False)
    orig_is_gift: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_idx: Mapped[int] = mapped_column(Integer, default=0)


class BillTopupTx(Base):
    """钱包充值 / 充值退款流水（不计入游戏支出，防双计）。"""

    __tablename__ = "bill_topup_txs"
    __table_args__ = (Index("ix_btt_import_date", "import_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[str] = mapped_column(String(20))
    desc: Mapped[str] = mapped_column(String(300), default="")
    currency: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Float)
    sign: Mapped[int] = mapped_column(Integer, default=1)
    cny_fen: Mapped[int | None] = mapped_column(BigInteger)
    fx_rate: Mapped[float | None] = mapped_column(Float)
    fx_note: Mapped[str] = mapped_column(String(60), default="")
    payment: Mapped[str] = mapped_column(String(120), default="")
    tx_type: Mapped[str] = mapped_column(String(20), default="钱包充值")
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False)


class BillCdkGame(Base):
    """零售 CDK / 礼物 / 免费入库（账单无金额，手动计价 fen 列；free 只展示不计价）。

    本表是「许可归属」账（账单导入侧的库存事实）；激活动作的事件留痕在
    redeem 域 redeem_activations（激活事件账），两者分工不同、互不回写。
    """

    __tablename__ = "bill_cdk_games"
    __table_args__ = (Index("ix_bcg_import_acq", "import_id", "acq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    appid: Mapped[int | None] = mapped_column(BigInteger)
    date: Mapped[str] = mapped_column(String(20), default="")
    acq: Mapped[str] = mapped_column(String(10))  # cdk | gift | free
    acq_label: Mapped[str] = mapped_column(String(40), default="")
    manual_fen: Mapped[int | None] = mapped_column(BigInteger)
