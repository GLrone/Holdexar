"""Long-running domain work and historical conversation tools."""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared

# 运行状态 → （行值词条, 色调）；一级状态语义见 runtime/state.py
_TASK_STATUS_ROW: dict[str, tuple[str, str]] = {
    "queued": ("taskQueued", "warn"),
    "running": ("taskRunning", "ok"),
    "awaiting_approval": ("taskQueued", "warn"),
    "done": ("taskDone", "ok"),
    "failed": ("taskFailed", "bad"),
    "cancelled": ("taskCancelled", "warn"),
    "budget_exhausted": ("taskCancelled", "warn"),
}

_READ_MAX_TURNS = 30
"""单次回读最多取多少轮：区间是给模型看的正文，不能一次拉满整段历史。"""

_READ_DIGEST_TOKENS = 3000
"""单次回读的文本预算（成本上限，与装配侧同一份估值口径）。"""


def _task_row(run: dict, progress: dict | None = None, *,
              vKey: str | None = None, tone: str | None = None,
              v: str | None = None) -> dict:
    """任务行：左侧走 kindKey（任务名词条）、右侧走状态词条 + 进度原值。"""
    from app.domains.agent import service as agent_service

    kind = str((run.get("meta") or {}).get("task") or "")
    default_vKey, default_tone = _TASK_STATUS_ROW.get(str(run.get("status")), ("taskQueued", "warn"))
    if (run.get("meta") or {}).get("domainStatus") == "partial":
        default_vKey, default_tone = "taskPartial", "warn"
    vKey = vKey or default_vKey
    tone = tone or default_tone
    if v is None:
        prog = progress or (run.get("meta") or {}).get("progress") or {}
        done, total = prog.get("done"), prog.get("total")
        v = f"{done}/{total}" if done is not None and total else None
    return {
        "k": "",
        "kindKey": f"pilot.task.{agent_service.TASK_LABELS.get(kind, kind)}",
        "vKey": vKey,
        "tone": tone,
        "v": v or "",
        "taskKind": kind,
        "run_id": run.get("run_id"),
    }


async def list_tasks() -> dict:
    """运行中的价格任务与最近终态，进度直接来自域记录。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=100, price_tasks_only=True)
    ours = [r for r in runs if (r.get("meta") or {}).get("task") in agent_service.TASK_LABELS]
    live = [r for r in ours if r.get("status") not in agent_state.TERMINAL_STATES]
    rows: list[dict] = []
    for r in live:
        rows.append(_task_row(r))
    for r in [r for r in ours if r.get("status") in agent_state.TERMINAL_STATES][:3]:
        rows.append(_task_row(r))
    if not rows:
        rows.append({"k": "", "vKey": "taskNoRunning", "tone": "warn", "v": ""})
    return _shared.rows_result("tasks", rows, total=len(live))


async def start_task(kind: str, params: dict | None = None) -> dict:
    """调用原价格入口或真实开发任务，返回实际运行身份。"""
    from app.domains.agent import service as agent_service
    from app.domains.crawl import service as crawl_service

    label = agent_service.TASK_LABELS.get(kind)
    if label is None:
        return _shared.rows_result("tasks", [{"k": "", "vKey": "taskUnknown", "tone": "bad", "v": ""}])
    params = params or {}
    try:
        if kind == "dev_pytest":
            from app.domains.agent.runtime import dev_tasks

            selection = str(params.get("select") or "")
            dev_tasks.command(selection)
            accepted = await agent_service.create_run(runner="task", trigger="manual",
                meta={"task": kind, "select": selection})
            run_id = accepted["run_id"]
            await agent_service.start_run(run_id)
        elif kind == "price_refresh":
            accepted = await crawl_service.start_full_queue()
            run_id = f"cycle:{accepted['cycleId']}"
        else:
            options = {}
            if kind == "price_repair":
                options = {
                    "regions": [str(r) for r in (params.get("regions") or [])][:20] or None,
                    "appids": [int(a) for a in (params.get("appids") or []) if int(a) > 0][:300] or None,
                }
                options = {key: value for key, value in options.items() if value is not None}
            cooldown = params.get("cooldown_minutes")
            if cooldown is not None or kind == "price_repair":
                options["missing_cooldown"] = int(cooldown) if cooldown is not None else 0
            accepted = await crawl_service.start_job(
                scope="appids", kind="missing" if kind == "price_missing" else "repair",
                trigger="manual", **options,
            )
            run_id = f"crawl:{accepted['id']}"
    except (RuntimeError, ValueError) as e:
        return _shared.rows_result("tasks", [
            {"k": "", "kindKey": f"pilot.task.{label}",
             "vKey": "taskBusy", "tone": "warn", "v": str(e)[:60]},
        ])
    run = await agent_service.get_run(run_id)
    if run is None:
        raise LookupError(f"accepted run not found: {run_id}")
    return _shared.rows_result("tasks", [_task_row(run)])


async def cancel_task(run_id: str | None = None, kind: str | None = None) -> dict:
    """请求停止运行中的长任务（按运行标识或任务类型定位）。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=100, price_tasks_only=True)
    live = [
        r for r in runs
        if (r.get("meta") or {}).get("task") in agent_service.TASK_LABELS
        and r.get("status") not in agent_state.TERMINAL_STATES
    ]
    target: dict | None = None
    if run_id:
        candidate = await agent_service.get_run(run_id)
        if candidate and (candidate.get("meta") or {}).get("task") in agent_service.TASK_LABELS:
            target = candidate
    elif kind:
        target = next((r for r in live if (r.get("meta") or {}).get("task") == kind), None)
    elif len(live) == 1:
        target = live[0]
    if target is None:
        empty_key = "taskNoRunning" if not live else "taskAmbiguous"
        return _shared.rows_result("tasks", [{"k": "", "vKey": empty_key, "tone": "warn", "v": ""}])
    try:
        res = await agent_service.cancel_run(target["run_id"], reason="user")
    except agent_service.RunCancellationUnavailable:
        return _shared.rows_result("tasks", [_task_row(target, vKey="taskStopUnavailable", tone="warn")])
    return _shared.rows_result("tasks", [_task_row(
        target, vKey="taskStopRequested" if res.get("accepted") else None,
    )])


async def list_sessions() -> dict:
    """历史会话清单：标题/轮数/更新时间（账本只读投影，最近 20 段）。"""
    items = _shared.pilot_store().list_recent(20)
    rows = [
        {"k": it["title"] or it["sid"][:8], "vKey": "sessionTurns",
         "data": {"count": it["turn_total"]}, "at": it["updated_at"]}
        for it in items
    ]
    return _shared.rows_result("sessions", rows, total=len(items))


async def read_session(sid: str, offset: int = 0, count: int = _shared._READ_ROWS_LIMIT) -> dict:
    """读某段历史会话：指定区间的问答行 + 该区间的文本摘要（模型消费 digest，卡片消费 rows）。

    offset = 从最新一轮往回跳过多少轮（0 = 从最新开始），count = 本次读多少轮。
    这两个参数是压缩后的回读通道：本会话被蒸馏掉的那段区间，模型可以往回翻原文，
    不必靠记忆复述数字。区间之外的内容不返回，越界不报错、只返回能取到的部分。"""
    state = await _shared.pilot_store().load(str(sid or ""))
    if state is None:
        return {"kind": "empty", "note": "no_data"}
    store = _shared.pilot_store()
    total = len(state.turns)
    skip = max(0, int(offset or 0))
    size = max(1, min(_READ_MAX_TURNS, int(count or _shared._READ_ROWS_LIMIT)))
    end = max(0, total - skip)
    start = max(0, end - size)
    window = state.turns[start:end]
    rows = []
    for t in window:
        resp = t.get("resp") or {}
        answer = str(resp.get("answer") or "")
        tools_txt = "；".join(str(x) for x in (resp.get("tools") or []))
        rows.append({
            "k": str(t.get("q") or "")[:48],
            "v": (answer or tools_txt)[:80],
            "at": t.get("ts"),
        })
    digest = None
    try:
        from app.domains.pilot import context as pilot_context

        digest = pilot_context.digest_lines(window, budget=_READ_DIGEST_TOKENS)
    except Exception:  # noqa: BLE001 — 摘要失败不影响清单行
        digest = None
    result = _shared.rows_result("sessionRead", rows, total=total)
    result["sid"] = str(sid or "")
    result["sessionTitle"] = store.title_of(state)
    result["startTurn"] = start
    result["turnCount"] = len(window)
    result["summaryThrough"] = state.summary_through
    result["digest"] = digest or ""
    return result


async def _list_tasks(args: dict, sid: str | None = None) -> dict:
    return await list_tasks()


async def _start_task(args: dict, sid: str | None = None) -> dict:
    params = args.get("params")
    return await start_task(
        str(args.get("kind") or ""),
        dict(params) if isinstance(params, dict) else None,
    )


async def _cancel_task(args: dict, sid: str | None = None) -> dict:
    return await cancel_task(
        run_id=str(args.get("run_id") or "") or None,
        kind=str(args.get("kind") or "") or None,
    )


async def _list_sessions(args: dict, sid: str | None = None) -> dict:
    return await list_sessions()


async def _agent_runs_list(args: dict, sid: str | None = None) -> dict:
    """运行账本清单：长任务/编排运行的当前态与最近终态（只读投影）。"""
    from app.domains.agent import service as agent_service

    limit = max(1, min(30, int(args.get("limit") or 10)))
    runs = await agent_service.list_runs(limit=limit)
    rows = [
        {"k": str(r.get("run_id"))[:8], "run_id": r["run_id"], "v": str(r.get("status") or ""),
         "v2": str((r.get("meta") or {}).get("task") or r.get("runner") or ""),
         "at": r.get("finished_at") or r.get("started_at") or r.get("created_at")}
        for r in runs
    ]
    return _shared.rows_result("agentRuns", rows, total=len(rows))


async def _agent_run_events(args: dict, sid: str | None = None) -> dict:
    """单个运行的事件回放（受理/进度/终态，只读）。"""
    from app.domains.agent import service as agent_service

    run_id = str(args.get("run_id") or "")
    if not run_id:
        return {"kind": "empty", "note": "no_run_id"}
    run = await agent_service.get_run(run_id)
    if run is None:
        return {"kind": "empty", "note": "no_run"}
    after = int(args.get("after_seq") or 0)
    limit = max(1, min(100, int(args.get("limit") or 30)))
    events = await agent_service.list_events(run_id, after_seq=after, limit=limit)
    rows = [
        {"k": str(e.get("event_type") or ""), "v": str(e.get("seq")), "at": e.get("created_at")}
        for e in events
    ]
    out = _shared.rows_result("agentEvents", rows, total=len(rows))
    out["runId"] = run_id
    out["runStatus"] = run.get("status")
    return out


async def _read_session(args: dict, sid: str | None = None) -> dict:
    return await read_session(
        str(args.get("sid") or ""),
        int(args.get("offset") or 0),
        int(args.get("count") or _shared._READ_ROWS_LIMIT),
    )


SPECS = [
    ToolSpec(
        name="list_tasks", group="read", risk="low",
        description="查看正在运行的耗时任务（价格刷新 / 价格补抓）与最近结束的几次。"
                    "用户问『刷新到哪了』『还在跑吗』『跑完了吗』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_tasks, step_label="tasks",
    ),
    ToolSpec(
        name="start_task", group="read", risk="high",
        description="发起一个耗时后台任务，受理后立即返回、不阻塞回答。"
                    "kind 可取 price_refresh（全部价格刷新）、price_repair（补抓缺价/失败的游戏）、"
                    "price_missing（按欠账账本批量补抓，冷却 24h）、"
                    "dev_pytest（开发验收：跑仓库自动测试选集，仅源码环境）。"
                    "price_repair 支持可选 params：regions（地区代码，只补这些区）、"
                    "appids（对象列表）、cooldown_minutes（冷却覆盖分钟数，默认 0 立即补）；"
                    "dev_pytest 的 params.select 取 smoke/agent/mcp/crawl/db。"
                    "仅当用户明确要求刷新 / 补价格 / 跑测试时调用；发起后告知可问进度或让它停下",
        parameters={"type": "object", "properties": {
            "kind": {"type": "string",
                     "enum": ["price_refresh", "price_repair", "price_missing", "dev_pytest"],
                     "description": "任务类型"},
            "params": {
                "type": "object",
                "description": "任务参数（白名单字段，未支持的任务忽略）",
                "properties": {
                    "regions": {"type": "array", "items": {"type": "string"},
                                "description": "地区代码列表（如 cn/us/ru）"},
                    "appids": {"type": "array", "items": {"type": "integer"},
                               "description": "对象 appid 列表（≤300）"},
                    "cooldown_minutes": {"type": "integer",
                                         "description": "补抓冷却覆盖（分钟）"},
                    "select": {"type": "string",
                               "enum": ["smoke", "agent", "mcp", "crawl", "db"],
                               "description": "开发测试选集（dev_pytest 用）"},
                },
            },
        }, "required": ["kind"]},
        handler=_start_task, step_label="taskStart",
    ),
    ToolSpec(
        name="cancel_task", group="read", risk="high",
        description="停止正在运行的耗时任务。用户说『停下来』『别刷了』『取消』时调用；"
                    "只有一个任务在跑时可只给 kind 或都不给",
        parameters={"type": "object", "properties": {
            "run_id": {"type": "string", "description": "运行标识（来自 list_tasks 结果）"},
            "kind": {"type": "string",
                     "enum": ["price_refresh", "price_repair", "price_missing", "dev_pytest"]},
        }},
        handler=_cancel_task, step_label="taskCancel",
    ),
    ToolSpec(
        name="list_sessions", group="read", risk="low",
        description="列出用户与领航员的历史对话清单（标题与轮数）。"
                    "用户想找回或查看以前的对话时调用；对话内容用 read_session 读取",
        parameters={"type": "object", "properties": {}},
        handler=_list_sessions, step_label="sessions",
    ),
    ToolSpec(
        name="read_session", group="read", risk="low",
        description="读取某段历史对话的内容。"
                    "用户问『我们之前聊过什么』或需要旧对话里的信息时调用；"
                    "会话 id 可先用会话清单工具获取。"
                    "offset 从最新一轮往回跳过若干轮（默认 0=从最新开始），"
                    "count 指定本次读多少轮（默认 8，最多 30）；"
                    "本会话被压缩后如需更早的原文，用 offset 逐段往回读",
        parameters={"type": "object", "properties": {
            "sid": {"type": "string", "description": "会话 id（来自会话清单）"},
            "offset": {"type": "integer", "description": "从最新往回跳过的轮数，默认 0"},
            "count": {"type": "integer", "description": "本次读取的轮数，默认 8，最多 30"},
        }, "required": ["sid"]},
        handler=_read_session, step_label="sessionRead",
    ),
    ToolSpec(
        name="agent_runs_list", group="read", risk="low",
        description="查看后台运行账本：最近的长任务与编排运行的状态"
                    "（排队/运行/完成/失败/取消）。核对任务真实执行与终态时调用",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30"},
        }},
        handler=_agent_runs_list, step_label="agentRuns",
    ),
    ToolSpec(
        name="agent_run_events", group="read", risk="low",
        description="读取某个后台运行的事件回放（受理/进度采样/终态），"
                    "配合 agent_runs_list 的运行标识使用；after_seq 支持增量续读",
        parameters={"type": "object", "properties": {
            "run_id": {"type": "string", "description": "运行标识（来自运行账本清单）"},
            "after_seq": {"type": "integer", "description": "只取该序号之后的事件，默认 0"},
            "limit": {"type": "integer", "description": "返回条数，默认 30，最多 100"},
        }, "required": ["run_id"]},
        handler=_agent_run_events, step_label="agentEvents",
    ),
]
