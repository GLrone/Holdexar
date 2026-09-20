"""P1.4-A：`/proxies/{name}/delay` 最小健康检测（L0 = 传输层）。

三层职责必须分清，否则会变成「同一个业务接口测两遍」：

    L0 = 传输层     —— 这个节点能不能完成一次代理连接（本模块 `health_check_pool`）
    L1 = 出口身份   —— 它的出口 IP 是什么（`exit_ip_check_pool`，只记录）
    L2 = 业务层     —— 能不能完成 Holdexar 生产所需的 StoreBrowse 请求（只记录）

**L0 不用生产业务接口当目标。** 实测生产主机 `api.steampowered.com` 的响应在
1.2s~22s 之间抖动、且偶发超时（连它上面最轻的接口也 4/6 成功）；而 L0 失败会推进
`state`，把生产业务接口当 L0 目标等于**把外部业务服务的抖动耦合进节点状态机**——
一次网络抖动就能成片杀掉节点。业务可用性归 L2，那里失败不改状态。

L0 目标选的是最轻的连通性探测（204、无响应体、跨地区可达性好）。选型来自真内核
`/delay` 的实测对比，不是凭经验拍一个公网地址。

**探测形状取自真实内核实测**（本机 mihomo）：

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
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.browse_store import StoreBrowseAPI, _to_int
from app.domains.proxypool.models import HealthObservation, ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import pool_path
from app.domains.proxypool.runtime import mixed_port_of
from app.domains.proxypool.state import NODE_DEAD, evaluate_node_state

logger = logging.getLogger(__name__)

# L0 传输层目标：204、无响应体、请求最轻（选型见模块文档）
PROBE_TARGET_URL = "http://www.gstatic.com/generate_204"

PROBE_LEVEL = "L0"
DEFAULT_TIMEOUT_MS = 5000
# DEAD 恢复探测每轮上限：DEAD 不在池文件里，池内 L0 探不到它，没有这条路节点一旦
# DEAD 就永久出局。分批轮转补探，且批小到不会把 L0 的写事务窗口拉长（写锁问题）。
RECOVERY_BATCH_SIZE = 4

# ── L1：出口 IP ──────────────────────────────────────────────────
L1_LEVEL = "L1"
# 生产默认用公网 IP 回显服务（可覆盖）；测试换成本地目标
EXIT_IP_TARGET_URL = "https://api.ipify.org?format=json"
DEFAULT_L1_TIMEOUT = 15.0

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
    """一次探测的原始结论。"""

    runtime_name: str
    ok: bool
    delay_ms: int | None
    detail: str


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
        async with httpx.AsyncClient(timeout=client_timeout, headers=headers) as client:
            resp = await client.get(
                f"{controller_url}/proxies/{path}/delay",
                params={"url": url, "timeout": timeout_ms},
            )
    except Exception as exc:  # noqa: BLE001 —— 控制器不可达是「内核没起来」的信号
        return ProbeResult(runtime_name, False, None,
                           f"控制器请求异常：{type(exc).__name__}")

    if resp.status_code == 404:
        return ProbeResult(runtime_name, False, None, NOT_FOUND_DETAIL)
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
    doc = yaml.safe_load(pool_path(data_dir).read_text(encoding="utf-8"))
    return tuple(str(proxy["name"]) for proxy in doc.get("proxies", []))


async def _probe_and_record(
    session: AsyncSession, node: ProxyNode, *,
    controller_url: str,
    secret: str,
    probe_name: str,
    now: datetime,
    url: str,
    timeout_ms: int,
) -> HealthOutcome:
    """一次 L0 探测 + 落观测 + 推状态；池内探测与 DEAD 恢复探测共用同一条语义。

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
    # 连续失败计数：成功归零、失败累加。它既是状态机的输入（退休线判据），
    # 也是台账事实——判 DEAD 的节点必须带着失败次数，不能留下"DEAD 且计数 0"。
    failures = 0 if result.ok else (node.consecutive_failures or 0) + 1
    node.consecutive_failures = failures
    # 唯一的状态决策入口：健康只提供证据，规则仍归状态机
    target = evaluate_node_state(
        previous,
        source_seen=bool(source_count),
        probe_ok=result.ok,
        consecutive_failures=failures,
    )
    node.state = target
    session.add(HealthObservation(
        node_id=node.node_id,
        level=PROBE_LEVEL,
        ok=result.ok,
        latency_ms=result.delay_ms,
        detail=result.detail or None,
        observed_at=now,
    ))
    return HealthOutcome(
        node_id=node.node_id,
        runtime_name=node.runtime_name,
        ok=result.ok,
        delay_ms=result.delay_ms,
        detail=result.detail,
        previous_state=previous,
        state=target,
    )


async def health_check_pool(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str = PROBE_TARGET_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> tuple[HealthOutcome, ...]:
    """对池内每个节点做一次 L0 探测：落一条观测，并按状态机推进 `state`。"""
    names = _pool_names(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[HealthOutcome] = []
    for name in names:
        node = by_name.get(name)
        if node is None:
            # 池里有、Registry 没有：这不该发生（reconcile 会先报差额）。
            # 不静默造行，留给上面的对账去暴露。
            continue

        outcomes.append(await _probe_and_record(
            session, node, controller_url=controller_url, secret=secret,
            probe_name=name, now=now, url=url, timeout_ms=timeout_ms,
        ))

    await session.flush()
    return tuple(outcomes)


async def recover_dead_nodes(
    session: AsyncSession, *,
    controller_url: str,
    secret: str,
    now: datetime,
    url: str = PROBE_TARGET_URL,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    batch: int = RECOVERY_BATCH_SIZE,
) -> tuple[HealthOutcome, ...]:
    """对 DEAD 节点分批做 L0 恢复探测：成功回 `ACTIVE` 并清零计数，失败保持 `DEAD` 并累加。

    分批轮转：**已在累计失败的最先收口**（否则要等轮转一整圈才回到它，退休线在很长时间里
    不可达），其余按「最久没有被探过」排序（`health_observations` 的最后一条），一次只取
    `batch` 个——DEAD 不一次性全打出去，也不新建调度系统。成功使节点重新合格，
    合格集变化由 `run_l0_cycle` 既有的 before/after 比较去请求重建。

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
        .where(ProxyNode.state == NODE_DEAD)
        .order_by(
            ProxyNode.consecutive_failures.desc(),
            last_probe.c.last_at.asc().nullsfirst(),
            ProxyNode.node_id,
        )
        .limit(batch)
    )).all()

    outcomes: list[HealthOutcome] = []
    for node, original_name in rows:
        outcomes.append(await _probe_and_record(
            session, node, controller_url=controller_url, secret=secret,
            probe_name=str(original_name), now=now, url=url, timeout_ms=timeout_ms,
        ))

    await session.flush()
    if outcomes:
        recovered = sum(1 for o in outcomes if o.state != NODE_DEAD)
        logger.info(
            "[L0恢复] DEAD 补探 %d 个：成功 %d / 仍 DEAD %d",
            len(outcomes), recovered, len(outcomes) - recovered,
        )
    return tuple(outcomes)


# ══ L1：出口 IP ══════════════════════════════════════════════════
# 路径（已实测）：PUT /proxies/GLOBAL 选中节点 → 经 mixed-port 发真实请求 →
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
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as control:
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
        async with httpx.AsyncClient(
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
) -> tuple[ExitIpOutcome, ...]:
    """对池内节点**串行**做一次 L1 出口 IP 观测。

    成功 → `ProxyNode.exit_ip` + `HealthObservation(level="L1")`；
    失败 → 只落观测与 `detail`，**不动 `exit_ip`、不动 `state`**（一次目标服务
    故障不该擦掉已经观测到的出口事实）。
    """
    names = _pool_names(data_dir)
    mixed_port = mixed_port_of(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[ExitIpOutcome] = []
    for name in names:  # 严格串行（见 probe_exit_ip）
        node = by_name.get(name)
        if node is None:
            continue
        result = await probe_exit_ip(controller_url, secret, mixed_port, name,
                                     url=url, timeout=timeout)
        if result.ok and result.exit_ip:
            node.exit_ip = result.exit_ip
        session.add(HealthObservation(
            node_id=node.node_id,
            level=L1_LEVEL,
            ok=result.ok,
            latency_ms=result.latency_ms,
            detail=result.detail or None,
            observed_at=now,
        ))
        outcomes.append(ExitIpOutcome(
            node_id=node.node_id,
            runtime_name=name,
            ok=result.ok,
            exit_ip=result.exit_ip,
            detail=result.detail,
            latency_ms=result.latency_ms,
        ))

    await session.flush()
    return tuple(outcomes)


# ══ L2：生产业务可用性 ═══════════════════════════════════════════
# 回答的是「这个节点能不能真正完成 Holdexar 生产所需的 StoreBrowse 请求」：
# HTTP 200 只是及格线，还要业务语义成立（信封 / AppID / success / 价格字段）。
#
# 判据全部取自**生产实测**，不是猜的：
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
    # 生产实测是整数 1；布尔 true 属于契约不符（True == 1 会蒙混过关，故显式拒绝）
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
        async with httpx.AsyncClient(
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
) -> tuple[BusinessOutcome, ...]:
    """对池内节点**串行**做一次 L2 业务观测。

    只写 `HealthObservation(level="L2")`：不改 `state`、不擦 `exit_ip`、
    不碰 `last_l0_at` / `last_l1_at` / `capacity_score`。
    """
    names = _pool_names(data_dir)
    mixed_port = mixed_port_of(data_dir)
    rows = await session.execute(
        select(ProxyNode).where(ProxyNode.runtime_name.in_(names))
    )
    by_name = {row.runtime_name: row for row in rows.scalars()}

    outcomes: list[BusinessOutcome] = []
    for name in names:  # 严格串行（见 probe_business）
        node = by_name.get(name)
        if node is None:
            continue
        result = await probe_business(controller_url, secret, mixed_port, name,
                                      url=url, appid=appid, timeout=timeout)
        session.add(HealthObservation(
            node_id=node.node_id,
            level=L2_LEVEL,
            ok=result.ok,
            latency_ms=result.latency_ms,
            detail=result.detail or None,
            observed_at=now,
        ))
        outcomes.append(BusinessOutcome(
            node_id=node.node_id,
            runtime_name=name,
            ok=result.ok,
            http_status=result.http_status,
            final_price_in_cents=result.final_price_in_cents,
            detail=result.detail,
            latency_ms=result.latency_ms,
        ))

    await session.flush()
    return tuple(outcomes)