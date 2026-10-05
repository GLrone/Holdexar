"""pilot 域请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PilotCompactMarker(BaseModel):
    after_turn: int
    trigger: str = "auto"
    ts: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None


class PilotProviderPayload(BaseModel):
    """供应商创建 / 更新载荷；api_key 三态（缺省不改 / 空串清空 / 有值重写）。

    context_window 缺省 = 不改；显式空 = 清除（回落固定历史预算）。"""

    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    models: list[str] | None = None
    models_disabled: list[str] | None = None
    api_key: str | None = None
    context_window: int | None = None


class PilotProviderOut(BaseModel):
    id: str
    name: str
    protocol: str
    base_url: str
    models: list[str] = []
    models_disabled: list[str] = []
    has_key: bool = False
    context_window: int | None = None


class PilotProviderListOut(BaseModel):
    items: list[PilotProviderOut] = []


class PilotConfigPayload(BaseModel):
    protocol: str
    enabled: bool
    base_url: str
    model: str
    models: list[str] = []
    models_disabled: list[str] = []
    has_api_key: bool
    monthly_cap: int
    context_window: int | None = None
    """活跃供应商的模型窗口（token，用户可选配置）；未知 = None（历史预算回落固定值）。"""
    usage_inp: int = 0
    usage_out: int = 0
    usage_calls: int = 0
    usage_total: int = 0
    active: str = ""
    """活跃供应商 id（领航台在用的那家；无供应商账本时为空）。"""
    providers: list[PilotProviderOut] = []
    """供应商清单投影（has_key 布尔，不含密钥原文）。"""


class PilotConfigUpdate(BaseModel):
    """api_key 缺省 = 不改动；显式空串 = 清空（router 按 fields_set 区分）。"""

    protocol: str | None = None
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    models: list[str] | None = None
    models_disabled: list[str] | None = None
    api_key: str | None = None
    monthly_cap: int | None = None
    context_window: int | None = None
    """无供应商账本时的存量单键写路径；显式空 = 清除（回落固定历史预算）。"""
    active: str | None = None
    """切换活跃供应商（切换时模型自动重置为新家启用清单）。"""


class DetectRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    protocol: str | None = None
    """密钥缺省时按 provider_id 取该供应商已存密钥，不回落活跃供应商。"""
    provider_id: str = ""


class DetectResponse(BaseModel):
    protocol: str
    vendor: str
    models: list[str]
    suggested: list[str]
    key_valid: bool | None = None
    """探测失败原因：key_invalid / not_found / upstream / unreachable；成功为 None。"""
    reason: str | None = None


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
    # 跨会话引用：本轮把另一段会话的要点摘要注入上下文（一次性，不入被引会话）
    reference_sid: str | None = Field(default=None, max_length=64)


class AskResponse(BaseModel):
    answer: str
    """推理模型的思维链原文（reasoning_content）；普通模型或降级路径为 null。"""
    thinking: str | None = None
    think_ms: int | None = None
    """思考工时（毫秒，思考通道活跃墙钟时间累计）；无思考/降级轮为 null。"""
    source: str
    reason: str | None = None
    facts: dict | None = None
    """本轮工具取到的结构化卡片（games / price / action），供前端组件渲染。"""
    cards: list = []
    """本轮 agent 时间线（tools.tool_step 产出，由 phases 展平而得）；降级路径为空列表。"""
    steps: list = []
    """本轮按循环步分段的阶段记录（step / thinking / text / think_ms / truncated / terminal / steps）；thinking 与 steps 均由它派生。"""
    phases: list = []
    cached: bool = False
    ctx_tokens: int | None = None
    """本轮发给模型的上下文估算 token 数（agent 路径才有；降级轮为 null）。"""
    ctx_budget: int | None = None
    """上下文历史预算（装配让位基准），前端状态栏的分母。"""
    elapsed_ms: int | None = None
    """本轮总耗时（毫秒，agent 路径墙钟，含思考与工具执行）；降级轮为 null。"""
    archived_through: int | None = None
    """本轮完成时已归档进要点存档的轮数边界（过程链记忆条目用）；降级轮为 null。"""
    ctx_breakdown: list | None = None
    """上下文构成估算（五来源字符数，agent 路径才有）；前端分段条占比口径。"""
    cache_hit_rate: float | None = None
    """输入缓存命中率（0-1，provider 报告缓存 token 时才有；本轮最近一次请求口径）。"""
    usage_in: int = 0
    """本轮输入 token 合计（provider 用量口径，含缓存部分；未回传用量为 0）。"""
    usage_out: int = 0
    """本轮输出 token 合计（provider 回传口径；未回传为 0）。"""
    cache_read_tokens: int | None = None
    """本轮缓存读 token 合计（跨请求累计；provider 从未报告为 null）。"""
    cache_write_tokens: int | None = None
    """本轮缓存写 token 合计（仅 Anthropic 系报告；从未报告为 null）。"""
    cache_base_tokens: int | None = None
    """本轮计费输入总量（含缓存读/写部分，缓存命中率分母口径）；无用量回传为 null。"""
    decode_ms: int = 0
    """生成墙钟合计（毫秒，各请求首个内容增量→流结束；仅使用量同步回传的请求计入）。"""
    decode_out: int = 0
    """计入生成墙钟的那些请求的输出 token 合计（decode_ms 的配对分子）。"""
    ttft_ms: int = 0
    """首字延迟合计（毫秒，请求起点→首个内容增量，跨请求累计）。"""
    ttft_n: int = 0
    """计入首字延迟的请求数（ttft_ms 的均值分母）。"""
    title: str | None = None
    """当前会话生效标题（账本 title 行或首问兜底）；前端标题栏实时显示用。"""


class PilotProposalConfirm(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    pid: str = Field(min_length=1, max_length=32)
    approve: bool = True


class PilotProposalOut(BaseModel):
    ok: bool
    state: str = ""
    action: str = ""
    total: int = 0
    done: int = 0
    failed: list = []
    reason: str | None = None


class PilotSessionListItem(BaseModel):
    sid: str
    title: str = ""
    turn_total: int = 0
    updated_at: str | None = None
    """账本文件 mtime（ISO）：最近一次落账时刻，列表展示用。"""
    running: bool = False
    """ask 流在途（agent 正在生成本会话回复）。"""


class PilotSessionListOut(BaseModel):
    items: list[PilotSessionListItem] = []


class PilotSessionTitleUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=60)


class PilotSessionTurn(BaseModel):
    """会话账本里的一轮问答（用户问题 + 终态投影），前端按其还原历史轮。"""

    q: str
    resp: AskResponse
    ts: str | None = None


class PilotProposalState(BaseModel):
    """提议终态投影（pid → 状态/成功数）：历史轮卡片按它校正，失效提议不再可点。"""

    pid: str
    state: str
    done: int | None = None
    failedCount: int = 0


class PilotSessionStats(BaseModel):
    """会话级统计投影（对账本全部轮次的只读折叠，读取侧现算，不另立账）。"""

    turns: int = 0
    steps: int = 0
    """工具执行步合计（各轮 steps 展平计数）。"""
    elapsed_ms: int = 0
    """整轮工时合计（各轮 elapsed_ms 求和，含思考与工具执行）。"""
    ttft_ms: int = 0
    ttft_n: int = 0
    decode_ms: int = 0
    decode_out: int = 0
    """速度口径 = decode_out / decode_ms（加权聚合，非各轮速率均值）。"""
    usage_in: int = 0
    usage_out: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_base_tokens: int | None = None
    """计费输入合计（缓存命中率分母；为 null 时无命中率可言）。"""

    @property
    def cache_hit(self) -> float | None:
        if self.cache_read_tokens is None or not self.cache_base_tokens:
            return None
        return min(1.0, self.cache_read_tokens / self.cache_base_tokens)


class PilotSessionOut(BaseModel):
    session_id: str
    turns: list[PilotSessionTurn] = []
    updated_at: str | None = None
    turn_total: int = 0
    """账本里的总轮数。"""
    summary_through: int = 0
    """已压缩进要点存档的轮数边界（前 N 轮不再进模型上下文原文）。"""
    markers: list[PilotCompactMarker] = []
    """压缩边界列表（按发生顺序）；前端在对话流对应位置插分隔线。"""
    title: str = ""
    """会话生效标题（账本 title 行或首问兜底）。"""
    proposals: list[PilotProposalState] = []
    """本会话出现过的批量提议终态（按 pid），历史轮卡片据此校正可点性。"""
    stats: PilotSessionStats | None = None
    """全部轮次的统计折叠（底栏统计与消耗明细的种子；空会话为 null）。"""


class ToolRunRequest(BaseModel):
    """快捷动作直达：+ 菜单点选的工具名（须在 QUICK_TOOLS 白名单内）。"""
    name: str
    label: str = ""
    session_id: str | None = None


class ToolRunResponse(BaseModel):
    step: dict
    cards: list = []
