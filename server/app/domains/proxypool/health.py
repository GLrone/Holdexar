"""传输层健康检测（L0）：`/proxies/{name}/delay` 最小探测。

三层分工：L0=传输连通（本模块）/ L1=出口身份 / L2=业务可用（只记录不改状态）。
L0 目标不用生产业务接口——其抖动会经状态推进耦合进节点状态机，成片误杀；
用最轻 204 探测。响应区分：200 {"delay":n}=成功，503=节点在探测失败，
404=名字没定位到（路径编码问题）；名字进 path 一律 quote(name, safe="")。
状态推进只经 evaluate_node_state()；本模块维护连续失败计数（成功归零）作
状态机输入与退休判据。RETIRED 不在池中不被探测，故不会被一次成功自动复活
（复活规则未定义，不提前设计）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, write_gate
from app.core.logging import log_event
from app.core.tls import ssl_context as _ssl_context
from app.crawler.browse_store import StoreBrowseAPI, _to_int
from app.domains.proxies.clash_manager import _port_listening, parse_probe_lanes
from app.domains.proxypool.models import HealthObservation, HealthRun, ProxyNode
from app.domains.proxypool.pool import pool_file_names
from app.domains.proxypool.runtime import mixed_port_of, runtime_config_path
from app.domains.proxypool.state import NODE_DEAD, NODE_RETIRED, evaluate_node_state

logger = logging.getLogger(__name__)

# L0 传输层目标：204、无响应体、请求最轻（选型见模块文档）
PROBE_TARGET_URL = "http://www.gstatic.com/generate_204"

PROBE_LEVEL = "L0"
DEFAULT_TIMEOUT_MS = 5000
# DEAD 恢复探测每轮上限：DEAD 不在池文件里，池内 L0 探不到它，没有这条路节点一旦
# DEAD 就永久出局。分批轮转补探；批大小同时受 L0 写事务窗口约束，取值以「一轮能
# 覆盖可回收集的显著比例」为准，不随池规模无限放大。
RECOVERY_BATCH_SIZE = 24

# ── L1：出口 IP ──────────────────────────────────────────────────
L1_LEVEL = "L1"
# 生产默认用公网 IP 回显服务（可覆盖）；测试换成本地目标
EXIT_IP_TARGET_URL = "https://api.ipify.org?format=json"
DEFAULT_L1_TIMEOUT = 15.0

# L1/L2 整池探测的落账分块：每探这么多节点提交一次。整池一个事务会从首个写入
# （体检台账 run 行 flush）起按住 SQLite 写锁，直到整轮探测结束——L1+L2 串行
# 探完全池可达分钟级，期间全部其他写者（价格链建任务、钱包轮转…）被饿死到
# busy_timeout 超时。分块后写锁窗口 = 一块的落账耗时（毫秒级），网络等待不持锁。
PROBE_COMMIT_EVERY = 10

# L0 与探测 lane 通道的单波并发上限：lane 数与并发同源（每波 lane 各选各的节点），
# L0 不碰选择器、仅以信号量封顶。
PROBE_CONCURRENCY = 20

# L1 失败必须能区分「节点/选择失败」与「目标服务故障」——否则会建立一个
# 「公网 IP 回显服务健康状态决定代理池健康」的错误系统。
NODE_PROBE_FAILED = "NODE_PROBE_FAILED"
TARGET_SERVICE_FAILED = "TARGET_SERVICE_FAILED"
INVALID_IP_RESPONSE = "INVALID_IP_RESPONSE"

# ── L2：生产业务（StoreBrowse）────────────────────────────────────
L2_LEVEL = "L2"
DEFAULT_L2_TIMEOUT = 30.0
BUSINESS_OK = "BUSINESS_OK"
INVALID_BUSINESS_RESPONSE = "INVALID_BUSINESS_RESPONSE"
# 与 crawler 生产探测同一个 appid / 区服（Half-Life 2，长期在售）
DEFAULT_BUSINESS_APPID = 220
BUSINESS_CC = "us"


def _publish_probe_progress(level: str, probed: int, total: int | None = None) -> None:
    """体检维护包络内的探测进度 → SSE（progress 通道，可丢）。

    只在维护预约位为体检时发布：启动链 L1、手动节点检测不持维护位，各有
    自己的进度面，从这里静默跳过。进度的事实源是 health_observations 按
    run_id 的累计行数，事件只是让前端少轮询一次。`total` 是本轮计划的
    探测总量，给了才随事件下发。"""
    from app.crawler.occupancy import MAINTENANCE_HEALTH, maintenance_kind
    from app.core.events import bus

    if maintenance_kind() != MAINTENANCE_HEALTH:
        return
    payload: dict = {"kind": MAINTENANCE_HEALTH, "level": level, "probed": probed}
    if total is not None:
        payload["total"] = total
    try:
        bus.publish("maintenance.progress", **payload)
    except Exception:  # noqa: BLE001 —— 进度事件不是生产依赖
        pass


async def current_maintenance_run() -> dict | None:
    """当前进行中的体检运行（health_runs 未收尾行 + 逐层观测计数）。

    run 行在建轮时即提交、观测按 PROBE_COMMIT_EVERY 逐块落库——读账本即得
    实时进度，不另设进度状态。无进行中运行返回 None。`totals` 是本轮计划的
    探测总量（两层各 = 池文件节点数），供前端把进度表达成比例。"""
    from app.core.config import get_settings
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(HealthRun)
                .where(
                    HealthRun.finished_at.is_(None),
                    # 手动节点检测也写 run 行但不持维护预约位；本读点只在
                    # 预约位在位时被调用，只对 scheduled 运行行负责
                    HealthRun.channel == "scheduled",
                )
                .order_by(HealthRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        counts = {"l1": 0, "l2": 0}
        level_rows = await session.execute(
            select(HealthObservation.level, func.count())
            .where(HealthObservation.run_id == row.id)
            .group_by(HealthObservation.level)
        )
        for level, n in level_rows:
            key = (level or "").lower()
            if key in counts:
                counts[key] = n
        pool_total = len(pool_file_names(get_settings().data_dir))
        return {
            "runId": row.id,
            "channel": row.channel,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "probed": counts,
            "totals": {"l1": pool_total, "l2": pool_total},
        }


def business_probe_url(appid: int = DEFAULT_BUSINESS_APPID,
                       cc: str = BUSINESS_CC) -> str:
    """L2 的业务目标：**直接复用** crawler 的 StoreBrowse 契约。

    不在这里另建一套 URL / 参数：生产打的是 `IStoreBrowseService/GetItems`
    （`api.steampowered.com`），而旧探针打 `store.steampowered.com/api/appdetails`
    ——两者不是同一个主机，可达性并不一致。若健康检测自己维护一套，crawler 改了
    请求之后检测不会跟着改，就会出现「健康绿、真实 crawler 红」。
    """
    return StoreBrowseAPI.probe_url(cc, appid)

NOT_FOUND_DETAIL = "内核里没有这个节点：名字没定位到（HTTP 404）"
PROBE_FAILED_DETAIL = "内核探测失败"


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的原始结论。

    `valid=False` 表示**这一探没有测到节点本身**（内核不认识这个名字、
    控制器不可达）：它不是"经该节点访问国外网站超时"，因此不构成健康证据，
    不能累加失败次数、更不能推进状态——否则一次换端口/时序错位就能把整池判死。
    判"节点坏"的唯一依据是**经该节点发真实请求失败/超时**。
    """

    runtime_name: str
    ok: bool
    delay_ms: int | None
    detail: str
    valid: bool = True


@dataclass(frozen=True)
class HealthOutcome:
    """探测结论 + 它带来的状态变化（供调用方记录/断言）。"""

    node_id: str
    runtime_name: str
    ok: bool
    delay_ms: int | None
    detail: str
    previous_state: str
    state: str


async def probe_node(
    controller_url: str, secret: str, runtime_name: str, *,
    url: str = PROBE_TARGET_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    client_timeout: float = 30.0,
) -> ProbeResult:
    """让内核经指定 proxy 探测目标 URL，返回最基础的一次观测。"""
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    path = quote(runtime_name, safe="")  # `#` 不编码会被当成 fragment 截断整个 path
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=client_timeout, headers=headers,
                                     verify=_ssl_context(True)) as client:
            resp = await client.get(
                f"{controller_url}/proxies/{path}/delay",
                params={"url": url, "timeout": timeout_ms},
            )
    except Exception as exc:  # noqa: BLE001 —— 控制器不可达是「内核没起来」的信号
        # 控制器不可达 ≠ 节点坏：这一探根本没测到节点，不产生健康证据
        return ProbeResult(runtime_name, False, None,
                           f"控制器请求异常：{type(exc).__name__}", valid=False)

    if resp.status_code == 404:
        # 内核不认识这个名字：探错了对象，不是节点访问国外网站超时
        return ProbeResult(runtime_name, False, None, NOT_FOUND_DETAIL, valid=False)
    if resp.status_code in (401, 403):
        return ProbeResult(runtime_name, False, None,
                           f"控制器认证失败（HTTP {resp.status_code}）", valid=False)
    if resp.status_code != 200:
        return ProbeResult(runtime_name, False, None,
                           f"{PROBE_FAILED_DETAIL}（HTTP {resp.status_code}）")

    try:
        delay = resp.json().get("delay")
    except ValueError:
        return ProbeResult(runtime_name, False, None,
                           f"{PROBE_FAILED_DETAIL}（响应不是 JSON）")
    if not isinstance(delay, int) or delay <= 0:
        return ProbeResult(runtime_name, False, None,
                           f"{PROBE_FAILED_DETAIL}（delay={delay!r}）")
    return ProbeResult(runtime_name, True, delay, "")


def _pool_names(data_dir: Path) -> tuple[str, ...]:
    """要探测的对象 = 池文件里的节点（`build_pool` 的三个门禁已经校验过它）。"""
    return pool_file_names(data_dir)


def pool_probe_lanes(data_dir: Path) -> list[tuple[str, int]]:
    """池运行时配置里的探测 lane（组名, listener 端口），端口在听才算数。

    文件缺失 / 解析失败 / 无 lane → 空列表，调用方退回 GLOBAL 串行路径。
    端口没在听（内核未起、或注入探测 lane 之前启动的内核）同样剔除——
    走死端口的探测会把「通道不通」误判成「节点不通」。"""
    try:
        text = runtime_config_path(data_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    return [lane for lane in parse_probe_lanes(text) if _port_listening(lane[1])]


async def _probe_and_decide(
    session: AsyncSession, node: ProxyNode, *,
    controller_url: str,
    secret: str,
    probe_name: str,
    now: datetime,
    url: str,
    timeout_ms: int,
    state_push: bool = True,
) -> tuple[HealthOutcome, dict, tuple[int, str] | None]:
    """一次传输层探测 + 结论判定（**零写入**）；是否推进状态由 `state_push` 决定。

    返回 `(结论, 观测行参数, 待应用状态变更)`：探测的网络等待与源计数读不得
    压着未提交写事务（会话有挂起写入时这次读会 autoflush 出闸外写锁），观测
    与状态变更由调用方在写调度器内落库。

    **池内 L0 巡检传 `state_push=False`（纯记录）**：传输层可达性不再是节点生死
    判据——生死归 Steam 业务探测（`business_check_pool`）与恢复探测。`state_push=True`
    只用于 DEAD 恢复探测（目标已是 Steam 端点）：成功回 ACTIVE、失败累加计数。
    `probe_name` 是**执行探测的内核认识的名字**：池内节点用 `runtime_name`（池内核的
    运行配置里就是它），DEAD 节点不在池文件里、池内核不认识，改用订阅原名
    （`ProxyNode.original_name`）打到持有全量订阅节点的旧链路内核上。
    """
    result = await probe_node(controller_url, secret, probe_name,
                              url=url, timeout_ms=timeout_ms)
    return await _decide_probe_result(
        session, node, result, now=now, url=url, timeout_ms=timeout_ms,
        state_push=state_push,
    )


async def _decide_probe_result(
    session: AsyncSession, node: ProxyNode, result: ProbeResult, *,
    now: datetime,
    url: str,
    timeout_ms: int,
    state_push: bool = True,
) -> tuple[HealthOutcome, dict, tuple[int, str] | None]:
    """探测结论判定（结论已由调用方取得；与 `_probe_and_decide` 共用同一套规则）。"""
    if state_push:
        # 归属订阅仍在 = 来源在场（节点跟着订阅走，归属是一对一的身份组成部分）
        source_seen = node.subscription_id is not None
    else:
        source_seen = False
    previous = node.state
    observation = dict(
        node_id=node.node_id,
        level=PROBE_LEVEL,
        ok=False,
        latency_ms=None,
        detail=result.detail or None,
        observed_at=now,
        channel="scheduled",
        target=url,
    )
    if not result.valid:
        # 这一探没测到节点本身（内核不认识这个名字 / 控制器不可达）：只留观测，
        # **不累加失败计数、不推进状态**。判"节点坏"的唯一依据是经该节点发真实
        # 请求失败或超时；把"探不到对象"记成节点不健康会让一次换端口/时序错位
        # 把整池判死。
        return (
            HealthOutcome(
                node_id=node.node_id,
                runtime_name=node.runtime_name,
                ok=False,
                delay_ms=None,
                detail=result.detail,
                previous_state=previous,
                state=previous,
            ),
            observation,
            None,
        )

    state = previous
    mutations: tuple[int, str] | None = None
    if state_push:
        # 连续失败计数：成功归零、失败累加。它既是状态机的输入（退休线判据），
        # 也是台账事实——判 DEAD 的节点必须带着失败次数，不能留下"DEAD 且计数 0"。
        failures = 0 if result.ok else (node.consecutive_failures or 0) + 1
        # 唯一的状态决策入口：健康只提供证据，规则仍归状态机
        target = evaluate_node_state(
            previous,
            source_seen=source_seen,
            probe_ok=result.ok,
            consecutive_failures=failures,
        )
        mutations = (failures, target)
        state = target
    observation.update(ok=result.ok, latency_ms=result.delay_ms)
    return (
        HealthOutcome(
            node_id=node.node_id,
            runtime_name=node.runtime_name,
            ok=result.ok,
            delay_ms=result.delay_ms,
            detail=result.detail,
            previous_state=previous,
            state=state,
        ),
        observation,
        mutations,
    )


async def latest_l0_delays(session: AsyncSession) -> dict[str, int]:
    """每个节点最近一次**成功** L0 探测的延迟（node_id → ms）。

    出口槽排序的延迟事实源：缺条目 = 没测过或没成功过（排序按未测处理，
    不凭空造默认值）。只取 ok=True 的观测——失败观测的 latency_ms 是空的
    或是目标服务耗时，不构成「这个节点多快」的证据。
    """
    latest = (
        select(
            HealthObservation.node_id.label("node_id"),
            func.max(HealthObservation.observed_at).label("mx"),
        )
        .where(HealthObservation.level == PROBE_LEVEL, HealthObservation.ok.is_(True))
        .group_by(HealthObservation.node_id)
        .subquery()
    )
    rows = await session.execute(
        select(HealthObservation.node_id, HealthObservation.latency_ms)
        .join(
            latest,
            (latest.c.node_id == HealthObservation.node_id)
            & (latest.c.mx == HealthObservation.observed_at),
        )
        .where(HealthObservation.level == PROBE_LEVEL, HealthObservation.ok.is_(True))
    )
    return {
        node_id: int(ms)
        for node_id, ms in rows.all()
        if isinstance(ms, int) and ms > 0
    }


async def health_check_pool(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str = PROBE_TARGET_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    names: tuple[str, ...] | None = None,
) -> tuple[HealthOutcome, ...]:
    """对池内每个节点做一次 L0 传输探测：只落观测，**不推状态**。

    节点生死判据已归 Steam 业务探测（`business_check_pool`）：传输层可达性只回答
    「这条线通不通」，继续拿它判生死会让 gstatic 通而 Steam 不通的节点被误杀。
    `names` 不给就取池文件当前节点；给了就只探这些——供调用方**分块**调用。

    探测（网络）与落库分离：波内按 `PROBE_CONCURRENCY` 并发探（L0 不碰选择器，
    天然可并发），每块探完在写调度器内落库提交——写事务不跨网络等待。
    """
    targets = tuple(names) if names is not None else _pool_names(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(targets))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def _probe(name: str) -> ProbeResult:
        async with semaphore:
            return await probe_node(controller_url, secret, name,
                                    url=url, timeout_ms=timeout_ms)

    outcomes: list[HealthOutcome] = []
    for start in range(0, len(targets), PROBE_COMMIT_EVERY):
        chunk = [n for n in targets[start : start + PROBE_COMMIT_EVERY] if n in by_name]
        results = await asyncio.gather(*(_probe(name) for name in chunk))
        observations: list[dict] = []
        for name, result in zip(chunk, results):
            node = by_name[name]
            outcome, observation, _mutations = await _decide_probe_result(
                session, node, result, now=now, url=url, timeout_ms=timeout_ms,
                state_push=False,
            )
            outcomes.append(outcome)
            observations.append(observation)
        async with write_gate(WritePriority.BACKGROUND, label="health_observation"):
            for observation in observations:
                session.add(HealthObservation(**observation))
            await session.commit()
    return tuple(outcomes)


async def recover_dead_nodes(
    session: AsyncSession, *,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    batch: int = RECOVERY_BATCH_SIZE,
) -> tuple[HealthOutcome, ...]:
    """对 DEAD 节点分批做恢复探测：目标默认 **Steam 业务端点**（与生死判据同一标准），
    成功回 `ACTIVE` 并清零计数，失败保持 `DEAD` 并累加。

    分批轮转：按「最久没有被探过」排序（`health_observations` 的最后一条），一次只取
    `batch` 个——退休线不可达的慢性失败节点若永远排在最前，其余节点要等轮转一整圈才
    回到，回收覆盖率会长期为零。DEAD 不一次性全打出去，也不新建调度系统。成功使节点
    重新合格，合格集变化由 `run_l0_cycle` 既有的 before/after 比较去请求重建。

    `controller_url` 指向**持有这些节点配置的内核**（DEAD 不在池文件里，池内核不认识
    它们的名字）。没有来源名的节点无法定位到内核里的配置，跳过不动它。
    """
    if batch <= 0:
        return ()

    last_probe = (
        select(
            HealthObservation.node_id.label("node_id"),
            func.max(HealthObservation.observed_at).label("last_at"),
        )
        .group_by(HealthObservation.node_id)
        .subquery()
    )
    rows = (await session.execute(
        select(ProxyNode, ProxyNode.original_name)
        .outerjoin(last_probe, last_probe.c.node_id == ProxyNode.node_id)
        .where(ProxyNode.state.in_((NODE_DEAD, NODE_RETIRED)))
        .order_by(
            last_probe.c.last_at.asc().nullsfirst(),
            ProxyNode.node_id,
        )
        .limit(batch)
    )).all()

    probe_url = url or business_probe_url()
    outcomes: list[HealthOutcome] = []
    pending: list[tuple[ProxyNode, dict, tuple[int, str] | None]] = []
    for node, original_name in rows:
        outcome, observation, mutations = await _probe_and_decide(
            session, node, controller_url=controller_url, secret=secret,
            probe_name=str(original_name), now=now, url=probe_url,
            timeout_ms=timeout_ms,
        )
        outcomes.append(outcome)
        pending.append((node, observation, mutations))

    # 探测（网络）与落库分离：状态变更与观测在写调度器内一次落库提交
    async with write_gate(WritePriority.BACKGROUND, label="health_recovery"):
        # 防丢失更新（与 business_check_pool 同一防线）：补探是分钟级网络段，
        # 载入时的 state / consecutive_failures 早已陈旧——闸内按主键重读
        # （刷新本会话同一对象）后用新鲜基线重算状态迁移，窗口内手动体检
        # ingest 与订阅 settle 的写入不因陈旧基线被覆盖。
        if pending:
            await session.execute(
                select(ProxyNode)
                .where(ProxyNode.node_id.in_([n.node_id for n, _, _ in pending]))
                .execution_options(populate_existing=True)
            )
        for node, observation, mutations in pending:
            if mutations is not None:
                ok = bool(observation.get("ok"))
                failures = 0 if ok else (node.consecutive_failures or 0) + 1
                node.consecutive_failures = failures
                node.state = evaluate_node_state(
                    node.state,
                    source_seen=True,
                    probe_ok=ok,
                    consecutive_failures=failures,
                )
            session.add(HealthObservation(**observation))
        await session.commit()
    if outcomes:
        still_out = sum(1 for o in outcomes if o.state in (NODE_DEAD, NODE_RETIRED))
        log_event(
            logger,
            f"出池节点恢复探测完成：补探 {len(outcomes)} 个，成功 {len(outcomes) - still_out} 个，仍未回池 {still_out} 个",
            detail={
                "补探节点数": len(outcomes),
                "恢复成功数": len(outcomes) - still_out,
                "仍未回池数": still_out,
            },
        )
    return tuple(outcomes)


# ══ L1：出口 IP ══════════════════════════════════════════════════
# 路径：PUT /proxies/GLOBAL 选中节点 → 经 mixed-port 发真实请求 →
# 目标回显读回出口 IP。mixed-port 由运行配置携带（`prepare_runtime_config`），
# 这里只读它，不做运行期 PATCH。
#
# **L1 首版只记录观测与 `exit_ip`，不碰 `state`**：连 `evaluate_node_state()` 都不
# 调用。否则「ipify 503 → L1 fail → DEAD」就建立了「公网 IP 回显服务健康状态决定
# 代理池健康」的错误系统。状态怎么消费，留给后面的规则层。


@dataclass(frozen=True)
class ExitIpResult:
    """一次出口 IP 探测的原始结论。"""

    runtime_name: str
    ok: bool
    exit_ip: str | None
    detail: str
    # 记录用；**不参与评分**——L0 的 `/delay` 与这里的业务请求耗时是两个量
    latency_ms: int | None


@dataclass(frozen=True)
class ExitIpOutcome:
    node_id: str
    runtime_name: str
    ok: bool
    exit_ip: str | None
    detail: str
    latency_ms: int | None


def _extract_ip(text: str) -> str | None:
    """从回显响应里取出口 IP：兼容 JSON（ipify `format=json`）与纯文本。"""
    candidate = text.strip()
    if candidate.startswith("{"):
        try:
            payload = json.loads(candidate)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        candidate = str(payload.get("ip") or "").strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


async def _select_selector(controller_url: str, secret: str, selector: str,
                           runtime_name: str, timeout: float) -> str | None:
    """把指定 selector（GLOBAL 或探测 lane 组）切到目标节点。成功返回 None，失败返回 detail。

    回读 `now` 是**归因正确性**的保证：若此刻该 selector 不是目标节点，接下来测到的
    任何东西都不属于它——宁可记失败，也不能把它人的结果记到这个节点名下。
    L1 与 L2 共用这一步：切换是选择器状态，两处逻辑必须一致。
    """
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    async with httpx.AsyncClient(trust_env=False, timeout=timeout, headers=headers,
                                 verify=_ssl_context(True)) as control:
        try:
            put = await control.put(
                f"{controller_url}/proxies/{quote(selector, safe='')}",
                json={"name": runtime_name},
            )
            if put.status_code >= 400:
                return (f"{NODE_PROBE_FAILED}: 选择节点失败"
                        f"（HTTP {put.status_code}）")
            now = (await control.get(
                f"{controller_url}/proxies/{quote(selector, safe='')}"
            )).json().get("now")
        except Exception as exc:  # noqa: BLE001
            return f"{NODE_PROBE_FAILED}: 控制器不可达（{type(exc).__name__}）"

    if now != runtime_name:
        return (f"{NODE_PROBE_FAILED}: {selector} 的 now={now!r} "
                f"不是 {runtime_name!r}")
    return None


async def _select_global(controller_url: str, secret: str, runtime_name: str,
                         timeout: float) -> str | None:
    """把 `GLOBAL` 切到指定节点（lane 组变体见 `_select_selector`）。"""
    return await _select_selector(controller_url, secret, "GLOBAL", runtime_name, timeout)


async def _request_via_port(
    port: int, target: str, timeout: float,
) -> tuple[int | None, str, int | None, str]:
    """经本机 listener 端口发一次真实 GET。返回 (status, text, latency_ms, 异常类型名)；
    网络失败 status=None。"""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False,
            proxy=f"http://127.0.0.1:{port}", timeout=timeout,
            verify=_ssl_context(True),
        ) as proxied:
            resp = await proxied.get(target)
    except Exception as exc:  # noqa: BLE001
        return None, "", None, type(exc).__name__
    return resp.status_code, resp.text, int((time.monotonic() - started) * 1000), ""


async def probe_exit_ip(
    controller_url: str,
    secret: str,
    mixed_port: int,
    runtime_name: str, *,
    url: str = EXIT_IP_TARGET_URL,
    timeout: float = DEFAULT_L1_TIMEOUT,
) -> ExitIpResult:
    """选中该节点，再经 mixed-port 发一次真实请求，读回它的出口 IP。

    **必须串行调用**：`GLOBAL` 是全局选择器，切换是全局状态；并发探测会让
    「这个出口 IP 属于哪个节点」不可信。这是测量正确性问题，不是性能问题。
    并行通道见 `probe_exit_ip_lane`（每组选择互不干扰）。
    """
    failure = await _select_global(controller_url, secret, runtime_name, timeout)
    if failure is not None:
        return ExitIpResult(runtime_name, False, None, failure, None)

    status, text, latency_ms, exc = await _request_via_port(mixed_port, url, timeout)
    if status is None:
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: {exc}", None)
    if status != 200:
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: HTTP {status}", latency_ms)
    exit_ip = _extract_ip(text)
    if exit_ip is None:
        return ExitIpResult(runtime_name, False, None,
                            f"{INVALID_IP_RESPONSE}: {text.strip()[:60]!r}",
                            latency_ms)
    return ExitIpResult(runtime_name, True, exit_ip, "", latency_ms)


async def probe_exit_ip_lane(
    controller_url: str,
    secret: str,
    lane_group: str,
    lane_port: int,
    runtime_name: str, *,
    url: str = EXIT_IP_TARGET_URL,
    timeout: float = DEFAULT_L1_TIMEOUT,
) -> ExitIpResult:
    """经探测 lane 的出口 IP 变体：切**本 lane 组** → 经该组 listener 端口请求。

    同一条 lane 内切组与请求必须顺序执行（组选择是组内状态）；不同 lane 的
    选择互不干扰，可并发。回读 `now` 不对齐按探不到处理，与 GLOBAL 归因纪律
    同一条。"""
    failure = await _select_selector(controller_url, secret, lane_group,
                                     runtime_name, timeout)
    if failure is not None:
        return ExitIpResult(runtime_name, False, None, failure, None)

    status, text, latency_ms, exc = await _request_via_port(lane_port, url, timeout)
    if status is None:
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: {exc}", None)
    if status != 200:
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: HTTP {status}", latency_ms)
    exit_ip = _extract_ip(text)
    if exit_ip is None:
        return ExitIpResult(runtime_name, False, None,
                            f"{INVALID_IP_RESPONSE}: {text.strip()[:60]!r}",
                            latency_ms)
    return ExitIpResult(runtime_name, True, exit_ip, "", latency_ms)


async def _commit_exit_ip_wave(
    session: AsyncSession,
    probed: list[tuple[ProxyNode, ExitIpResult]], *,
    now: datetime,
    run_id: int | None,
    channel: str,
    url: str,
) -> None:
    """一波 L1 结论落账：短提交进闸，探测网络段绝不进闸。"""
    async with write_gate(WritePriority.BACKGROUND, label="health_observation"):
        # 防丢失更新：块内探测是网络段，载入的 exit_ip 可能已被
        # 手动体检 ingest 更新——闸内重读再写（与 L2 同一防线）。
        if probed:
            await session.execute(
                select(ProxyNode)
                .where(ProxyNode.node_id.in_([n.node_id for n, _ in probed]))
                .execution_options(populate_existing=True)
            )
        for node, result in probed:
            if result.ok and result.exit_ip:
                node.exit_ip = result.exit_ip
            session.add(HealthObservation(
                node_id=node.node_id,
                level=L1_LEVEL,
                ok=result.ok,
                latency_ms=result.latency_ms,
                detail=result.detail or None,
                observed_at=now,
                run_id=run_id,
                channel=channel,
                target=url,
            ))
        await session.commit()


async def exit_ip_check_pool(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str = EXIT_IP_TARGET_URL,
    timeout: float = DEFAULT_L1_TIMEOUT,
    names: tuple[str, ...] | None = None,
    run_id: int | None = None,
    channel: str = "scheduled",
) -> tuple[ExitIpOutcome, ...]:
    """对池内节点做一次 L1 出口 IP 观测（探测 lane 在位时按 lane 并发）。

    成功 → `ProxyNode.exit_ip` + `HealthObservation(level="L1")`；
    失败 → 只落观测与 `detail`，**不动 `exit_ip`、不动 `state`**（一次目标服务
    故障不该擦掉已经观测到的出口事实）。

    探测（网络）与落库分离：lane 在位时分波并发（每条 lane 一个独立组选择，
    波内 (lane, 节点) 各自顺序执行），波末一次短提交；无 lane 退回 GLOBAL 串行
    路径并按 `PROBE_COMMIT_EVERY` 分块——写事务不跨网络等待，写者位在块间空出。

    `names` 不给就取池文件当前节点；给了就只探这些——供调用方做**有界**首轮探测
    （启动链不能为了出口身份把整池探完才开门）。
    """
    if names is None:
        names = _pool_names(data_dir)
    lanes = pool_probe_lanes(data_dir)
    if not lanes:
        mixed_port = mixed_port_of(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[ExitIpOutcome] = []
    wave_size = len(lanes) if lanes else PROBE_COMMIT_EVERY
    for start in range(0, len(names), wave_size):
        chunk = names[start : start + wave_size]
        if lanes:
            pairs = [(lanes[i], name) for i, name in enumerate(chunk) if name in by_name]
            results = await asyncio.gather(*(
                probe_exit_ip_lane(controller_url, secret, group, port, name,
                                   url=url, timeout=timeout)
                for (group, port), name in pairs
            ))
            probed = [(by_name[name], result)
                      for (_, name), result in zip(pairs, results)]
        else:
            probed: list[tuple[ProxyNode, ExitIpResult]] = []
            for name in chunk:  # 严格串行（GLOBAL 兜底路径）
                node = by_name.get(name)
                if node is None:
                    continue
                result = await probe_exit_ip(controller_url, secret, mixed_port, name,
                                             url=url, timeout=timeout)
                probed.append((node, result))
        outcomes.extend(ExitIpOutcome(
            node_id=node.node_id,
            runtime_name=result.runtime_name,
            ok=result.ok,
            exit_ip=result.exit_ip,
            detail=result.detail,
            latency_ms=result.latency_ms,
        ) for node, result in probed)
        await _commit_exit_ip_wave(
            session, probed, now=now, run_id=run_id, channel=channel, url=url,
        )
        _publish_probe_progress(L1_LEVEL, len(outcomes), total=len(names))
    return tuple(outcomes)


# ══ L2：生产业务可用性 ═══════════════════════════════════════════
# 回答的是「这个节点能不能真正完成 Holdexar 生产所需的 StoreBrowse 请求」：
# HTTP 200 只是及格线，还要业务语义成立（信封 / AppID / success / 价格字段）。
#
# 上游契约的两条口径：
# - `success` 是整数 `1`（**不是布尔 true**）——所以这里显式拒绝 bool；
# - `final_price_in_cents` 是**字符串** `"999"`——所以按 crawler 的 `_to_int` 语义
#   解析，而不是要求 int。
#
# 与 L1 一致：**只记录观测，不碰 `state`、不擦 `exit_ip`、不碰 L0/L1 的时间戳**。
# 业务目标 503 不得变成节点 DEAD——否则就是让外部业务服务的健康决定代理池的健康。


@dataclass(frozen=True)
class BusinessResult:
    runtime_name: str
    ok: bool
    http_status: int | None
    final_price_in_cents: int | None
    detail: str
    latency_ms: int | None


@dataclass(frozen=True)
class BusinessOutcome:
    node_id: str
    runtime_name: str
    ok: bool
    http_status: int | None
    final_price_in_cents: int | None
    detail: str
    latency_ms: int | None


def _validate_business(payload, appid: int) -> tuple[bool, str, int | None]:
    """按生产 StoreBrowse 契约判定业务语义。返回 (是否成功, 不满足的原因, 价格)。"""
    if not isinstance(payload, dict):
        return False, "响应不是 JSON 对象", None
    envelope = payload.get("response")
    if not isinstance(envelope, dict):
        return False, "缺少 response 信封", None
    items = envelope.get("store_items")
    if not isinstance(items, list):
        return False, "store_items 不是 list", None

    item = None
    for candidate in items:
        if not isinstance(candidate, dict):
            continue
        if appid in (_to_int(candidate.get("appid")), _to_int(candidate.get("id"))):
            item = candidate
            break
    if item is None:
        return False, f"store_items 里没有 appid={appid}", None

    success = item.get("success")
    # success 契约是整数 1；布尔 true 属于契约不符（True == 1 会蒙混过关，故显式拒绝）
    if isinstance(success, bool) or success != 1:
        return False, f"success 不是整数 1（实际 {success!r}）", None

    best = item.get("best_purchase_option")
    if not isinstance(best, dict):
        return False, "缺少 best_purchase_option", None

    final = _to_int(best.get("final_price_in_cents"))
    if final is None:
        return False, ("final_price_in_cents 无法按 _to_int 语义解析"
                       f"（实际 {best.get('final_price_in_cents')!r}）"), None
    return True, "", final


async def probe_business(
    controller_url: str,
    secret: str,
    mixed_port: int,
    runtime_name: str, *,
    url: str | None = None,
    appid: int = DEFAULT_BUSINESS_APPID,
    timeout: float = DEFAULT_L2_TIMEOUT,
) -> BusinessResult:
    """选中该节点，经 mixed-port 打一次**生产 StoreBrowse** 业务请求。

    `url` 不传就用 `business_probe_url(appid)`（与 crawler 同一契约）；测试传本地目标。
    **必须串行调用**（同 L1：`GLOBAL` 是全局状态）。并行通道见 `probe_business_lane`。
    """
    target = url or business_probe_url(appid)

    failure = await _select_global(controller_url, secret, runtime_name, timeout)
    if failure is not None:
        return BusinessResult(runtime_name, False, None, None, failure, None)

    status, text, latency_ms, exc = await _request_via_port(mixed_port, target, timeout)
    if status is None:
        return BusinessResult(runtime_name, False, None, None,
                              f"{TARGET_SERVICE_FAILED}: {exc}", None)
    if status != 200:
        return BusinessResult(runtime_name, False, status, None,
                              f"{TARGET_SERVICE_FAILED}: HTTP {status}",
                              latency_ms)
    try:
        payload = json.loads(text)
    except ValueError:
        return BusinessResult(runtime_name, False, 200, None,
                              f"{INVALID_BUSINESS_RESPONSE}: 响应不是 JSON",
                              latency_ms)

    ok, reason, final = _validate_business(payload, appid)
    if not ok:
        return BusinessResult(runtime_name, False, 200, None,
                              f"{INVALID_BUSINESS_RESPONSE}: {reason}", latency_ms)
    return BusinessResult(
        runtime_name, True, 200, final,
        f"{BUSINESS_OK} appid={appid} final_price_in_cents={final}", latency_ms
    )


async def probe_business_lane(
    controller_url: str,
    secret: str,
    lane_group: str,
    lane_port: int,
    runtime_name: str, *,
    url: str | None = None,
    appid: int = DEFAULT_BUSINESS_APPID,
    timeout: float = DEFAULT_L2_TIMEOUT,
) -> BusinessResult:
    """经探测 lane 的业务变体：切**本 lane 组** → 经该组 listener 端口打生产请求。

    同一条 lane 内切组与请求必须顺序执行；不同 lane 互相独立。业务校验与
    GLOBAL 变体共用同一契约（`_validate_business`）。"""
    target = url or business_probe_url(appid)

    failure = await _select_selector(controller_url, secret, lane_group,
                                     runtime_name, timeout)
    if failure is not None:
        return BusinessResult(runtime_name, False, None, None, failure, None)

    status, text, latency_ms, exc = await _request_via_port(lane_port, target, timeout)
    if status is None:
        return BusinessResult(runtime_name, False, None, None,
                              f"{TARGET_SERVICE_FAILED}: {exc}", None)
    if status != 200:
        return BusinessResult(runtime_name, False, status, None,
                              f"{TARGET_SERVICE_FAILED}: HTTP {status}",
                              latency_ms)
    try:
        payload = json.loads(text)
    except ValueError:
        return BusinessResult(runtime_name, False, 200, None,
                              f"{INVALID_BUSINESS_RESPONSE}: 响应不是 JSON",
                              latency_ms)

    ok, reason, final = _validate_business(payload, appid)
    if not ok:
        return BusinessResult(runtime_name, False, 200, None,
                              f"{INVALID_BUSINESS_RESPONSE}: {reason}", latency_ms)
    return BusinessResult(
        runtime_name, True, 200, final,
        f"{BUSINESS_OK} appid={appid} final_price_in_cents={final}", latency_ms
    )


async def _commit_business_wave(
    session: AsyncSession,
    probed: list[tuple[ProxyNode, BusinessResult]], *,
    now: datetime,
    run_id: int | None,
    channel: str,
    target_url: str,
) -> None:
    """一波 L2 结论落账（含状态机推进）：短提交进闸，探测网络段绝不进闸。"""
    async with write_gate(WritePriority.BACKGROUND, label="health_observation"):
        # 防丢失更新：探测是分钟级网络段，载入时读到的 state /
        # consecutive_failures 早已陈旧——闸内按主键重读当前行（刷新本会话
        # 同一对象），把探测结论套到新鲜值上再 evaluate，窗口内手动体检
        # ingest 与订阅同步 settle 对同一节点的写入不因陈旧基线被覆盖。
        if probed:
            await session.execute(
                select(ProxyNode)
                .where(ProxyNode.node_id.in_([n.node_id for n, _ in probed]))
                .execution_options(populate_existing=True)
            )
        for node, result in probed:
            # 只有有效业务响应能晋升；收到 HTTP 响应说明请求已到达目标服务，
            # 包括 5xx 在内都不作为节点故障证据。
            if result.ok:
                probe_ok: bool | None = True
            elif result.http_status is not None:
                probe_ok = None
            elif result.detail.startswith(NODE_PROBE_FAILED):
                probe_ok = None
            else:  # 经节点发请求超时/连不上：节点失败证据
                probe_ok = False
            if probe_ok is not None:
                failures = 0 if probe_ok else (node.consecutive_failures or 0) + 1
                node.consecutive_failures = failures
                node.state = evaluate_node_state(
                    node.state,
                    source_seen=True,  # 池文件成员 = 来源在场
                    probe_ok=probe_ok,
                    consecutive_failures=failures,
                )
                # 业务结论时间随结论落章：体检账本对齐（bridge.ingest_ledger）
                # 靠它与账本检测时刻比新旧，缺了它「池判死 → 旧账本复活」
                # 的翻烧饼守卫永远不生效
                node.last_l2_at = now
            session.add(HealthObservation(
                node_id=node.node_id,
                level=L2_LEVEL,
                ok=result.ok,
                latency_ms=result.latency_ms,
                detail=result.detail or None,
                observed_at=now,
                run_id=run_id,
                channel=channel,
                target=target_url,
            ))
        await session.commit()


async def business_check_pool(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str | None = None,
    appid: int = DEFAULT_BUSINESS_APPID,
    timeout: float = DEFAULT_L2_TIMEOUT,
    run_id: int | None = None,
    channel: str = "scheduled",
) -> tuple[BusinessOutcome, ...]:
    """对池内节点做一次 L2 业务观测——**节点生死判据的唯一定时来源**。

    状态语义：「能不能访问 Steam」= 经该节点的请求是否拿到 Steam 的 HTTP 响应。
    拿到任何响应（200 / 429 / 503）都算传输成功——Steam 侧故障不罚节点（否则
    Steam 一抖整池陪葬）；只有超时/连不上归为节点失败证据，喂 `evaluate_node_state`
    （连续 3 次达退休线）。200 但契约不符是业务质量观测，记录不罚——契约变化
    不该杀节点。不擦 `exit_ip`、不碰 `last_l0_at` / `last_l1_at` / `capacity_score`。

    探测 lane 在位时分波并发：每条 lane 一个独立组选择，波内 (lane, 节点) 各自
    顺序执行（切组 → 业务 GET），波末一次短提交；无 lane 退回 GLOBAL 串行路径
    （测试环境与旧配置兜底）并按 `PROBE_COMMIT_EVERY` 分块。
    """
    target_url = url or business_probe_url(appid)
    names = _pool_names(data_dir)
    lanes = pool_probe_lanes(data_dir)
    if not lanes:
        try:
            mixed_port = mixed_port_of(data_dir)
        except Exception:  # noqa: BLE001 —— 运行配置缺失（单测环境）：端口缺省，探针自会失败
            mixed_port = 0
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[BusinessOutcome] = []
    wave_size = len(lanes) if lanes else PROBE_COMMIT_EVERY
    for start in range(0, len(names), wave_size):
        chunk = names[start : start + wave_size]
        if lanes:
            pairs = [(lanes[i], name) for i, name in enumerate(chunk) if name in by_name]
            results = await asyncio.gather(*(
                probe_business_lane(controller_url, secret, group, port, name,
                                    url=url, appid=appid, timeout=timeout)
                for (group, port), name in pairs
            ))
            probed = [(by_name[name], result)
                      for (_, name), result in zip(pairs, results)]
        else:
            probed: list[tuple[ProxyNode, BusinessResult]] = []
            for name in chunk:  # 严格串行（GLOBAL 兜底路径）
                node = by_name.get(name)
                if node is None:
                    continue
                result = await probe_business(controller_url, secret, mixed_port, name,
                                              url=url, appid=appid, timeout=timeout)
                probed.append((node, result))
        outcomes.extend(BusinessOutcome(
            node_id=node.node_id,
            runtime_name=result.runtime_name,
            ok=result.ok,
            http_status=result.http_status,
            final_price_in_cents=result.final_price_in_cents,
            detail=result.detail,
            latency_ms=result.latency_ms,
        ) for node, result in probed)
        await _commit_business_wave(
            session, probed, now=now, run_id=run_id, channel=channel,
            target_url=target_url,
        )
        _publish_probe_progress(L2_LEVEL, len(outcomes), total=len(names))
    return tuple(outcomes)
