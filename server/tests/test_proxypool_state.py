"""proxypool 领域单元测试：节点身份计算 + 状态机。

纯领域测试：不起 Mihomo、不出网、不依赖爬虫。目的是把「什么是一个节点、
它有哪些状态、状态之间怎么走」钉死，后续所有模块都只能引用它。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.proxypool.models import make_runtime_name, node_fingerprint  # noqa: E402
from app.domains.proxypool.state import (  # noqa: E402
    DEFAULT_RETIRE_AFTER_FAILED_PROBES,
    LANE_DEAD,
    LANE_DEGRADED,
    LANE_EMPTY,
    LANE_INIT,
    LANE_READY,
    LANE_SWITCHING,
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_NEW,
    NODE_RETIRED,
    NODE_STALE,
    IllegalTransitionError,
    apply_lane_transition,
    apply_transition,
    can_transition,
    can_transition_lane,
    evaluate_node_state,
    legal_lane_targets,
    legal_targets,
    should_retire,
)

_BASE_CONFIG = {
    "type": "vmess",
    "name": "香港 01",
    "server": "hk01.example.net",
    "port": 443,
    "uuid": "11111111-2222-3333-4444-555555555555",
    "cipher": "auto",
    "udp": True,
}


# ── 节点身份：fingerprint ─────────────────────────────────────────
def test_fingerprint_stable_on_key_order() -> None:
    a = {"type": "trojan", "server": "s.example.net", "port": 443, "password": "p1"}
    b = {"password": "p1", "port": 443, "server": "s.example.net", "type": "trojan"}
    assert node_fingerprint(a) == node_fingerprint(b)


def test_fingerprint_ignores_consumer_switches() -> None:
    base = dict(_BASE_CONFIG)
    noisy = dict(
        base,
        udp=False, tfo=True, mptcp=True, uot=True, xudp=False,
        smux={"enabled": True}, **{"display-name": "来源默认名"},
    )
    assert node_fingerprint(noisy) == node_fingerprint(base)
    # 名字是被刻意忽略的一类：来源改名不算换节点
    assert node_fingerprint(dict(base, name="完全不同的名字")) == node_fingerprint(base)


@pytest.mark.parametrize(
    "mutate",
    [
        {"server": "hk02.example.net"},
        {"port": 8443},
        {"uuid": "99999999-2222-3333-4444-555555555555"},
    ],
)
def test_fingerprint_differs_on_connection_params(mutate: dict) -> None:
    assert node_fingerprint({**_BASE_CONFIG, **mutate}) != node_fingerprint(_BASE_CONFIG)


# ── 节点身份：runtime_name（防执行器按 name 静默去重） ────────────
def test_runtime_name_isolates_across_subscriptions() -> None:
    taken: set[str] = set()
    assert {make_runtime_name(1, "香港01", taken),
            make_runtime_name(2, "香港01", taken)} == {"1|香港01", "2|香港01"}


def test_runtime_name_dedups_within_subscription() -> None:
    taken: set[str] = set()
    names = []
    for _ in range(3):
        name = make_runtime_name(7, "香港01", taken)
        names.append(name)
        taken.add(name)
    assert names == ["7|香港01", "7|香港01#2", "7|香港01#3"]
    assert len(set(names)) == 3


def test_runtime_name_is_stable() -> None:
    """结果只取决于已占用集合，与集合的遍历顺序无关。"""
    expected = make_runtime_name(1, "香港03", {"1|香港01", "1|香港02"})
    for _ in range(3):
        assert make_runtime_name(1, "香港03", {"1|香港02", "1|香港01"}) == expected
    assert make_runtime_name(1, "香港01", {"1|香港01", "1|香港01#2"}) == "1|香港01#3"


# ── 节点状态转移 ──────────────────────────────────────────────────
_LEGAL_NODE = [
    (NODE_NEW, NODE_ACTIVE), (NODE_NEW, NODE_DEAD), (NODE_NEW, NODE_STALE),
    (NODE_ACTIVE, NODE_STALE), (NODE_ACTIVE, NODE_DEAD),
    (NODE_STALE, NODE_ACTIVE), (NODE_STALE, NODE_DEAD), (NODE_STALE, NODE_RETIRED),
    (NODE_DEAD, NODE_ACTIVE), (NODE_DEAD, NODE_STALE), (NODE_DEAD, NODE_RETIRED),
    (NODE_RETIRED, NODE_ACTIVE), (NODE_RETIRED, NODE_STALE),
]


@pytest.mark.parametrize("current,nxt", _LEGAL_NODE)
def test_all_legal_node_transitions(current: str, nxt: str) -> None:
    assert can_transition(current, nxt)
    assert apply_transition(current, nxt) == nxt


@pytest.mark.parametrize(
    "current,nxt",
    [
        (NODE_ACTIVE, NODE_RETIRED),   # 必须先经 STALE 或 DEAD
        (NODE_NEW, NODE_RETIRED),
        (NODE_RETIRED, NODE_DEAD),     # 退休节点重新评估后直接进 ACTIVE / STALE
        (NODE_ACTIVE, NODE_NEW),
        (NODE_STALE, NODE_NEW),
        (NODE_ACTIVE, NODE_ACTIVE),
        ("NOSUCHSTATE", NODE_ACTIVE),
    ],
)
def test_illegal_node_transitions_raise(current: str, nxt: str) -> None:
    assert not can_transition(current, nxt)
    with pytest.raises(IllegalTransitionError) as exc:
        apply_transition(current, nxt)
    err = exc.value
    assert (err.current, err.target) == (current, nxt)
    assert err.allowed == legal_targets(current)


# ── Lane 状态转移 ─────────────────────────────────────────────────
_LEGAL_LANE = [
    (LANE_EMPTY, LANE_INIT),
    (LANE_INIT, LANE_READY), (LANE_INIT, LANE_EMPTY),
    (LANE_READY, LANE_SWITCHING), (LANE_READY, LANE_DEGRADED),
    (LANE_READY, LANE_DEAD), (LANE_READY, LANE_EMPTY),
    (LANE_SWITCHING, LANE_READY), (LANE_SWITCHING, LANE_DEGRADED),
    (LANE_SWITCHING, LANE_DEAD),
    (LANE_DEGRADED, LANE_READY), (LANE_DEGRADED, LANE_SWITCHING),
    (LANE_DEGRADED, LANE_DEAD), (LANE_DEGRADED, LANE_EMPTY),
    (LANE_DEAD, LANE_EMPTY), (LANE_DEAD, LANE_INIT), (LANE_DEAD, LANE_READY),
]


@pytest.mark.parametrize("current,nxt", _LEGAL_LANE)
def test_lane_transitions(current: str, nxt: str) -> None:
    assert can_transition_lane(current, nxt)
    assert apply_lane_transition(current, nxt) == nxt


@pytest.mark.parametrize(
    "current,nxt",
    [
        (LANE_EMPTY, LANE_READY), (LANE_INIT, LANE_DEAD),
        (LANE_SWITCHING, LANE_EMPTY), (LANE_DEAD, LANE_DEGRADED),
        (LANE_EMPTY, LANE_EMPTY),
    ],
)
def test_lane_illegal_transitions_raise(current: str, nxt: str) -> None:
    assert not can_transition_lane(current, nxt)
    with pytest.raises(IllegalTransitionError) as exc:
        apply_lane_transition(current, nxt)
    assert exc.value.allowed == legal_lane_targets(current)


def test_node_dead_and_lane_dead_have_different_targets() -> None:
    """两个域共用 DEAD 字样但语义不同 —— 这是 Lane 单独一张表的理由。"""
    assert legal_targets(NODE_DEAD) == frozenset({NODE_ACTIVE, NODE_STALE, NODE_RETIRED})
    assert legal_lane_targets(LANE_DEAD) == frozenset({LANE_EMPTY, LANE_INIT, LANE_READY})


# ── 退休语义 ──────────────────────────────────────────────────────
def test_stale_retires_after_threshold() -> None:
    below = DEFAULT_RETIRE_AFTER_FAILED_PROBES - 1
    assert not should_retire(NODE_STALE, below)
    assert should_retire(NODE_STALE, DEFAULT_RETIRE_AFTER_FAILED_PROBES)
    assert evaluate_node_state(
        NODE_STALE, source_seen=False, probe_ok=False, consecutive_failures=below
    ) == NODE_DEAD
    assert evaluate_node_state(
        NODE_STALE, source_seen=False, probe_ok=False,
        consecutive_failures=DEFAULT_RETIRE_AFTER_FAILED_PROBES,
    ) == NODE_RETIRED


def test_retire_threshold_is_configurable() -> None:
    """阈值必须是入参：传 5 时 3 次连续失败不许退休（证明不是写死常量）。"""
    assert not should_retire(NODE_DEAD, 3, retire_after_failed_probes=5)
    assert should_retire(NODE_DEAD, 5, retire_after_failed_probes=5)
    assert evaluate_node_state(
        NODE_STALE, source_seen=False, probe_ok=False,
        consecutive_failures=3, retire_after_failed_probes=5,
    ) == NODE_DEAD
    assert evaluate_node_state(
        NODE_STALE, source_seen=False, probe_ok=False,
        consecutive_failures=5, retire_after_failed_probes=5,
    ) == NODE_RETIRED


def test_retired_recovery_paths() -> None:
    assert can_transition(NODE_RETIRED, NODE_ACTIVE)
    assert can_transition(NODE_RETIRED, NODE_STALE)
    assert not can_transition(NODE_RETIRED, NODE_DEAD)
    assert evaluate_node_state(
        NODE_RETIRED, source_seen=True, probe_ok=True, consecutive_failures=9
    ) == NODE_ACTIVE
    assert evaluate_node_state(
        NODE_RETIRED, source_seen=False, probe_ok=True, consecutive_failures=9
    ) == NODE_STALE
    # 没拿到成功证据就不许擅自回池
    assert evaluate_node_state(
        NODE_RETIRED, source_seen=True, probe_ok=None, consecutive_failures=9
    ) == NODE_RETIRED


def test_dead_is_not_retired() -> None:
    """DEAD 是「暂时不通」，不等于退休：可自由复活，也不被失败计数误判。"""
    assert not should_retire(NODE_DEAD, 1)
    assert evaluate_node_state(
        NODE_DEAD, source_seen=True, probe_ok=False, consecutive_failures=1
    ) == NODE_DEAD
    assert evaluate_node_state(
        NODE_DEAD, source_seen=True, probe_ok=True, consecutive_failures=9
    ) == NODE_ACTIVE
    # ACTIVE 临时失败只到 DEAD，绝不直跳 RETIRED
    assert evaluate_node_state(
        NODE_ACTIVE, source_seen=True, probe_ok=False, consecutive_failures=99
    ) == NODE_DEAD


# ── 决策矩阵 ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "current,source_seen,probe_ok,failures,expected",
    [
        # probe_ok=True：成功体检是最强证据，只看来源决定 ACTIVE / STALE
        (NODE_NEW, True, True, 0, NODE_ACTIVE),
        (NODE_NEW, False, True, 0, NODE_STALE),
        (NODE_ACTIVE, True, True, 0, NODE_ACTIVE),
        (NODE_ACTIVE, False, True, 0, NODE_STALE),
        (NODE_STALE, True, True, 9, NODE_ACTIVE),
        (NODE_STALE, False, True, 9, NODE_STALE),
        (NODE_DEAD, True, True, 9, NODE_ACTIVE),
        (NODE_DEAD, False, True, 9, NODE_STALE),
        (NODE_RETIRED, True, True, 9, NODE_ACTIVE),
        (NODE_RETIRED, False, True, 9, NODE_STALE),
        # probe_ok=False：达线且已离开运行集才退休，其余一律 DEAD
        (NODE_NEW, True, False, 9, NODE_DEAD),
        (NODE_ACTIVE, True, False, 9, NODE_DEAD),
        (NODE_STALE, True, False, 2, NODE_DEAD),
        (NODE_STALE, True, False, 3, NODE_RETIRED),
        (NODE_DEAD, False, False, 3, NODE_RETIRED),
        (NODE_RETIRED, False, False, 9, NODE_RETIRED),
        # probe_ok=None：只按来源判。未测不得把 DEAD / RETIRED 拉回 ACTIVE；
        # 无来源又无体检证据（NEW / DEAD）一律 DEAD——STALE 是池成员，其语义是
        # 「订阅已不再返回 **但 仍通过健康验证**」，DEAD 从未通过体检。
        (NODE_NEW, True, None, 0, NODE_ACTIVE),
        (NODE_ACTIVE, True, None, 0, NODE_ACTIVE),
        (NODE_ACTIVE, False, None, 0, NODE_STALE),
        (NODE_NEW, False, None, 0, NODE_DEAD),
        (NODE_STALE, True, None, 0, NODE_STALE),
        (NODE_STALE, False, None, 0, NODE_STALE),
        (NODE_DEAD, True, None, 0, NODE_DEAD),
        (NODE_DEAD, False, None, 0, NODE_DEAD),
        (NODE_RETIRED, True, None, 0, NODE_RETIRED),
        (NODE_RETIRED, False, None, 0, NODE_RETIRED),
    ],
)
def test_evaluate_node_state_matrix(
    current: str, source_seen: bool, probe_ok: bool | None,
    failures: int, expected: str,
) -> None:
    got = evaluate_node_state(
        current, source_seen=source_seen, probe_ok=probe_ok, consecutive_failures=failures
    )
    assert got == expected
    # 决策函数的输出必须是合法转移（或原地空转）——不许绕过状态机
    if got != current:
        assert can_transition(current, got)


def test_evaluate_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        should_retire(NODE_STALE, 3, retire_after_failed_probes=0)
    with pytest.raises(ValueError):
        evaluate_node_state(
            NODE_STALE, source_seen=False, probe_ok=False,
            consecutive_failures=3, retire_after_failed_probes=0,
        )


def test_dead_node_losing_its_last_source_stays_out_of_the_pool() -> None:
    """失去最后一个来源，不得把一个不可用节点"升级"进池。

    池成员 = NEW / ACTIVE / STALE。STALE 的语义是「订阅已不再返回 **但 仍通过
    健康验证**」——它是"降权仍服务"，不是"来源没了"。若"来源消失"这一路把 DEAD
    判成 STALE，则一个已被体检判定不可用的节点，**只因订阅不再提供它**就回到
    流量池；NEW（从未体检）同理会被判成"历史上仍可工作"。
    """
    lost_source = dict(source_seen=False, probe_ok=None, consecutive_failures=0)
    # 无来源 + 无体检证据：不得进池
    assert evaluate_node_state(NODE_DEAD, **lost_source) == NODE_DEAD
    assert evaluate_node_state(NODE_NEW, **lost_source) == NODE_DEAD
    # 曾经通过体检的 ACTIVE / STALE 失去来源 → 降权保留（STALE 仍可服务）
    assert evaluate_node_state(NODE_ACTIVE, **lost_source) == NODE_STALE
    assert evaluate_node_state(NODE_STALE, **lost_source) == NODE_STALE
    # 有来源也不能把 DEAD 拉回运行权重（既有语义，一并锁住）
    assert evaluate_node_state(
        NODE_DEAD, source_seen=True, probe_ok=None, consecutive_failures=0
    ) == NODE_DEAD
