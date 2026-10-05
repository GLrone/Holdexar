"""运行面与会话工具：长任务调度（第三层权限）与历史会话读取。

调度与写工具的本质区别：本组**不获得任何写数据权限**——调度只把已存在于
用户界面的长任务交给运行账本（agent_runs），业务写入仍由域内编排按既有
规则执行；kind 是登记表静态白名单（app.domains.agent.runtime.tasks），
模型不能自由拼参数；破坏性动作（删除 / 停用 / 清空）不入登记表。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.crawler.utils import get_beijing_time_obj

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


def _task_row(run: dict, progress: dict | None = None, *,
              vKey: str | None = None, tone: str | None = None,
              v: str | None = None) -> dict:
    """任务行：左侧走 kindKey（任务名词条）、右侧走状态词条 + 进度原值。"""
    from app.domains.agent.runtime import tasks as task_registry

    kind = str((run.get("meta") or {}).get("task") or "")
    default_vKey, default_tone = _TASK_STATUS_ROW.get(str(run.get("status")), ("taskQueued", "warn"))
    vKey = vKey or default_vKey
    tone = tone or default_tone
    if v is None:
        prog = progress or {}
        done, total = prog.get("done"), prog.get("total")
        v = f"{done}/{total}" if done is not None and total else None
    return {
        "k": "",
        "kindKey": f"pilot.task.{task_registry.label_of(kind)}",
        "vKey": vKey,
        "tone": tone,
        "v": v or "",
        "taskKind": kind,
    }


async def list_tasks() -> dict:
    """运行中的长任务与最近终态（运行账本只读投影，进度来自采样事件）。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=20)
    ours = [r for r in runs if r.get("runner") == "task"]
    live = [r for r in ours if r.get("status") not in agent_state.TERMINAL_STATES]
    rows: list[dict] = []
    for r in live:
        rows.append(_task_row(r, await agent_service.latest_progress(r["run_id"])))
    for r in [r for r in ours if r.get("status") in agent_state.TERMINAL_STATES][:3]:
        rows.append(_task_row(r))
    if not rows:
        rows.append({"k": "", "vKey": "taskNoRunning", "tone": "warn", "v": ""})
    return _shared.rows_result("tasks", rows, total=len(live))


async def start_task(kind: str) -> dict:
    """发起登记表内的长任务；受理在本次调用内完成，不等任务跑完。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import tasks as task_registry

    spec = task_registry.get(kind)
    if spec is None:
        return _shared.rows_result("tasks", [{"k": "", "vKey": "taskUnknown", "tone": "bad", "v": ""}])
    try:
        accepted = await spec.start()
    except (RuntimeError, ValueError) as e:
        # 域侧业务拒绝（已有任务在跑 / 无可抓对象）：如实回灌，不建空转账本
        return _shared.rows_result("tasks", [
            {"k": "", "kindKey": f"pilot.task.{spec.label}",
             "vKey": "taskBusy", "tone": "warn", "v": str(e)[:60]},
        ])
    ref = dict(accepted or {})
    ref["startedAt"] = get_beijing_time_obj().replace(tzinfo=None).isoformat()
    created = await agent_service.create_run(
        trigger="manual", runner="task",
        meta={"task": kind, "ref": ref, "adopted": True},
    )
    await agent_service.start_run(created["run_id"])
    return _shared.rows_result("tasks", [_task_row(
        {"meta": {"task": kind}, "status": "running"}, {"total": ref.get("total")},
    )])


async def cancel_task(run_id: str | None = None, kind: str | None = None) -> dict:
    """请求停止运行中的长任务（按运行标识或任务类型定位）。"""
    from app.domains.agent import service as agent_service
    from app.domains.agent.runtime import state as agent_state

    runs = await agent_service.list_runs(limit=20)
    live = [
        r for r in runs
        if r.get("runner") == "task" and r.get("status") not in agent_state.TERMINAL_STATES
    ]
    target: dict | None = None
    if run_id:
        target = next((r for r in live if r["run_id"] == run_id), None)
    elif kind:
        target = next((r for r in live if (r.get("meta") or {}).get("task") == kind), None)
    elif len(live) == 1:
        target = live[0]
    if target is None:
        empty_key = "taskNoRunning" if not live else "taskAmbiguous"
        return _shared.rows_result("tasks", [{"k": "", "vKey": empty_key, "tone": "warn", "v": ""}])
    res = await agent_service.cancel_run(target["run_id"], reason="user")
    return _shared.rows_result("tasks", [_task_row(
        target, vKey="taskCancelled" if res.get("accepted") else None,
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


async def read_session(sid: str) -> dict:
    """读某段历史会话：最近几轮问答行 + 预算内摘要（模型消费 digest，卡片消费 rows）。"""
    state = await _shared.pilot_store().load(str(sid or ""))
    if state is None:
        return {"kind": "empty", "note": "no_data"}
    store = _shared.pilot_store()
    rows = []
    for t in state.turns[-_shared._READ_ROWS_LIMIT:]:
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

        digest = pilot_context.reference_digest(state)
    except Exception:  # noqa: BLE001 — 摘要失败不影响清单行
        digest = None
    result = _shared.rows_result("sessionRead", rows, total=len(state.turns))
    result["sid"] = str(sid or "")
    result["sessionTitle"] = store.title_of(state)
    result["digest"] = digest or ""
    return result


async def _list_tasks(args: dict, sid: str | None = None) -> dict:
    return await list_tasks()


async def _start_task(args: dict, sid: str | None = None) -> dict:
    return await start_task(str(args.get("kind") or ""))


async def _cancel_task(args: dict, sid: str | None = None) -> dict:
    return await cancel_task(
        run_id=str(args.get("run_id") or "") or None,
        kind=str(args.get("kind") or "") or None,
    )


async def _list_sessions(args: dict, sid: str | None = None) -> dict:
    return await list_sessions()


async def _read_session(args: dict, sid: str | None = None) -> dict:
    return await read_session(str(args.get("sid") or ""))


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
                    "kind 只能取 price_refresh（全部价格刷新）或 price_repair（补抓缺价格的游戏）。"
                    "仅当用户明确要求刷新价格 / 补价格时调用；发起后告知可在对话里问进度或让它停下",
        parameters={"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["price_refresh", "price_repair"],
                     "description": "任务类型"},
        }, "required": ["kind"]},
        handler=_start_task, step_label="taskStart",
    ),
    ToolSpec(
        name="cancel_task", group="read", risk="high",
        description="停止正在运行的耗时任务。用户说『停下来』『别刷了』『取消』时调用；"
                    "只有一个任务在跑时可只给 kind 或都不给",
        parameters={"type": "object", "properties": {
            "run_id": {"type": "string", "description": "运行标识（来自 list_tasks 结果）"},
            "kind": {"type": "string", "enum": ["price_refresh", "price_repair"]},
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
        description="读取某段历史对话的内容摘要。"
                    "用户问『我们之前聊过什么』或需要旧对话里的信息时调用；"
                    "会话 id 可先用会话清单工具获取",
        parameters={"type": "object", "properties": {
            "sid": {"type": "string", "description": "会话 id（来自会话清单）"},
        }, "required": ["sid"]},
        handler=_read_session, step_label="sessionRead",
    ),
]
