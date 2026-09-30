"""pilot 域请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PilotConfigPayload(BaseModel):
    protocol: str
    enabled: bool
    base_url: str
    model: str
    models: list[str] = []
    has_api_key: bool
    monthly_cap: int
    usage_inp: int = 0
    usage_out: int = 0
    usage_calls: int = 0
    usage_total: int = 0


class PilotConfigUpdate(BaseModel):
    """api_key 缺省 = 不改动；显式空串 = 清空（router 按 fields_set 区分）。"""

    protocol: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    models: list[str] | None = None
    api_key: str | None = None
    monthly_cap: int | None = None


class DetectRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    protocol: str | None = None


class DetectResponse(BaseModel):
    protocol: str
    vendor: str
    models: list[str]
    suggested: list[str]
    key_valid: bool | None = None


class TestRequest(BaseModel):
    """连通性测试：字段缺省时回落到已存配置。"""

    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class TestResponse(BaseModel):
    ok: bool
    latency_ms: int
    model: str = ""
    reply: str = ""
    reason: str | None = None
    detail: str = ""


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
    """本轮工具取到的结构化卡片（games / price / action），供前端组件渲染。"""
    cards: list = []
    cached: bool = False
