"""account 域模型：多账号 Cookie 绑定表。

主账号 = bound_at 最早的账号（第一个绑定的，用户语义「我的大号」）；
当前账号（active）= 全局唯一 is_active=True 的操作账号，钱包/CDK/免费领取跟随。
两者解耦：切 active 不改变主账号。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SteamAccount(Base):
    __tablename__ = "steam_accounts"

    steam_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    # 登录态明文（sessionid / steamCountry / steamLoginSecure 三件套 +
    # steamRefresh_steam / steamRememberLogin 续期凭据），仅本机库；
    # 访问令牌约 24 小时到期，续期靠这枚凭据换新（见 session.py）
    cookies: Mapped[str] = mapped_column(String(4000), default="")
    persona_name: Mapped[str] = mapped_column(String(100), default="")
    avatar_url: Mapped[str] = mapped_column(String(300), default="")
    # 每账号独立钱包快照：{balance, balance_display, currency_code, region_code, ...}
    wallet_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # Steam 真实在线状态（miniprofile/GetPlayerSummaries 双通道，每分钟轮转刷新）：
    # 在线 = miniprofile.online 或 personastate 1-6；游戏中 = gameextrainfo/in_game
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    in_game: Mapped[str] = mapped_column(String(200), default="")
    bound_at: Mapped[datetime | None] = mapped_column(DateTime)
    wallet_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 最近一次钱包同步失败原因（空串 = 正常）；用于每分钟轮转的指数退避
    wallet_error: Mapped[str] = mapped_column(String(300), default="")
    # 递增退避级别：0=正常，每次失败 +1（成功清零）；间隔查
    # service._WALLET_BACKOFF_MINUTES（2→5→15→30min 封顶）
    wallet_backoff_level: Mapped[int] = mapped_column(Integer, default=0)
    # 钱包熔断连续失败计数（成功清零）：达 _WALLET_BREAKER_SLOW_AFTER 进
    # 10min 慢车道，达 _WALLET_BREAKER_FREEZE_AFTER 置 wallet_frozen 终态
    wallet_fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    # 熔断冻结终态：自动轮转不再请求该账号，等手动刷新余额或换绑 Cookie 解锁
    wallet_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
