"""Registry 合格节点与订阅连接依赖生成池产物，完整校验后原子写盘。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import (
    ProxyNode,
    is_info_placeholder,
)
from app.domains.proxypool.state import NODE_ACTIVE, NODE_NEW, NODE_STALE

POOL_FILENAME = "crawl-pool.yaml"
POOL_ELIGIBLE_STATES = frozenset({NODE_NEW, NODE_ACTIVE, NODE_STALE})

# 内核能装载一条代理所需的最小键：缺任何一个都不该进池（写进去只会被内核拒绝，
# 比不写更糟——那会变成「池里有它、内核里没有」的静默差额）。
_REQUIRED_CONFIG_KEYS = ("type", "server", "port")


class PoolBuildError(RuntimeError):
    """门禁未通过。**写盘前**抛出，因此调用方可以确信旧池未被破坏。"""


@dataclass(frozen=True)
class PoolBuild:
    """一次池生成的账目（数量是门禁的观测值，供调用方记录/断言）。"""

    path: Path
    expected_count: int
    written_count: int
    runtime_names: tuple[str, ...]
    auxiliary_names: tuple[str, ...] = ()


def pool_path(data_dir: Path) -> Path:
    return Path(data_dir) / "proxypool" / POOL_FILENAME


def pool_file_names(data_dir: Path) -> tuple[str, ...]:
    """池文件当前写入的节点名（没有文件时返回空）。

    这是**落盘产物**的读数，与 `eligible_nodes`（合格集）区分开：判断池文件有没有
    跟上当前合格集要比**集合**，比数量会在"换了一个节点但数量相同"时漏判。
    """
    path = pool_path(data_dir)
    if not path.is_file():
        return ()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(str(entry.get("name")) for entry in (doc.get("proxies") or []))


def pool_auxiliary_names(data_dir: Path) -> tuple[str, ...]:
    """池产物中的连接依赖组名称。"""
    path = pool_path(data_dir)
    if not path.is_file():
        return ()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(str(group["name"]) for group in doc.get("proxy-groups", []))


def _is_complete(node: ProxyNode) -> bool:
    """资格之外的「配置完整」：池里每一条都必须是内核装载得了的。"""
    if not node.runtime_name:
        return False
    config = node.normalized_config
    if not isinstance(config, Mapping) or not config:
        return False
    return all(config.get(key) not in (None, "") for key in _REQUIRED_CONFIG_KEYS)


def _render_bundle(
    nodes: Sequence[ProxyNode], source_documents: Mapping[int, Mapping] | None = None,
) -> dict[str, list[dict]]:
    """按订阅改写连接引用，保留节点参数与输入顺序。"""
    from app.domains.proxies.runtime_config import render_node_bundle

    grouped: dict[int | None, list[tuple[int, ProxyNode]]] = {}
    for index, node in enumerate(nodes):
        grouped.setdefault(node.subscription_id, []).append((index, node))
    proxies: list[dict | None] = [None] * len(nodes)
    groups: list[dict] = []
    for sid, members in grouped.items():
        names: dict[str, str] = {}
        ambiguous: set[str] = set()
        payloads = []
        for _, node in members:
            payload = {**dict(node.normalized_config), "name": node.runtime_name}
            original = str(node.original_name or node.normalized_config.get("name") or "")
            if original in names and names[original] != node.runtime_name:
                ambiguous.add(original)
            names[original] = node.runtime_name
            payloads.append(payload)
        for name in ambiguous:
            names.pop(name, None)
        source = (source_documents or {}).get(sid) or {}
        try:
            bundle = render_node_bundle(
                payloads, names, groups=source.get("proxy-groups") or (), namespace=str(sid),
                source_names=[str(node.original_name or node.normalized_config.get("name") or "")
                              for _, node in members],
            )
        except ValueError as exc:
            raise PoolBuildError(f"节点连接依赖不完整：{exc}") from exc
        groups.extend(bundle["proxy-groups"])
        for (index, _), payload in zip(members, bundle["proxies"], strict=True):
            proxies[index] = payload
    return {"proxies": proxies, "proxy-groups": groups}


def render_pool(
    nodes: Sequence[ProxyNode], *, source_documents: Mapping[int, Mapping] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """纯渲染层：不碰数据库、不碰磁盘，只做重复名门禁并产出 YAML 文本。

    单独暴露是为了让「重名即拒绝」这条规则**不依赖数据库唯一约束兜底**也测得到。
    """
    names = tuple(str(node.runtime_name) for node in nodes)

    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise PoolBuildError(
            f"池内 runtime_name 重复：{sorted(set(duplicates))}"
            "（写盘前即拒绝：宁可不生成，也不生成一个静默少节点的池）"
        )

    bundle = _render_bundle(nodes, source_documents)
    if not bundle["proxy-groups"]:
        bundle.pop("proxy-groups")
    text = yaml.safe_dump(
        bundle, allow_unicode=True, sort_keys=False
    )
    return text, names


async def eligible_nodes(session: AsyncSession) -> list[ProxyNode]:
    """当前合格节点（池的输入集）。**只读、不落盘**。

    维护事务要回答"此刻池里还有谁"（例如恢复 GLOBAL 时要判断原节点是否已出池），
    不能为此写一次池文件——落盘是 `build_pool` 的职责。

    合格 = 状态合格 + 配置完整 + **仍归属订阅**（`subscription_id` 非空）：解除
    归属的节点（订阅撤掉它、订阅退出生产池或被删）不再占运行位。
    信息占位节点（机场公告位）即使已混入账本也**永不合格**——它们不是可用的
    代理端点。
    """
    from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription

    subscriptions = (await session.execute(select(ProxySubscription))).scalars().all()
    blocked = {s.id for s in subscriptions if s.resource_block_reason() is not None}
    query = (
        select(ProxyNode)
        .where(ProxyNode.state.in_(sorted(POOL_ELIGIBLE_STATES)))
        .where(ProxyNode.subscription_id.is_not(None))
        .join(ProxySubscription, ProxySubscription.id == ProxyNode.subscription_id)
        .where(
            (ProxySubscription.admission_status == ADMISSION_ACTIVE)
            | ProxySubscription.admission_status.is_(None)
        )
    )
    if blocked:
        query = query.where(ProxyNode.subscription_id.not_in(blocked))
    rows = await session.execute(query.order_by(ProxyNode.id))
    return [
        node for node in rows.scalars()
        if _is_complete(node) and not is_info_placeholder(node.runtime_name)
    ]


async def eligible_runtime_names(session: AsyncSession) -> tuple[str, ...]:
    """合格节点的运行名（顺序与 `build_pool` 一致：按主键）。"""
    return tuple(node.runtime_name for node in await eligible_nodes(session))


async def source_documents_for_nodes(
    session: AsyncSession, nodes: Sequence[ProxyNode], *, data_dir: Path,
    errors: dict[int, str] | None = None,
) -> dict[int, dict]:
    """只为无法由同订阅节点解释的连接目标读取原文。"""
    from app.domains.proxies.runtime_config import BUILTIN_PROXY_NAMES
    from app.domains.proxypool.subscription import SnapshotError, load_source_documents

    source_names: dict[int, set[str]] = {}
    for node in nodes:
        source_names.setdefault(node.subscription_id, set()).add(str(node.original_name or node.normalized_config.get("name") or ""))
    needs_source = {
        node.subscription_id for node in nodes
        if node.normalized_config.get("dialer-proxy")
        and node.normalized_config["dialer-proxy"] not in BUILTIN_PROXY_NAMES
        and node.normalized_config["dialer-proxy"] not in source_names[node.subscription_id]
    }
    try:
        return await load_source_documents(session, needs_source, data_dir=data_dir, errors=errors)
    except SnapshotError as exc:
        raise PoolBuildError(f"节点连接依赖来源不可用：{exc}") from exc


async def build_pool(session: AsyncSession, *, data_dir: Path) -> PoolBuild:
    """读 Registry → 渲染 → 三门禁 → 落盘 → 复读。"""
    eligible = await eligible_nodes(session)
    expected_count = len(eligible)
    sources = await source_documents_for_nodes(session, eligible, data_dir=data_dir)
    text, names = render_pool(eligible, source_documents=sources)

    # 门禁一/三：生成的文本自己必须解析得回来，且条目数不能缩水。
    # 都放在写盘之前——失败时旧池保持原样（不靠回滚补）。
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("proxies"), list):
        raise PoolBuildError("生成的池不是 {proxies: [...]} 形态")
    written_count = len(parsed["proxies"])
    if written_count != expected_count:
        raise PoolBuildError(
            f"写入数量 {written_count} ≠ 预期 {expected_count}"
            "（存在被静默丢掉的节点）"
        )

    path = pool_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

    # 写完复读：磁盘上的名字集合必须与预期一致（名字是池与内核之间唯一的对账钥匙）
    replayed = yaml.safe_load(path.read_text(encoding="utf-8"))
    disk_names = tuple(str(p["name"]) for p in replayed["proxies"])
    if set(disk_names) != set(names):
        raise PoolBuildError("磁盘复读的名字集合与预期不一致")

    return PoolBuild(
        path=path,
        expected_count=expected_count,
        written_count=written_count,
        runtime_names=names,
        auxiliary_names=tuple(str(g["name"]) for g in parsed.get("proxy-groups", [])),
    )
