"""pilot 域请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PilotConfigPayload(BaseModel):
    enabled: bool
    base_url: str
    model: str
    has_api_key: bool
    monthly_cap: int
    usage_month: int


class PilotConfigUpdate(BaseModel):
    """api_key 缺省 = 不改动；显式空串 = 清空（router 按 fields_set 区分）。"""

    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    monthly_cap: int | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    appid: int | None = None
    # 领航台会话 id：前端每次开抽屉生成一次，同一会话内追问/指代靠它串起
    session_id: str | None = Field(default=None, max_length=64)


class AskResponse(BaseModel):
    answer: str
    """推理模型的思维链原文（reasoning_content）；普通模型或降级路径为 null。"""
    thinking: str | None = None
    source: str
    reason: str | None = None
    facts: dict | None = None
    cached: bool = False
