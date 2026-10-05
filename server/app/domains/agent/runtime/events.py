"""事件类型与 payload 入账边界。

agent_events 是事实账本，不是无限制 raw log。入账规则（build_payload
统一施加，写入口之外无第二条路径）：

1. 白名单键：每类事件只接受 EVENT_FIELDS 声明的键，其余键丢弃——
   新事件类型或新字段先在此登记，防止账本结构漂移；
2. 敏感键剔除：键名命中敏感词形（凭据 / Cookie / 令牌 / 密钥 /
   授权头 / 代理凭据）一律不入账——账本不承载凭据，敏感引用由
   调用方改为摘要或机器码；
3. 体积上限：序列化超过 _MAX_PAYLOAD_BYTES 的 payload 换成
   {truncated, bytes} 摘要——巨型工具原始输出以摘要落账，全文留在
   产生它的域内。

推理原文（thinking）不属于任何事件类型的白名单键，默认不入账；
对话正文（用户输入）经 turn.received 落账，是消息历史派生的依据。
"""
from __future__ import annotations

import json
import re

EV_RUN_STATE = "run.state"
EV_RUN_FINISHED = "run.finished"
EV_TURN_RECEIVED = "turn.received"
EV_STEP_STARTED = "step.started"
EV_STEP_COMPLETED = "step.completed"
EV_CANCEL_REQUESTED = "run.cancel_requested"
EV_TASK_STARTED = "task.started"
EV_TASK_PROGRESS = "task.progress"
EV_TASK_FAILED = "task.failed"

EVENT_TYPES = (
    EV_RUN_STATE,
    EV_RUN_FINISHED,
    EV_TURN_RECEIVED,
    EV_STEP_STARTED,
    EV_STEP_COMPLETED,
    EV_CANCEL_REQUESTED,
    EV_TASK_STARTED,
    EV_TASK_PROGRESS,
    EV_TASK_FAILED,
)

# 事件类型 → 允许入账的 payload 键集（lifecycle / message / step / task /
# cancel 元数据各归其类；usage / tool / approval / decision 元数据
# 随对应阶段接入时在此登记）
EVENT_FIELDS: dict[str, frozenset[str]] = {
    EV_RUN_STATE: frozenset({"from", "to", "reason"}),
    EV_RUN_FINISHED: frozenset({"status", "error_code"}),
    EV_TURN_RECEIVED: frozenset({"text", "appid"}),
    EV_STEP_STARTED: frozenset({"step"}),
    EV_STEP_COMPLETED: frozenset({"step", "summary"}),
    EV_CANCEL_REQUESTED: frozenset({"reason", "by"}),
    # 任务类事件只记调度面事实（哪个任务、采样进度）；业务事实仍归域账本
    EV_TASK_STARTED: frozenset({"kind", "label", "total", "ref"}),
    EV_TASK_PROGRESS: frozenset({"phase", "done", "total", "note"}),
    EV_TASK_FAILED: frozenset({"reason", "error_code"}),
}

_SENSITIVE_KEY_RE = re.compile(
    r"api[_-]?key|cookie|token|secret|password|passwd|authorization|credential|proxy[_-]?url",
    re.IGNORECASE,
)
_MAX_PAYLOAD_BYTES = 8192


def build_payload(event_type: str, payload: dict | None) -> dict:
    """payload 入账边界：白名单键 → 敏感键剔除 → 体积上限。

    返回新 dict，绝不修改调用方传入对象。"""
    allowed = EVENT_FIELDS.get(event_type)
    clean: dict = {}
    for key, value in (payload or {}).items():
        if allowed is not None and key not in allowed:
            continue
        if _SENSITIVE_KEY_RE.search(str(key)):
            continue
        if value is None:
            continue
        clean[key] = value
    try:
        size = len(json.dumps(clean, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return {"unserializable": True}
    if size > _MAX_PAYLOAD_BYTES:
        return {"truncated": True, "bytes": size}
    return clean
