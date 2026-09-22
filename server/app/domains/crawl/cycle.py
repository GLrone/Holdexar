"""价格刷新周期（Price Refresh Cycle）：一轮价格刷新的生命周期与归属。

`PriceCycle` 是一级业务对象，与 CrawlJob 是 1:N——本轮启动的每个 job 都挂
`cycle_id`，Cycle 的状态机是唯一的一级生命周期：

    planning → running → repairing → finalizing
                                   → completed / partial / failed / cancelled

- planning    冻结本轮期望集（对象 × 地区）与阶段列表
- running     本轮抓取阶段
- repairing  本轮抓取结束时仍有待补欠账（补抓由既有 repair 通道承担）
- finalizing  不再产生抓取，按本轮 job 结果收敛终态
- completed   本轮 job 全部成功且未进过 repairing
- partial     部分 job 失败/被停止，或本轮进过 repairing（有单元没拿到）
- failed      本轮没跑起来（无区可爬 / 无 job 成功）
- cancelled   本轮被用户停止

期望集在 planning 落库后即冻结：此后监控池增删、区服配置变化都不改写本轮
分母——覆盖率的分母必须可复现。

Repair 归属边界：5min `price_repair` 扫全库 missing 账本，与本轮期望集没有
确定性关联，其 job 的 `cycle_id` 保持 NULL（暂不归属）；Cycle 的 repairing
只记录本轮确实遗留了欠账，不另发起抓取。

状态只由本模块的 `advance` 写入；job / worker / scheduler 不得直接改 Cycle
状态。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, JSON, select
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_session_factory
from app.domains.crawl.models import CrawlJob

logger = logging.getLogger(__name__)

PLANNING = "planning"
RUNNING = "running"
REPAIRING = "repairing"
FINALIZING = "finalizing"
COMPLETED = "completed"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = (COMPLETED, PARTIAL, FAILED, CANCELLED)

# 未收敛状态：进程重启时遗留行要收敛成终态，否则「这一轮跑到哪了」永远无解
OPEN_STATES = (PLANNING, RUNNING, REPAIRING, FINALIZING)

# 允许的跃迁。worker / job 的内部信息（重试第几次、等代理、解析区服）一律
# 不进这张表——那属于 attempt 级，不是 Cycle 的生命周期。
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PLANNING: (RUNNING, FAILED),
    RUNNING: (REPAIRING, FINALIZING, FAILED, CANCELLED),
    REPAIRING: (FINALIZING, FAILED, CANCELLED),
    FINALIZING: (COMPLETED, PARTIAL, FAILED, CANCELLED),
}


class PriceCycle(Base):
    """一轮价格刷新：本轮该刷什么（期望集）、跑到哪（状态）、结果如何（终态）。

    期望集在 planning 阶段冻结后不再改写——监控池增删、区服配置变化都不
    影响本轮分母，否则「这一轮覆盖了多少」无从复现。
    """

    __tablename__ = "price_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # scheduled（6h 网格主轮）| manual（用户手动发起）
    kind: Mapped[str] = mapped_column(String(20))
    # planning | running | repairing | finalizing | completed | partial | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default=PLANNING)
    # 主范围：pool（监控层）/ catalog（目录层）/ appids（显式列表）
    scope: Mapped[str] = mapped_column(String(20), default="")
    # 冻结的期望集：{"appids": [...], "regions": [...]}——本轮覆盖率分母
    expected_json: Mapped[dict | None] = mapped_column(JSON)
    # 冻结的本轮阶段（spec 列表原样留存，供回溯「这轮跑了哪几段」）
    specs_json: Mapped[list | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 进入各阶段的真实时刻（NULL = 本轮没进过该阶段）；阶段耗时由它们相减得出
    running_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 进入 repairing 的时刻（NULL = 本轮结束时没有待补欠账）
    repairing_at: Mapped[datetime | None] = mapped_column(DateTime)
    finalizing_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)

    # ── 生产统计（Cycle 收敛时写入，全为 NULL = 本轮没有留下统计）──
    # 口径见 crawl/stats.py：计数来自冻结期望集 + 本轮价格结果，
    # 不是另算一套；解释不了来源的数字不写。
    targets_total: Mapped[int | None] = mapped_column(Integer)
    # 本轮「处理完」的对象数：全部期望区服都拿到明确终态（含 missing/blocked），
    # 与「是否成功」无关——targets_done 满格 + coverage 不满可以同时成立
    targets_done: Mapped[int | None] = mapped_column(Integer)
    units_expected: Mapped[int | None] = mapped_column(Integer)
    units_ok: Mapped[int | None] = mapped_column(Integer)
    units_locked: Mapped[int | None] = mapped_column(Integer)
    # 明确失败的单元 = missing + blocked
    units_failed: Mapped[int | None] = mapped_column(Integer)
    units_unobserved: Mapped[int | None] = mapped_column(Integer)
    coverage: Mapped[float | None] = mapped_column(Float)
    coverage_confirmed: Mapped[float | None] = mapped_column(Float)
    # 对象级：价格记录的最后观察时刻已 ≥ STALE_HOURS 的对象数（与 Freshness 同口径）
    stale_count: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    # {"planning": ms, "running": ms, "repairing": ms, "finalizing": ms}
    stage_ms_json: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_price_cycle_status", "status", "started_at"),)


async def create(kind: str, scope: str = "") -> int:
    """创建 Cycle（planning）。返回 cycle_id。"""
    async with get_session_factory()() as session:
        cycle = PriceCycle(
            kind=kind, status=PLANNING, scope=scope, started_at=datetime.now()
        )
        session.add(cycle)
        await session.commit()
        return cycle.id


async def freeze_expected(cycle_id: int, specs: list[dict]) -> tuple[list[str] | None, int]:
    """planning：解析并冻结本轮地区与对象集，返回 (regions, expected_units)。

    期望集 = 本轮带 scope 的阶段解析出的对象并集（去重保序）× 本轮地区，
    落库后不再随监控池增删 / 区服配置变化改写。区服不可用（一个区都没启用）
    时返回 (None, 0)，由调用方决定本轮终态。

    地区走 `crawl_service.effective_regions`——与本轮 job 实际使用的区服
    同源，冻结口径与执行口径不可能分叉。
    """
    from app.domains.crawl import service as crawl_service

    try:
        regions = await crawl_service.effective_regions(None)
    except ValueError as e:
        logger.warning("[周期] Cycle %d 区服不可用：%s", cycle_id, e)
        return None, 0

    appids: list[int] = []
    seen: set[int] = set()
    for spec in specs:
        scope = spec.get("scope")
        if not scope:
            continue
        try:
            resolved = await crawl_service.plan_scope_appids(scope, spec.get("appids"))
        except ValueError as e:
            # 该阶段本轮无可刷对象（空列表 / 未知 scope）：与 run_sequential
            # 同款容忍——跳过该阶段，不让它掀翻整轮记账
            logger.info("[周期] 阶段 %s 范围解析跳过：%s", scope, e)
            continue
        for appid in resolved:
            if appid not in seen:
                seen.add(appid)
                appids.append(appid)

    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is not None:
            cycle.expected_json = {"appids": appids, "regions": regions}
            cycle.specs_json = [dict(s) for s in specs]
            await session.commit()
    return regions, len(appids) * len(regions)


async def advance(cycle_id: int | None, status: str, *, error: str | None = None) -> bool:
    """Cycle 状态的唯一写入口。

    非法跃迁（含从终态再推进）一律拒绝并返回 False：多个 job / worker /
    scheduler 并发收敛同一个 Cycle 时只有第一条合法推进生效，`cycle_id`
    为 None（本轮未挂 Cycle）时静默跳过。
    """
    if cycle_id is None:
        return False
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is None:
            return False
        current = cycle.status
        if current in TERMINAL_STATES:
            logger.info(
                "[周期] Cycle %d 已终态（%s），忽略推进到 %s", cycle_id, current, status
            )
            return False
        if status not in _TRANSITIONS.get(current, ()):
            logger.warning(
                "[周期] Cycle %d 拒绝非法跃迁 %s → %s", cycle_id, current, status
            )
            return False
        cycle.status = status
        now = datetime.now()
        if status == RUNNING:
            cycle.running_at = now
        elif status == REPAIRING:
            cycle.repairing_at = now
        elif status == FINALIZING:
            cycle.finalizing_at = now
        if error is not None:
            cycle.error = error
        if status in TERMINAL_STATES:
            cycle.finished_at = now
        await session.commit()
    return True


def expected_units(expected: dict | None) -> int:
    """期望单元数 = 对象数 × 地区数（本轮覆盖率分母）。"""
    if not expected:
        return 0
    return len(expected.get("appids") or []) * len(expected.get("regions") or [])


def decide_terminal(
    job_statuses: list[str], *, expected_units: int, entered_repairing: bool
) -> str:
    """终态判定：看整轮 job 结果，不是最后一个 job 的状态。

    - 一个 job 都没启动：期望集为空 = 本轮无事可做，否则没跑起来
    - 全部被停止 = cancelled
    - 没有一个 job 成功（全部 failed）= failed
    - 有成功也有失败/停止 = partial（部分成功不粗暴归为 failed）
    - 全部成功：本轮进过 repairing（有单元没拿到）= partial，否则 completed
    """
    if not job_statuses:
        return COMPLETED if expected_units <= 0 else FAILED
    if all(s == "stopped" for s in job_statuses):
        return CANCELLED
    if "done" not in job_statuses:
        return FAILED
    if any(s in ("failed", "stopped") for s in job_statuses):
        return PARTIAL
    return PARTIAL if entered_repairing else COMPLETED


async def jobs_of(cycle_id: int) -> list[dict]:
    """本轮挂 `cycle_id` 的全部 job（按启动序）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(CrawlJob)
                .where(CrawlJob.cycle_id == cycle_id)
                .order_by(CrawlJob.id)
            )
        ).scalars().all()
    return [{"id": j.id, "kind": j.kind, "status": j.status} for j in rows]


def _stats_of(cycle: PriceCycle) -> dict | None:
    """Cycle 行上的生产统计；本轮没留下统计（未收敛 / 进程中断）时返回 None。

    `units_expected` 为 NULL 即「没有统计」：期望单元数恒可算，NULL 只可能
    来自从没写过统计。
    """
    if cycle.units_expected is None:
        return None
    return {
        "targetsTotal": cycle.targets_total,
        "targetsDone": cycle.targets_done,
        "unitsExpected": cycle.units_expected,
        "unitsOk": cycle.units_ok,
        "unitsLocked": cycle.units_locked,
        "unitsFailed": cycle.units_failed,
        "unitsUnobserved": cycle.units_unobserved,
        "coverage": cycle.coverage,
        "coverageConfirmed": cycle.coverage_confirmed,
        "staleCount": cycle.stale_count,
        "durationSeconds": cycle.duration_seconds,
        "stageMs": cycle.stage_ms_json,
    }


def _to_dict(cycle: PriceCycle) -> dict:
    return {
        "id": cycle.id,
        "kind": cycle.kind,
        "status": cycle.status,
        "scope": cycle.scope,
        "expectedUnits": expected_units(cycle.expected_json),
        "enteredRepairing": cycle.repairing_at is not None,
        "startedAt": cycle.started_at.isoformat() if cycle.started_at else None,
        "runningAt": cycle.running_at.isoformat() if cycle.running_at else None,
        "repairingAt": cycle.repairing_at.isoformat() if cycle.repairing_at else None,
        "finalizingAt": (
            cycle.finalizing_at.isoformat() if cycle.finalizing_at else None
        ),
        "finishedAt": cycle.finished_at.isoformat() if cycle.finished_at else None,
        "error": cycle.error,
        "stats": _stats_of(cycle),
    }


async def write_stats(cycle_id: int, stats: dict) -> bool:
    """把统计写回 Cycle 行。只有 `crawl/stats.py` 调用它。

    统计是观测结果，不参与控制：任何读取面（调度、重试、worker 数）都不得
    以这些数字作为输入。
    """
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is None:
            return False
        cycle.targets_total = stats["targetsTotal"]
        cycle.targets_done = stats["targetsDone"]
        cycle.units_expected = stats["unitsExpected"]
        cycle.units_ok = stats["unitsOk"]
        cycle.units_locked = stats["unitsLocked"]
        cycle.units_failed = stats["unitsFailed"]
        cycle.units_unobserved = stats["unitsUnobserved"]
        cycle.coverage = stats["coverage"]
        cycle.coverage_confirmed = stats["coverageConfirmed"]
        cycle.stale_count = stats["staleCount"]
        cycle.duration_seconds = stats["durationSeconds"]
        cycle.stage_ms_json = stats["stageMs"]
        await session.commit()
    return True


async def get(cycle_id: int) -> dict | None:
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        return _to_dict(cycle) if cycle is not None else None


async def list_cycles(limit: int = 10) -> list[dict]:
    """最近若干轮（新→旧），每轮带本轮 job 归属。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PriceCycle).order_by(PriceCycle.id.desc()).limit(limit)
            )
        ).scalars().all()
        out = [_to_dict(c) for c in rows]
        ids = [c.id for c in rows]
    jobs: dict[int, list[dict]] = {}
    for cid in ids:
        jobs[cid] = await jobs_of(cid)
    for item in out:
        item["jobs"] = jobs.get(item["id"], [])
    return out


async def cleanup_orphan_cycles() -> int:
    """进程启动时把上一进程遗留的未收敛 Cycle 收敛到 failed。

    与 `cleanup_orphan_jobs` 同一步骤：遗留的 running Cycle 若不清，
    「这一轮跑到哪了」永远无解（状态卡在中间态、finished_at 空着）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PriceCycle).where(PriceCycle.status.in_(OPEN_STATES))
            )
        ).scalars().all()
        for cycle in rows:
            cycle.status = FAILED
            cycle.error = "进程重启中断"
            cycle.finished_at = datetime.now()
        if rows:
            await session.commit()
            logger.warning("清理了 %d 个中断价格周期", len(rows))
    return len(rows)
