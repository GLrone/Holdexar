"""容量探测器的纯逻辑单测：稳定判据、串线分类、分位数、档位配置渲染。

不拉真内核（真内核链路由 `test_proxypool_runtime` 覆盖，实机容量以
`scripts/capacity_probe.py` 的报告为准）。
"""
from __future__ import annotations

import json

import pytest
import yaml

from app.domains.proxypool.capacity import (
    CapacityCriteria,
    LaneStat,
    RunMetrics,
    _render_tier_config,
    classify_lane_leaks,
    evaluate_run,
    percentile,
)

CRITERIA = CapacityCriteria()


def _healthy(lanes: int = 8, **overrides) -> RunMetrics:
    """全项达标的档位观测量（在此基础上逐项破坏做判定测试）。"""
    metrics = RunMetrics(
        lanes=lanes,
        started=True,
        listener_ready=lanes,
        startup_ms=900.0,
        kernel_alive=True,
        controller_ok=True,
        binding_ok=True,
        requests=lanes * 10,
        ok=lanes * 10,
        p50_ms=300.0,
        p95_ms=600.0,
        p99_ms=900.0,
        rss_mb_start=100.0,
        rss_mb_end=110.0,
    )
    for key, value in overrides.items():
        setattr(metrics, key, value)
    return metrics


# ── 分位数 ────────────────────────────────────────────────────────


def test_percentile_empty_and_single():
    assert percentile([], 0.95) is None
    assert percentile([42.0], 0.5) == 42.0
    assert percentile([42.0], 0.95) == 42.0


def test_percentile_interpolates():
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(values, 0.5) == pytest.approx(50.5)
    assert percentile(values, 0.95) == pytest.approx(95.05)
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0


# ── 稳定判据 ──────────────────────────────────────────────────────


def test_evaluate_pass_when_all_criteria_met():
    assert evaluate_run(_healthy(), expected_lanes=8, criteria=CRITERIA,
                        latency_reference_ms=None) == []


def test_evaluate_partial_listeners_fails():
    metrics = _healthy(listener_ready=6)
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert any("6/8" in r for r in reasons)


def test_evaluate_low_success_rate_fails():
    metrics = _healthy(ok=78)  # 80 请求成功 78 → 97.5% < 99%
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert any("成功率" in r for r in reasons)


def test_evaluate_excludes_rate_limited_from_denominator():
    metrics = _healthy(requests=800, ok=400, rate_limited=400)  # 分母 400 全成功
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert reasons == []


def test_evaluate_zero_requests_fails():
    metrics = _healthy(requests=0, ok=0)
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert any("未发出" in r for r in reasons)


def test_evaluate_binding_and_kernel_and_controller_rules():
    base = dict(latency_reference_ms=None)
    assert any("绑定" in r for r in evaluate_run(
        _healthy(binding_ok=False), expected_lanes=8, criteria=CRITERIA, **base))
    assert any("内核" in r for r in evaluate_run(
        _healthy(kernel_alive=False), expected_lanes=8, criteria=CRITERIA, **base))
    assert any("控制器" in r for r in evaluate_run(
        _healthy(controller_ok=False), expected_lanes=8, criteria=CRITERIA, **base))


def test_evaluate_hard_leak_fails():
    metrics = _healthy(hard_leak_lanes=[3, 5])
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert any("串线" in r for r in reasons)


def test_evaluate_latency_against_reference():
    def _eval(ref):
        return evaluate_run(_healthy(p95_ms=2500.0), expected_lanes=8,
                            criteria=CRITERIA, latency_reference_ms=ref)

    # 无基准不判延迟；有基准时 3×600+500=2300 < 2500 → 超限
    assert not any("p95" in r for r in _eval(None))
    assert any("p95" in r for r in _eval(600.0))
    assert not any("p95" in r for r in evaluate_run(
        _healthy(p95_ms=2000.0), expected_lanes=8, criteria=CRITERIA,
        latency_reference_ms=600.0))


def test_evaluate_rss_growth_ladder_and_soak_bounds():
    # ladder 界：start×1.5 + 64 = 214
    assert not any("内存" in r for r in evaluate_run(
        _healthy(rss_mb_start=100.0, rss_mb_end=200.0), expected_lanes=8,
        criteria=CRITERIA, latency_reference_ms=None))
    assert any("内存" in r for r in evaluate_run(
        _healthy(rss_mb_start=100.0, rss_mb_end=300.0), expected_lanes=8,
        criteria=CRITERIA, latency_reference_ms=None))
    # soak 界更紧：start×1.25 + 64 = 189
    assert any("内存" in r for r in evaluate_run(
        _healthy(phase="soak", rss_mb_start=100.0, rss_mb_end=190.0),
        expected_lanes=8, criteria=CRITERIA, latency_reference_ms=None))
    assert not any("内存" in r for r in evaluate_run(
        _healthy(phase="soak", rss_mb_start=100.0, rss_mb_end=180.0),
        expected_lanes=8, criteria=CRITERIA, latency_reference_ms=None))


def test_evaluate_not_started_short_circuits():
    metrics = _healthy(started=False)
    reasons = evaluate_run(metrics, expected_lanes=8, criteria=CRITERIA,
                           latency_reference_ms=None)
    assert reasons == ["内核进程未启动成功"]


# ── 串线分类 ──────────────────────────────────────────────────────


def test_classify_leaks_baseline_deviation_and_rotation():
    lane_ok = LaneStat(lane=0, port=1, baseline_ip="1.1.1.1",
                       ips={"1.1.1.1": 10})
    lane_flap = LaneStat(lane=1, port=2, baseline_ip="2.2.2.2",
                         ips={"2.2.2.2": 6, "9.9.9.9": 4})  # 40% 偏离：轮换但非硬串线
    lane_leak = LaneStat(lane=2, port=3, baseline_ip="3.3.3.3",
                         ips={"3.3.3.3": 2, "8.8.8.8": 8})  # 80% 偏离：硬串线
    lane_noip = LaneStat(lane=3, port=4)  # 无 IP 观测（steam 模式形态）不参与

    mismatch, rotating, hard = classify_lane_leaks(
        [lane_ok, lane_flap, lane_leak, lane_noip], ratio_threshold=0.5
    )
    assert mismatch == 12
    assert rotating == [1, 2]
    assert hard == [2]


def test_classify_leaks_single_flap_is_not_hard():
    lane = LaneStat(lane=0, port=1, baseline_ip="1.1.1.1",
                    ips={"1.1.1.1": 99, "9.9.9.9": 1})
    mismatch, rotating, hard = classify_lane_leaks([lane], ratio_threshold=0.5)
    assert mismatch == 1
    assert rotating == [0]
    assert hard == []


# ── 档位配置渲染 ──────────────────────────────────────────────────


_PROXIES = [
    {"name": "node-a", "type": "ss", "server": "10.0.0.1", "port": 8388,
     "cipher": "aes-128-gcm", "password": "x"},
    {"name": "node-b", "type": "ss", "server": "10.0.0.2", "port": 8388,
     "cipher": "aes-128-gcm", "password": "x"},
]


def _load_config(tmp_path: Path, lanes: int, direct: bool) -> dict:
    config = _render_tier_config(
        tmp_path, _PROXIES, lanes=lanes, direct_mode=direct
    )
    return yaml.safe_load(config.read_text(encoding="utf-8"))


def test_render_tier_config_lane_count_not_capped_by_production_limit(tmp_path):
    # lane 数是被测变量：超过生产 MAX_LANES(60) 的档位照样渲染
    doc = _load_config(tmp_path, lanes=100, direct=False)
    assert len(doc["proxy-groups"]) == 100
    assert len(doc["listeners"]) == 100
    assert doc["listeners"][63]["name"] == "lane-63-in"
    assert doc["listeners"][63]["proxy"] == "lane-63"
    assert doc["proxy-groups"][63]["proxies"] == ["node-a", "node-b"]


def test_render_tier_config_direct_mode_prepends_direct(tmp_path):
    doc = _load_config(tmp_path, lanes=2, direct=True)
    for group in doc["proxy-groups"]:
        assert group["proxies"][0] == "DIRECT"
        assert group["proxies"][1:] == ["node-a", "node-b"]


def test_render_tier_config_runtime_keys_and_unique_ports(tmp_path):
    doc = _load_config(tmp_path, lanes=4, direct=False)
    assert doc["mode"] == "global"
    assert doc["allow-lan"] is False
    assert doc["bind-address"] == "127.0.0.1"
    assert doc["external-controller"].startswith("127.0.0.1:")
    assert doc["secret"]
    ports = [entry["port"] for entry in doc["listeners"]]
    assert len(set(ports)) == len(ports)
    assert doc["mixed-port"] not in ports


# ── 报告 ─────────────────────────────────────────────────────────


def test_criteria_roundtrip():
    payload = CRITERIA.to_dict()
    assert payload["min_success_rate"] == 0.99
    assert CapacityCriteria(**payload) == CRITERIA


def test_metrics_roundtrip_via_json(tmp_path):
    metrics = _healthy()
    payload = json.dumps(metrics.to_dict(), ensure_ascii=False)
    restored = json.loads(payload)
    assert restored["listener_ready"] == 8
    assert restored["failure_reasons"] == []
