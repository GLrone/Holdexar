"""P1.4-A：`/proxies/{name}/delay` 最小健康检测（L0）。

本阶段只回答一个问题：

> **这个节点经 Mihomo 发起一次真实 HTTP(S) 探测，能否在规定时间内拿到预期状态。**

不回答出口 IP（L1），也不回答能不能扛住爬虫业务（L2）——那两层各有自己的证据。

**探测目标不用公共 `generate_204`**：Holdexar 要的是「这个节点能不能访问我们真正
依赖的 Steam 目标」，所以默认目标固定成同域的 Steam API（见 `PROBE_TARGET_URL`）。
即便这一层成功，也**不等于**「爬虫一定可用」——真实业务请求是 L2 的事。

**观测形状取自真实内核实测，不是文档推演**（本机 mihomo 实测）：

    HTTP 200  {"delay": 2}                                     → 成功
    HTTP 503  {"message":"An error occurred in the delay test"} → 节点在、探测失败
    HTTP 404  {"message":"Resource not found"}                  → 名字没定位到

后两者的区分很重要：404 说明**路径编码或名字有问题**（例如 `#` 没编码会被当成
fragment 截断），503 才是节点本身不可用。名字进 path 一律 `quote(name, safe="")`。

状态推进**只经由** `evaluate_node_state()`——本模块不自己写状态规则，也不硬改
`state`。R1 版策略：`consecutive_failures=0`（失败计数与退休阈值是运营规则，归后面
的阶段），所以探测失败一律 `DEAD`，不会因阈值 `RETIRED`；而 `RETIRED` 节点不在池里、
根本不会被探测，因此**不会被一次成功自动复活**——复活规则还没定义，不能提前设计。
"""
from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import HealthObservation, ProxyNode, ProxyNodeSource
from app.domains.proxypool.pool import pool_path
from app.domains.proxypool.runtime import mixed_port_of
from app.domains.proxypool.state import evaluate_node_state

# 与业务同域的稳定探测目标（可覆盖；测试用本地目标，生产用这个）
PROBE_TARGET_URL = (
    "https://store.steampowered.com/api/appdetails"
    "?appids=220&cc=us&l=english&filters=price_overview"
)

PROBE_LEVEL = "L0"
DEFAULT_TIMEOUT_MS = 5000

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

        result = await probe_node(controller_url, secret, name,
                                  url=url, timeout_ms=timeout_ms)
        source_count = await session.scalar(
            select(func.count())
            .select_from(ProxyNodeSource)
            .where(ProxyNodeSource.node_id == node.node_id)
        )
        previous = node.state
        # 唯一的状态决策入口：健康只提供证据，规则仍归状态机
        target = evaluate_node_state(
            previous,
            source_seen=bool(source_count),
            probe_ok=result.ok,
            consecutive_failures=0,  # 失败计数/退休阈值 v1 不推进（运营规则，后续阶段）
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
        outcomes.append(HealthOutcome(
            node_id=node.node_id,
            runtime_name=name,
            ok=result.ok,
            delay_ms=result.delay_ms,
            detail=result.detail,
            previous_state=previous,
            state=target,
        ))

    await session.flush()
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
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as control:
        try:
            put = await control.put(
                f"{controller_url}/proxies/GLOBAL", json={"name": runtime_name}
            )
            if put.status_code >= 400:
                return ExitIpResult(runtime_name, False, None,
                                    f"{NODE_PROBE_FAILED}: 选择节点失败"
                                    f"（HTTP {put.status_code}）", None)
            now = (await control.get(
                f"{controller_url}/proxies/GLOBAL")).json().get("now")
        except Exception as exc:  # noqa: BLE001
            return ExitIpResult(runtime_name, False, None,
                                f"{NODE_PROBE_FAILED}: 控制器不可达"
                                f"（{type(exc).__name__}）", None)

    if now != runtime_name:
        # 归因不成立：此刻测到的 IP 不属于这个节点。宁可记失败，也不能记错归属。
        return ExitIpResult(runtime_name, False, None,
                            f"{NODE_PROBE_FAILED}: GLOBAL 的 now={now!r} "
                            f"不是 {runtime_name!r}", None)

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