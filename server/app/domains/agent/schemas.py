"""Agent Runtime API 模型（最小运行面）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AgentRunCreate(BaseModel):
    session_id: str | None = None
    prompt: str | None = Field(default=None, max_length=2000)
    trigger: str = "manual"
    # 执行器：fake（Runtime 骨架）/ task（登记表内的真实长任务）
    runner: str = Field(default="fake", pattern="^(fake|task)$")
    # task 执行器参数（登记表 kind）；fake 参数 scenario/steps/step_delay
    task: str | None = Field(default=None, max_length=32)
    ref: dict | None = None
    # fake 执行器参数（scenario：success / failure）
    scenario: str = "success"
    steps: int = Field(default=3, ge=1, le=20)
    step_delay: float = Field(default=0.0, ge=0, le=5.0)


class AgentTaskItem(BaseModel):
    """可调度任务形态（登记表投影：kind 是静态白名单，由后端给出）。"""

    kind: str
    label: str


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


class AgentRunListOut(BaseModel):
    items: list[AgentRunView]


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


class AgentMemoryItem(BaseModel):
    id: str
    category: str
    key: str
    value: dict
    confidence: float
    source: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentMemoryUpsert(BaseModel):
    category: str = Field(..., max_length=32)
    key: str = Field(..., max_length=64)
    value: dict | list | str | int | float | bool
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str | None = Field(default=None, max_length=64)


class AgentMemoryListOut(BaseModel):
    items: list[AgentMemoryItem]

