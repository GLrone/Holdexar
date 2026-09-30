"""pilot 服务：领航台问答编排（agent 循环内核）。

调度机理（agent 循环模型）：模型是驾驶员——每轮把会话
消息 + 工具表交给 LLM 流式输出（thinking / answer / tool_calls），工具
由服务层执行后把结果回灌进消息，循环直到模型给出终答或步数上限。

风险门（确定性，与模型无关）：工具白名单只有加关注 / 设提醒两个单对象
可逆动作；问句命中守卫词（批量 / 删除 / 停用类）时写工具拒绝执行。

降级路径：LLM 未配置 / 超限 / 失败时回退确定性事实摘要（source=facts）
与指引（source=guide），回退原因以机器码（reason）下发、前端翻成用户
语言。罗盘数据飞轮：每轮问答追加 decisions.jsonl（shadow）。"""
from __future__ import annotations

import json
import re
import time

from app.core.paths import resolve_data_dir
from app.crawler.utils import get_beijing_time_obj
from app.domains.pilot import config as pilot_config
from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import llm as pilot_llm
from app.domains.pilot import tools as pilot_tools

_MAX_STEPS = 6
_SESSION_TTL_S = 1800
_SESSION_MAX = 50
_SESSION_MAX_TURNS = 6
_sessions: dict[str, dict] = {}

_SYSTEM_PROMPT = (
    "你是「领航员」，Holdexar 的游戏比价助手。回答规则：\n"
    "- 优先调用工具获取真实数据（检索、价格事实、推荐、加关注、设提醒），"
    "不编造价格、日期或评价数据；\n"
    "- 用户要求关注 / 设提醒时，先确认是哪一款（必要时用检索工具），再调用"
    "对应工具执行，执行后用一句话确认结果；\n"
    "- 事实不足时直说不足，并建议用户在应用内查看对应页面；\n"
    "- 用简体中文，简洁分点，先结论后依据；\n"
    "- 涉及购买建议时，说明当前价与史低、近一年区间的关系作为依据。"
)


def _session_get_or_create(session_id: str) -> dict:
    st = _sessions.get(session_id)
    if st is None:
        if len(_sessions) >= _SESSION_MAX:
            oldest = min(_sessions, key=lambda k: _sessions[k]["ts"])
            _sessions.pop(oldest, None)
        st = {"ts": time.monotonic(), "turns": [], "last_game": None}
        _sessions[session_id] = st
    st["ts"] = time.monotonic()
    return st


def _session_history(session: dict | None) -> list[dict]:
    """会话消息历史：按轮取最近若干轮，轮内消息保持 assistant/tool 配对完整。"""
    if not session:
        return []
    turns = session.get("turns") or []
    return [m for t in turns[-_SESSION_MAX_TURNS:] for m in t]


def _session_store_game(session: dict | None, appid: int, name: str | None) -> None:
    if not session or not appid:
        return
    session["last_game"] = {"appid": appid, "name": name}


def _log_decision(entry: dict) -> None:
    """罗盘数据飞轮：问答轮次追加落盘（JSONL，shadow）。

    只追加、无读取方、失败静默——飞轮不依赖日志，日志也不阻塞问答。
    数据供未来自训决策引擎做意图标注与动作结果回归。
    """
    try:
        base = resolve_data_dir() / "pilot"
        base.mkdir(parents=True, exist_ok=True)
        with (base / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _empty(source: str, reason: str | None, facts: dict | None) -> dict:
    return {"answer": "", "thinking": None, "source": source, "reason": reason, "facts": facts}


async def _fallback_deterministic(
    question: str, appid: int | None, session: dict | None, intent: str
) -> dict:
    """无 LLM 时的确定性回退：指引 / 价格与推荐事实 / 通知无数据。

    问句解析逻辑与 agent 路径一致（守卫词、阈值、对象解析），仅不做模型
    叙述——回答文本始终由前端模板渲染，后端不拼用户文案。"""
    if intent == pilot_intent.HOW_TO:
        return _empty("guide", None, None)
    if intent == pilot_intent.CREATE_ALERT:
        title = pilot_tools.extract_title(question)
        matches = await pilot_tools.search_games(question, query=title) if title \
            else await pilot_tools.search_games(question)
        if len(matches) == 1:
            target_type, value = pilot_tools.extract_alert(question)
            if target_type == "price" and value is None:
                return _empty("guide", None, None)
            result = await pilot_tools.alert_add(
                matches[0]["appid"], target_type=target_type, target_value_fen=value
            )
            return _empty("facts", None, {"kind": "action", **result})
        if matches:
            return _empty("facts", "need_target", {"kind": "games", "items": matches})
        return _empty("none", "no_data", None)
    if intent == pilot_intent.ADD_MONITOR:
        title = pilot_tools.extract_title(question)
        matches = await pilot_tools.search_games(question, query=title) if title \
            else await pilot_tools.search_games(question)
        if len(matches) == 1:
            result = await pilot_tools.monitor_add(matches[0]["appid"])
            return _empty("facts", None, {"kind": "action", **result})
        if matches:
            return _empty("facts", "need_target", {"kind": "games", "items": matches})
        return _empty("none", "no_data", None)
    if intent == pilot_intent.PRICE_ANALYSIS:
        target = appid
        if target is None:
            matches = await pilot_tools.search_games(question)
            if len(matches) == 1:
                target = matches[0]["appid"]
            elif not matches:
                ref = session.get("last_game") if session else None
                if ref and any(p in question for p in ("它", "这游戏", "该游戏", "这款")):
                    target = ref["appid"]
        facts = await pilot_tools.price_facts(target) if target else None
        if facts is not None:
            _session_store_game(session, target, facts.get("name"))
            return _empty("facts", None, facts)
        items = await pilot_tools.recommend_games(question)
        if items:
            return _empty("facts", "need_target", {"kind": "games", "items": items})
        return _empty("none", "no_data", None)
    if intent == pilot_intent.FIND_GAMES:
        items = await pilot_tools.recommend_games(question)
        if not items:
            return _empty("none", "no_data", None)
        return _empty("facts", None, {"kind": "games", "items": items})
    return _empty("none", "llm_off", None)


def _tool_summary(name: str, result: dict) -> str:
    """工具事件的上屏摘要（机器码 + 最小数据，前端不翻译这个字段）。"""
    if result.get("kind") == "denied":
        return f"{name}: denied"
    if name == "search_games":
        return f"search_games: {len(result.get('items') or [])} hits"
    if name == "recommend_games":
        return f"recommend_games: {len(result.get('items') or [])} hits"
    if name == "get_price_briefing":
        return f"get_price_briefing: {result.get('name') or 'no_data'}"
    if name == "add_follow":
        return f"add_follow: {result.get('name') or result.get('appid')}"
    if name == "create_price_alert":
        return f"create_price_alert: {result.get('name') or result.get('appid')}"
    return name


def _card_of(tool_name: str, result: dict) -> dict | None:
    """工具结果 → 前端可渲染的结构化卡片（组件复用的数据层）。"""
    kind = result.get("kind")
    if kind == "games" and result.get("items"):
        return {"kind": "games", "items": result["items"][:5]}
    if kind == "price":
        return {k: v for k, v in result.items() if k != "kind"} | {"kind": "price"}
    if kind == "action":
        return {"kind": "action", **result}
    return None


async def _agent_stream(question: str, appid: int | None, session: dict, cfg: dict):
    """agent 循环（流式）。产出与 ask_stream 相同的事件形态，另加
    {"type": "tool", "name":..., "summary":...} 工具执行事件。"""
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += _session_history(session)
    user_msg = question if not appid else f"{question}（用户当前正在看 AppID {appid} 的游戏详情）"
    messages.append({"role": "user", "content": user_msg})

    answer_all: list[str] = []
    think_all: list[str] = []
    usage_total = [0, 0]
    tool_log: list[str] = []
    cards: list[dict] = []
    last_game: dict | None = None
    reason: str | None = None

    for _step in range(_MAX_STEPS):
        round_answer: list[str] = []
        tool_calls = None
        async for kind, delta in pilot_llm.chat_stream(
            protocol=cfg["protocol"],
            base_url=pilot_llm.effective_base_url(cfg["protocol"], cfg["base_url"]),
            api_key=cfg["api_key"],
            model=cfg["model"],
            messages=messages,
            tools=pilot_tools.tool_specs(),
        ):
            if kind == "usage":
                usage_total[0] += delta[0]
                usage_total[1] += delta[1]
                continue
            if kind == "tool_calls":
                tool_calls = delta
                continue
            (think_all if kind == "thinking" else answer_all).append(delta)
            if kind == "answer":
                round_answer.append(delta)
            yield {"type": kind, "delta": delta}

        if not tool_calls:
            # 终答轮：assistant 消息回写进会话历史（下一轮模型可见上文）
            messages.append({"role": "assistant", "content": "".join(round_answer) or None})
            break

        # 模型要求调工具：assistant 消息（含 tool_calls）入列，执行后回灌
        messages.append({
            "role": "assistant",
            "content": "".join(round_answer) or None,
            "tool_calls": [
                {
                    "id": t["id"],
                    "type": "function",
                    "function": {"name": t["name"], "arguments": json.dumps(t["arguments"], ensure_ascii=False)},
                }
                for t in tool_calls
            ],
        })
        for t in tool_calls:
            result = await pilot_tools.execute_tool(
                t["name"], t["arguments"], guarded=pilot_intent.is_guarded(question)
            )
            summary = _tool_summary(t["name"], result)
            tool_log.append(summary)
            yield {"type": "tool", "name": t["name"], "summary": summary}
            card = _card_of(t["name"], result)
            if card is not None and card not in cards:
                cards.append(card)
            if t["name"] in ("add_follow", "create_price_alert") and result.get("appid"):
                last_game = {"appid": result["appid"], "name": result.get("name")}
            messages.append({
                "role": "tool",
                "tool_call_id": t["id"] or f"{t['name']}-{len(messages)}",
                "content": json.dumps(result, ensure_ascii=False),
            })

    answer = "".join(answer_all)
    thinking = "".join(think_all) or None
    if usage_total != [0, 0]:
        await pilot_config.add_usage(usage_total[0], usage_total[1])
    if session is not None:
        session["turns"].append(messages[1:])
        if last_game:
            _session_store_game(session, last_game["appid"], last_game.get("name"))
    yield {
        "type": "done",
        "answer": answer,
        "thinking": thinking,
        "source": "llm",
        "reason": reason,
        "facts": None,
        "cards": cards[:6],
        "tools": tool_log,
    }


async def ask(question: str, appid: int | None = None, session_id: str | None = None) -> dict:
    """非流式入口（SSE 之外的消费形态 / 测试用）。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("empty question")
    session = _session_get_or_create(session_id) if session_id else None
    intent = pilot_intent.route(q)
    cfg = await pilot_config.load_config()
    if not pilot_config.llm_ready(cfg):
        resp = await _fallback_deterministic(q, appid, session, intent)
        _log_decision({"ts": get_beijing_time_obj().isoformat(timespec="seconds"),
                       "question": q, "intent": intent, "mode": "fallback",
                       "source": resp["source"], "reason": resp["reason"], "cached": False})
        return {**resp, "cached": False}
    if pilot_config.over_cap(await pilot_config.usage_month(), cfg["monthly_cap"]):
        resp = await _fallback_deterministic(q, appid, session, intent)
        resp["reason"] = "cap_reached" if resp["source"] != "guide" else resp["reason"]
        _log_decision({"ts": get_beijing_time_obj().isoformat(timespec="seconds"),
                       "question": q, "intent": intent, "mode": "fallback",
                       "source": resp["source"], "reason": resp["reason"], "cached": False})
        return {**resp, "cached": False}
    result: dict | None = None
    async for event in ask_stream(q, appid, session_id):
        if event["type"] == "done":
            result = event
    if result is None:
        result = _empty("none", "llm_failed", None)
    return {**result, "cached": False}


async def ask_stream(question: str, appid: int | None = None, session_id: str | None = None):
    """流式编排。事件形态：
    {"type": "thinking" | "answer", "delta": str} 增量；
    {"type": "tool", "name": str, "summary": str} 工具执行；
    {"type": "done", ...结果字段} 终态；
    {"type": "error", "reason": str} 前置异常。
    """
    q = (question or "").strip()
    if not q:
        yield {"type": "error", "reason": "bad_request"}
        return
    session = _session_get_or_create(session_id) if session_id else None
    intent = pilot_intent.route(q)
    cfg = await pilot_config.load_config()

    usage_now = await pilot_config.usage_month()
    cap = pilot_config.over_cap(usage_now, cfg["monthly_cap"])
    if not pilot_config.llm_ready(cfg) or cap:
        resp = await _fallback_deterministic(q, appid, session, intent)
        if cap and resp["source"] != "guide":
            resp["reason"] = "cap_reached"
        resp["cards"] = [resp["facts"]] if resp.get("facts") else []
        _log_decision({"ts": get_beijing_time_obj().isoformat(timespec="seconds"),
                       "question": q, "intent": intent, "mode": "fallback",
                       "source": resp["source"], "reason": resp["reason"], "cached": False})
        yield {"type": "done", **resp}
        return

    result: dict | None = None
    try:
        async for event in _agent_stream(q, appid, session, cfg):
            if event["type"] == "tool":
                yield event
                continue
            if event["type"] == "done":
                result = event
                continue
            yield event
    except pilot_llm.PilotLlmError:
        # 循环内已流出的内容不收回；无流出时降级为确定性事实
        if result is None:
            resp = await _fallback_deterministic(q, appid, session, intent)
            resp["reason"] = "llm_failed" if resp["source"] != "guide" else resp["reason"]
            _log_decision({"ts": get_beijing_time_obj().isoformat(timespec="seconds"),
                           "question": q, "intent": intent, "mode": "fallback",
                           "source": resp["source"], "reason": resp["reason"], "cached": False})
            yield {"type": "done", **resp}
            return
        result["reason"] = "llm_failed"

    if result is None:
        result = _empty("none", "llm_failed", None)
    _log_decision({
        "ts": get_beijing_time_obj().isoformat(timespec="seconds"),
        "question": q, "intent": intent, "mode": "agent",
        "protocol": cfg["protocol"],
        "source": result["source"], "reason": result["reason"],
        "tools": result.get("tools") or [], "cached": False,
    })
    yield {"type": "done", **result}
