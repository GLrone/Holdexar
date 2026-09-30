"""pilot 服务：领航台问答编排。

流程：确定性意图路由 → 只读工具取事实 → LLM 解读 → 回答。LLM 未配置、
调用失败或月度用量超限时回退事实摘要（source=facts），回退原因以机器码
（reason）下发、由前端翻成用户语言；提示本身（answer）只来自 LLM 或为
空，不在这里拼用户文案。

提供两种消费形态，共用同一前置（_prepare）与缓存：
- ask()          非流式，一次返回完整结果；
- ask_stream()   流式，thinking / answer 双通道增量 + facts + done 事件
                 （thinking 通道承载推理模型的思维链，普通模型无此通道）。
"""
from __future__ import annotations

import json
import time

from app.domains.pilot import config as pilot_config
from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import llm as pilot_llm
from app.domains.pilot import tools as pilot_tools

_CACHE_TTL_S = 600
_CACHE_MAX = 200
_cache: dict[tuple, tuple[dict, float]] = {}

_SYSTEM_PROMPT = (
    "你是「领航员」，Holdexar 的游戏比价助手。回答规则：\n"
    "- 只依据提供的事实作答，不编造价格、日期或评价数据；\n"
    "- 事实不足时直说不足，并建议用户在应用内查看对应页面；\n"
    "- 用简体中文，简洁分点，先结论后依据；\n"
    "- 涉及购买建议时，说明当前价与史低、近一年区间的关系作为依据。"
)


def _cache_get(key: tuple) -> dict | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    resp, ts = hit
    if time.monotonic() - ts > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return resp


def _cache_put(key: tuple, resp: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][1])
        _cache.pop(oldest, None)
    _cache[key] = (resp, time.monotonic())


def _user_message(question: str, facts: dict | None) -> str:
    if facts is None:
        return question
    return (
        "事实数据（JSON，cnyFen 单位为分）：\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        + "\n\n用户问题："
        + question
    )


async def _prepare(question: str, appid: int | None) -> tuple[str, dict | None]:
    """共享前置：意图路由 + 工具取事实。

    返回 (stage, payload)：stage ∈ guide / facts / none / chat / write；
    facts 形态下 payload 为事实 dict，write 形态下为 {"action": 动作名}，
    其余为 None。
    """
    intent = pilot_intent.route(question)
    if intent == pilot_intent.HOW_TO:
        return "guide", None
    if intent == pilot_intent.ADD_MONITOR:
        return "write", {"action": "monitor_add"}
    if intent == pilot_intent.CREATE_ALERT:
        return "write", {"action": "alert_add"}
    if intent == pilot_intent.PRICE_ANALYSIS:
        target = appid
        if target is None:
            matches = await pilot_tools.search_games(question)
            if len(matches) == 1:
                target = matches[0]["appid"]
        facts = await pilot_tools.price_facts(target) if target else None
        if facts is None:
            # 问句没点名到具体游戏：回退属性筛选给候选，让模型带着候选作答，
            # 而不是用 no_data 把主入口的问句顶回来
            items = await pilot_tools.recommend_games(question)
            if items:
                return "facts", {"kind": "games", "items": items}
            return "none", None
        return "facts", facts
    if intent == pilot_intent.FIND_GAMES:
        items = await pilot_tools.recommend_games(question)
        if not items:
            return "none", None
        return "facts", {"kind": "games", "items": items}
    return "chat", None


def _empty(source: str, reason: str | None, facts: dict | None) -> dict:
    return {"answer": "", "thinking": None, "source": source, "reason": reason, "facts": facts}


async def _respond(*, question: str, facts: dict | None, is_chat: bool) -> dict:
    """非流式 LLM 消费；未配置/失败/超限时按语义回退。"""
    cfg = await pilot_config.load_config()
    ready = pilot_config.llm_ready(cfg)
    over_cap = ready and (await pilot_config.usage_month()) >= cfg["monthly_cap"]
    if is_chat and not ready:
        return _empty("none", "llm_off", None)
    reason = None
    if ready and not over_cap:
        try:
            answer, prompt_tokens, completion_tokens = await pilot_llm.chat_complete(
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                model=cfg["model"],
                system=_SYSTEM_PROMPT,
                user=_user_message(question, facts),
            )
            await pilot_config.add_usage(prompt_tokens + completion_tokens)
            return {"answer": answer, "thinking": None, "source": "llm", "reason": None, "facts": facts}
        except pilot_llm.PilotLlmError:
            reason = "llm_failed"
    elif over_cap:
        reason = "cap_reached"
    if is_chat:
        return _empty("none", reason or "llm_off", None)
    return _empty("facts", reason, facts)


async def _resolve_write(
    question: str, appid: int | None, action: str
) -> tuple[str, dict | None]:
    """写动作解析与执行（风险门之后的确定性路径）。

    返回 (stage2, payload)：
    - ("done", action 事实)   已执行；
    - ("facts", 候选列表)     问句未唯一点名游戏，给候选让模型追问；
    - ("guide", None)         提醒缺阈值等不可臆造的条件；
    - ("none", None)          库内无此游戏。
    """
    title = pilot_tools.extract_title(question)
    if title:
        queries = [title]
    else:
        queries = pilot_tools.clean_query_tokens(question) or [question]
    matches: list[dict] = []
    for query in queries:
        matches = await pilot_tools.search_games(question, query=query)
        if matches:
            break
    target = appid
    if target is None:
        if len(matches) == 1:
            target = matches[0]["appid"]
        elif matches:
            return "facts", {"kind": "games", "items": matches}
        else:
            return "none", None
    if action == "alert_add":
        target_type, value = pilot_tools.extract_alert(question)
        if target_type == "price" and value is None:
            return "guide", None
        result = await pilot_tools.alert_add(target, target_type=target_type, target_value_fen=value)
    else:
        result = await pilot_tools.monitor_add(target)
    return "done", {"kind": "action", **result}


async def ask(question: str, appid: int | None = None) -> dict:
    q = (question or "").strip()
    if not q:
        raise ValueError("empty question")
    cache_key = (q, appid or 0)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    stage, facts = await _prepare(q, appid)
    if stage == "guide":
        resp = _empty("guide", None, None)
    elif stage == "write":
        stage2, payload = await _resolve_write(q, appid, facts["action"])
        if stage2 == "done":
            resp = await _respond(question=q, facts=payload, is_chat=False)
        elif stage2 == "facts":
            resp = _empty("facts", "need_target", payload)
        elif stage2 == "guide":
            resp = _empty("guide", None, None)
        else:
            resp = _empty("none", "no_data", None)
    elif stage == "none":
        resp = _empty("none", "no_data", None)
    else:
        resp = await _respond(question=q, facts=facts, is_chat=stage == "chat")

    _cache_put(cache_key, resp)
    return {**resp, "cached": False}


async def ask_stream(question: str, appid: int | None = None):
    """流式编排。事件形态：
    {"type": "thinking" | "answer", "delta": str} 增量；
    {"type": "facts", "facts": dict} 事实就绪；
    {"type": "done", ...结果字段（含 cached）} 终态；
    {"type": "error", "reason": str} 前置异常。
    """
    q = (question or "").strip()
    if not q:
        yield {"type": "error", "reason": "bad_request"}
        return
    cache_key = (q, appid or 0)
    cached = _cache_get(cache_key)
    if cached is not None:
        out = {**cached, "cached": True}
        if out.get("thinking"):
            yield {"type": "thinking", "delta": out["thinking"]}
        if out.get("answer"):
            yield {"type": "answer", "delta": out["answer"]}
        yield {"type": "done", **out}
        return

    stage, facts = await _prepare(q, appid)
    if facts is not None and stage != "write":
        yield {"type": "facts", "facts": facts}

    if stage == "guide":
        resp = _empty("guide", None, None)
        _cache_put(cache_key, resp)
        yield {"type": "done", **resp}
        return
    if stage == "write":
        stage2, payload = await _resolve_write(q, appid, facts["action"])
        if stage2 == "done":
            # 写确认是短句，走非流式解读即可
            resp = await _respond(question=q, facts=payload, is_chat=False)
        elif stage2 == "facts":
            resp = _empty("facts", "need_target", payload)
        elif stage2 == "guide":
            resp = _empty("guide", None, None)
        else:
            resp = _empty("none", "no_data", None)
        _cache_put(cache_key, resp)
        yield {"type": "done", **resp}
        return
    if stage == "none":
        resp = _empty("none", "no_data", None)
        _cache_put(cache_key, resp)
        yield {"type": "done", **resp}
        return
    is_chat = stage == "chat"

    cfg = await pilot_config.load_config()
    ready = pilot_config.llm_ready(cfg)
    over_cap = ready and (await pilot_config.usage_month()) >= cfg["monthly_cap"]
    if is_chat and not ready:
        resp = _empty("none", "llm_off", None)
        _cache_put(cache_key, resp)
        yield {"type": "done", **resp}
        return

    reason: str | None = None
    if ready and not over_cap:
        answer_parts: list[str] = []
        think_parts: list[str] = []
        usage: tuple[int, int] | None = None
        try:
            async for kind, delta in pilot_llm.chat_complete_stream(
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                model=cfg["model"],
                system=_SYSTEM_PROMPT,
                user=_user_message(q, facts),
            ):
                if kind == "usage":
                    usage = delta
                    continue
                (think_parts if kind == "thinking" else answer_parts).append(delta)
                yield {"type": kind, "delta": delta}
            if usage:
                await pilot_config.add_usage(usage[0] + usage[1])
            resp = {
                "answer": "".join(answer_parts),
                "thinking": "".join(think_parts) or None,
                "source": "llm",
                "reason": None,
                "facts": facts,
            }
            _cache_put(cache_key, resp)
            yield {"type": "done", **resp, "cached": False}
            return
        except pilot_llm.PilotLlmError:
            reason = "llm_failed"
            # 已流出的内容不收回：标记降级原因后终止，前端按原因补提示
            if answer_parts or think_parts:
                resp = {
                    "answer": "".join(answer_parts),
                    "thinking": "".join(think_parts) or None,
                    "source": "llm",
                    "reason": "llm_failed",
                    "facts": facts,
                }
                _cache_put(cache_key, resp)
                yield {"type": "done", **resp}
                return
    elif over_cap:
        reason = "cap_reached"

    if is_chat:
        resp = _empty("none", reason or "llm_off", None)
    else:
        resp = _empty("facts", reason, facts)
    _cache_put(cache_key, resp)
    yield {"type": "done", **resp}
