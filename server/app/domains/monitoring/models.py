"""monitoring 域模型：Catalog 之上的监控生命周期。

Catalog（games / bundles）只表达「Holdexar 知道这个对象存在」，不等于用户
想让它持续参与价格爬取。本域把三件事拆成三张表：

- `monitor_sources`：谁想监控它。一行 = 一个 (target, source) 来源；同一
  target 可有多个来源，多账户的同一 appid 只落成一行（家族愿望单去重）。
- `monitor_targets`：监控对象当前状态。一行 = 一个 target，状态在
  active / released / excluded 之间迁移，重新激活复用同一行。
- `monitor_exclusions`：用户明确不要监控。排除不是删除——只有显式用户操作
  才落行；永久免费 / 下架是业务状态，由 `games.free_kind` / `games.removed_at`
  表达，不写这里。

target_type 当前只取 `game` / `bundle`；target_id 分别是 appid / bundle_id。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 监控对象类型。只做 game / bundle 两种，不泛化到 DLC / 系列 / 发行商。
TARGET_GAME = "game"
TARGET_BUNDLE = "bundle"
TARGET_TYPES = (TARGET_GAME, TARGET_BUNDLE)

# 监控状态
STATE_ACTIVE = "active"       # 有有效来源且未被排除 → 有资格进入 crawl
STATE_RELEASED = "released"   # 没有任何有效来源 → 留在 Catalog，不进 crawl
STATE_EXCLUDED = "excluded"   # 用户明确排除 → 无论来源如何都不进 crawl
STATES = (STATE_ACTIVE, STATE_RELEASED, STATE_EXCLUDED)

# 不进 crawl 的两个状态（refresh_bundles 的候选集过滤用）
NON_CRAWL_STATES = (STATE_RELEASED, STATE_EXCLUDED)


class MonitorTarget(Base):
    """监控对象：一个 (target_type, target_id) 只有一行，状态就地迁移。"""

    __tablename__ = "monitor_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(16), default=TARGET_GAME)
    target_id: Mapped[int] = mapped_column(BigInteger)
    # active / released / excluded
    state: Mapped[str] = mapped_column(String(16), default=STATE_RELEASED)
    # 由当前有效来源里的最高优先级推导；只决定 crawl 队列先后，
    # 不决定监控资格（资格由 state 独占表达）
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ux_monitor_target", "target_type", "target_id", unique=True),
        Index("ix_monitor_target_state", "target_type", "state"),
        Index("ix_monitor_target_order", "state", "priority", "target_id"),
    )


class MonitorSource(Base):
    """监控来源：家族愿望单 / 星标关注 / 手动入池 / 已购 / 导入 / 榜单。

    多账户的同一 appid 只保留一行（唯一约束按 target + source），
    「任一账户仍想要」= 该来源有效——这是家族愿望单去重的落点。
    """

    __tablename__ = "monitor_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(16), default=TARGET_GAME)
    target_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32))
    # 来源优先级快照（SOURCE_PRIORITY 的当前值），排序时取 max 免二次查表
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ux_monitor_source", "target_type", "target_id", "source", unique=True),
        Index("ix_monitor_source_target", "target_type", "target_id"),
        Index("ix_monitor_source_lookup", "source", "active"),
    )


class MonitorExclusion(Base):
    """监控排除：用户明确表达「我知道它存在，但别再爬它」。

    同一 target 同时只允许一行 active（部分唯一索引）；解除排除只翻
    active 并记 cleared_at，行本身留作历史——排除与解除都可回溯。
    """

    __tablename__ = "monitor_exclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(16), default=TARGET_GAME)
    target_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 解除排除的时刻；active=1 时为 NULL
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index(
            "ux_monitor_exclusion_active",
            "target_type",
            "target_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
        Index("ix_monitor_exclusion_target", "target_type", "target_id"),
    )
