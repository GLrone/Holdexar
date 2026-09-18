"""proxypool 状态机：状态常量 + 显式转移表 + 唯一出口。

调用方**不得**直接给 `node.state` / `lane.state` 赋值，一律经本模块的
`apply_transition` / `apply_lane_transition`：非法转移立刻抛
`IllegalTransitionError`（异常自带 from / to 与合法目标集）。

节点四态语义（写在这里，不靠调用方猜）：

- ACTIVE：订阅中存在 且 近期健康 —— 一等调度成员。
- STALE：订阅已不再返回 但 仍通过健康验证 —— 保留在池，降权。
- DEAD：当前不可用，Registry 保留，可自由复活。
- RETIRED：连续失败达阈值，移出运行池，Registry 留档。

DEAD 与 RETIRED 的分界：DEAD 只是暂时不通（拿到成功证据即回 ACTIVE / STALE），
RETIRED 是被认定不再值得进池，只有新的健康成功证据才让它回 ACTIVE / STALE。
"""
from __future__ import annotations

from collections.abc import Iterable

NODE_NEW = "NEW"
NODE_ACTIVE = "ACTIVE"
NODE_STALE = "STALE"
NODE_DEAD = "DEAD"
NODE_RETIRED = "RETIRED"

LANE_EMPTY = "EMPTY"
LANE_INIT = "INIT"
LANE_READY = "READY"
LANE_DEGRADED = "DEGRADED"
LANE_SWITCHING = "SWITCHING"
LANE_DEAD = "DEAD"

NODE_STATES: frozenset[str] = frozenset(
    {NODE_NEW, NODE_ACTIVE, NODE_STALE, NODE_DEAD, NODE_RETIRED}
)
LANE_STATES: frozenset[str] = frozenset(
    {LANE_EMPTY, LANE_INIT, LANE_READY, LANE_DEGRADED, LANE_SWITCHING, LANE_DEAD}
)

# 退休阈值的缺省值。业务侧一律显式传参，便于后续用长周期运行数据校准。
DEFAULT_RETIRE_AFTER_FAILED_PROBES = 3

# 允许「失败累计 → 退休」的起点：必须先离开在运行集的 NEW / ACTIVE，
# ACTIVE → RETIRED 这种直跳由转移表本身挡住。
_RETIRE_FROM: frozenset[str] = frozenset({NODE_STALE, NODE_DEAD})

_NODE_TRANSITIONS: dict[str, frozenset[str]] = {
    NODE_NEW: frozenset({NODE_ACTIVE, NODE_DEAD, NODE_STALE}),
    NODE_ACTIVE: frozenset({NODE_STALE, NODE_DEAD}),
    NODE_STALE: frozenset({NODE_ACTIVE, NODE_DEAD, NODE_RETIRED}),
    NODE_DEAD: frozenset({NODE_ACTIVE, NODE_STALE, NODE_RETIRED}),
    NODE_RETIRED: frozenset({NODE_ACTIVE, NODE_STALE}),
}

_LANE_TRANSITIONS: dict[str, frozenset[str]] = {
    LANE_EMPTY: frozenset({LANE_INIT}),
    LANE_INIT: frozenset({LANE_READY, LANE_EMPTY}),
    LANE_READY: frozenset({LANE_SWITCHING, LANE_DEGRADED, LANE_DEAD, LANE_EMPTY}),
    LANE_SWITCHING: frozenset({LANE_READY, LANE_DEGRADED, LANE_DEAD}),
    LANE_DEGRADED: frozenset({LANE_READY, LANE_SWITCHING, LANE_DEAD, LANE_EMPTY}),
    LANE_DEAD: frozenset({LANE_EMPTY, LANE_INIT, LANE_READY}),
}


class IllegalTransitionError(Exception):
    """非法状态转移：携带起点、终点与该起点的全部合法目标。"""

    def __init__(self, current: str, nxt: str, allowed: Iterable[str]) -> None:
        self.current = current
        self.target = nxt
        self.allowed = frozenset(allowed)
        shown = "、".join(sorted(self.allowed)) or "（无：起点未知）"
        super().__init__(
            f"非法状态转移 {current} → {nxt}；{current} 的合法目标为 {shown}"
        )


def _check_threshold(retire_after_failed_probes: int) -> None:
    if retire_after_failed_probes < 1:
        raise ValueError(
            f"retire_after_failed_probes 必须 >= 1，实际 {retire_after_failed_probes}"
        )


# ── 节点 ──────────────────────────────────────────────────────────
def legal_targets(current: str) -> frozenset[str]:
    """该起点的全部合法目标集（未知起点 → 空集）。"""
    return _NODE_TRANSITIONS.get(current, frozenset())


def can_transition(current: str, nxt: str) -> bool:
    return nxt in legal_targets(current)


def apply_transition(current: str, nxt: str) -> str:
    """节点状态变更的唯一出口；非法组合抛 `IllegalTransitionError`。"""
    allowed = legal_targets(current)
    if nxt not in allowed:
        raise IllegalTransitionError(current, nxt, allowed)
    return nxt


# ── Lane ──────────────────────────────────────────────────────────
# Lane 单独一套函数：两个域都有 DEAD，但合法目标完全不相交
# （节点 DEAD → ACTIVE/STALE/RETIRED；Lane DEAD → EMPTY/INIT/READY），
# 合成一个泛型函数会把两者混成一团。
def legal_lane_targets(current: str) -> frozenset[str]:
    return _LANE_TRANSITIONS.get(current, frozenset())


def can_transition_lane(current: str, nxt: str) -> bool:
    return nxt in legal_lane_targets(current)


def apply_lane_transition(current: str, nxt: str) -> str:
    allowed = legal_lane_targets(current)
    if nxt not in allowed:
        raise IllegalTransitionError(current, nxt, allowed)
    return nxt


# ── 退休判定 ──────────────────────────────────────────────────────
def should_retire(
    state: str,
    consecutive_failures: int,
    retire_after_failed_probes: int = DEFAULT_RETIRE_AFTER_FAILED_PROBES,
) -> bool:
    """连续失败是否已达退休线。

    起点限定在 STALE / DEAD：NEW / ACTIVE 必须先经 DEAD 或 STALE，
    RETIRED 已经退出，不再重复判。
    """
    _check_threshold(retire_after_failed_probes)
    return state in _RETIRE_FROM and consecutive_failures >= retire_after_failed_probes


# ── 决策函数 ──────────────────────────────────────────────────────
def _settle(current: str, nxt: str) -> str:
    """落到目标态：原地不动视为合法空转，其余必须走合法转移。"""
    return current if nxt == current else apply_transition(current, nxt)


def evaluate_node_state(
    current: str,
    *,
    source_seen: bool,
    probe_ok: bool | None,
    consecutive_failures: int,
    retire_after_failed_probes: int = DEFAULT_RETIRE_AFTER_FAILED_PROBES,
) -> str:
    """按「来源 + 体检」两路证据算出目标态；结果必然是一条合法转移。

    - probe_ok True  → source_seen ? ACTIVE : STALE（成功的体检是最强证据）
    - probe_ok False → 达线且当前为 STALE / DEAD 才 RETIRED，否则 DEAD
    - probe_ok None  → 只按来源判：source_seen 且当前 NEW / ACTIVE 才 ACTIVE
                       （避免未测节点从 DEAD / RETIRED 直接跃回运行权重）
    """
    _check_threshold(retire_after_failed_probes)

    if probe_ok is None:
        if source_seen:
            target = NODE_ACTIVE if current in {NODE_NEW, NODE_ACTIVE} else current
        else:
            target = current if current in {NODE_STALE, NODE_RETIRED} else NODE_STALE
        return _settle(current, target)

    if probe_ok:
        return _settle(current, NODE_ACTIVE if source_seen else NODE_STALE)

    if current == NODE_RETIRED:
        return current

    target = (
        NODE_RETIRED
        if consecutive_failures >= retire_after_failed_probes and current in _RETIRE_FROM
        else NODE_DEAD
    )
    return _settle(current, target)
