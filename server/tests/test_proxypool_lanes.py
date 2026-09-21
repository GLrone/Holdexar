"""多入口（lane）：运行配置生成、入口就绪判定、lane 绑定与按入口分账。

被测：
- `runtime.prepare_runtime_config` 生成的 `proxy-groups` / `listeners` 形状；
- `runtime.lane_count_for` / `is_lane_group` / `runtime_lane_ports` / `lane_proxy_urls`；
- `runtime.plan_lane_assignment` 的分配不变量（同节点不占两条 lane、优先沿用上次绑定）；
- `runtime.reconcile` 不把运行期 lane 组当成池外多余节点；
- `rate_limit.LaneRateLimits` 与 `http_client.LaneBreakers` 的按入口分账；
- `crawler.runner.build_worker_clients` 的 worker → 入口绑定。

除最后一个真内核用例（无内核资产时整组跳过）外全部不依赖网络与内核。
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from app.crawler.http_client import ExitBreakers, global_429_breaker
from app.crawler.rate_limit import ExitRateLimits, steam_rate_limiter
from app.crawler.runner import CrawlRunConfig, build_worker_clients
from app.domains.proxypool import runtime as rt
from app.domains.proxypool.pool import POOL_FILENAME


def _write_pool(data_dir: Path, names: list[str]) -> Path:
    path = Path(data_dir) / "proxypool" / POOL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"proxies": [{"name": n, "type": "ss", "server": f"{n}.example.net",
                          "port": 1, "cipher": "aes-128-gcm", "password": "x"}
                         for n in names]},
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _holding_port() -> tuple[socket.socket, int]:
    """占住一个本机端口当「正在监听的入口」。

    队列开大一些：就绪判定是 TCP connect 探活，连上就断，不 accept；backlog 只有 1
    时第二次探活会被拒，测出来的是「探过就不可用」这个假象。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    return sock, int(sock.getsockname()[1])


# ── 1. 运行配置形状 ──────────────────────────────────────────────
def test_runtime_config_emits_one_group_and_listener_per_lane(tmp_path) -> None:
    _write_pool(tmp_path, ["a", "b", "c"])
    out = rt.prepare_runtime_config(tmp_path)
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))

    assert doc["mode"] == "global", "GLOBAL 仍是维护入口，不改模式"
    assert isinstance(doc["mixed-port"], int) and doc["mixed-port"] > 0
    assert doc["bind-address"] == "127.0.0.1"
    assert [g["name"] for g in doc["proxy-groups"]] == ["lane-0", "lane-1", "lane-2"]
    for group in doc["proxy-groups"]:
        assert group["type"] == "select", "确定性调度必须用 select 组，不是 url-test/load-balance"
        assert group["proxies"] == ["a", "b", "c"], "每条 lane 的候选集都是池内全部节点"

    assert [lis["proxy"] for lis in doc["listeners"]] == ["lane-0", "lane-1", "lane-2"]
    assert [lis["name"] for lis in doc["listeners"]] == [
        "lane-0-in", "lane-1-in", "lane-2-in",
    ]
    assert all(lis["type"] == "mixed" and lis["listen"] == "127.0.0.1"
               for lis in doc["listeners"])
    ports = [lis["port"] for lis in doc["listeners"]]
    assert len(set(ports)) == 3 and all(p > 0 for p in ports)

    assert doc["external-controller"] not in ("", None)
    assert doc["secret"]


def test_runtime_config_lane_count_follows_pool_size(tmp_path) -> None:
    _write_pool(tmp_path, ["a", "b"])
    doc = yaml.safe_load(rt.prepare_runtime_config(tmp_path).read_text(encoding="utf-8"))
    assert len(doc["listeners"]) == 2, "一个节点一条 lane，不虚开工位"

    _write_pool(tmp_path, [f"n{i}" for i in range(rt.MAX_LANES + 20)])
    doc = yaml.safe_load(rt.prepare_runtime_config(tmp_path).read_text(encoding="utf-8"))
    assert len(doc["listeners"]) == rt.MAX_LANES, "lane 数封顶 MAX_LANES"

    _write_pool(tmp_path, ["a", "b", "c"])
    doc = yaml.safe_load(
        rt.prepare_runtime_config(tmp_path, lanes=2).read_text(encoding="utf-8")
    )
    assert len(doc["listeners"]) == 2, "显式 lane 数优先（且不超过池规模）"


@pytest.mark.parametrize(
    ("pool_size", "requested", "expected"),
    [
        (0, None, 0),
        (1, None, 1),
        (3, None, 3),
        (100, None, rt.MAX_LANES),
        (100, 4, 4),
        (2, 9, 2),
        (3, 0, 0),
    ],
)
def test_lane_count_for(pool_size, requested, expected) -> None:
    assert rt.lane_count_for(pool_size, requested) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("lane-0", True), ("lane-12", True), ("lane-", False), ("lane-0-in", False),
     ("1|node", False), ("GLOBAL", False)],
)
def test_is_lane_group(name, expected) -> None:
    assert rt.is_lane_group(name) is expected


# ── 2. 入口就绪判定（fail closed）────────────────────────────────
def test_lane_ports_read_back_in_order(tmp_path) -> None:
    _write_pool(tmp_path, ["a", "b"])
    rt.prepare_runtime_config(tmp_path)
    doc = yaml.safe_load(rt.runtime_config_path(tmp_path).read_text(encoding="utf-8"))
    expected = tuple(lis["port"] for lis in doc["listeners"])
    assert rt.runtime_lane_ports(tmp_path) == expected


def test_lane_ports_empty_without_config(tmp_path) -> None:
    assert rt.runtime_lane_ports(tmp_path) == ()
    assert rt.lane_proxy_urls(tmp_path) == []


def test_lane_proxy_urls_all_or_nothing(tmp_path) -> None:
    """有 lane 但入口没全就绪 → 空列表；全部在听 → 全给。"""
    _write_pool(tmp_path, ["a", "b"])
    rt.prepare_runtime_config(tmp_path)
    assert rt.lane_proxy_urls(tmp_path) == [], "端口还没在听时不得交出地址"

    h0, p0 = _holding_port()
    h1, p1 = _holding_port()
    try:
        # 只让第一条 lane 就绪：部分就绪必须整体判不可用（否则 worker 绑定错位）
        _patch_lane_port(tmp_path, 0, p0)
        assert rt.lane_proxy_urls(tmp_path) == []
        _patch_lane_port(tmp_path, 1, p1)
        assert rt.lane_proxy_urls(tmp_path) == [
            f"http://127.0.0.1:{p0}", f"http://127.0.0.1:{p1}",
        ]
    finally:
        h0.close()
        h1.close()


def _patch_lane_port(data_dir: Path, index: int, port: int) -> None:
    doc = yaml.safe_load(rt.runtime_config_path(data_dir).read_text(encoding="utf-8"))
    doc["listeners"][index]["port"] = port
    rt.runtime_config_path(data_dir).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_require_lane_proxy_urls_fails_closed_when_lanes_not_ready(tmp_path) -> None:
    _write_pool(tmp_path, ["a", "b"])
    rt.prepare_runtime_config(tmp_path)
    with pytest.raises(rt.RuntimeUnavailableError):
        rt.require_lane_proxy_urls(tmp_path)


def test_require_lane_proxy_urls_falls_back_to_global_without_lanes(tmp_path) -> None:
    """旧产物（运行配置里没有 lane）：退回同一 Runtime 的 GLOBAL 入口，而不是报错。"""
    _write_pool(tmp_path, ["a"])
    rt.prepare_runtime_config(tmp_path)
    doc = yaml.safe_load(rt.runtime_config_path(tmp_path).read_text(encoding="utf-8"))
    doc.pop("listeners")
    rt.runtime_config_path(tmp_path).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    sock, held = _holding_port()
    try:
        doc["mixed-port"] = held
        rt.runtime_config_path(tmp_path).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        assert rt.require_lane_proxy_urls(tmp_path) == [f"http://127.0.0.1:{held}"]
    finally:
        sock.close()


# ── 3. lane 绑定 ─────────────────────────────────────────────────
def test_plan_lane_assignment_keeps_previous_and_never_duplicates() -> None:
    plan = rt.plan_lane_assignment(["b", "a"], ["a", "b", "c", "d"], lanes=2)
    assert plan == ["b", "a"], "上次绑定仍在可用集里就原样沿用"


def test_plan_lane_assignment_fills_all_lanes_on_first_start() -> None:
    """首次启动没有上次绑定：**仍必须把每条 lane 都分配出去**。

    按「上次绑定」定长度会让首启一条都不分，各 lane 全留在组内默认项上——所有
    lane 走同一个节点，多入口静默退化成单出口。
    """
    plan = rt.plan_lane_assignment([], ["a", "b", "c"], lanes=3)
    assert plan == ["a", "b", "c"]


def test_plan_lane_assignment_length_is_lane_count_not_previous() -> None:
    for previous in ([], [None], ["a", "b"], ["a", "b", "c", "d"]):
        assert len(rt.plan_lane_assignment(previous, ["x", "y"], lanes=3)) == 3


def test_plan_lane_assignment_refills_after_node_left_pool() -> None:
    plan = rt.plan_lane_assignment(["gone", "a"], ["a", "b", "c"], lanes=2)
    assert plan == ["b", "a"], "出池的节点让位，且不与已占用的 a 重复"


def test_plan_lane_assignment_no_duplicates_when_short_of_nodes() -> None:
    plan = rt.plan_lane_assignment([None, None, None], ["a"], lanes=3)
    assert plan == ["a", None, None], "可用节点不够时尾部留空，不重复绑同一出口"


def test_plan_lane_assignment_drops_duplicate_previous() -> None:
    """上次两条 lane 绑了同一节点（异常状态）：本次只保留一条。"""
    plan = rt.plan_lane_assignment(["a", "a"], ["a", "b"], lanes=2)
    assert plan == ["a", "b"]


def test_plan_lane_assignment_empty_inputs() -> None:
    assert rt.plan_lane_assignment([], ["a", "b"], lanes=0) == []
    assert rt.plan_lane_assignment([None, None], [], lanes=2) == [None, None]


# ── 4. 对账不把运行期 lane 组当池外节点 ──────────────────────────
def test_reconcile_ignores_runtime_lane_groups() -> None:
    ledger = rt.reconcile(
        registry_names={"1|a", "1|b"},
        pool_names={"1|a", "1|b"},
        observed_names={"1|a", "1|b", "DIRECT", "REJECT", "GLOBAL", "lane-0", "lane-1"},
    )
    assert ledger.ok and not ledger.unexpected


def test_reconcile_still_flags_real_extra_nodes() -> None:
    ledger = rt.reconcile(
        registry_names={"1|a"},
        pool_names={"1|a"},
        observed_names={"1|a", "lane-0", "1|zzz"},
    )
    assert not ledger.ok and ledger.unexpected == {"1|zzz"}


# ── 5. 按出口分账：限流与熔断 ────────────────────────────────────
def test_exit_rate_limits_one_budget_per_exit() -> None:
    limits = ExitRateLimits(["1.1.1.1", "2.2.2.2", "3.3.3.3"])
    assert limits.exit_count == 3
    assert limits.per_exit == 200, "每个出口各自拥有一份生产预算（不是按出口数均分）"
    assert limits.for_exit("1.1.1.1") is not limits.for_exit("2.2.2.2")
    assert limits.for_exit("1.1.1.1") is limits.for_exit("1.1.1.1")


def test_exit_rate_limits_same_exit_shares_budget() -> None:
    """同一出口出现两次（同出口放两个 worker）只得到一份预算，不放大额度。"""
    limits = ExitRateLimits(["1.1.1.1", "1.1.1.1", "2.2.2.2"])
    assert limits.exit_count == 2
    assert limits.for_exit("1.1.1.1") is limits.for_exit("1.1.1.1")


def test_exit_rate_limits_zero_exits_uses_process_singleton() -> None:
    limits = ExitRateLimits([])
    assert limits.exit_count == 0
    assert limits.for_exit(None) is steam_rate_limiter, "单入口形态行为不变"


def test_exit_breakers_are_per_exit() -> None:
    breakers = ExitBreakers(["1.1.1.1", "2.2.2.2"])
    assert breakers.exit_count == 2
    assert breakers.for_exit("1.1.1.1") is not breakers.for_exit("2.2.2.2")
    assert breakers.for_exit("1.1.1.1") is breakers.for_exit("1.1.1.1")
    assert ExitBreakers([]).for_exit(None) is global_429_breaker


# ── 6. worker → 出口绑定 ─────────────────────────────────────────
def _dummy_client():
    from app.crawler.http_client import SteamHttpClient

    return SteamHttpClient(timeout=1, max_retries=1)


def test_build_worker_clients_binds_one_lane_per_worker() -> None:
    urls = [f"http://127.0.0.1:{9000 + i}" for i in range(3)]
    keys = ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
    factory = build_worker_clients(
        CrawlRunConfig(proxy_urls=urls, exit_keys=keys), _dummy_client()
    )
    assert factory is not None
    clients = [factory(i) for i in range(6)]
    assert [c.proxy_url for c in clients] == urls + urls, "按序号回绕绑定"
    assert [c.exit_key for c in clients] == keys + keys

    # 同一个出口上的 worker 共享同一份预算与熔断器（不会多开一份额度）
    assert clients[0].rate_limiter is clients[3].rate_limiter
    assert clients[0].breaker is clients[3].breaker
    # 不同出口各自独立
    assert clients[0].rate_limiter is not clients[1].rate_limiter
    assert clients[0].breaker is not clients[1].breaker


def test_build_worker_clients_shares_budget_within_same_exit() -> None:
    """一个出口放两个 worker：预算只有一份（上限不被 worker 数放大）。"""
    urls = ["http://127.0.0.1:9001", "http://127.0.0.1:9002"]
    factory = build_worker_clients(
        CrawlRunConfig(proxy_urls=urls, exit_keys=["9.9.9.9", "9.9.9.9"]),
        _dummy_client(),
    )
    a, b = factory(0), factory(1)
    assert a.rate_limiter is b.rate_limiter
    assert a.breaker is b.breaker


def test_build_worker_clients_none_for_single_entry() -> None:
    assert build_worker_clients(CrawlRunConfig(), _dummy_client()) is None
    assert build_worker_clients(
        CrawlRunConfig(proxy_urls=[]), _dummy_client()
    ) is None


def test_build_worker_clients_single_lane_keeps_total_budget() -> None:
    factory = build_worker_clients(
        CrawlRunConfig(proxy_urls=["http://127.0.0.1:9001"], exit_keys=["1.1.1.1"]),
        _dummy_client(),
    )
    client = factory(0)
    assert client.rate_limiter.max_requests == 200, "一条出口时预算仍是 200/5min"


def test_build_worker_clients_without_exit_keys_falls_back_to_singleton() -> None:
    """没有出口身份（旧调用方）时不编造 key，退回进程级单例。"""
    factory = build_worker_clients(
        CrawlRunConfig(proxy_urls=["http://127.0.0.1:9001"]), _dummy_client()
    )
    client = factory(0)
    assert client.exit_key is None
    assert client.rate_limiter is steam_rate_limiter