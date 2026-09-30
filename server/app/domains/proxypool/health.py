"""传输层健康检测（L0）：`/proxies/{name}/delay` 最小探测。

三层职责必须分清，否则会变成「同一个业务接口测两遍」：

    L0 = 传输层     —— 这个节点能不能完成一次代理连接（本模块 `health_check_pool`）
    L1 = 出口身份   —— 它的出口 IP 是什么（`exit_ip_check_pool`，只记录）
    L2 = 业务层     —— 能不能完成 Holdexar 生产所需的 StoreBrowse 请求（只记录）

**L0 不用生产业务接口当目标。** 生产业务主机的响应延迟抖动大且偶发超时；而 L0 失败会推进
`state`，把生产业务接口当 L0 目标等于**把外部业务服务的抖动耦合进节点状态机**——
一次网络抖动就能成片杀掉节点。业务可用性归 L2，那里失败不改状态。

L0 目标选最轻的连通性探测（204、无响应体、跨地区可达性好）。

**探测响应形状（本版内核）：**

    HTTP 200  {"delay": 2}                                     → 成功
    HTTP 503  {"message":"An error occurred in the delay test"} → 节点在、探测失败
    HTTP 404  {"message":"Resource not found"}                  → 名字没定位到

后两者的区分很重要：404 说明**路径编码或名字有问题**（例如 `#` 没编码会被当成
fragment 截断），503 才是节点本身不可用。名字进 path 一律 `quote(name, safe="")`。

状态推进**只经由** `evaluate_node_state()`——本模块不自己写状态规则，也不硬改
`state`。连续失败计数在本模块维护（成功归零、失败累加）并作为状态机输入：它既是
退休线（`RETIRED`）的判据，也是台账事实——`DEAD` 节点带着"连续失败过几次"的记录。
`RETIRED` 节点不在池里、根本不会被探测，因此**不会被一次成功自动复活**——复活
规则还没定义，不能提前设计。
"""
from __future__ import annotations

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
from app.crawler.browse_store import StoreBrowseAPI, _to_int
from app.domains.proxypool.models import HealthObservation, ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import pool_file_names
from app.domains.proxypool.runtime import mixed_port_of
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
        async with httpx.AsyncClient(trust_env=False, timeout=client_timeout, headers=headers) as client:
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
    运行配置里就是它），DEAD 节点不在池文件里、池内核不认识，改用来源订阅名
    （`ProxyNodeSource.original_name`）打到持有全量订阅节点的旧链路内核上。
    """
    result = await probe_node(controller_url, secret, probe_name,
                              url=url, timeout_ms=timeout_ms)
    source_count = await session.scalar(
        select(func.count())
        .select_from(ProxyNodeSource)
        .where(ProxyNodeSource.node_id == node.node_id)
    )
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
            source_seen=bool(source_count),
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

    探测（网络）与落库分离：全部探完后在写调度器内一次性落库提交——写事务
    不跨网络等待，节点间的源计数读也不会 autoflush 出闸外写锁。
    """
    targets = tuple(names) if names is not None else _pool_names(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(targets))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[HealthOutcome] = []
    observations: list[dict] = []
    for name in targets:
        node = by_name.get(name)
        if node is None:
            # 池里有、Registry 没有：这不该发生（reconcile 会先报差额）。
            # 不静默造行，留给上面的对账去暴露。
            continue

        outcome, observation, _mutations = await _probe_and_decide(
            session, node, controller_url=controller_url, secret=secret,
            probe_name=name, now=now, url=url, timeout_ms=timeout_ms,
            state_push=False,
        )
        outcomes.append(outcome)
        observations.append(observation)

    async with write_gate(WritePriority.BACKGROUND):
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
    source = (
        select(
            ProxyNodeSource.node_id.label("node_id"),
            func.min(ProxyNodeSource.original_name).label("original_name"),
        )
        .group_by(ProxyNodeSource.node_id)
        .subquery()
    )
    rows = (await session.execute(
        select(ProxyNode, source.c.original_name)
        .join(source, source.c.node_id == ProxyNode.node_id)
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
    async with write_gate(WritePriority.BACKGROUND):
        for node, observation, mutations in pending:
            if mutations is not None:
                failures, target = mutations
                node.consecutive_failures = failures
                node.state = target
            session.add(HealthObservation(**observation))
        await session.commit()
    if outcomes:
        still_out = sum(1 for o in outcomes if o.state in (NODE_DEAD, NODE_RETIRED))
        logger.info(
            "[L0恢复] 出池节点补探 %d 个：成功 %d / 仍未进池 %d",
            len(outcomes), len(outcomes) - still_out, still_out,
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


async def _select_global(controller_url: str, secret: str, runtime_name: str,
                         timeout: float) -> str | None:
    """把 `GLOBAL` 切到指定节点。成功返回 None，失败返回 detail。

    回读 `now` 是**归因正确性**的保证：若此刻 `GLOBAL` 不是目标节点，接下来测到的
    任何东西都不属于它——宁可记失败，也不能把它人的结果记到这个节点名下。
    L1 与 L2 共用这一步：切换是唯一的全局状态，两处逻辑必须一致。
    """
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    async with httpx.AsyncClient(trust_env=False, timeout=timeout, headers=headers) as control:
        try:
            put = await control.put(
                f"{controller_url}/proxies/GLOBAL", json={"name": runtime_name}
            )
            if put.status_code >= 400:
                return (f"{NODE_PROBE_FAILED}: 选择节点失败"
                        f"（HTTP {put.status_code}）")
            now = (await control.get(
                f"{controller_url}/proxies/GLOBAL")).json().get("now")
        except Exception as exc:  # noqa: BLE001
            return f"{NODE_PROBE_FAILED}: 控制器不可达（{type(exc).__name__}）"

    if now != runtime_name:
        return (f"{NODE_PROBE_FAILED}: GLOBAL 的 now={now!r} "
                f"不是 {runtime_name!r}")
    return None


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
    """
    failure = await _select_global(controller_url, secret, runtime_name, timeout)
    if failure is not None:
        return ExitIpResult(runtime_name, False, None, failure, None)

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False, 
            proxy=f"http://127.0.0.1:{mixed_port}", timeout=timeout
        ) as proxied:
            resp = await proxied.get(url)
    except Exception as exc:  # noqa: BLE001
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: {type(exc).__name__}", None)
    latency_ms = int((time.monotonic() - started) * 1000)

    if resp.status_code != 200:
        return ExitIpResult(runtime_name, False, None,
                            f"{TARGET_SERVICE_FAILED}: HTTP {resp.status_code}",
                            latency_ms)
    exit_ip = _extract_ip(resp.text)
    if exit_ip is None:
        return ExitIpResult(runtime_name, False, None,
                            f"{INVALID_IP_RESPONSE}: {resp.text.strip()[:60]!r}",
                            latency_ms)
    return ExitIpResult(runtime_name, True, exit_ip, "", latency_ms)


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
    """对池内节点**串行**做一次 L1 出口 IP 观测。

    成功 → `ProxyNode.exit_ip` + `HealthObservation(level="L1")`；
    失败 → 只落观测与 `detail`，**不动 `exit_ip`、不动 `state`**（一次目标服务
    故障不该擦掉已经观测到的出口事实）。

    探测（网络）与落库分离：按 `PROBE_COMMIT_EVERY` 分块，块内先探完，再在
    写调度器内一次性落库提交——写事务不跨网络等待，写者位在块间空出。

    `names` 不给就取池文件当前节点；给了就只探这些——供调用方做**有界**首轮探测
    （启动链不能为了出口身份把整池串行探完才开门）。
    """
    if names is None:
        names = _pool_names(data_dir)
    mixed_port = mixed_port_of(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[ExitIpOutcome] = []
    for start in range(0, len(names), PROBE_COMMIT_EVERY):
        chunk = names[start : start + PROBE_COMMIT_EVERY]
        probed: list[tuple[ProxyNode, ExitIpResult]] = []
        for name in chunk:  # 严格串行（见 probe_exit_ip）
            node = by_name.get(name)
            if node is None:
                continue
            result = await probe_exit_ip(controller_url, secret, mixed_port, name,
                                         url=url, timeout=timeout)
            probed.append((node, result))
            outcomes.append(ExitIpOutcome(
                node_id=node.node_id,
                runtime_name=name,
                ok=result.ok,
                exit_ip=result.exit_ip,
                detail=result.detail,
                latency_ms=result.latency_ms,
            ))
        async with write_gate(WritePriority.BACKGROUND):
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
    **必须串行调用**（同 L1：`GLOBAL` 是全局状态）。
    """
    target = url or business_probe_url(appid)

    failure = await _select_global(controller_url, secret, runtime_name, timeout)
    if failure is not None:
        return BusinessResult(runtime_name, False, None, None, failure, None)

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False, 
            proxy=f"http://127.0.0.1:{mixed_port}", timeout=timeout
        ) as proxied:
            resp = await proxied.get(target)
    except Exception as exc:  # noqa: BLE001 —— 传输失败不归因给节点
        return BusinessResult(runtime_name, False, None, None,
                              f"{TARGET_SERVICE_FAILED}: {type(exc).__name__}", None)
    latency_ms = int((time.monotonic() - started) * 1000)

    if resp.status_code != 200:
        return BusinessResult(runtime_name, False, resp.status_code, None,
                              f"{TARGET_SERVICE_FAILED}: HTTP {resp.status_code}",
                              latency_ms)
    try:
        payload = resp.json()
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
    """对池内节点**串行**做一次 L2 业务观测——**节点生死判据的唯一定时来源**。

    状态语义：「能不能访问 Steam」= 经该节点的请求是否拿到 Steam 的 HTTP 响应。
    拿到任何响应（200 / 429 / 503）都算传输成功——Steam 侧故障不罚节点（否则
    Steam 一抖整池陪葬）；只有超时/连不上归为节点失败证据，喂 `evaluate_node_state`
    （连续 3 次达退休线）。200 但契约不符是业务质量观测，记录不罚——契约变化
    不该杀节点。不擦 `exit_ip`、不碰 `last_l0_at` / `last_l1_at` / `capacity_score`。
    """
    target_url = url or business_probe_url(appid)
    names = _pool_names(data_dir)
    try:
        mixed_port = mixed_port_of(data_dir)
    except Exception:  # noqa: BLE001 —— 运行配置缺失（单测环境）：端口缺省，探针自会失败
        mixed_port = 0
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[BusinessOutcome] = []
    for start in range(0, len(names), PROBE_COMMIT_EVERY):
        chunk = names[start : start + PROBE_COMMIT_EVERY]
        probed: list[tuple[ProxyNode, BusinessResult]] = []
        for name in chunk:  # 严格串行（见 probe_business）
            node = by_name.get(name)
            if node is None:
                continue
            result = await probe_business(controller_url, secret, mixed_port, name,
                                          url=url, appid=appid, timeout=timeout)
            probed.append((node, result))
            outcomes.append(BusinessOutcome(
                node_id=node.node_id,
                runtime_name=name,
                ok=result.ok,
                http_status=result.http_status,
                final_price_in_cents=result.final_price_in_cents,
                detail=result.detail,
                latency_ms=result.latency_ms,
            ))
        async with write_gate(WritePriority.BACKGROUND):
            for node, result in probed:
                # 生死证据判定：None = 不构成节点证据（选不中节点是探针基础设施问题）。
                # 拿到 Steam 侧响应（200 / 429 / 503 等）= 传输成功，Steam 故障不罚节点；
                # 502/504 是**代理链网关错误**（mihomo 上游链失败时本地生成，Steam 正常
                # 不回裸 502/504）——与超时/连不上同归节点失败证据。
                if result.ok:
                    probe_ok: bool | None = True
                elif result.http_status in (502, 504):
                    probe_ok = False
                elif result.http_status is not None:
                    probe_ok = True
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
    return tuple(outcomes)