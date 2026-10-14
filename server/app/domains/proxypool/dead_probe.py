"""出池节点（DEAD/RETIRED）的恢复探测：自建临时内核，不依赖任何常驻内核。

── 为什么不能借现有内核 ──────────────────────────────────────
池内核只渲染**合格**节点（`build_pool` 的输入是 `eligible_nodes`），DEAD 不在
其中 —— 拿池内 L0 去探必然得到「名字没定位到（HTTP 404）」，而 404 又会坐实
DEAD，形成「判死 → 出池 → 探不到 → 继续判死」的闭环。旧链路内核（订阅整份
yaml）虽然认识全部节点，但它是可选的常驻进程，用户没跑它就整条恢复通道失效。

── 本模块的做法 ──────────────────────────────────────────────
从数据库里现取待恢复节点的 `normalized_config`，渲染一份**只含这些节点**的最小
内核配置（每节点一个 listener 端口 + 一条 MATCH 兜底），起一个短命内核，逐节点
用控制器 `/proxies/{name}/delay` 探测，探完立即停进程。整个过程不写池文件、
不动运行配置、不占用池内端口段。

配置里刻意**不写任何 GEOIP/GEOSITE 规则**（只有 `MATCH`），因此不需要随包
GeoIP/GeoSite 数据，启动是毫秒级；带规则会在缺数据时现场联网下载并长时间阻塞。
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, write_gate
from app.core.logging import log_event
from app.domains.proxypool.models import ProxyNode
from app.domains.proxypool.state import NODE_DEAD, NODE_RETIRED

logger = logging.getLogger(__name__)

# 短命内核的端口段：与池内核（21100 探测段 + 动态段）、旧链路内核（7890 段）
# 都不重叠，避免三方抢口。
PROBE_PORT_BASE = 23200
PROBE_PORT_SPAN = 200
# 每批探测的节点数上限：一个进程内每个节点占一个 listener，批次越大启动越慢
BATCH_SIZE = 24
# 启动后等控制器就绪的上限
STARTUP_WAIT = 12.0
# 单节点探测超时（毫秒，传给内核的 /delay 接口）
NODE_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class RecoveredNode:
    """一个待恢复节点的探测输入（只带渲染配置需要的字段）。"""

    node_id: str
    runtime_name: str
    original_name: str
    config: dict


@dataclass(frozen=True)
class ProbeOutcome:
    node_id: str
    runtime_name: str
    ok: bool
    delay_ms: int | None
    detail: str


def _free_port(preferred: int) -> int | None:
    """优先用 preferred，被占则向上顺延；整段占满返回 None。"""
    from app.domains.proxies.clash_manager import _port_listening

    for port in range(preferred, preferred + 8):
        if not _port_listening(port):
            return port
    return None


def _render_config(nodes: list[RecoveredNode], *, controller_port: int, secret: str,
                   listener_base: int) -> tuple[str, dict[str, int]]:
    """渲染最小内核配置，返回 (yaml 文本, {runtime_name: 端口})。

    每个节点一个 `mixed` listener，`proxy` 直指该节点 —— 不经过任何规则引擎，
    所以不需要 GeoIP/GeoSite 数据。

    `dialer-proxy` 按本批节点解析成运行名（与池渲染同一份实现）：目标不在本批
    且非内置逻辑节点即抛 `ValueError`——绝不把依赖悄悄换成直连。调用方负责
    先把不可渲染的节点筛掉（见 `probe_out_of_pool_nodes`），本函数只做渲染。
    """
    from app.domains.proxies.runtime_config import resolve_node_dependencies

    by_original = {n.original_name: n.runtime_name for n in nodes}
    payloads = [{**dict(n.config), "name": n.runtime_name} for n in nodes]
    proxies = resolve_node_dependencies(payloads, by_original)

    ports: dict[str, int] = {}
    listeners: list[dict] = []
    for index, node in enumerate(nodes):
        port = listener_base + index
        ports[node.runtime_name] = port
        listeners.append({
            "name": f"probe-{index}",
            "type": "mixed",
            "port": port,
            "listen": "127.0.0.1",
            "proxy": node.runtime_name,
        })
    doc = {
        "mode": "global",
        "log-level": "warning",
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "external-controller": f"127.0.0.1:{controller_port}",
        "secret": secret,
        "proxies": proxies,
        "listeners": listeners,
        # 一条兜底规则即可：本内核只做「每节点一个本地监听器」，不做分流。
        # 规则表里没有 GEOIP/GEOSITE，因此不触发地理数据加载。
        "rules": ["MATCH,DIRECT"],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), ports


def _renderable_nodes(nodes: list[RecoveredNode]) -> list[RecoveredNode]:
    """筛出本批可渲染的节点：中转依赖在本批内或为内置逻辑节点。

    本批只装 DEAD/RETIRED，健康的中转节点不在其中——依赖它恢复的节点这轮渲染
    不出来。跳过（本轮不探、状态不动）好过让整批渲染失败，也好过把依赖换成
    直连：前者把整批恢复拖死，后者换了用户没要求的连接路径。
    """
    from app.domains.proxies.runtime_config import resolve_node_dependencies

    by_original = {n.original_name: n.runtime_name for n in nodes}
    renderable: list[RecoveredNode] = []
    for node in nodes:
        payload = {**dict(node.config), "name": node.runtime_name}
        try:
            resolve_node_dependencies([payload], by_original)
        except ValueError as exc:
            log_event(
                logger,
                "出池节点依赖的中转本轮不在批次，跳过该节点",
                detail={"节点": node.runtime_name, "原因": str(exc)},
            )
            continue
        renderable.append(node)
    return renderable


async def _wait_controller(base_url: str, secret: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                resp = await client.get(f"{base_url}/version", headers=headers)
                if resp.status_code == 200:
                    return True
        except Exception:  # noqa: BLE001 —— 还没起来，继续等
            pass
        await asyncio.sleep(0.2)
    return False


async def _probe_one(base_url: str, secret: str, name: str, url: str) -> tuple[bool, int | None, str]:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    from urllib.parse import quote

    path = quote(name, safe="")
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, headers=headers) as client:
            resp = await client.get(
                f"{base_url}/proxies/{path}/delay",
                params={"url": url, "timeout": NODE_TIMEOUT_MS},
            )
    except Exception as exc:  # noqa: BLE001
        return False, None, f"控制器请求异常：{type(exc).__name__}"
    if resp.status_code != 200:
        return False, None, f"探测未通过（HTTP {resp.status_code}）"
    try:
        delay = resp.json().get("delay")
    except ValueError:
        return False, None, "探测未通过（响应不是 JSON）"
    if not isinstance(delay, int) or delay <= 0:
        return False, None, f"探测未通过（delay={delay!r}）"
    return True, delay, ""


async def probe_out_of_pool_nodes(
    nodes: list[RecoveredNode],
    *,
    exe_path: str,
    work_dir: Path,
    url: str,
) -> list[ProbeOutcome]:
    """起一个短命内核，探测这批节点，返回逐节点结论。任何失败都返回 ok=False。

    `work_dir` 是一个全新的临时目录（调用方负责清理）：内核的 `-d` 指向它，
    缓存与日志都落在那里，不污染池目录。
    """
    if not nodes:
        return []
    controller_port = _free_port(PROBE_PORT_BASE)
    if controller_port is None:
        return [
            ProbeOutcome(n.node_id, n.runtime_name, False, None, "没有可用的探测端口")
            for n in nodes
        ]
    listener_base = controller_port + 20
    secret = f"dead-probe-{controller_port}"
    # 依赖的中转不在本批的节点先筛掉：探测它们只会拿到「名字没定位到」的 404，
    # 进而被记成一次失败、把它往退休线上推——这正是判死闭环的成因。
    probe_nodes = _renderable_nodes(nodes)
    if not probe_nodes:
        return []
    text, _ports = _render_config(
        probe_nodes, controller_port=controller_port, secret=secret,
        listener_base=listener_base,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    config_path = work_dir / "config.yaml"
    config_path.write_text(text, encoding="utf-8")

    log_file = open(work_dir / "probe.log", "ab")
    proc: subprocess.Popen | None = None
    base_url = f"http://127.0.0.1:{controller_port}"
    try:
        proc = subprocess.Popen(
            [exe_path, "-d", str(work_dir), "-f", str(config_path)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        if not await _wait_controller(base_url, secret, STARTUP_WAIT):
            return [
                ProbeOutcome(n.node_id, n.runtime_name, False, None, "临时内核控制器未就绪")
                for n in probe_nodes
            ]
        outcomes = []
        for node in probe_nodes:
            ok, delay, detail = await _probe_one(base_url, secret, node.runtime_name, url)
            outcomes.append(
                ProbeOutcome(node.node_id, node.runtime_name, ok, delay, detail)
            )
        return outcomes
    except Exception as exc:  # noqa: BLE001 —— 恢复探测是尽力而为，失败不阻断周期
        log_event(
            logger,
            "出池节点恢复探测失败，本轮跳过",
            tag="跳过",
            level=logging.WARNING,
            detail={"原因": f"{type(exc).__name__}: {exc}"},
        )
        return [
            ProbeOutcome(n.node_id, n.runtime_name, False, None, f"探测异常：{type(exc).__name__}")
            for n in probe_nodes
        ]
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()


async def load_recoverable_nodes(
    session: AsyncSession, *, batch: int = BATCH_SIZE
) -> list[RecoveredNode]:
    """取一批待恢复节点（DEAD/RETIRED），按「最久没被探过」轮转。"""
    from app.domains.proxypool.models import HealthObservation

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
        .order_by(last_probe.c.last_at.asc().nullsfirst(), ProxyNode.node_id)
        .limit(batch)
    )).all()
    nodes: list[RecoveredNode] = []
    for node, original_name in rows:
        config = node.normalized_config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        if not isinstance(config, dict) or not config.get("server") or not config.get("type"):
            continue
        nodes.append(
            RecoveredNode(
                node_id=str(node.node_id),
                runtime_name=str(node.runtime_name),
                original_name=str(original_name or node.runtime_name),
                config=dict(config),
            )
        )
    return nodes


async def recover_dead_nodes_self_hosted(
    session: AsyncSession, *,
    data_dir: Path,
    now: datetime,
    url: str | None = None,
    batch: int = BATCH_SIZE,
) -> tuple[ProbeOutcome, ...]:
    """自建临时内核恢复一批出池节点，并把成功者写回 ACTIVE。

    与 `recover_dead_nodes`（借常驻旧链路内核）取同样的结论口径，区别是本函数
    自己起内核，因此**不依赖任何常驻进程**——旧链路内核没跑时这才是唯一出路。
    状态推进沿用同一条规则：成功 → `evaluate_node_state(probe_ok=True)`；
    失败 → 累加计数（达到退休线由状态机判 RETIRED）。

    停内核、删临时目录都在本函数内完成；写库过写调度器短临界区提交。
    """
    from app.domains.proxypool.health import business_probe_url
    from app.domains.proxies.clash_manager import kernel_exe

    nodes = await load_recoverable_nodes(session, batch=batch)
    if not nodes:
        return ()
    exe = kernel_exe(data_dir)
    if not exe.is_file():
        return ()
    probe_url = url or business_probe_url()

    tmp_root = data_dir / "proxypool" / "_dead_probe"
    work_dir = tmp_root / f"run-{int(time.time() * 1000)}"
    try:
        outcomes = await probe_out_of_pool_nodes(
            nodes, exe_path=str(exe), work_dir=work_dir, url=probe_url,
        )
    finally:
        # 临时目录用完即删：它是本轮探测的一次性工作区，不留任何可被误解为
        # 运行配置的残留（保留失败现场的价值低于目录整洁与磁盘占用）。
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    if not outcomes:
        return ()

    by_id = {n.node_id: n for n in nodes}
    from app.domains.proxypool.models import HealthObservation
    from app.domains.proxypool.state import evaluate_node_state as _evaluate

    async with write_gate(WritePriority.BACKGROUND, label="dead_probe_recover"):
        await session.execute(
            select(ProxyNode)
            .where(ProxyNode.node_id.in_(list(by_id)))
            .execution_options(populate_existing=True)
        )
        recovered = 0
        for outcome in outcomes:
            node = await session.scalar(
                select(ProxyNode).where(ProxyNode.node_id == outcome.node_id)
            )
            if node is None:
                continue
            failures = 0 if outcome.ok else (node.consecutive_failures or 0) + 1
            previous = node.state
            node.consecutive_failures = failures
            node.state = _evaluate(
                node.state, source_seen=True, probe_ok=outcome.ok,
                consecutive_failures=failures,
            )
            if outcome.ok:
                recovered += 1
                node.last_l2_at = now
            session.add(
                HealthObservation(
                    node_id=node.node_id,
                    level="L2",
                    ok=outcome.ok,
                    latency_ms=outcome.delay_ms,
                    detail=outcome.detail or "SELF_HOSTED_RECOVERY_OK",
                    observed_at=now,
                    run_id=None,
                    channel="scheduled",
                    target=probe_url,
                )
            )
            if node.state != previous:
                log_event(
                    logger,
                    f"出池节点恢复探测：{node.runtime_name} {previous} → {node.state}",
                    detail={"延迟": outcome.delay_ms, "结论": outcome.detail or "通过"},
                )
        await session.commit()

    log_event(
        logger,
        f"自建内核恢复探测完成：补探 {len(outcomes)} 个，成功 {recovered} 个",
        detail={"补探节点数": len(outcomes), "恢复成功数": recovered},
    )
    return tuple(outcomes)

