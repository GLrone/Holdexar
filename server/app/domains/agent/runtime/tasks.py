"""任务登记表：可被调度面（pilot / 运行面）发起的长任务。

登记准入：
- **只登记已存在于用户界面的长任务**（任务页「全部」档、补抓按钮）——
  禁止新造只有调度方能触发的任务，否则等于绕过用户面新增业务动作；
- `start` 只调用域内既有受理入口，不自建调度器、不绕过域内占用闸
  （占用冲突由域抛 RuntimeError/ValueError，运行账本按 failed 如实收敛）；
- `observe` 只读域账本投影，**不写任何账本**：域账本仍是业务事实源，
  这里给出的进度是采样观测（fail-soft，允许丢）；
- `stop` 走域内既有停止口，且必须是协作式的——链任务不可 cancel
  （取消会穿透编排的异常边界，把域账本停在中间态）。

不在本表内的任务形态（体检、捆绑包链尾等）后续按同一契约登记，不另开机制。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# 域终态全集（price_cycles / crawl_jobs 的收尾字符串）；observe 只在
# 观测到终态时给出 outcome，任务执行器经 state.map_domain_status 收口
_CYCLE_TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
_JOB_TERMINAL = frozenset({"done", "failed", "stopped"})


@dataclass(frozen=True)
class TaskSpec:
    """一个可调度的长任务：受理 / 观察 / 停止三件事，都是域内既有能力。"""

    kind: str
    label: str
    start: Callable[[], Awaitable[dict]]
    observe: Callable[[dict], Awaitable[dict]]
    stop: Callable[[], Awaitable[bool]] | None = None


async def _start_price_refresh() -> dict:
    """价格刷新（全部）：与任务页「全部」档同一个受理口。"""
    from app.domains.crawl import service as crawl_service

    return await crawl_service.start_full_queue()


async def _observe_price_refresh(ref: dict) -> dict:
    """观察全队列：链句柄 + 当前段 + 本轮批次进度（全部只读投影）。"""
    from app.domains.crawl import cycle as price_cycle
    from app.domains.crawl import service as crawl_service

    job_id = crawl_service.active_job_id()
    chained = crawl_service.full_queue_running()
    phase = "running" if job_id is not None else ("starting" if chained else "idle")
    out: dict = {
        "active": chained or job_id is not None,
        "phase": phase,
        "done": None,
        "total": None,
        "note": None,
    }
    cycles = await price_cycle.list_cycles(1)
    started_at = ref.get("startedAt")
    if cycles and (started_at is None or (cycles[0].get("startedAt") or "") >= started_at):
        cycle = cycles[0]
        out["cycleId"] = cycle.get("id")
        out["done"] = cycle.get("batchesDone")
        out["total"] = cycle.get("batchesExpected")
        out["note"] = cycle.get("status")
        if cycle.get("status") in _CYCLE_TERMINAL:
            out["outcome"] = cycle.get("status")
    elif job_id is not None:
        jobs = await crawl_service.list_jobs(1)
        if jobs:
            out["done"] = (jobs[0].get("stats") or {}).get("processed")
            out["note"] = jobs[0].get("status")
    return out


async def _stop_price_refresh() -> bool:
    from app.domains.crawl import service as crawl_service

    return await crawl_service.stop_full_queue()


async def _start_price_repair() -> dict:
    """价格补抓：与任务页「补游戏价格」按钮同一个受理口（冷却 0 = 立即补）。"""
    from app.domains.crawl import service as crawl_service

    return await crawl_service.start_job(
        scope="appids", kind="repair", missing_cooldown=0
    )


async def _observe_price_repair(ref: dict) -> dict:
    from app.domains.crawl import service as crawl_service

    # 认领规则：受理路径按 job id 认领（最新 job 可能是别人插进来的手动
    # 任务）；桥接采样（未受理具体 job）只认本运行期内启动的 job——
    # 进度与终态都不能张冠李戴到历史 job 上
    want_id = ref.get("id")
    started_at = ref.get("startedAt")
    jobs = await crawl_service.list_jobs(5)
    if want_id is not None:
        job = next((j for j in jobs if j.get("id") == want_id), None)
    elif started_at is not None:
        job = next(
            (j for j in jobs if (j.get("startedAt") or "") >= started_at), None
        )
    else:
        job = jobs[0] if jobs else None
    if want_id is not None and job is None:
        # 受理的 job 行尚未可见（域侧拒收 / 事务在途）——按未观测处理
        return {"active": False, "phase": "idle", "done": None,
                "total": ref.get("count"), "note": "unobserved"}
    stats = (job or {}).get("stats") or {}
    active = job is not None and crawl_service.active_job_id() is not None
    out = {
        "active": active,
        "phase": "running" if active else "idle",
        "done": stats.get("processed"),
        "total": ref.get("count") or stats.get("total"),
        "note": (job or {}).get("status"),
        "jobId": (job or {}).get("id"),
    }
    status = (job or {}).get("status")
    if status in _JOB_TERMINAL and (
        started_at is None or ((job.get("startedAt") or "") >= started_at)
    ):
        out["outcome"] = status
    return out


async def _stop_price_repair() -> bool:
    from app.domains.crawl import service as crawl_service

    return await crawl_service.stop_job()


_REGISTRY: dict[str, TaskSpec] = {
    spec.kind: spec
    for spec in (
        TaskSpec(
            kind="price_refresh",
            label="priceRefresh",
            start=_start_price_refresh,
            observe=_observe_price_refresh,
            stop=_stop_price_refresh,
        ),
        TaskSpec(
            kind="price_repair",
            label="priceRepair",
            start=_start_price_repair,
            observe=_observe_price_repair,
            stop=_stop_price_repair,
        ),
    )
}

TASK_KINDS: tuple[str, ...] = tuple(_REGISTRY)


def get(kind: str) -> TaskSpec | None:
    return _REGISTRY.get(kind)


def label_of(kind: str) -> str:
    spec = _REGISTRY.get(kind)
    return spec.label if spec else kind


def all_specs() -> tuple[TaskSpec, ...]:
    return tuple(_REGISTRY.values())
