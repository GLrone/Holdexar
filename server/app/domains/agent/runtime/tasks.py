"""任务登记表：可被调度面（pilot / 运行面）发起的长任务。

登记准入（设计稿见 RUNTIME_CONTROL_PLANE_PLAN 阶段四）：
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

    job_id = crawl_service.active_job_id()
    jobs = await crawl_service.list_jobs(1)
    job = jobs[0] if jobs else None
    stats = (job or {}).get("stats") or {}
    active = job_id is not None
    return {
        "active": active,
        "phase": "running" if active else "idle",
        "done": stats.get("processed"),
        "total": ref.get("count") or stats.get("total"),
        "note": (job or {}).get("status"),
        "jobId": (job or {}).get("id"),
    }


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
