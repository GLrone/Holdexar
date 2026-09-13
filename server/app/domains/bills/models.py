"""bills 域模型：Steam 完整账单（导入快照 + 解析后的交易/充值/CDK 行）。

语义对齐 E:\\汇率档案\\汇率导出脚本 的解析产物；
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
    nickname: Mapped[str] = mapped_column(String(120), default="")
    avatar_base64: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str] = mapped_column(String(300), default="")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 解析汇总（导入时计算冻结；明细行变更后可重算）
    game_net_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    game_spend_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    game_refund_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    fx_missing: Mapped[int] = mapped_column(Integer, default=0)
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
    # 新导出字段（与 Steam 消费历史分类器对齐）
    wallet_balance: Mapped[str] = mapped_column(String(60), default="")  # 行余额列原文（钱包曲线用）
    base_price: Mapped[str] = mapped_column(String(60), default="")  # 外区基准价原文（转区对比用）
    payment_parts_json: Mapped[str | None] = mapped_column(Text)  # 混合支付拆分 [{pm, amount}]
    gift_to_json: Mapped[str | None] = mapped_column(Text)  # 新 UI 礼物受赠人（含多个）
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
    """零售 CDK / 礼物 / 免费入库（账单无金额，手动计价 fen 列；free 只展示不计价）。"""

    __tablename__ = "bill_cdk_games"
    __table_args__ = (Index("ix_bcg_import_acq", "import_id", "acq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(300))
    # 许可 appid（steam_fetch 从名称列商店链接提取；老快照/外部导出为 NULL
    # → 前端命中关联退回名字粗匹配）
    appid: Mapped[int | None] = mapped_column(BigInteger)
    date: Mapped[str] = mapped_column(String(20), default="")
    acq: Mapped[str] = mapped_column(String(10))  # cdk | gift | free
    acq_label: Mapped[str] = mapped_column(String(40), default="")
    manual_fen: Mapped[int | None] = mapped_column(BigInteger)  # 手动录入实付（分）
