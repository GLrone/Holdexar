"""Run 级 lane 快照：active lanes ≤ 当前唯一出口数，且 run 内固定。

被测：`runtime.select_run_lanes` / `runtime.lane_run_plan`（生产唯一取值口）。

背景（真实链路发现）：内核的 listener 数是上一次重建时定下的，而出口身份（L1）之后
还会被刷新——两者之间存在重建窗口，窗口期内「lane 数」可能多于「当前唯一出口数」
（同一出口被两条 lane 各绑一个节点）。运行期必须以**当前快照**收敛，不能照搬台账。
"""
from __future__ import annotations

import pytest

from app.domains.proxypool import runtime as rt

MAX_CRAWL_WORKERS = 60


def _bindings(exit_by_node: dict[str, str]) -> list[dict]:
    """把「节点 → 出口」快照变成一份 lane 台账（每条 lane 绑一个节点）。"""
    return [
        {"lane": i, "url": f"http://127.0.0.1:{9000 + i}", "exitIp": exit_ip,
         "node": node}
        for i, (node, exit_ip) in enumerate(exit_by_node.items())
    ]


def _lanes(chosen: list[dict]) -> int:
    return len(chosen)


# ── 1. 不变量：active lanes ≤ 唯一出口数 ─────────────────────────
@pytest.mark.parametrize(
    ("nodes", "exits", "expected_lanes"),
    [
        # 33 个节点 / 33 个出口 → 33
        ({f"n{i}": f"10.0.0.{i}" for i in range(33)}, None, 33),
        # 58 个节点但只有 33 个出口 → 33（同一出口的第二条 lane 被剔除）
        ({f"n{i}": f"10.0.0.{i % 33}" for i in range(58)}, None, 33),
        # 60 个出口 → 60
        ({f"n{i}": f"10.0.0.{i}" for i in range(60)}, None, 60),
        # 72 个出口 → 封顶 60
        ({f"n{i}": f"10.0.0.{i}" for i in range(72)}, None, MAX_CRAWL_WORKERS),
        # 5 个出口 → 5
        ({f"n{i}": f"10.0.0.{i}" for i in range(5)}, None, 5),
    ],
)
def test_active_lanes_never_exceed_unique_exits(nodes, exits, expected_lanes) -> None:
    bindings = _bindings(nodes)
    chosen = rt.select_run_lanes(bindings, nodes, max_lanes=MAX_CRAWL_WORKERS)
    assert _lanes(chosen) == expected_lanes
    assert _lanes(chosen) <= len(set(nodes.values()))


def test_the_34th_lane_is_dropped_when_two_lanes_share_an_exit() -> None:
    """真实现象复现：34 条 lane / 33 个唯一出口 → 收敛到 33。"""
    # 34 条 lane，其中 lane-1 与 lane-2 绑的两个节点当前出口相同
    nodes = {f"n{i}": f"10.0.0.{i}" for i in range(34)}
    nodes["n2"] = nodes["n1"]  # 第二个节点落到同一出口
    bindings = _bindings(nodes)
    assert len(bindings) == 34
    assert len(set(nodes.values())) == 33

    chosen = rt.select_run_lanes(bindings, nodes, max_lanes=MAX_CRAWL_WORKERS)
    assert len(chosen) == 33, "同一出口不得占两条 lane"
    assert len({b["exitIp"] for b in chosen}) == 33
    assert [b["lane"] for b in chosen] == [i for i in range(34) if i != 2], (
        "保留序号最小的那条（确定性）"
    )


def test_lane_with_unknown_exit_is_not_selected() -> None:
    """绑到「当前出口未知」节点的 lane 不占工位（它没有容量依据）。"""
    nodes = {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3"}
    bindings = _bindings(nodes)
    snapshot = {"a": "10.0.0.1", "b": "10.0.0.2"}  # c 当前没有出口身份
    chosen = rt.select_run_lanes(bindings, snapshot, max_lanes=60)
    assert [b["node"] for b in chosen] == ["a", "b"]


# ── 2. worker 限制 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    ("exits", "config", "expected"),
    [(33, 30, 30), (33, 60, 33), (72, 60, 60), (5, 30, 5)],
)
def test_run_lanes_respect_configured_worker_limit(exits, config, expected) -> None:
    nodes = {f"n{i}": f"10.0.0.{i}" for i in range(exits)}
    chosen = rt.select_run_lanes(
        _bindings(nodes), nodes, max_lanes=min(config, MAX_CRAWL_WORKERS)
    )
    assert len(chosen) == expected
    workers = max(1, min(config, len(chosen), MAX_CRAWL_WORKERS))
    assert workers == expected


# ── 3. 兼容语义：快照缺失 / 为空时保持旧行为 ─────────────────────
def test_none_snapshot_keeps_legacy_behaviour() -> None:
    bindings = _bindings({f"n{i}": "" for i in range(58)})
    assert len(rt.select_run_lanes(bindings, None, max_lanes=60)) == 58


def test_empty_snapshot_falls_back_to_node_capacity() -> None:
    """一个出口都没探到：退回按节点计容量（兼容设计），不把 run 变成零入口。"""
    bindings = _bindings({f"n{i}": "" for i in range(58)})
    chosen = rt.select_run_lanes(bindings, {}, max_lanes=60)
    assert len(chosen) == 58
    assert all(b["exitIp"] in (None, "") for b in chosen)


def test_run_plan_keeps_lane_keys_when_exit_unknown(tmp_path) -> None:
    """没有出口身份时键退回 `lane:<序号>`（同 lane 共享预算）。

    入口就绪是 fail-closed 判据，所以这里占住真实端口当「listener 在听」。
    """
    import socket

    import yaml

    from app.domains.proxypool.pool import POOL_FILENAME

    path = tmp_path / "proxypool" / POOL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": f"n{i}", "type": "ss", "server": f"s{i}.example.net", "port": 1,
         "cipher": "aes-128-gcm", "password": "x"} for i in range(3)
    ]}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    rt.prepare_runtime_config(tmp_path)

    holders = []
    try:
        doc = yaml.safe_load(rt.runtime_config_path(tmp_path).read_text(encoding="utf-8"))
        for entry in doc["listeners"]:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(64)
            holders.append(sock)
            entry["port"] = int(sock.getsockname()[1])
        rt.runtime_config_path(tmp_path).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        plan = rt.lane_run_plan(tmp_path, exit_by_node={})
    finally:
        for sock in holders:
            sock.close()

    assert plan["exit_keys"] == ["lane:0", "lane:1", "lane:2"]
    assert plan["nodes"] == ["", "", ""]
    assert plan["known_exits"] == 0


# ── 4. Run 级快照：run 内固定，后台变化不影响本次 run ─────────────
def test_run_plan_is_a_snapshot_and_does_not_move_with_background_changes() -> None:
    """run 开始取一次 → 之后 ExitPool 变了，本次 run 的列表不变，下一次才变。"""
    before = {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3"}
    bindings = _bindings(before)
    run_plan = rt.select_run_lanes(bindings, before, max_lanes=60)
    snapshot_urls = [b["url"] for b in run_plan]

    # 后台维护：c 的出口变了、a 掉出池、d 新增出口
    after = {"b": "10.0.0.2", "c": "10.0.0.9", "d": "10.0.0.4"}
    next_plan = rt.select_run_lanes(bindings, after, max_lanes=60)

    assert [b["url"] for b in run_plan] == snapshot_urls, "已取的 run 列表是快照，不再变"
    assert [b["node"] for b in next_plan] == ["b", "c"], "下一次 run 才按新快照重选"


def test_run_plan_order_is_deterministic() -> None:
    nodes = {f"n{i}": f"10.0.0.{i}" for i in range(10)}
    bindings = _bindings(nodes)
    first = [b["lane"] for b in rt.select_run_lanes(bindings, nodes, max_lanes=60)]
    second = [
        b["lane"]
        for b in rt.select_run_lanes(list(reversed(bindings)), nodes, max_lanes=60)
    ]
    assert first == second == sorted(first), "输出恒按 lane 序号升序"