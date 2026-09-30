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

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, write_gate
from app.crawler.occupancy import crawler_busy
from app.domains.proxypool.bridge import ingest_ledger, rebind_missing_sources
from app.domains.proxypool.exits import exit_snapshot, select_exit_slots, slot_signature
from app.domains.proxypool.health import (
    DEFAULT_BUSINESS_APPID,
    PROBE_COMMIT_EVERY,
    BusinessOutcome,
    HealthOutcome,
    business_check_pool,
    exit_ip_check_pool,
    health_check_pool,
    latest_l0_delays,
    recover_dead_nodes,
)
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

# 池内 L0 每探这么多个节点提交一次：写锁窗口 = 一块的落账耗时，不随池规模线性
# 增长。与 L1/L2 内部分块（health.PROBE_COMMIT_EVERY）同值同源。
L0_COMMIT_EVERY = PROBE_COMMIT_EVERY

# 首轮出口身份发现的上限：启动链里跑的是一次**有界** L1——整池串行探完才开门是
# 不可接受的（本地软件的开箱体验优先），但"一个出口都没探到"会让首轮爬取退化到
# 按节点计容量。上限取与生产 worker 上限同量级，够把容量模型建出来即可。
STARTUP_L1_MAX_NODES = 60
STARTUP_L1_BUDGET_SECONDS = 240.0

_pending = False
# L1/L2 体检探测进行中（跨整池串行探测，可达分钟级）。给任务页「系统在干嘛」
# 提供读口——体检不是 CrawlJob，任务列表看不到它。
_maintenance_running = False


def maintenance_running() -> bool:
    """体检（L1/L2 整池探测）当前是否在跑。"""
    return _maintenance_running


async def run_startup_l1(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    target_url: str | None = None,
    max_nodes: int = STARTUP_L1_MAX_NODES,
    budget_seconds: float = STARTUP_L1_BUDGET_SECONDS,
) -> tuple:
    """首轮出口身份发现（有界）：只探**还没有出口 IP** 的池内节点。

    目的只有一个：让 bootstrap 之后、第一次真实价格刷新之前，`ProxyNode.exit_ip`
    已经有值，容量模型（一个出口一个工位）从首轮起就成立。它不是新的健康机制——
    用的是同一条 L1 探针，只是限了节点数与时间预算，并按块提交把写锁窗口压在秒级。

    已经有出口 IP 的节点**不重探**：它们的身份由 5 分钟一拍的维护周期负责刷新，
    启动链不该重复付这份时间。
    """
    known = await _names_with_exit(session)
    names = tuple(n for n in pool_file_names(data_dir) if n not in known)[
        : max(0, int(max_nodes))
    ]
    if not names:
        return ()
    extra = {"url": target_url} if target_url else {}
    outcomes: list = []
    deadline = time.monotonic() + max(0.0, float(budget_seconds))
    for start in range(0, len(names), L0_COMMIT_EVERY):
        if time.monotonic() >= deadline:
            logger.info(
                "[首轮L1] 时间预算用尽（已探 %d/%d 个节点），其余交给维护周期",
                len(outcomes), len(names),
            )
            break
        chunk = names[start:start + L0_COMMIT_EVERY]
        outcomes.extend(await exit_ip_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, names=chunk, **extra,
        ))
        await session.commit()
    return tuple(outcomes)


async def _names_with_exit(session: AsyncSession) -> set[str]:
    """已经有出口 IP 的合格节点名（启动链据此跳过重复探测）。"""
    return set(await exit_snapshot(session))


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
    recovery_controller: tuple[str, str] | None = None,
) -> tuple[HealthOutcome, ...]:
    """L0：传输层健康。**可与 crawl 并行**（不动 `GLOBAL`）。

    只做两件事：改 `state`；若合格集因此变化，置 `rebuild_pending`。
    重建交给空闲时的 `run_pending_rebuild()`——占线时绝不 stop 内核。

    池内 L0 之外，还对 DEAD 节点分批做恢复探测（`recovery_controller` = 持有这些节点
    配置的内核）：DEAD 不在池文件里，池内 L0 永远探不到它，没有这条路径节点一旦 DEAD
    就永久出局、失败计数也停住。恢复成功使节点重新合格，合格集变化由下面的
    before/after 比较去请求重建。

    **写锁窗口**：池内探针按 `L0_COMMIT_EVERY` 分块，逐块提交——整池一次提交会随池规模
    把写锁按住数分钟，同时段其它 job 的写入会撞满 `busy_timeout`。
    """
    # 两账本对齐（体检 → 池）：先按已准入订阅的最新成功快照补回缺失的来源关联
    # （缺来源在合格集口径里等于出局，不补回就永久不可探），再把已落库的节点体检
    # 结论按来源推进池账本。前沿放在合格集快照之前——复活节点带来的合格集变化由
    # 下面的 before/after 比较照常请求重建，不新增调度、不新增信号。
    # 对齐失败只记日志：它是维护动作，不得拖垮 L0 与重建判定。
    # 对齐是写事务：过写调度器提交，随后的合格集读才不会把挂起写入
    # autoflush 到闸外。
    async with write_gate(WritePriority.BACKGROUND):
        try:
            repaired = await rebind_missing_sources(session, now=now)
            if repaired:
                logger.info("[来源补回] 订阅 %s 的来源关联按最新快照补齐", list(repaired))
            ingested = await ingest_ledger(session, now=now)
            if ingested.changed:
                logger.info(
                    "[体检→池] 账本对齐：命中 %d（复活 %d / 新出口 %d，陈久跳过 %d）",
                    ingested.matched, ingested.activated, ingested.exit_ips_added,
                    ingested.skipped_stale,
                )
            await session.commit()
        except Exception:  # noqa: BLE001 —— 对齐失败不阻断 L0 与重建判定
            logger.exception("[体检→池] 两账本对齐失败（本轮跳过）")
            await session.rollback()
    before = await eligible_runtime_names(session)
    outcomes: list[HealthOutcome] = []
    targets = pool_file_names(data_dir)
    extra = {"url": target_url} if target_url else {}
    for start in range(0, len(targets), L0_COMMIT_EVERY):
        chunk = targets[start:start + L0_COMMIT_EVERY]
        # 探测与落库的分离在 health_check_pool 内部完成（内部过闸提交）
        outcomes.extend(await health_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, names=chunk, **extra,
        ))
        await session.commit()
    if recovery_controller is not None:
        await recover_dead_nodes(
            session, controller_url=recovery_controller[0], secret=recovery_controller[1],
            now=now, **extra,
        )
        await session.commit()
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

    crawl 占线时返回 `None`（**跳过**，不排队）。恢复用的是**当前**合格集而非事务
    开始时缓存的列表：L0 允许与维护并行，池可能在维护期间就变了——那时若原节点
    已出池，必须落到当前池第一项，而不是恢复一个已经不存在的选择。
    """
    if crawler_busy():
        return None

    global _maintenance_running
    _maintenance_running = True
    try:
        return await _maintenance_probe(
            session, data_dir=data_dir, controller_url=controller_url,
            secret=secret, now=now, l1_url=l1_url, l2_url=l2_url, appid=appid,
        )
    finally:
        _maintenance_running = False


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
    # 本轮体检的运行台账：独立计时（duration_ms）+ 节点级结论汇总，前端展示
    # 「本次体检耗时 / 健康数」以此为准；逐节点观测按 run_id 归属。
    run = HealthRun(channel="scheduled", started_at=now)
    session.add(run)
    # run 行即刻提交：探测是分钟级串行网络段，写事务若从这里一直开到探测循环的
    # 首个分块提交，SQLite 写锁会被按住 10 个探测的时长（慢节点日可达数百秒），
    # 同拍的价格落库/钱包/设置写入全部排队甚至撞满 busy_timeout。观测行的归属
    # 只依赖 run.id，run 行先落库不影响一致性。
    async with write_gate(WritePriority.BACKGROUND):
        await session.flush()
        await session.commit()
    before_eligible = await eligible_runtime_names(session)
    # L1 会刷新出口身份，而 listener 数在重建时就定死了：身份变了（同一出口被两条
    # lane 绑、或某节点换了落地）就必须再收敛一次 listener 数，否则台账一直比当前
    # 出口集多。运行期的不变量由 `select_run_lanes` 就地保证，这里负责让内核侧收敛。
    exits_before = await exit_snapshot(session)
    # 每个探针各自兜异常：探针的程序异常不得逃出维护事务——那会跳过收尾提交，
    # 把本轮已写好的遥测留在未提交事务里。异常只记日志、该层本轮无结果（空
    # tuple），不改分类、不伪装成功；已提交的观测不受影响。
    try:
        l1 = await exit_ip_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, run_id=run.id, **({"url": l1_url} if l1_url else {}),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[L1] 出口 IP 探针程序异常：本轮 L1 无结果")
        l1 = ()
    try:
        l2 = await business_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, appid=appid, run_id=run.id, **({"url": l2_url} if l2_url else {}),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[L2] 业务探针程序异常：本轮 L2 无结果")
        l2 = ()
    run.total = len(l2)
    run.steam_ok = sum(1 for o in l2 if getattr(o, "ok", False))
    run.ip_known = sum(1 for o in l1
                       if getattr(o, "ok", False) and getattr(o, "exit_ip", None))
    run.failed = run.total - run.steam_ok
    run.finished_at = datetime.now()
    run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
    # 收尾提交放在恢复 GLOBAL（网络调用）之前：节点状态与观测此时已齐，
    # 写锁不得跨网络段持有。
    async with write_gate(WritePriority.BACKGROUND):
        await session.commit()
    # L2 现在喂节点状态机：合格集可能因判死/复活而变化，与 L0 周期同款
    # before/after 比较请求重建，不另立信号。
    if await eligible_runtime_names(session) != before_eligible:
        request_rebuild()

    exits_after = await exit_snapshot(session)
    if slot_signature(exits_after) != slot_signature(exits_before):
        request_rebuild()
        logger.info(
            "[L1] 出口身份变化：%d → %d 个已知出口，置 rebuild_pending（空闲时收敛 listener 数）",
            len(exits_before), len(exits_after),
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
    恢复路径。闸内任何读取异常一律放行重建（宁多动一次，不漏一次真重建）。
    """
    try:
        alive = current_runtime_proxy_url(data_dir) is not None
        eligible = set(await eligible_runtime_names(session))
        if alive and not eligible:
            return "合格集为空且 Runtime 在跑：拒绝物化空池（保留现核等订阅恢复）"
        if set(pool_file_names(data_dir)) != eligible:
            return None
        ports = runtime_lane_ports(data_dir)
        slots = select_exit_slots(
            await eligible_nodes(session), delays=await latest_l0_delays(session)
        )
        # 期望 lane 数必须与重建的口径一致（`len(slots) or None` 走 lane_count_for），
        # 否则闸会放过/误伤与重建产物不同的现状
        expected_lanes = lane_count_for(len(eligible), len(slots) or None)
        if len(ports) != expected_lanes:
            return None
        plan = {int(e.get("lane", -1)): e for e in read_lane_plan(data_dir)}
        for i, slot in enumerate(slots):
            entry = plan.get(i) or {}
            node = str(entry.get("node") or "")
            exit_ip = str(entry["exitIp"]) if entry.get("exitIp") else None
            if node != slot.runtime_name or exit_ip != slot.exit_ip:
                return None
        if alive:
            return "池与 lane 计划均与现状一致：重建无目标差异"
        return None
    except Exception:  # noqa: BLE001 —— 闸 itself 故障时放行重建，不拦真需求
        logger.exception("[重建] 空转闸读取异常，按需重建处理")
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
    """
    if crawler_busy():
        return None
    if not take_rebuild_pending():
        return None
    skip = await _rebuild_skip_reason(session, data_dir=data_dir)
    if skip is not None:
        logger.info("[重建] 跳过本次重建：%s", skip)
        return None
    return await rebuild_runtime(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        runtime=runtime, exe_path=exe_path,
    )


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
    recovery_controller: tuple[str, str] | None = None,
) -> ProxypoolCycleResult:
    """一个周期的固定顺序：**先判断节点是否坏了，再决定重建，重建后才做依赖 GLOBAL 的维护。**

    ① L0（可与 crawler 并行）
    ② 重新判断占用：busy → 本轮到此为止（不重建、不 L1/L2、不切 GLOBAL）
    ③ 空闲 → 消费 `rebuild_pending` 并重建
    ④ 重建后确认 Runtime ready（新 controller/端口）
    ⑤ 最后才 L1/L2——观察对象因此始终接近生产状态

    顺序不能颠倒：若先 L1/L2，它们面对的还是"含已死节点"的旧 Runtime，没有意义。

    **事务边界**：L0 独立提交一次；维护段（重建 + L1/L2）的写事务不跨网络段——
    run 行即落即提交，两个探测池函数返回前提交，收尾提交在恢复 GLOBAL 之前。
    L1/L2 串行探完池内节点是分钟级网络等待，任何一段开在未提交事务里都会把
    SQLite 写锁按住数个探测的时长，同拍的订阅刷新/账单/价格落库等 job 会撞满
    `busy_timeout` 全部失败。每次持锁只到单块提交的毫秒级。
    """
    l0 = await run_l0_cycle(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        now=now, target_url=l0_target_url, recovery_controller=recovery_controller,
    )
    await session.commit()

    if crawler_busy():
        # L0 是唯一允许在 crawler 占线时运行的维护动作
        return ProxypoolCycleResult(l0=l0, busy=True, rebuilt=None, maintenance=None)

    rebuilt = await run_pending_rebuild(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        runtime=runtime, exe_path=exe_path,
    )
    if rebuilt is not None:
        # 重建换掉了实例：后续维护必须用**新的** controller
        controller_url = rebuilt.controller_url
        secret = getattr(runtime, "secret", secret)

    maintenance = await run_maintenance_cycle(
        session, data_dir=data_dir, controller_url=controller_url, secret=secret,
        now=now, l1_url=l1_url, l2_url=l2_url, appid=appid,
    )
    return ProxypoolCycleResult(
        l0=l0, busy=False, rebuilt=rebuilt, maintenance=maintenance
    )