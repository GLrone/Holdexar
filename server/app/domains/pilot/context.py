"""pilot 上下文装配：估算、裁剪、预算与压缩。

派生视图纪律：会话文件是唯一事实源（session.py），本模块只产出「发给
模型的视图」——工具结果按需裁剪、超预算时让位，任何裁剪都不改动存储。

- 估算 estimate_tokens 是唯一计价权威（CJK 感知启发式），预算判断、
  裁剪收敛、压缩触发都消费同一份测量；
- 装配序：要点存档（system）> 未存档轮次原文 > 当前问题；超预算从最老
  未存档轮次整轮让位，始终保留最近 RECENT_KEEP_TURNS 轮逐字原文；
- 压缩 compact 把更早轮次蒸馏成要点存档（LLM，与既有存档合并去陈），
  由服务层落盘；失败返回 None，装配退回「整轮让位」，问答不中断。
"""
from __future__ import annotations

import json

from app.domains.pilot import llm as pilot_llm

HISTORY_BUDGET_TOKENS = 6000
# 窗口派生的历史预算边界与预留：窗口 − 回答预留 − 安全垫 − 非历史开销，
# 夹在 [MIN, MAX]——问答型领航员不随大窗口无限扩历史（每轮都按 token 付费）
_HISTORY_BUDGET_MIN = 2000
_HISTORY_BUDGET_MAX = 12000
_OUTPUT_RESERVE_TOKENS = 2048
_WINDOW_BUFFER_TOKENS = 1024
RECENT_KEEP_TURNS = 2
TOOL_CONTENT_LIMIT = 3000
COMPACT_MAX_TOKENS = 380
REFERENCE_BUDGET_TOKENS = 800

_SUMMARY_FRAME = "本会话因上下文预算进行过压缩：以下摘要把更早的对话浓缩为背景，之后是最近几轮的原文。摘要不是当前请求，请结合它继续对话：\n"

_COMPACT_INSTRUCTION = (
    "请把以下此前对话轮次蒸馏成一份要点存档，供同一会话的后续提问作为背景。"
    "固定小节（无内容写「无」）：对话主线 / 涉及游戏 / 已执行动作 / "
    "关键价格事实（保留精确数值与 AppID）/ 用户偏好与未决事项。"
    "只依据对话内容，不新增信息，总长不超过 300 字。\n"
)

_OVERFLOW_MARKERS = (
    "context length", "context_length", "maximum context", "context window",
    "too many tokens", "input length", "prompt is too long", "reduce the length",
)


def user_text(question: str, appid: int | None) -> str:
    """当前问题 → 用户消息文本（游戏详情页进入时附带对象上下文）。"""
    return question if not appid else f"{question}（用户当前正在看 AppID {appid} 的游戏详情）"


def summary_message(summary: str) -> dict:
    """要点存档 → user 消息（ZCode 压缩语义：摘要以 user 角色衔接在历史前，
    system 位留给永不压缩的系统提示；连续 user 消息各协议均接受）。"""
    return {"role": "user", "content": _SUMMARY_FRAME + summary}


def derive_history_budget(context_window: int | None, overhead_tokens: int = 0) -> int:
    """模型窗口 → 历史预算：窗口 − 回答预留 − 安全垫 − 非历史开销（system/工具表）。

    窗口未知（未配置或非法）回落 HISTORY_BUDGET_TOKENS；派生值夹在
    [MIN, MAX]——小窗口模型靠下限保住让位机制的腾挪空间，大窗口靠上限
    控制单轮成本。"""
    if not context_window or context_window <= 0:
        return HISTORY_BUDGET_TOKENS
    return max(
        _HISTORY_BUDGET_MIN,
        min(
            _HISTORY_BUDGET_MAX,
            context_window - _OUTPUT_RESERVE_TOKENS - _WINDOW_BUFFER_TOKENS - max(0, overhead_tokens),
        ),
    )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef")
    return cjk + (len(text) - cjk) // 4 + 1


def estimate_messages(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += 4
        total += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(json.dumps(tc.get("arguments") or {}, ensure_ascii=False)) + 8
    return total


def prune_tool_content(text: str, limit: int = TOOL_CONTENT_LIMIT) -> str:
    """超长工具结果保留头尾、省略中段（按码元切分，不破坏代理对）。"""
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return f"{text[:head]}\n…[中间内容过长已省略]…\n{text[len(text) - tail:]}"


def looks_like_overflow(err_text: str) -> bool:
    lowered = (err_text or "").lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _replay_line(turn: dict) -> dict | None:
    """无消息轮次（快捷动作/导航直通/降级轮）→ 单条回放消息。

    这些轮次没有模型消息可放，但卡片与工具事实用户真实看到过——不回放
    就是账本断裂（模型不知道界面上发生过什么）。按 _turn_line 同源口径
    合成一条 user 消息，标注「非当前请求」防模型误答。"""
    resp = turn.get("resp") or {}
    if not (resp.get("tools") or resp.get("cards") or resp.get("answer")):
        return None
    parts = [f"用户：{turn.get('q') or ''}"]
    if resp.get("answer"):
        parts.append(f"助手：{resp['answer']}")
    if resp.get("tools"):
        parts.append("工具：" + "；".join(str(x) for x in resp["tools"]))
    if resp.get("cards"):
        kinds = [str(c.get("kind") or "") for c in resp["cards"] if isinstance(c, dict)]
        parts.append("已生成界面卡片：" + "、".join(k for k in kinds if k))
    return {"role": "user", "content": "[系统回放·非当前请求] " + "\n".join(parts)}


def _turn_view(turn: dict) -> list[dict]:
    msgs = turn.get("messages") or []
    if not msgs:
        replay = _replay_line(turn)
        return [replay] if replay else []
    out = []
    for m in msgs:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            content = prune_tool_content(m["content"])
            if content != m["content"]:
                out.append({**m, "content": content})
                continue
        out.append(m)
    return out


def _turn_line(turn: dict) -> str:
    resp = turn.get("resp") or {}
    parts = [f"用户：{turn.get('q') or ''}"]
    answer = str(resp.get("answer") or "")
    tools = [str(x) for x in resp.get("tools") or []]
    if answer:
        parts.append(f"助手：{answer}")
    if tools:
        parts.append("工具：" + "；".join(tools))
    if not answer and not tools:
        parts.append("助手：（事实摘要轮，无模型回答）")
    return "\n".join(parts)


def assemble(state, question: str, appid: int | None, *,
             budget: int | None = None) -> list[dict]:
    """会话状态 + 当前问题 → system 之后的模型消息序列（预算内）。

    budget 缺省回落 HISTORY_BUDGET_TOKENS（运行时读取，测试可打桩）。"""
    budget = HISTORY_BUDGET_TOKENS if budget is None else budget
    turns = state.turns if state else []
    summary = state.summary if state else None
    start = state.summary_through if state else 0
    current = {"role": "user", "content": user_text(question, appid)}

    def _build(from_idx: int) -> list[dict]:
        msgs = [summary_message(summary)] if summary else []
        msgs += [m for t in turns[from_idx:] for m in _turn_view(t)]
        msgs.append(current)
        return msgs

    msgs = _build(start)
    while (
        len(turns) - start > RECENT_KEEP_TURNS
        and estimate_messages(msgs) > budget
    ):
        start += 1
        msgs = _build(start)
    return msgs


def needs_compaction(messages: list[dict], *,
                     budget: int | None = None) -> bool:
    budget = HISTORY_BUDGET_TOKENS if budget is None else budget
    return estimate_messages(messages) > budget


def full_view_tokens(state, question: str, appid: int | None) -> int:
    """不裁剪的全量视图体量：存档 + 全部未存档轮次 + 当前问题。

    这是「压缩前体量」的诚实口径——assemble 触发时的装配体量已被
    RECENT_KEEP_TURNS 下限裁过，压不掉它，账本记它没有信息量。"""
    if state is None:
        return 0
    msgs = ([summary_message(state.summary)] if state.summary else [])
    msgs += [m for t in state.turns[state.summary_through:] for m in _turn_view(t)]
    msgs.append({"role": "user", "content": user_text(question, appid)})
    return estimate_messages(msgs)


def reference_digest(state) -> str | None:
    """被引会话 → 预算内摘要：有存档用存档，否则从最近轮次往回收集对话行。"""
    if state is None:
        return None
    if state.summary:
        return state.summary
    picked: list[str] = []
    total = 0
    for t in reversed(state.turns):
        line = _turn_line(t)
        cost = estimate_tokens(line)
        if total + cost > REFERENCE_BUDGET_TOKENS:
            continue
        picked.append(line)
        total += cost
    if not picked:
        return None
    return "\n---\n".join(reversed(picked))


def reference_message(title: str, digest: str) -> dict:
    """跨会话引用 → system 消息（一次性背景，不是当前请求）。"""
    head = f"用户引用了另一段对话《{title or '历史会话'}》。以下是该对话的要点，仅作背景参考，不是当前请求：\n"
    return {"role": "system", "content": head + digest}


async def compact(state, cfg: dict) -> tuple[str, int, tuple[int, int]] | None:
    """把更早轮次蒸馏进要点存档（与既有存档合并），返回 (存档, 存到第几轮, 用量)。

    无可蒸馏轮次、模型失败或空回答返回 None——调用方保持原状即可。"""
    if state is None:
        return None
    through = len(state.turns) - RECENT_KEEP_TURNS
    start = state.summary_through
    if through <= start:
        return None
    prompt = _COMPACT_INSTRUCTION
    if state.summary:
        prompt += f"已有存档如下，请合并去重后输出新存档：\n{state.summary}\n\n"
    prompt += "对话轮次：\n" + "\n---\n".join(_turn_line(t) for t in state.turns[start:through])

    inp = out = 0
    parts: list[str] = []
    try:
        async for kind, delta in pilot_llm.chat_stream(
            protocol=cfg["protocol"],
            base_url=pilot_llm.effective_base_url(cfg["protocol"], cfg["base_url"]),
            api_key=cfg["api_key"],
            model=cfg["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=COMPACT_MAX_TOKENS,
        ):
            if kind == "usage":
                inp, out = delta
            elif kind == "answer":
                parts.append(delta)
    except pilot_llm.PilotLlmError:
        return None  # 压缩失败不中断问答：装配退回整轮让位（与契约一致）
    text = "".join(parts).strip()
    if not text:
        return None
    return text, through, (inp, out)
