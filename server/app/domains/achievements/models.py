"""achievements 域模型：成就定义 + 玩家解锁态 + 游戏层汇总。

三张表分工：
- achievement_defs：成就定义（来自公开社区成就页），一行一成就。主键是
  图标资产名 image_name——公开页不给 apiname，图标文件名（`XXX.jpg` /
  锁定态 `XXX_BW.jpg`）是两页之间唯一稳定且可跨同步保持的行标识。
- achievement_states：玩家解锁态（来自公开个人成就页），一账号一成就一行。
- achievement_games：游戏层汇总（时长/最近游玩/成就进度），列表与 KPI 主表。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AchievementDef(Base):
    __tablename__ = "achievement_defs"

    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # 图标资产名（不含扩展名与 _BW 后缀），如 SURVIVE_CONTAINER_RIDE
    image_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    # 游戏内 API 名（GetGameAchievements 的 internal_name）：解锁态按它精确对上；
    # 仅社区页爬虫通道补进来的定义为空
    apiname: Mapped[str | None] = mapped_column(String(160))
    name: Mapped[str | None] = mapped_column(String(400))
    description: Mapped[str | None] = mapped_column(String(800))
    icon_url: Mapped[str | None] = mapped_column(String(500))
    # 锁定态灰图（个人页 `_BW` 资产；缺失时前端对彩图做灰度降级）
    icon_gray_url: Mapped[str | None] = mapped_column(String(500))
    # 全服解锁占比 0~100（公开成就页）
    global_percent: Mapped[float | None] = mapped_column(Float)
    # 全局页展示序（按占比降序）
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AchievementState(Base):
    __tablename__ = "achievement_states"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    image_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    achieved: Mapped[bool] = mapped_column(Boolean, default=False)
    # 解锁时刻（epoch 秒；0 = 未解锁）。来源为页面本地化时间文本，分钟级精度
    unlock_time: Mapped[int] = mapped_column(BigInteger, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AchievementGame(Base):
    __tablename__ = "achievement_games"

    steamid: Mapped[str] = mapped_column(String(20), primary_key=True)
    appid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(400))
    playtime_min: Mapped[int] = mapped_column(Integer, default=0)
    last_played: Mapped[int] = mapped_column(BigInteger, default=0)
    total_achievements: Mapped[int] = mapped_column(Integer, default=0)
    unlocked: Mapped[int] = mapped_column(Integer, default=0)
    platinum: Mapped[bool] = mapped_column(Boolean, default=False)
    # 来源：owned=本号已购（含玩过的免费游戏）；shared=库外（家庭共享等，
    # 本号未拥有但有解锁）；manual=手动补录。库外行 playtime_min 恒为 0
    # （Steam 不提供非拥有游戏的时长），前端据此标注「库外·时长未知」。
    source: Mapped[str] = mapped_column(String(12), default="owned")
    # 定义清单上次抓取时刻（社区页变更慢，按龄触发增量刷新）
    schema_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 解锁态上次抓取时刻
    state_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)