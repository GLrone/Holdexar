"""Agent Runtime API 模型（最小运行面）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AgentRunCreate(BaseModel):
    session_id: str | None = None
    prompt: str | None = Field(default=None, max_length=2000)
    trigger: str = "manual"
    # fake 执行器参数（scenario：success / failure）；真实执行器接入后
    # 由其专属参数取代
    scenario: str = "success"
    steps: int = Field(default=3, ge=1, le=20)
    step_delay: float = Field(default=0.0, ge=0, le=5.0)


class AgentRunCreated(BaseModel):
    run_id: str
    session_id: str
    status: str


class AgentRunView(BaseModel):
    run_id: str
    session_id: str
    runner: str
    trigger: str
    meta: dict | None = None
    status: str
    error_code: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AgentEventItem(BaseModel):
    seq: int
    event_type: str
    payload: dict | None = None
    created_at: datetime | None = None


class AgentEventPage(BaseModel):
    run_id: str
    items: list[AgentEventItem]


class AgentCancelRequest(BaseModel):
    reason: str = Field(default="user", max_length=100)


class AgentCancelResult(BaseModel):
    """accepted = 本轮是否新受理的取消请求（重复请求为 False）；
    status = 响应时刻的运行状态（取消收敛异步完成）。"""

    accepted: bool
    status: str
