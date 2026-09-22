"""P1.6-B 调度边界：**谁能在什么时刻占用 Runtime。**

    Crawl 是 Runtime 的占用者；L1/L2 是维护者；rebuild 是破坏性操作。
    维护者只能在占用结束后做破坏性操作。

三条边界，全部由实测事实决定（P1.4/P1.5）：

- **L0** 走 `/proxies/{name}/delay`，不改 `GLOBAL` → **可与 crawl 并行**；它只改 `state`，
  池内容因此变脏时**只置 `rebuild_pending`**，绝不立即 stop 内核（P1.5 实测重启会打断
  在途请求）。
- **L1/L2** 会 `PUT /proxies/GLOBAL` → 与 crawl 并行会改掉 crawler 的出口并让归因失真
  → **占线时直接跳过**（观察任务，不排队，避免积压成自己的调度负担）。跑完必须
  **恢复 GLOBAL**，否则每轮维护都会偷偷改生产出口。
- **rebuild** stop/start 内核 → 只能空闲时做；请求先落成 `rebuild_pending`，
  空闲时消费。

`rebuild_pending` 是**可合并的信号**（`false→true`、`true→true`），代表"当前 Runtime 的
池内容已脏，需要一次重建"，不是 L0 专属，也不做计数。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.occupancy import crawler_busy
from app.domains.proxypool.exits import exit_snapshot, slot_signature
from app.domains.proxypool.health import (
    DEFAULT_BUSINESS_APPID,
    BusinessOutcome,
    HealthOutcome,
    business_check_pool,
    exit_ip_check_pool,
    health_check_pool,
    recover_dead_nodes,
)
from app.domains.proxypool.pool import eligible_runtime_names, pool_file_names
from app.domains.proxypool.runtime import (
    RebuildResult,
    _KernelRuntime,
    apply_global_selection,
    current_global_selection,
    rebuild_runtime,
    restore_selection,
)

logger = logging.getLogger(__name__)

# 池内 L0 每探这么多个节点提交一次：写锁窗口 = 一块的探测耗时，不随池规模线性增长。
# 单块最坏情况（全部超时）≈ 10 × DEFAULT_TIMEOUT_MS，仍在 60s busy_timeout 之内。
L0_COMMIT_EVERY = 10

# 首轮出口身份发现的上限：启动链里跑的是一次**有界** L1——整池串行探完才开门是
# 不可接受的（本地软件的开箱体验优先），但"一个出口都没探到"会让首轮爬取退化到
# 按节点计容量。上限取与生产 worker 上限同量级，够把容量模型建出来即可。
STARTUP_L1_MAX_NODES = 60
STARTUP_L1_BUDGET_SECONDS = 240.0

_pending = False


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

    previous = await current_global_selection(controller_url, secret)
    # L1 会刷新出口身份，而 listener 数在重建时就定死了：身份变了（同一出口被两条
    # lane 绑、或某节点换了落地）就必须再收敛一次 listener 数，否则台账一直比当前
    # 出口集多。运行期的不变量由 `select_run_lanes` 就地保证，这里负责让内核侧收敛。
    exits_before = await exit_snapshot(session)
    # 每个探针各自兜异常：探针的程序异常不得逃到调度器——那会跳过调用方的 commit，
    # 把本轮已经写好的 L0/L1 遥测一起回滚。异常只记日志、该层本轮无结果（空 tuple），
    # 不改分类、不伪装成功。
    try:
        l1 = await exit_ip_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, **({"url": l1_url} if l1_url else {}),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[L1] 出口 IP 探针程序异常：本轮 L1 无结果")
        l1 = ()
    try:
        l2 = await business_check_pool(
            session, data_dir=data_dir, controller_url=controller_url, secret=secret,
            now=now, appid=appid, **({"url": l2_url} if l2_url else {}),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[L2] 业务探针程序异常：本轮 L2 无结果")
        l2 = ()

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
    """
    if crawler_busy():
        return None
    if not take_rebuild_pending():
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

    **事务边界**：L0 独立提交一次，维护（重建 + L1/L2）再提交一次。L1/L2 要串行探
    完池内节点，整段落在一个事务里会让写锁跨分钟被占住，同拍的订阅刷新/账单等 job
    会撞满 `busy_timeout` 全部失败；分两段后每次持锁只到秒级。
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