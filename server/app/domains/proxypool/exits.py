"""生产容量模型：**独立出口 IP 才是 crawl 的容量单位**，节点不是。

一个机场常把多个节点落在同一个落地 IP 上（多入口同落地），因此「N 个节点」不等于
「N 个出口」：拿节点数当并发容量会把请求重新压回同一个 IP，正是 lane 要解决的问题。
本模块把「池内合格节点」收敛成一组 `ExitSlot`——一个出口槽一个节点，槽数是并发容量的
事实上限：

    合格节点 → 按 exit_ip 分组 → 每组取一个代表节点 → 排序 → 最多 MAX_CRAWL_WORKERS 个槽

两条边界：

- **不知道出口 IP 的节点不进槽**：出口身份是本层的容量依据，没有它就无从判断两个节点
  是不是同一个出口；它们仍留在池里，等探到出口 IP 后自然进入下一轮。
- **槽数封顶 `MAX_CRAWL_WORKERS`，不是出口池容量**：出口多于上限时只选前 N 个执行，
  其余保持可用、不占工位，供后续调度或替换故障出口。

排序刻意保持可解释、无随机：同一份输入永远给出同一份槽位表，否则每次作业都在换出口，
连接池反复重建、生产统计失去连续性、排障无从下手。第一版只用现有事实（节点状态、
出口 IP、三层健康时间戳、主键），不引入容量评分。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domains.proxypool.models import ProxyNode
from app.domains.proxypool.state import NODE_ACTIVE, NODE_NEW, NODE_STALE

# 执行容量上限：与本项目的 worker 上限同源（crawler/config.WORKERS_MAX）。
# 它是「一次 crawl 最多开多少个工位」，**不是**出口池能容纳多少个出口。
MAX_CRAWL_WORKERS = 60

# 节点状态在选槽时的优先级：有健康证据的排前面，仅由来源到达的次之，
# 来源已撤但曾健康的再次（STALE 仍在池里，可继续服务）。
_STATE_RANK = {NODE_ACTIVE: 0, NODE_NEW: 1, NODE_STALE: 2}


@dataclass(frozen=True)
class ExitSlot:
    """一个可用出口工位：出口 IP 是身份，代表节点是当前执行者。

    `alternatives` 是同出口的其它候选运行名——同落地不同入口。代表节点失效时可以在
    **不改容量模型**的前提下换一个节点顶上（槽还是那个槽）。
    """

    exit_ip: str
    runtime_name: str
    node_id: str
    alternatives: tuple[str, ...] = ()
    lane_index: int = -1
    listener_port: int | None = None

    @property
    def key(self) -> str:
        """预算/熔断/统计的账本键：按出口 IP，不按 lane 序号。

        同一个出口上将来放几个 worker，它们共享同一份预算与同一个熔断器——按序号记账
        会让「多开一个 worker」凭空多出一份额度。
        """
        return self.exit_ip


def _time_key(value: datetime | None) -> float:
    """时间戳排序键：越新越大；缺失排最后（不参与「谁更可信」的比较）。"""
    return value.timestamp() if isinstance(value, datetime) else float("-inf")


def _node_rank(node: ProxyNode) -> tuple:
    """同出口内选代表节点的顺序：状态 → 最近业务成功 → 最近出口确认 → 主键。"""
    return (
        _STATE_RANK.get(node.state, 9),
        -_time_key(node.last_l2_at),
        -_time_key(node.last_l1_at),
        int(node.id or 0),
    )


def group_exit_candidates(nodes: Iterable[ProxyNode]) -> dict[str, list[ProxyNode]]:
    """按出口 IP 分组；不知道出口 IP 的节点不参与（返回里没有它们的键）。"""
    groups: dict[str, list[ProxyNode]] = {}
    for node in nodes:
        if not node.exit_ip or not node.runtime_name:
            continue
        groups.setdefault(str(node.exit_ip), []).append(node)
    return groups


def select_exit_slots(
    nodes: Sequence[ProxyNode], *, max_workers: int = MAX_CRAWL_WORKERS
) -> list[ExitSlot]:
    """合格节点 → 出口槽表（每个出口一个槽，最多 `max_workers` 个）。

    排序口径（全部来自现有事实，可复现）：
    1. 出口内先按 `_node_rank` 定出代表节点与候选顺序；
    2. 出口之间按「代表节点的状态排名、最近业务成功时间、出口 IP」升/降序；
    3. 取前 `max_workers` 个出口。

    稳定性的意义：同一份池在两次作业之间给出同一份槽位表，出口集合不会无故漂移。
    """
    groups = group_exit_candidates(nodes)
    ranked: list[tuple[tuple, ExitSlot]] = []
    for exit_ip, members in groups.items():
        ordered = sorted(members, key=_node_rank)
        head = ordered[0]
        slot = ExitSlot(
            exit_ip=exit_ip,
            runtime_name=str(head.runtime_name),
            node_id=str(head.node_id),
            alternatives=tuple(str(n.runtime_name) for n in ordered[1:]),
        )
        sort_key = (
            _STATE_RANK.get(head.state, 9),
            -_time_key(head.last_l2_at),
            -_time_key(head.last_l1_at),
            exit_ip,
        )
        ranked.append((sort_key, slot))

    ranked.sort(key=lambda item: item[0])
    limit = max(0, int(max_workers))
    return [slot for _key, slot in ranked[:limit]]


def assign_lane_indexes(slots: Sequence[ExitSlot]) -> list[ExitSlot]:
    """给槽表编上 lane 序号（槽序即 lane 序，一次 run 内不变）。"""
    return [
        ExitSlot(
            exit_ip=s.exit_ip, runtime_name=s.runtime_name, node_id=s.node_id,
            alternatives=s.alternatives, lane_index=i, listener_port=s.listener_port,
        )
        for i, s in enumerate(slots)
    ]


def slots_to_plan(slots: Sequence[ExitSlot], ports: Sequence[int]) -> list[dict]:
    """槽表 + listener 端口 → 可持久化的 lane 计划（供归因与诊断读取）。"""
    plan: list[dict] = []
    for i, slot in enumerate(slots):
        plan.append({
            "lane": i,
            "port": int(ports[i]) if i < len(ports) else None,
            "exitIp": slot.exit_ip,
            "node": slot.runtime_name,
            "alternatives": list(slot.alternatives),
        })
    return plan


async def exit_snapshot(session) -> dict[str, str]:
    """当前出口身份快照：`runtime_name -> exit_ip`（只收已知出口的合格节点）。

    run 的容量由它决定，不由上一次重建时写下的 lane 计划决定——L1 会持续刷新出口
    身份，两份数据之间必然存在重建窗口，运行期必须以快照为准。
    """
    from app.domains.proxypool.pool import eligible_nodes

    return {
        node.runtime_name: str(node.exit_ip)
        for node in await eligible_nodes(session)
        if node.exit_ip
    }


def slot_signature(exit_by_node: Mapping[str, str] | None) -> tuple[str, ...]:
    """快照签名（`出口 IP | 节点` 排序后的元组）：判断"出口身份是否变了"。

    只比集合不比数量：换了一个节点但出口数不变同样要收敛（listener 绑定已经过期）。
    """
    if not exit_by_node:
        return ()
    return tuple(sorted(f"{v}|{k}" for k, v in exit_by_node.items() if v))