"""出池节点恢复：临时内核加载同订阅依赖，仅探测待恢复目标。"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import WritePriority, write_gate
from app.core.logging import log_event
from app.core.net import port_listening
from app.domains.proxies import clash_manager
from app.domains.proxies.kernel_process import (
    PRECHECK_ADMISSION,
    KernelPrecheckError,
    confirm_config_text,
    spawn_kernel,
)
from app.domains.proxies.runtime_config import validate_execution_config
from app.domains.proxypool.models import ProxyNode
from app.domains.proxypool.state import NODE_DEAD, NODE_RETIRED

logger = logging.getLogger(__name__)

# 短命内核使用独立端口段，避免与其他 Runtime 的监听端口冲突。
PROBE_PORT_BASE = 23200
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
    subscription_id: int | None = None


# 恢复探测结论的归因：只有 NODE 属于节点失败证据，其余都是「这一探没测到节点」。
CAUSE_NODE = "node"
CAUSE_DEPENDENCY = "dependency"
CAUSE_CONFIG = "config"
CAUSE_KERNEL = "kernel"
CAUSE_CONTROLLER = "controller"
CAUSE_INFRA = "infra"


@dataclass(frozen=True)
class ProbeOutcome:
    node_id: str
    runtime_name: str
    ok: bool
    delay_ms: int | None
    detail: str
    node_failure: bool = True
    cause: str = CAUSE_NODE


def _free_port(preferred: int) -> int | None:
    """优先用 preferred，被占则向上顺延；整段占满返回 None。"""
    for port in range(preferred, preferred + 8):
        if not port_listening(port):
            return port
    return None


def _recovery_bundles(
    nodes: Sequence[RecoveredNode], *,
    dependencies: Sequence[RecoveredNode] = (),
    source_documents: Mapping[int, dict] | None = None,
    source_errors: Mapping[int, str] | None = None,
) -> tuple[list[RecoveredNode], dict, list[ProbeOutcome]]:
    from app.domains.proxies.runtime_config import render_node_bundle

    available = {node.node_id: node for node in (*dependencies, *nodes)}
    by_subscription: dict[int | None, list[RecoveredNode]] = {}
    for node in available.values():
        by_subscription.setdefault(node.subscription_id, []).append(node)
    documents = source_documents or {}
    proxies: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    ready: list[RecoveredNode] = []
    rejected: list[ProbeOutcome] = []
    for node in nodes:
        peers = by_subscription[node.subscription_id]
        try:
            if not node.config.get("server") or not node.config.get("type"):
                raise ValueError("节点连接配置不完整")
            bundle = render_node_bundle(
                [{**peer.config, "name": peer.original_name} for peer in peers],
                {peer.original_name: peer.runtime_name for peer in peers},
                groups=documents.get(node.subscription_id, {}).get("proxy-groups") or (),
                namespace=str(node.subscription_id), roots=[node.original_name],
            )
            for proxy in bundle["proxies"]:
                if not proxy.get("type") or (
                    proxy.get("type") not in {"direct", "reject"} and not proxy.get("server")
                ):
                    raise ValueError(f"节点连接配置不完整：{proxy['name']}")
        except ValueError as exc:
            source_error = (source_errors or {}).get(node.subscription_id)
            detail = f"节点依赖不可用：{exc}"
            if source_error:
                detail = f"{detail}；{source_error}"
            rejected.append(ProbeOutcome(
                node.node_id, node.runtime_name, False, None,
                detail, node_failure=False, cause=CAUSE_DEPENDENCY,
            ))
            continue
        ready.append(node)
        proxies.update((item["name"], item) for item in bundle["proxies"])
        groups.update((item["name"], item) for item in bundle["proxy-groups"])
    return ready, {"proxies": list(proxies.values()), "proxy-groups": list(groups.values())}, rejected


def _render_config(nodes: list[RecoveredNode], *, controller_port: int, secret: str,
                   listener_base: int, dependencies: Sequence[RecoveredNode] = (),
                   source_documents: Mapping[int, dict] | None = None,
                   bundle: dict | None = None) -> tuple[str, dict[str, int]]:
    """依赖仅用于连接，监听端口只分配给恢复目标。"""
    if bundle is None:
        _ready, bundle, rejected = _recovery_bundles(
            nodes, dependencies=dependencies, source_documents=source_documents,
        )
        if rejected:
            raise ValueError(rejected[0].detail)

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
        **bundle,
        "listeners": listeners,
        "rules": ["MATCH,DIRECT"],
    }
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    validate_execution_config(text)
    return text, ports


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


async def _probe_one(base_url: str, secret: str, name: str, url: str):
    from app.domains.proxypool.health import probe_node

    return await probe_node(
        base_url, secret, name, url=url,
        timeout_ms=NODE_TIMEOUT_MS, client_timeout=20.0,
    )


async def probe_out_of_pool_nodes(
    nodes: list[RecoveredNode],
    *,
    exe_path: str,
    work_dir: Path,
    url: str,
    dependencies: Sequence[RecoveredNode] = (),
    source_documents: Mapping[int, dict] | None = None,
    source_errors: Mapping[int, str] | None = None,
) -> list[ProbeOutcome]:
    """临时内核并发探测；依赖错误与基础设施失败不作为节点失败证据。"""
    if not nodes:
        return []
    probe_nodes, bundle, rejected = _recovery_bundles(
        nodes, dependencies=dependencies, source_documents=source_documents,
        source_errors=source_errors,
    )
    if not probe_nodes:
        return rejected

    def unavailable(detail: str, cause: str = CAUSE_INFRA) -> list[ProbeOutcome]:
        return rejected + [
            ProbeOutcome(n.node_id, n.runtime_name, False, None, detail,
                         node_failure=False, cause=cause)
            for n in probe_nodes
        ]

    controller_port = _free_port(PROBE_PORT_BASE)
    if controller_port is None:
        return unavailable("没有可用的探测端口")
    listener_base = controller_port + 20
    secret = f"dead-probe-{controller_port}"
    log_file = None
    proc: subprocess.Popen | None = None
    base_url = f"http://127.0.0.1:{controller_port}"
    try:
        try:
            text, _ports = _render_config(
                probe_nodes, controller_port=controller_port, secret=secret,
                listener_base=listener_base, bundle=bundle,
            )
        except ValueError as exc:
            return unavailable(f"恢复探测配置未通过准入：{exc}", CAUSE_CONFIG)
        work_dir.mkdir(parents=True, exist_ok=True)
        config_path = work_dir / "config.yaml"
        config_path.write_text(text, encoding="utf-8")
        try:
            clash_manager.validate_config(exe_path, str(config_path), work_dir)
            confirm_config_text(config_path, text)
        except KernelPrecheckError as exc:
            log_event(
                logger,
                "出池节点恢复探测的内核预检未通过，本轮跳过",
                tag="跳过",
                level=logging.WARNING,
                detail={"预检类型": exc.kind, "原因": str(exc)},
            )
            cause = CAUSE_CONFIG if exc.kind == PRECHECK_ADMISSION else CAUSE_KERNEL
            return unavailable(f"临时内核预检未通过（{exc.kind}）：{exc}", cause)
        log_file = open(work_dir / "probe.log", "ab")
        try:
            proc = spawn_kernel(
                exe_path, ["-d", str(work_dir), "-f", str(config_path)], log_file=log_file,
            )
        except OSError as exc:
            return unavailable(f"临时内核无法启动：{type(exc).__name__}", CAUSE_KERNEL)
        if not await _wait_controller(base_url, secret, STARTUP_WAIT):
            return unavailable("临时内核控制器未就绪", CAUSE_CONTROLLER)
        from app.domains.proxypool.health import PROBE_CONCURRENCY

        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(node: RecoveredNode) -> ProbeOutcome:
            async with semaphore:
                result = await _probe_one(base_url, secret, node.runtime_name, url)
            return ProbeOutcome(
                node.node_id, node.runtime_name, result.ok, result.delay_ms, result.detail,
                node_failure=result.valid,
                cause=CAUSE_NODE if result.valid else CAUSE_CONTROLLER,
            )

        return rejected + list(await asyncio.gather(*(probe(node) for node in probe_nodes)))
    except Exception as exc:  # noqa: BLE001 —— 恢复探测是尽力而为，失败不阻断周期
        log_event(
            logger,
            "出池节点恢复探测失败，本轮跳过",
            tag="跳过",
            level=logging.WARNING,
            detail={"原因": f"{type(exc).__name__}: {exc}"},
        )
        return unavailable(f"探测异常：{type(exc).__name__}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_file is not None:
            log_file.close()


def _recovery_input(node: ProxyNode) -> RecoveredNode:
    config = node.normalized_config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = {}
    return RecoveredNode(
        node_id=str(node.node_id), runtime_name=str(node.runtime_name),
        original_name=str(node.original_name or node.runtime_name),
        config=dict(config) if isinstance(config, dict) else {},
        subscription_id=node.subscription_id,
    )


async def load_recoverable_nodes(
    session: AsyncSession, *, batch: int = BATCH_SIZE
) -> list[RecoveredNode]:
    """取一批待恢复节点（DEAD/RETIRED），按「最久没被探过」轮转。"""
    from app.domains.proxypool.models import HealthObservation

    if batch <= 0:
        return []
    last_probe = (
        select(
            HealthObservation.node_id.label("node_id"),
            func.max(HealthObservation.observed_at).label("last_at"),
        )
        .group_by(HealthObservation.node_id)
        .subquery()
    )
    rows = (await session.execute(
        select(ProxyNode)
        .outerjoin(last_probe, last_probe.c.node_id == ProxyNode.node_id)
        .where(ProxyNode.state.in_((NODE_DEAD, NODE_RETIRED)))
        .order_by(last_probe.c.last_at.asc().nullsfirst(), ProxyNode.node_id)
        .limit(batch)
    )).scalars().all()
    return [_recovery_input(node) for node in rows]


async def recover_dead_nodes_self_hosted(
    session: AsyncSession, *,
    data_dir: Path,
    now: datetime,
    url: str | None = None,
    batch: int = BATCH_SIZE,
) -> tuple[ProbeOutcome, ...]:
    """恢复结果记入既有健康观测，仅真实节点探测结果推进节点状态。"""
    from app.domains.proxypool.health import business_probe_url
    from app.domains.proxies.clash_manager import kernel_exe

    nodes = await load_recoverable_nodes(session, batch=batch)
    if not nodes:
        return ()
    exe = kernel_exe(data_dir)
    if not exe.is_file():
        return ()
    probe_url = url or business_probe_url()
    from app.domains.proxypool.subscription import load_source_documents

    subscription_ids = {node.subscription_id for node in nodes if node.subscription_id is not None}
    peers = (await session.scalars(
        select(ProxyNode).where(ProxyNode.subscription_id.in_(subscription_ids))
    )).all()
    dependencies = [_recovery_input(node) for node in peers]
    source_errors: dict[int, str] = {}
    documents = await load_source_documents(
        session, subscription_ids, data_dir=data_dir, errors=source_errors,
    )
    unavailable_sources: dict[int | None, str] = {None: "节点来源不存在"}
    source_failures = [
        ProbeOutcome(node.node_id, node.runtime_name, False, None,
                     unavailable_sources[node.subscription_id], node_failure=False,
                     cause=CAUSE_DEPENDENCY)
        for node in nodes if node.subscription_id in unavailable_sources
    ]
    targets = [node for node in nodes if node.subscription_id not in unavailable_sources]

    tmp_root = data_dir / "proxypool" / "_dead_probe"
    work_dir = tmp_root / f"run-{int(time.time() * 1000)}"
    try:
        outcomes = await probe_out_of_pool_nodes(
            targets, exe_path=str(exe), work_dir=work_dir, url=probe_url,
            dependencies=dependencies, source_documents=documents, source_errors=source_errors,
        )
        by_outcome = {outcome.node_id: outcome for outcome in (*source_failures, *outcomes)}
        outcomes = [by_outcome[node.node_id] for node in nodes if node.node_id in by_outcome]
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    if not outcomes:
        return ()

    by_id = {n.node_id: n for n in nodes}
    from app.domains.proxypool.models import HealthObservation
    from app.domains.proxypool.state import evaluate_node_state as _evaluate

    async with write_gate(WritePriority.BACKGROUND, label="dead_probe_recover"):
        refreshed = (await session.scalars(
            select(ProxyNode)
            .where(ProxyNode.node_id.in_(list(by_id)))
            .execution_options(populate_existing=True)
        )).all()
        fresh_by_id = {node.node_id: node for node in refreshed}
        recovered = 0
        observations = []
        for outcome in outcomes:
            node = fresh_by_id.get(outcome.node_id)
            if node is None:
                continue
            previous = node.state
            if outcome.ok or outcome.node_failure:
                failures = 0 if outcome.ok else (node.consecutive_failures or 0) + 1
                node.consecutive_failures = failures
                node.state = _evaluate(
                    node.state, source_seen=node.subscription_id is not None,
                    probe_ok=outcome.ok,
                    consecutive_failures=failures,
                )
                node.last_l2_at = now
            if outcome.ok:
                recovered += 1
            observations.append(
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
        session.add_all(observations)
        await session.commit()

    log_event(
        logger,
        f"自建内核恢复探测完成：补探 {len(outcomes)} 个，成功 {recovered} 个",
        detail={"补探节点数": len(outcomes), "恢复成功数": recovered},
    )
    return tuple(outcomes)

