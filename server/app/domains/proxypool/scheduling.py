"""Runtime 调度边界：**谁能在什么时刻占用 Runtime。**

    Crawl 是 Runtime 的占用者；L1/L2 是维护者；rebuild 是破坏性操作。
    维护者只能在占用结束后做破坏性操作。

三条边界：

- **L0** 走 `/proxies/{name}/delay`，不改 `GLOBAL` → **可与 crawl 并行**；它只改 `state`，
  池内容因此变脏时**只置 `rebuild_pending`**，绝不立即 stop 内核（重启会打断
  在途请求）。
- **L1/L2** 会 `PUT /proxies/GLOBAL` → 与 crawl 并行会改掉 crawler 的出口并让归因失真
  → **占线时直接跳过**（观察任务，不排队，避免积压成自己的调度负担）。跑完必须
  **恢复 GLOBAL**，否则每轮维护都会偷偷改生产出口。
- **rebuild** 优先热重载（`PUT /configs`，进程不动、端口不变），控制器不可达或
  重载未通过才回退 stop/start；请求先落成 `rebuild_pending`，空闲时消费。

`rebuild_pending` 是**可合并的信号**（`false→true`、`true→true`），代表"当前 Runtime 的
池内容已脏，需要一次重建"，不是 L0 专属，也不做计数。消费侧另有**空转闸**：目标与
现状完全一致、或合格集已空而 Runtime 还活着时，重建被跳过（见 `run_pending_rebuild`）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, write_gate
from app.core.logging import log_event
from app.crawler.occupancy import (
    MAINTENANCE_HEALTH,
    MAINTENANCE_REBUILD,
    CrawlerBusyError,
    begin_maintenance,
    crawler_busy,
    end_maintenance,
    maintenance_kind,
)
from app.domains.proxypool.bridge import (
    purge_orphan_subscription_sources,
    rebind_missing_sources,
)
from app.domains.proxypool.exits import exit_snapshot, select_exit_slots, slot_signature
from app.domains.proxypool.health import (
    DEFAULT_BUSINESS_APPID,
    NODE_PROBE_FAILED,
    PROBE_COMMIT_EVERY,
    BusinessOutcome,
    HealthOutcome,
    business_check_pool,
    exit_ip_check_pool,
    health_check_pool,
    latest_l0_delays,
)
from app.domains.proxypool.dead_probe import recover_dead_nodes_self_hosted
from app.domains.proxypool.models import HealthRun
from app.domains.proxypool.pool import eligible_nodes, eligible_runtime_names, pool_file_names
from app.domains.proxypool.runtime import (
    RebuildResult,
    _KernelRuntime,
    apply_global_selection,
    current_global_selection,
    current_runtime_proxy_url,
    lane_count_for,
    read_lane_plan,
    rebuild_runtime,
    restore_selection,
    runtime_lane_ports,
)

logger = logging.getLogger(__name__)

L0_COMMIT_EVERY = PROBE_COMMIT_EVERY

# rebuild 跨调用方互斥：take_rebuild_pending 的消费原子只保「取」不保「做」——
# take 与 rebuild_runtime 之间的 await 窗口里新置位的 pending 会让第二个调用方
# 也进入重建（双内核 stop/start）。全部入口（启动链两处 / 周期 job / bootstrap）
# 均不持写闸进入，锁内只有读取与 events.record 的独立短写闸——闸↔锁只有
# 「锁内取闸」单向序，AB-BA 不可能。重建本体对 session 零写入（Registry 写入
# 归订阅同步段），持闸横跨整段会把全体写者饿死在写者位上。
_rebuild_lock = asyncio.Lock()

_pending = False


def maintenance_running() -> bool:
    """体检（L1/L2 整池探测）当前是否在跑——预约位是唯一事实源，重建期间不点亮。"""
    return maintenance_kind() == MAINTENANCE_HEALTH


_MAINTENANCE_EVENT_LABELS = {
    MAINTENANCE_HEALTH: "代理体检",
    MAINTENANCE_REBUILD: "代理通道重建",
}

MAINTENANCE_INTERVAL_HOURS_KEY = "proxy.pool_maintenance_interval_hours"
MAINTENANCE_LAST_SIG_KEY = "proxy.pool_maintenance_last_sig"
DEFAULT_MAINTENANCE_INTERVAL_HOURS = 2


def _eligible_signature(names) -> str:
    """合格集签名：集合变化（判死/复活/换节点）必然改变它。"""
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


async def maintenance_due(session: AsyncSession, *, now: datetime) -> bool:
    """体检节流判据：最近一次成功收尾的 scheduled 体检在间隔内、且当前合格集
    签名与上次收尾记录一致 → 未到期；无历史 run、超间隔、签名变化、interval≤0
    或任何读取异常 → 到期。"""
    from app.domains.settings.service import get_value

    try:
        interval = await get_value(
            MAINTENANCE_INTERVAL_HOURS_KEY, DEFAULT_MAINTENANCE_INTERVAL_HOURS
        )
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = DEFAULT_MAINTENANCE_INTERVAL_HOURS
        if interval <= 0:
            return True
        last = (await session.execute(
            select(HealthRun)
            .where(
                HealthRun.channel == "scheduled",
                HealthRun.finished_at.is_not(None),
                HealthRun.end_reason.is_(None),
                # 探测通道故障（如控制器认证失配）会让整轮 0 成功——它不是
                # 「体检过且健康」的锚，隔 2 小时静默会漏掉真节点结论
                HealthRun.total > 0,
                HealthRun.steam_ok > 0,
            )
            .order_by(HealthRun.started_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if last is None or last.started_at is None:
            return True
        if now - last.started_at >= timedelta(hours=interval):
            return True
        signature = _eligible_signature(await eligible_runtime_names(session))
        return (await get_value(MAINTENANCE_LAST_SIG_KEY)) != signature
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "体检节流判据读取异常，本轮按到期处理",
            level=logging.WARNING,
            exc_info=True,
        )
        return True


async def _record_maintenance_signature(session: AsyncSession) -> None:
    """成功体检收尾记录当前合格集签名（节流判据的事实源）；写失败只记日志。"""
    from app.domains.settings.service import set_value

    try:
        await set_value(
            MAINTENANCE_LAST_SIG_KEY,
            _eligible_signature(await eligible_runtime_names(session)),
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "体检签名写入失败，下一拍体检可能重复执行",
            level=logging.WARNING,
            exc_info=True,
        )


async def _announce_maintenance(kind: str, *, totals: dict | None = None) -> None:
    """维护开始播报：用户可见日志 + SSE 实时事件（均 fail-soft）。

    维护期间爬取被预约位拒绝，这是前端与灵动岛唯一能投影「体检进行中 /
    重建进行中」的窗口——没有这一步，维护就是一段用户完全看不见的静默期。"""
    from app.core.events import bus

    payload: dict = {"kind": kind}
    if totals is not None:
        payload["totals"] = totals
    if kind == MAINTENANCE_HEALTH:
        count = (totals or {}).get("l1")
        count_text = str(count) if count is not None else "?"
        log_event(
            logger,
            f"代理体检开始：待检 {count_text} 个节点，价格抓取期间自动让路",
            tag="开始",
        )
    else:
        log_event(
            logger,
            f"{_MAINTENANCE_EVENT_LABELS[kind]}开始：期间价格抓取自动让路",
            tag="开始",
        )
    try:
        bus.publish("maintenance.started", **payload)
    except Exception:  # noqa: BLE001
        pass


async def _conclude_maintenance(
    kind: str, *, elapsed_s: float, summary: dict | None = None,
) -> None:
    """维护收场播报：用户可见日志 + SSE 事件（fail-soft）。summary 带节点级结论
    （total/ok/failed），异常中断路径没有结论，只有耗时。"""
    from app.core.events import bus

    payload: dict = {"kind": kind, "durationMs": int(elapsed_s * 1000)}
    payload.update(summary or {})
    if kind == MAINTENANCE_HEALTH and summary and "ok" in summary:
        log_event(
            logger,
            f"代理体检结束：用时 {elapsed_s / 60:.1f} 分钟，"
            f"正常 {summary.get('ok', 0)} · 异常 {summary.get('failed', 0)}",
            tag="结束",
        )
    elif kind == MAINTENANCE_HEALTH and summary:
        log_event(
            logger,
            f"代理体检结束：用时 {elapsed_s / 60:.1f} 分钟，"
            f"待检 {summary.get('total', 0)} 个节点，本轮无有效结论",
            tag="结束",
        )
    else:
        log_event(
            logger,
            f"{_MAINTENANCE_EVENT_LABELS[kind]}结束：用时 {elapsed_s / 60:.1f} 分钟",
            tag="结束",
        )
    try:
        bus.publish("maintenance.ended", **payload)
    except Exception:  # noqa: BLE001
        pass


def request_rebuild() -> None:
    """标记"池内容已脏，需要一次重建"。幂等：连续置位只算一次。"""
    global _pending
    _pending = True


def rebuild_pending() -> bool:
    return _pending


def take_rebuild_pending() -> bool:
    """取出并清空（消费语义）。返回此前是否处于待重建。"""
    global _pending
    was = _pending
    _pending = False
    return was


@dataclass(frozen=True)
class MaintenanceResult:
    """一次 L1/L2 维护事务的结果。"""

    previous_selection: str | None
    selection: str | None
    l1: tuple[BusinessOutcome, ...] | tuple
    l2: tuple[BusinessOutcome, ...]


async def run_l0_cycle(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    target_url: str | None = None,
) -> tuple[HealthOutcome, ...]:
    """L0：传输层健康。**可与 crawl 并行**（不动 `GLOBAL`）。

    只做两件事：改 `state`；若合格集因此变化，置 `rebuild_pending`。
    重建交给空闲时的 `run_pending_rebuild()`——占线时绝不 stop 内核。

    池内 L0 之外，还对 DEAD 节点分批做恢复探测（自建临时内核逐节点渲染探测）：DEAD 不在池文件里，池内 L0 永远探不到它，没有这条路径节点一旦 DEAD
    就永久出局、失败计数也停住。恢复成功使节点重新合格，合格集变化由下面的
    before/after 比较去请求重建。

    **写锁窗口**：池内探针按 `L0_COMMIT_EVERY` 分块，逐块提交——整池一次提交会随池规模
    把写锁按住数分钟，同时段其它 job 的写入会撞满 `busy_timeout`。
    """
    # 对齐是写事务：过写调度器提交，随后的合格集读才不会把挂起写入
    # autoflush 到闸外。
    async with write_gate(WritePriority.BACKGROUND):
        try:
            purged = await purge_orphan_subscription_sources(session, now=now)
            if purged:
                log_event(
                    logger,
                    f"已清理 {len(purged)} 条已删订阅的残留来源关联",
                    tag="已修复",
                    detail={"订阅编号": "、".join(str(i) for i in purged)},
                )
            repaired = await rebind_missing_sources(session, now=now)
            if repaired:
                log_event(
                    logger,
                    f"已按最新快照补齐 {len(repaired)} 条订阅的来源关联",
                    tag="已修复",
                    detail={"订阅编号": "、".join(str(i) for i in repaired)},
                )
            await session.commit()
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "体检结果与池账本对齐失败，本轮跳过",
                level=logging.ERROR,
                exc_info=True,
            )
            await session.rollback()
    before = await eligible_runtime_names(session)
    outcomes: list[HealthOutcome] = []
    targets = pool_file_names(data_dir)
    extra = {"url": target_url} if target_url else {}
    for start in range(0, len(targets), L0_COMMIT_EVERY):
        chunk = targets[start:start + L0_COMMIT_EVERY]
        outcomes.extend(await health_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, names=chunk, **extra,
        ))
    await recover_dead_nodes_self_hosted(
        session, data_dir=data_dir, now=now, **extra,
    )
    if await eligible_runtime_names(session) != before:
        request_rebuild()
    return tuple(outcomes)


async def run_maintenance_cycle(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    l1_url: str | None = None,
    l2_url: str | None = None,
    appid: int = DEFAULT_BUSINESS_APPID,
) -> MaintenanceResult | None:
    """L1/L2 维护事务：capture → 串行探测 → 恢复 `GLOBAL`。

    crawl 占线时返回 `None`（**跳过**，不排队）。维护全程持维护预约位（计入
    crawler_busy）：体检期间爬取被拒、rebuild 让路。恢复用的是**当前**合格集而非事务
    开始时缓存的列表：L0 允许与维护并行，池可能在维护期间就变了——那时若原节点
    已出池，必须落到当前池第一项，而不是恢复一个已经不存在的选择。
    """
    if crawler_busy():
        return None
    try:
        begin_maintenance(MAINTENANCE_HEALTH)
    except CrawlerBusyError:
        return None
    started = time.monotonic()
    planned_total = len(pool_file_names(data_dir))
    await _announce_maintenance(
        MAINTENANCE_HEALTH, totals={"l1": planned_total, "l2": planned_total}
    )
    try:
        result = await _maintenance_probe(
            session, data_dir=data_dir, controller_url=controller_url,
            secret=secret, now=now, l1_url=l1_url, l2_url=l2_url, appid=appid,
        )
        await _conclude_maintenance(
            MAINTENANCE_HEALTH,
            elapsed_s=time.monotonic() - started,
            summary=_health_run_summary(result),
        )
        await _record_maintenance_signature(session)
        return result
    except BaseException:
        await _conclude_maintenance(
            MAINTENANCE_HEALTH, elapsed_s=time.monotonic() - started,
        )
        raise
    finally:
        end_maintenance()


def _health_run_summary(result: MaintenanceResult | None) -> dict:
    """体检收场播报的节点级结论（L2 是生死判据层，汇总以它为准）。

    结果形状防御：维护包络对替身实现（测试哨兵）不假设 MaintenanceResult。
    整轮全部是探测通道失败（选不中节点，零节点证据）时不给 ok/failed——
    「正常 0 · 异常 N」是探测基础设施故障的假结论，不是节点结论。"""
    l2 = getattr(result, "l2", None) or ()
    total = len(l2)
    ok = sum(1 for o in l2 if getattr(o, "ok", False))
    if total and not ok and all(
        str(getattr(o, "detail", "") or "").startswith(NODE_PROBE_FAILED)
        for o in l2
    ):
        return {"total": total}
    return {"total": total, "ok": ok, "failed": total - ok}


async def _maintenance_probe(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    l1_url: str | None,
    l2_url: str | None,
    appid: int,
) -> MaintenanceResult:
    """维护探测主体（`run_maintenance_cycle` 的占用标志包络之内）。"""
    previous = await current_global_selection(controller_url, secret)
    run = HealthRun(channel="scheduled", started_at=now)
    session.add(run)
    async with write_gate(WritePriority.BACKGROUND, label="health_observation"):
        await session.flush()
        await session.commit()
    before_eligible = await eligible_runtime_names(session)
    exits_before = await exit_snapshot(session)
    # 每个探针各自兜异常：探针的程序异常不得逃出维护事务——那会跳过收尾提交，
    # 把本轮已写好的遥测留在未提交事务里。异常只记日志、该层本轮无结果（空
    # tuple），不改分类、不伪装成功；已提交的观测不受影响。失败证据同时写进
    # run 行（end_reason），空轮与真·空池在台账上可区分。
    probe_errors: list[str] = []
    try:
        l1 = await exit_ip_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, run_id=run.id, **({"url": l1_url} if l1_url else {}),
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "出口身份探针程序异常，本轮无出口身份结果",
            level=logging.ERROR,
            exc_info=True,
            detail={"run_id": run.id},
        )
        probe_errors.append("l1_error")
        l1 = ()
    try:
        l2 = await business_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, appid=appid, run_id=run.id, **({"url": l2_url} if l2_url else {}),
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "业务可用性探针程序异常，本轮无业务可用性结果",
            level=logging.ERROR,
            exc_info=True,
            detail={"run_id": run.id},
        )
        probe_errors.append("l2_error")
        l2 = ()
    run.total = len(l2)
    run.steam_ok = sum(1 for o in l2 if getattr(o, "ok", False))
    run.ip_known = sum(1 for o in l1
                       if getattr(o, "ok", False) and getattr(o, "exit_ip", None))
    run.failed = run.total - run.steam_ok
    run.end_reason = ",".join(probe_errors) or None
    run.finished_at = datetime.now()
    run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
    # 收尾提交放在恢复 GLOBAL（网络调用）之前：节点状态与观测此时已齐，
    # 写锁不得跨网络段持有。
    async with write_gate(WritePriority.BACKGROUND, label="health_observation"):
        await session.commit()
    if await eligible_runtime_names(session) != before_eligible:
        request_rebuild()

    exits_after = await exit_snapshot(session)
    if slot_signature(exits_after) != slot_signature(exits_before):
        request_rebuild()
        log_event(
            logger,
            f"出口身份变化：已知出口从 {len(exits_before)} 个变为 {len(exits_after)} 个，已标记待重建（空闲时收敛监听端口数）",
            detail={"变化前出口数": len(exits_before), "变化后出口数": len(exits_after)},
        )

    chosen = restore_selection(previous, await eligible_runtime_names(session))
    if chosen is not None:
        await apply_global_selection(controller_url, secret, chosen)
    return MaintenanceResult(
        previous_selection=previous, selection=chosen, l1=l1, l2=l2
    )


async def _rebuild_skip_reason(session: AsyncSession, *, data_dir: Path) -> str | None:
    """消费 pending 后判断这次重建是否还值得动手。返回跳过原因；`None` = 动手。

    两类空转都在这里拦下：
    - **目标未变**（合格集 = 池文件、lane 计划 = 当前出口槽、入口都活着）——
      重建是纯扰动，产出与现状完全相同的池，还要折腾一次内核；
    - **合格集已空而 Runtime 还活着**——物化空池只会杀掉在跑的内核，而爬取
      本就按 DB 快照 fail-closed（`crawl_lane_plan` 看不到出口槽），把内核留着
      等订阅恢复才是对的。池文件与合格集在此刻允许不一致。

    Runtime 不可达时**不跳过**（返回 `None`）：重建也是"内核掉了把它拉起来"的
    恢复路径。锁内任何读取异常一律放行重建（宁多动一次，不漏一次真重建）。
    """
    try:
        alive = current_runtime_proxy_url(data_dir) is not None
        eligible = set(await eligible_runtime_names(session))
        if alive and not eligible:
            return "合格集为空且 Runtime 在跑：拒绝物化空池（保留现核等订阅恢复）"
        if set(pool_file_names(data_dir)) != eligible:
            return None
        ports = runtime_lane_ports(data_dir)
        nodes = await eligible_nodes(session)
        slots = select_exit_slots(nodes, delays=await latest_l0_delays(session))
        expected_lanes = lane_count_for(len(eligible), len(slots) or None)
        if len(ports) != expected_lanes:
            return None
        plan = {int(e.get("lane", -1)): e for e in read_lane_plan(data_dir)}
        plan_exits = {str(e["exitIp"]) for e in plan.values() if e.get("exitIp")}
        slot_exits = {s.exit_ip for s in slots if s.exit_ip}
        if plan_exits or slot_exits:
            if plan_exits != slot_exits:
                return None
            by_name = {n.runtime_name: n for n in nodes}
            plan_by_exit = {
                str(e["exitIp"]): str(e.get("node") or "")
                for e in plan.values() if e.get("exitIp")
            }
            for exit_ip, node_name in plan_by_exit.items():
                node = by_name.get(node_name)
                if node is None or (node.exit_ip or "") != exit_ip:
                    return None
        if alive:
            return "池与 lane 计划均与现状一致：重建无目标差异"
        return None
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "重建必要性检查读取异常，本次仍执行重建",
            level=logging.ERROR,
            exc_info=True,
        )
        return None


async def run_pending_rebuild(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    runtime: _KernelRuntime,
    exe_path: str,
) -> RebuildResult | None:
    """空闲时消费 `rebuild_pending` 并重建 Runtime。

    占线或无待重建 → `None`（什么都不做，绝不 stop 内核）。消费发生在**动手之前**：
    重建过程中新产生的 pending 属于下一次重建，不会被这次吞掉。

    动手前先过空转闸（`_rebuild_skip_reason`）：目标与现状一致、或合格集已空而
    Runtime 还活着 → 跳过并返回 `None`；重建生效优先走热重载通道（见
    `rebuild_runtime`），失败自动回退进程重启。

    只持 `_rebuild_lock`，**不持写闸**：重建本体是文件渲染 + 内核生命周期操作
    （热重载 / stop/start）+ HTTP 等待，对 session 零写入；事件留痕走
    现有写入边界。重建段整段持闸会把交互写饿死在
    写者位上——网络与内核操作禁止进闸（写事务纪律第 1 条）。
    重建全程持维护预约位：先预约后消费，预约取得失败 pending 原样保留给下一拍。
    """
    if crawler_busy():
        return None
    async with _rebuild_lock:
        try:
            begin_maintenance(MAINTENANCE_REBUILD)
        except CrawlerBusyError:
            return None
        started = time.monotonic()
        await _announce_maintenance(MAINTENANCE_REBUILD)
        try:
            result = await _run_pending_rebuild_locked(
                session, data_dir=data_dir, controller_url=controller_url,
                secret=secret, runtime=runtime, exe_path=exe_path,
            )
            await _conclude_maintenance(
                MAINTENANCE_REBUILD, elapsed_s=time.monotonic() - started,
            )
            return result
        except BaseException:
            await _conclude_maintenance(
                MAINTENANCE_REBUILD, elapsed_s=time.monotonic() - started,
            )
            raise
        finally:
            end_maintenance()


async def _run_pending_rebuild_locked(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    runtime: _KernelRuntime,
    exe_path: str,
) -> RebuildResult | None:
    """预约在位后的重建主体（`run_pending_rebuild` 的锁内段）。"""
    if not take_rebuild_pending():
        return None
    skip = await _rebuild_skip_reason(session, data_dir=data_dir)
    if skip is not None:
        log_event(
            logger,
            "本次重建无需执行，已跳过",
            tag="跳过",
            detail={"原因": skip},
        )
        return None
    return await rebuild_runtime(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        runtime=runtime, exe_path=exe_path,
    )


async def consume_rebuild_after_crawl() -> None:
    """任务收尾（占用刚释放）后立即消费待重建，不等下一个 5min 拍。

    订阅删除/节点降级在任务进行中只做账面结算，内核级剔除被占用互斥推迟到
    任务结束——本函数就是"任务结束后剔除"的那一步：池 Runtime 在跑就热重建
    （进程/端口不动），把已标记移除的节点从内核里剔出去。Runtime 不在跑时
    重建属于"拉起内核"的恢复路径，仍归周期拍处理。全程 fail-soft。
    """
    from app.core.config import get_settings
    from app.core.database import get_session_factory
    from app.domains.proxies import clash_manager as _cm
    from app.domains.proxypool.runtime import (
        RuntimeConfigError,
        controller_endpoint_of,
        current_runtime_proxy_url,
    )

    try:
        data_dir = get_settings().data_dir
        if current_runtime_proxy_url(data_dir) is None:
            return
        base, secret = controller_endpoint_of(data_dir)
        async with get_session_factory()() as session:
            await run_pending_rebuild(
                session, data_dir=data_dir, controller_url=base, secret=secret,
                runtime=_cm.pool_runtime, exe_path=str(_cm.kernel_exe(data_dir)),
            )
    except (RuntimeConfigError, OSError):
        return
    except Exception:  # noqa: BLE001
        log_event(logger, "任务收尾重建消费异常", level=logging.ERROR, exc_info=True)


@dataclass(frozen=True)
class ProxypoolCycleResult:
    """一个调度周期的结果（供日志与测试断言）。"""

    l0: tuple[HealthOutcome, ...]
    busy: bool
    rebuilt: RebuildResult | None
    maintenance: MaintenanceResult | None


async def run_proxypool_cycle(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    runtime: _KernelRuntime,
    exe_path: str,
    now: datetime,
    l0_target_url: str | None = None,
    l1_url: str | None = None,
    l2_url: str | None = None,
    appid: int = DEFAULT_BUSINESS_APPID,
) -> ProxypoolCycleResult:
    """一个周期的固定顺序：**先判断节点是否坏了，再决定重建，重建后才做依赖 GLOBAL 的维护。**

    ① L0（可与 crawler 并行）
    ② 重新判断占用：busy → 本轮到此为止（不重建、不 L1/L2、不切 GLOBAL）
    ③ 空闲 → 消费 `rebuild_pending` 并重建
    ④ 重建后确认 Runtime ready（新 controller/端口）
    ⑤ 体检节流：间隔内且合格集签名未变 → 跳过 L1/L2（维护位不占）
    ⑥ 最后才 L1/L2——观察对象因此始终接近生产状态

    顺序不能颠倒：若先 L1/L2，它们面对的还是"含已死节点"的旧 Runtime，没有意义。

    **事务边界**：L0 独立提交一次；维护段（重建 + L1/L2）的写事务不跨网络段——
    run 行即落即提交，两个探测池函数返回前提交，收尾提交在恢复 GLOBAL 之前。
    L1/L2 串行探完池内节点是分钟级网络等待，任何一段开在未提交事务里都会把
    SQLite 写锁按住数个探测的时长，同拍的订阅刷新/账单/价格落库等 job 会撞满
    `busy_timeout` 全部失败。每次持锁只到单块提交的毫秒级。
    """
    l0 = await run_l0_cycle(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        now=now, target_url=l0_target_url,
    )
    async with write_gate(WritePriority.BACKGROUND):
        await session.commit()

    if crawler_busy():
        return ProxypoolCycleResult(l0=l0, busy=True, rebuilt=None, maintenance=None)

    rebuilt = await run_pending_rebuild(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        runtime=runtime, exe_path=exe_path,
    )
    if rebuilt is not None:
        controller_url = rebuilt.controller_url
        secret = getattr(runtime, "secret", secret)

    if not await maintenance_due(session, now=now):
        return ProxypoolCycleResult(l0=l0, busy=False, rebuilt=rebuilt, maintenance=None)

    maintenance = await run_maintenance_cycle(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        now=now, l1_url=l1_url, l2_url=l2_url, appid=appid,
    )
    return ProxypoolCycleResult(
        l0=l0, busy=False, rebuilt=rebuilt, maintenance=maintenance
    )
