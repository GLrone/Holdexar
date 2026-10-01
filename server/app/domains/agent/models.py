"""Agent Runtime 三表账本：会话 / 运行 / 事件。

账本连续性设计（一事实一主账本，多处投影）：
- `agent_events` 是唯一事实流水（append-only）：一次运行的生命周期、
  步进、用户输入、取消请求全部落事件行，`(run_id, seq)` 唯一定位，
  seq 在 run 内单调递增——回放 = 按 seq 升序读。
- `agent_runs` 是运行聚合投影：当前状态、起止时间、错误码由状态机
  跃迁入口（service.apply_transition）与事件同事务写入，绝不散落赋值。
  对话消息历史从事件派生，不建平行 message 表。
- `agent_sessions` 是会话载体；context_snapshot / context_version /
  context_updated_at 是上下文快照扩展点（完整 context manager 后续
  接入），恢复不依赖「重放全部历史事件」。

保留策略：P1 不自动清理；session 删除级联删除其 runs 与 events
（FK ondelete=CASCADE，引擎侧 foreign_keys=ON）。状态枚举与合法迁移
见 runtime/state.py，事件类型与 payload 边界见 runtime/events.py。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentSession(Base):
    """会话：跨运行的上下文载体。id 为服务端生成的 uuid4 hex。"""

    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    # 上下文快照扩展点：快照内容 / 快照版本号 / 快照写入时刻。
    # 空快照 = 会话仍以事件回放为唯一恢复路径。
    context_snapshot: Mapped[dict | None] = mapped_column(JSON)
    context_version: Mapped[int] = mapped_column(Integer, default=0)
    context_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentRun(Base):
    """运行：一次有账本的执行。状态只能经状态机跃迁入口变更。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_sessions.id", ondelete="CASCADE")
    )
    # 执行器标识：fake（Runtime 骨架验证）→ 后续阶段接入真实执行器
    runner: Mapped[str] = mapped_column(String(16), default="fake")
    # 触发来源：manual / scheduled / event
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    # 执行器专属参数（fake：scenario/steps/step_delay；真实执行器：
    # model/protocol 等），随 runner 类型自解释，不另开列
    meta: Mapped[dict | None] = mapped_column(JSON)
    # queued / running / awaiting_approval / done / failed / cancelled /
    # budget_exhausted（合法迁移表见 runtime/state.py）
    status: Mapped[str] = mapped_column(String(24), default="queued")
    # 终态错误码（failed 的机器归因；cancelled 无错误码）
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_agent_run_session", "session_id", "created_at"),
        Index("ix_agent_run_status", "status"),
    )


class AgentEvent(Base):
    """事件流水：一次运行的事实账本（append-only，只插不改不删）。

    seq 由写入口在写事务内按 max(seq)+1 分配，run 内从 1 单调递增；
    (run_id, seq) 唯一约束既防重复分配，也是「按 run + seq 定位、
    从任意 seq 续读」的回放基础。payload 经 runtime/events.py 的
    入账边界（白名单键 + 敏感键剔除 + 体积上限）后才落库。
    """

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="ux_agent_event_run_seq"),
    )
