"""family 域模型：家庭组档案 + 家庭库快照。

family_groups 按 steamid 一行存该账号所在的家庭组快照（成员列表 +
组名 + 同步时间），供家庭页与归属判定读取。

family_library_snapshots 家庭库持久化兜底（离线可看语义）：每次实时聚合后 upsert，进程重启/Cookie 失效时从快照
读——不再每次启动现拉 30s。核心字段 time_acquired（rt_time_acquired，
入库时间）与 owners_json（GetSharedLibraryApps 原序）是热力图/购买
动态/贡献分布的口径来源，Steam 侧历史事实，丢了不可再生。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FamilyGroup(Base):
    __tablename__ = "family_groups"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    family_groupid: Mapped[str | None] = mapped_column(String(40), index=True)
    family_name: Mapped[str | None] = mapped_column(String(120))
    # [{steamid, role, persona_name, avatar_url, steamid64}]
    members_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 主账号 Cookie 是否还能拉到（拉不到时提示重绑）
    fetch_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(300))
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FamilyLibrarySnapshot(Base):
    """家庭库单 app 快照行（一个家庭组一 appid 一行）。

    owners_json 顺序 = GetSharedLibraryApps owner_steamids 原序（入库先后）；
    time_acquired = rt_time_acquired（最早入库时间戳，秒）。
    """

    __tablename__ = "family_library_snapshots"

    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    family_groupid: Mapped[str] = mapped_column(String(40), primary_key=True, index=True)
    name: Mapped[str | None] = mapped_column(String(300))
    # 入库先后序拥有者（Steam 原序；[0]=最早入库，[-1]=最近入库=购买动态口径）
    owners_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    time_acquired: Mapped[int] = mapped_column(BigInteger, default=0)
    presence: Mapped[int] = mapped_column(Integer, default=0)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
