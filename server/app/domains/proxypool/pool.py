"""P1.3-B：Registry → 池文件（只渲染，不做发布协议）。

职责只有一句：**把 Registry 里能用的节点渲染成一个合法的池文件。**

- 资格：`state ∈ {NEW, ACTIVE, STALE}`；`DEAD` / `RETIRED` 不进池
- 取名：一律用**已持久化**的 `runtime_name`，覆盖 `normalized_config["name"]`
  （不覆盖就会让 Registry 已经解决的「稳定运行名」在进内核时退化成订阅原名）
- 三个门禁，**全部在写盘之前**：能重新解析 / 名字无重复 / 写入数 = 预期数。任一
  不过即 `PoolBuildError`，此时旧池文件一字未动。

**不做**：generation、candidate provider、rollback、Mihomo API reconcile、
proxy-group、健康检查、lane、worker lease、自动启停内核。池文件写完之后的事
（内核是否真的加载了它、名字对不对得上）属于下一步，不在这里预支。

落盘沿用本域既有约定（见 `subscription._write_raw`）：先写 `.tmp` 再 `replace`，
避免半截文件。

事务边界：本函数只读 Registry，**不 commit**——池文件不是数据库事务的一部分。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import ProxyNode
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


def pool_path(data_dir: Path) -> Path:
    return Path(data_dir) / "proxypool" / POOL_FILENAME


def _is_complete(node: ProxyNode) -> bool:
    """资格之外的「配置完整」：池里每一条都必须是内核装载得了的。"""
    if not node.runtime_name:
        return False
    config = node.normalized_config
    if not isinstance(config, Mapping) or not config:
        return False
    return all(config.get(key) not in (None, "") for key in _REQUIRED_CONFIG_KEYS)


def _render_proxies(nodes: Sequence[ProxyNode]) -> list[dict]:
    """把台账行变成池条目：配置副本 + 覆盖成持久化运行名。

    只改**副本**：`normalized_config` 是这条台账的事实副本，就地改写会污染它。
    """
    proxies: list[dict] = []
    for node in nodes:
        payload = dict(node.normalized_config)
        payload["name"] = node.runtime_name
        proxies.append(payload)
    return proxies


def render_pool(nodes: Sequence[ProxyNode]) -> tuple[str, tuple[str, ...]]:
    """纯渲染层：不碰数据库、不碰磁盘，只做重复名门禁并产出 YAML 文本。

    单独暴露是为了让「重名即拒绝」这条规则**不依赖数据库唯一约束兜底**也测得到。
    """
    proxies = _render_proxies(nodes)
    names = tuple(str(p["name"]) for p in proxies)

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

    text = yaml.safe_dump(
        {"proxies": proxies}, allow_unicode=True, sort_keys=False
    )
    return text, names


async def eligible_nodes(session: AsyncSession) -> list[ProxyNode]:
    """当前合格节点（池的输入集）。**只读、不落盘**。

    维护事务要回答"此刻池里还有谁"（例如恢复 GLOBAL 时要判断原节点是否已出池），
    不能为此写一次池文件——落盘是 `build_pool` 的职责。
    """
    rows = await session.execute(
        select(ProxyNode)
        .where(ProxyNode.state.in_(sorted(POOL_ELIGIBLE_STATES)))
        .order_by(ProxyNode.id)
    )
    return [node for node in rows.scalars() if _is_complete(node)]


async def eligible_runtime_names(session: AsyncSession) -> tuple[str, ...]:
    """合格节点的运行名（顺序与 `build_pool` 一致：按主键）。"""
    return tuple(node.runtime_name for node in await eligible_nodes(session))


async def build_pool(session: AsyncSession, *, data_dir: Path) -> PoolBuild:
    """读 Registry → 渲染 → 三门禁 → 落盘 → 复读。"""
    eligible = await eligible_nodes(session)
    expected_count = len(eligible)

    text, names = render_pool(eligible)

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
    )