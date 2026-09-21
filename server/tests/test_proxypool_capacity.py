"""生产容量模型：独立出口 IP 是容量单位，worker 上限 60。

被测：`proxypool/exits.py` 的 `select_exit_slots` / `group_exit_candidates` /
`assign_lane_indexes`，以及它到 lane 计划、worker 收敛的联动。

关键口径（每条都有反例用例）：
- 出口 IP 相同的一批节点**只占一个槽**（节点数 ≠ 出口数）；
- 不知道出口 IP 的节点不进槽（无出口身份即无容量依据）；
- 槽数封顶 `MAX_CRAWL_WORKERS`，超出的出口保持可用但不占工位；
- 同一份输入永远给出同一份槽表（无随机、不漂移）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domains.proxypool.exits import (
    MAX_CRAWL_WORKERS,
    assign_lane_indexes,
    group_exit_candidates,
    select_exit_slots,
    slots_to_plan,
)
from app.domains.proxypool.models import ProxyNode
from app.domains.proxypool.state import NODE_ACTIVE, NODE_NEW, NODE_STALE

NOW = datetime(2026, 9, 21, 12, 0, 0)


def _node(
    idx: int,
    *,
    exit_ip: str | None,
    state: str = NODE_ACTIVE,
    l1: datetime | None = None,
    l2: datetime | None = None,
    name: str | None = None,
) -> ProxyNode:
    """构造一条台账行：只填容量模型用得到的字段。"""
    return ProxyNode(
        id=idx, node_id=f"n{idx}", fingerprint=f"f{idx}",
        runtime_name=name or f"1|node{idx}", proxy_type="ss",
        server=f"s{idx}.example.net", state=state, exit_ip=exit_ip,
        last_l1_at=l1, last_l2_at=l2,
    )


# ── 1. 出口去重 ──────────────────────────────────────────────────
def test_same_exit_ip_collapses_to_one_slot() -> None:
    """16 个节点只有 5 个出口 → 只能得到 5 个槽（不是 16）。"""
    nodes = [_node(i, exit_ip=f"10.0.0.{i % 5}") for i in range(16)]
    slots = select_exit_slots(nodes)
    assert len(slots) == 5
    assert {s.exit_ip for s in slots} == {f"10.0.0.{i}" for i in range(5)}


def test_same_exit_keeps_alternatives_as_replacements() -> None:
    nodes = [_node(i, exit_ip="10.0.0.1") for i in range(3)]
    slots = select_exit_slots(nodes)
    assert len(slots) == 1
    assert slots[0].runtime_name == "1|node0", "状态与证据相同时取主键最小者"
    assert set(slots[0].alternatives) == {"1|node1", "1|node2"}


def test_nodes_without_exit_ip_do_not_take_slots() -> None:
    nodes = [_node(0, exit_ip="10.0.0.1"), _node(1, exit_ip=None), _node(2, exit_ip="")]
    slots = select_exit_slots(nodes)
    assert [s.exit_ip for s in slots] == ["10.0.0.1"], "出口身份未知的节点不占工位"


def test_group_exit_candidates_skips_unknown_exit() -> None:
    groups = group_exit_candidates([_node(0, exit_ip=None), _node(1, exit_ip="1.1.1.1")])
    assert list(groups) == ["1.1.1.1"]


# ── 2. 选择顺序可解释、可复现 ────────────────────────────────────
def test_active_preferred_over_new_and_stale() -> None:
    nodes = [
        _node(0, exit_ip="10.0.0.9", state=NODE_STALE),
        _node(1, exit_ip="10.0.0.2", state=NODE_ACTIVE),
        _node(2, exit_ip="10.0.0.5", state=NODE_NEW),
    ]
    assert [s.exit_ip for s in select_exit_slots(nodes)] == [
        "10.0.0.2", "10.0.0.5", "10.0.0.9",
    ]


def test_fresher_business_evidence_first() -> None:
    nodes = [
        _node(0, exit_ip="10.0.0.1", l2=NOW - timedelta(hours=6)),
        _node(1, exit_ip="10.0.0.2", l2=NOW - timedelta(minutes=5)),
    ]
    assert [s.exit_ip for s in select_exit_slots(nodes)] == ["10.0.0.2", "10.0.0.1"]


def test_selection_is_stable_across_calls_and_input_order() -> None:
    nodes = [_node(i, exit_ip=f"10.0.0.{i}") for i in range(8)]
    first = [s.exit_ip for s in select_exit_slots(nodes)]
    second = [s.exit_ip for s in select_exit_slots(list(reversed(nodes)))]
    assert first == second, "同一份池必须给出同一份槽表（顺序按出口 IP 兜底排序）"


# ── 3. 60 上限：<60 / =60 / >60 ──────────────────────────────────
@pytest.mark.parametrize(
    ("exit_count", "expected"),
    [(8, 8), (60, 60), (94, MAX_CRAWL_WORKERS)],
)
def test_slot_count_capped_by_max_workers(exit_count, expected) -> None:
    nodes = [_node(i, exit_ip=f"10.0.{i // 256}.{i % 256}") for i in range(exit_count)]
    slots = select_exit_slots(nodes)
    assert len(slots) == expected


def test_max_workers_constant_is_60() -> None:
    assert MAX_CRAWL_WORKERS == 60


def test_excess_exits_stay_out_but_intact() -> None:
    """72 个出口 → 60 个槽，另外 12 个只是没被选中，不是被删掉/标死。"""
    nodes = [_node(i, exit_ip=f"10.0.0.{i}") for i in range(72)]
    slots = select_exit_slots(nodes)
    assert len(slots) == 60
    picked = {s.exit_ip for s in slots}
    idle = {f"10.0.0.{i}" for i in range(72)} - picked
    assert len(idle) == 12
    assert all(n.state == NODE_ACTIVE for n in nodes if n.exit_ip in idle)


def test_max_workers_override_is_honoured() -> None:
    nodes = [_node(i, exit_ip=f"10.0.0.{i}") for i in range(10)]
    assert len(select_exit_slots(nodes, max_workers=3)) == 3
    assert len(select_exit_slots(nodes, max_workers=0)) == 0


# ── 4. 槽 → lane 计划 ───────────────────────────────────────────
def test_assign_lane_indexes_is_positional() -> None:
    nodes = [_node(i, exit_ip=f"10.0.0.{i}") for i in range(3)]
    slots = assign_lane_indexes(select_exit_slots(nodes))
    assert [s.lane_index for s in slots] == [0, 1, 2]


def test_slots_to_plan_pairs_lane_with_port_and_exit() -> None:
    nodes = [_node(i, exit_ip=f"10.0.0.{i}") for i in range(2)]
    slots = select_exit_slots(nodes)
    plan = slots_to_plan(slots, [41001, 41002])
    assert plan == [
        {"lane": 0, "port": 41001, "exitIp": "10.0.0.0", "node": "1|node0",
         "alternatives": []},
        {"lane": 1, "port": 41002, "exitIp": "10.0.0.1", "node": "1|node1",
         "alternatives": []},
    ]


def test_slots_to_plan_tolerates_missing_ports() -> None:
    slots = select_exit_slots([_node(0, exit_ip="10.0.0.1")])
    assert slots_to_plan(slots, [])[0]["port"] is None


# ── 5. 空池 ─────────────────────────────────────────────────────
def test_empty_pool_yields_no_slots() -> None:
    assert select_exit_slots([]) == []
    assert select_exit_slots([_node(0, exit_ip=None)]) == []
    assert slots_to_plan([], []) == []