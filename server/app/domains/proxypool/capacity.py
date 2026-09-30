"""池内核容量探测器：阶梯压测「本机 mihomo + lane 架构能稳定承载多少条 lane」。

探测器用**与生产完全相同的 Runtime 架构**（每条 lane = 一个 `lane-<i>` select 组 +
一个 mixed listener，listener 的 `proxy` 指向该组），对每个档位 N：

1. 独立临时目录渲染 N lane 配置，拉起**自己的**内核实例（与生产内核互不触碰，
   配置路径隔离，`ClashRuntime._kill_orphans` 只按本档配置路径清理）；
2. 等全部 listener 监听就绪（部分就绪即该档失败）；
3. 逐 lane 经控制器绑定节点（`assign_lanes` 回读确认）；
4. 全部 lane 并行、每条 lane 串行地发请求（生产 worker 与 lane 一对一同构），
   采集成功率 / 超时 / 串出口 / 延迟分位 / 内核 CPU 与内存；
5. 按稳定判据（`CapacityCriteria` + `evaluate_run`）判档，梯度爬升，
   连续两档失败即停，最后对通过档做持续负载 soak 确认。

产出 `CapacityReport`：理论容量（listener 全就绪的最高档）、验证容量
（ladder + soak 双过的最高档）、建议生产上限（验证容量 × 安全系数）。
报告为 JSON，落在数据目录 `proxypool/capacity/` 下，不入库。

三种模式（`mode`）：
- `nodes`：lane 绑出口槽代表节点，目标为出口 IP 回显——测生产同构链路的容量，
  并按「lane 的出口 IP 必须与自己首次观测一致」判串出口；
- `direct`：lane 全部绑 DIRECT（lane 组成员里前置 DIRECT）——排除代理链路，
  测内核与本机资源的裸容量；
- `steam`：lane 绑出口槽，目标为 Steam 商店轻量端点——真实业务面的末轮验证，
  单独发起 429 计数（限流属服务端行为，不计入内核容量成败）。

本模块的请求并发由探测器自驱，不走统一 worker 池：并发数 N 正是被测
变量，接入 worker 池会把并发钳在生产上限以内，测量即失效。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import httpx
import yaml

from app.domains.proxies.clash_manager import ClashRuntime, kernel_version
from app.domains.proxypool.health import EXIT_IP_TARGET_URL
from app.domains.proxypool.pool import POOL_FILENAME
from app.domains.proxypool.runtime import (
    LANE_GROUP_PREFIX,
    RUNTIME_BIND_ADDRESS,
    RUNTIME_CONFIG_FILENAME,
    RUNTIME_CONTROLLER_HOST,
    RUNTIME_CONTROLLER_SECRET,
    RUNTIME_MODE,
    _free_local_port,
    _port_listening,
    assign_lanes,
    controller_endpoint_of,
    plan_lane_assignment,
    runtime_lane_ports,
)

MODE_NODES = "nodes"
MODE_DIRECT = "direct"
MODE_STEAM = "steam"
MODES = (MODE_NODES, MODE_DIRECT, MODE_STEAM)

# `steam` 模式的业务目标：商店公开轻量端点（单 appid 价格字段），够判定 HTTP 可用性。
STEAM_TARGET_URL = (
    "https://store.steampowered.com/api/appdetails"
    "?appids=220&filters=price_overview"
)

DEFAULT_LADDER: tuple[int, ...] = (8, 16, 32, 48, 64, 80, 96, 128, 160, 192, 256, 320)

REPORT_SCHEMA = "proxypool-capacity-report/1"


@dataclass(frozen=True)
class CapacityCriteria:
    """稳定上限判定阈值。ladder 与 soak 共用同一套，soak 另加漂移界。"""

    # listener 全部就绪的等待上限（就绪不满即该档失败）
    ready_timeout_s: float = 20.0
    # 单请求超时（连接/读/写同值）
    request_timeout_s: float = 10.0
    # 成功率下限（分母剔除 429：限流是服务端行为，不是内核容量信号）
    min_success_rate: float = 0.99
    # 延迟退化界：p95 不得超过 基准p95 × latency_factor + latency_floor_ms
    latency_factor: float = 3.0
    latency_floor_ms: float = 500.0
    # 单档内内存增长界：结束 ≤ 开始 × rss_growth_factor + rss_growth_floor_mb
    rss_growth_factor: float = 1.5
    rss_growth_floor_mb: float = 64.0
    # 串出口判据：单 lane 偏离自身基线出口 IP 的占比超过该值即记硬串线
    hard_leak_ratio: float = 0.5
    # soak 延迟界：p95 不得超过该档 ladder p95 × soak_p95_factor + latency_floor_ms
    soak_p95_factor: float = 1.5
    # soak 内存漂移界：结束 ≤ 开始 × soak_rss_factor + rss_growth_floor_mb
    soak_rss_factor: float = 1.25
    # 建议生产上限 = 验证容量 × safety_factor
    safety_factor: float = 0.8

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LaneStat:
    """单 lane 的请求累计。`ips` 只在出口 IP 回显模式下填充。"""

    lane: int
    port: int
    node: str | None = None
    requests: int = 0
    ok: int = 0
    timeouts: int = 0
    connect_errors: int = 0
    rate_limited: int = 0
    http_errors: int = 0
    other_errors: int = 0
    baseline_ip: str | None = None
    ips: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)


@dataclass
class RunMetrics:
    """一档（ladder 或 soak）的全部观测量与判定结果。"""

    lanes: int
    phase: str = "ladder"
    started: bool = False
    listener_ready: int = 0
    startup_ms: float | None = None
    kernel_pid: int | None = None
    kernel_alive: bool = False
    kernel_restarted: bool = False
    controller_ok: bool = False
    binding_ok: bool = False
    unbound_lanes: int = 0
    duration_s: float = 0.0
    requests: int = 0
    ok: int = 0
    timeouts: int = 0
    connect_errors: int = 0
    rate_limited: int = 0
    http_errors: int = 0
    other_errors: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    cpu_pct_avg: float | None = None
    cpu_pct_max: float | None = None
    rss_mb_start: float | None = None
    rss_mb_end: float | None = None
    rss_mb_max: float | None = None
    mismatch_count: int = 0
    rotating_lanes: list[int] = field(default_factory=list)
    hard_leak_lanes: list[int] = field(default_factory=list)
    # 有失败或出口异常的 lane 明细（归因用：区分「个别坏出口」与「系统性过载」）
    suspect_lanes: list[dict] = field(default_factory=list)
    passed: bool | None = None
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def percentile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数；`q` 取 0~1，空序列返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


def classify_lane_leaks(
    lane_stats: Sequence[LaneStat], *, ratio_threshold: float
) -> tuple[int, list[int], list[int]]:
    """按 lane 的出口 IP 观测判串线：返回 (偏离请求数, 出口轮换 lane, 硬串线 lane)。

    基线 = 该 lane 首次成功观测的出口 IP；后续观测偏离基线即记一次偏离。
    出口轮换（同 lane 观测到 ≥2 个 IP）单列——轮换可能是落地侧行为，硬串线
    （偏离占比超过 `ratio_threshold`）才是「流量走到别的 lane 的出口」的信号。
    """
    mismatch = 0
    rotating: list[int] = []
    hard: list[int] = []
    for stat in lane_stats:
        checked = sum(stat.ips.values())
        if not checked or not stat.baseline_ip:
            continue
        if len(stat.ips) >= 2:
            rotating.append(stat.lane)
        wrong = checked - stat.ips.get(stat.baseline_ip, 0)
        mismatch += wrong
        if wrong / checked > ratio_threshold:
            hard.append(stat.lane)
    return mismatch, rotating, hard


def evaluate_run(
    metrics: RunMetrics, *, expected_lanes: int, criteria: CapacityCriteria,
    latency_reference_ms: float | None,
) -> list[str]:
    """稳定判据：全部通过返回空列表，否则逐条给出失败原因。

    `latency_reference_ms`：ladder 阶段传基准档（最小通过档）的 p95；
    soak 阶段传该档自身 ladder 的 p95（判 soak 期退化）。
    """
    reasons: list[str] = []
    if not metrics.started:
        reasons.append("内核进程未启动成功")
        return reasons
    if metrics.listener_ready < expected_lanes:
        reasons.append(
            f"listener 仅 {metrics.listener_ready}/{expected_lanes} 就绪"
        )
    if metrics.kernel_restarted or not metrics.kernel_alive:
        reasons.append("内核进程在运行期间退出或被替换")
    if not metrics.controller_ok:
        reasons.append("运行结束时控制器不可达")
    if not metrics.binding_ok:
        reasons.append("lane 绑定未成立（部分 lane 流量落在组内默认项上）")
    denom = metrics.requests - metrics.rate_limited
    if denom > 0:
        rate = metrics.ok / denom
        if rate < criteria.min_success_rate:
            reasons.append(
                f"成功率 {rate:.1%} 低于阈值 {criteria.min_success_rate:.0%}"
                f"（超时 {metrics.timeouts} / 连接错误 {metrics.connect_errors}"
                f" / HTTP 错误 {metrics.http_errors} / 其他 {metrics.other_errors}）"
            )
    elif metrics.requests == 0:
        reasons.append("未发出任何请求")
    if metrics.hard_leak_lanes:
        reasons.append(f"出口串线 lane：{metrics.hard_leak_lanes}")
    ref = latency_reference_ms
    p95 = metrics.p95_ms
    if ref is not None and p95 is not None and p95 > 0:
        limit = criteria.latency_factor * ref + criteria.latency_floor_ms
        if p95 > limit:
            reasons.append(
                f"p95 {p95:.0f}ms 超过容限（基准 {ref:.0f}ms"
                f" × {criteria.latency_factor} + {criteria.latency_floor_ms:.0f}ms）"
            )
    start, end = metrics.rss_mb_start, metrics.rss_mb_end
    if start is not None and end is not None and start > 0:
        growth_factor = (
            criteria.soak_rss_factor if metrics.phase == "soak"
            else criteria.rss_growth_factor
        )
        if end > start * growth_factor + criteria.rss_growth_floor_mb:
            reasons.append(
                f"内存增长异常：{start:.0f}MB → {end:.0f}MB（界 "
                f"{start * growth_factor + criteria.rss_growth_floor_mb:.0f}MB）"
            )
    return reasons


# ── 内核进程资源采样（win32 ctypes，无第三方依赖）────────────────────

_PPROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class ProcessResourceSampler:
    """对指定 pid 周期采样 CPU 时间与工作集；非 Windows 或句柄打不开时降级为不可用。

    cpu_pct 以单核为 100%（多核满载可超 100），与常用进程 CPU 口径一致。
    """

    def __init__(self, pid: int, *, interval_s: float = 2.0) -> None:
        self._pid = int(pid)
        self._interval_s = float(interval_s)
        self._samples: list[tuple[float, float, float]] = []  # (wall, cpu_100ns, rss)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None
        self.available = sys.platform == "win32"

    def start(self) -> bool:
        if not self.available:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = kernel32.OpenProcess(
            _PPROCESS_QUERY_LIMITED_INFORMATION, False, self._pid
        )
        if not self._handle:
            self.available = False
            return False
        try:
            self._samples.append(self._sample_once())
        except OSError:
            self._release()
            self.available = False
            return False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._samples.append(self._sample_once())
            except OSError:
                break

    def _sample_once(self) -> tuple[float, float, float]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        create, exit_ft, kernel_t, user_t = (
            _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME(),
        )
        if not kernel32.GetProcessTimes(
            self._handle,
            ctypes.byref(create), ctypes.byref(exit_ft),
            ctypes.byref(kernel_t), ctypes.byref(user_t),
        ):
            raise OSError("GetProcessTimes failed")
        cpu_100ns = float(
            ((kernel_t.dwHighDateTime << 32) | kernel_t.dwLowDateTime)
            + ((user_t.dwHighDateTime << 32) | user_t.dwLowDateTime)
        )
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(
            self._handle, ctypes.byref(pmc), pmc.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return (time.monotonic(), cpu_100ns, float(pmc.WorkingSetSize))

    def _release(self) -> None:
        if self._handle:
            ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            self._handle = None

    def stop(self) -> dict[str, float | None]:
        """结束采样并汇出：cpu_pct_avg / cpu_pct_max / rss_mb_start / end / max。"""
        empty: dict[str, float | None] = {
            "cpu_pct_avg": None, "cpu_pct_max": None,
            "rss_mb_start": None, "rss_mb_end": None, "rss_mb_max": None,
        }
        if not self.available or not self._thread:
            return empty
        self._stop.set()
        self._thread.join(timeout=self._interval_s + 5)
        try:
            self._samples.append(self._sample_once())
        except OSError:
            pass
        self._release()
        if len(self._samples) < 2:
            return empty
        cpu_pcts = [
            ((cpu - prev_cpu) / 1e7) / (wall - prev_wall) * 100.0
            for (prev_wall, prev_cpu, _), (wall, cpu, _) in zip(
                self._samples, self._samples[1:]
            )
            if wall > prev_wall
        ]
        rss = [s[2] for s in self._samples]
        return {
            "cpu_pct_avg": sum(cpu_pcts) / len(cpu_pcts) if cpu_pcts else None,
            "cpu_pct_max": max(cpu_pcts) if cpu_pcts else None,
            "rss_mb_start": rss[0] / (1024 * 1024),
            "rss_mb_end": rss[-1] / (1024 * 1024),
            "rss_mb_max": max(rss) / (1024 * 1024),
        }


def _machine_info() -> dict:
    info: dict = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "total_mem_gb": None,
        "concurrent_mihomo_processes": None,
    }
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kb = ctypes.c_uint64(0)
        if kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kb)):
            info["total_mem_gb"] = round(kb.value / (1024 * 1024), 1)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq mihomo.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout or ""
        count = sum(1 for line in out.splitlines() if "mihomo" in line.lower())
        info["concurrent_mihomo_processes"] = count
    except Exception:  # noqa: BLE001 —— 探测环境信息拿不到不阻断
        pass
    return info


def _render_tier_config(
    tier_dir: Path, proxies: Sequence[Mapping], *, lanes: int, direct_mode: bool
) -> Path:
    """渲染第 N 档内核启动配置：N 条 `lane-<i>` select 组 + N 个 mixed listener。

    与生产 `prepare_runtime_config` 同构（同一批常量、同一批键），差异只有两处：
    lane 数不设上限（被测对象就是 lane 数本身），`direct_mode` 下组成员前置
    DIRECT 以便 lane 绑直连。
    """
    names = [str(p["name"]) for p in proxies]
    members = (["DIRECT", *names] if direct_mode else names)
    groups = [
        {"name": f"{LANE_GROUP_PREFIX}{i}", "type": "select", "proxies": list(members)}
        for i in range(lanes)
    ]
    listeners = [
        {
            "name": f"{LANE_GROUP_PREFIX}{i}-in",
            "type": "mixed",
            "port": _free_local_port(),
            "listen": RUNTIME_BIND_ADDRESS,
            "proxy": f"{LANE_GROUP_PREFIX}{i}",
        }
        for i in range(lanes)
    ]
    doc = {
        "mode": RUNTIME_MODE,
        "allow-lan": False,
        "bind-address": RUNTIME_BIND_ADDRESS,
        "external-controller": f"{RUNTIME_CONTROLLER_HOST}:{_free_local_port()}",
        "secret": RUNTIME_CONTROLLER_SECRET,
        "mixed-port": _free_local_port(),
        "proxies": [dict(p) for p in proxies],
        "proxy-groups": groups,
        "listeners": listeners,
    }
    path = tier_dir / "proxypool" / RUNTIME_CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


@dataclass
class CapacityReport:
    created_at: str
    mode: str
    mihomo_version: str | None
    machine: dict
    nodes_eligible: int
    exits_available: int
    ladder: list[int]
    soak_seconds: float
    criteria: dict
    tiers: list[dict]
    soak: dict | None
    theoretical_capacity: int | None
    verified_capacity: int | None
    production_safe_capacity: int | None
    recommended_crawl_workers: int | None

    def to_dict(self) -> dict:
        return asdict(self)

    def dump(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


class KernelCapacityProbe:
    """阶梯容量探测驱动器。每次 run 自管全部内核实例的启停与清理。"""

    def __init__(
        self, *, exe_path: Path, pool_text: str, mode: str = MODE_NODES,
        slot_names: Sequence[str], exit_by_node: Mapping[str, str] | None = None,
        criteria: CapacityCriteria | None = None, rounds: int = 10,
        soak_seconds: float = 600.0, soak_interval_s: float = 1.0,
        work_root: Path, keep_work: bool = False,
        target_url: str | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"未知模式：{mode}")
        self.exe_path = Path(exe_path)
        self.mode = mode
        doc = yaml.safe_load(pool_text)
        proxies = doc.get("proxies") if isinstance(doc, Mapping) else None
        if not isinstance(proxies, list) or not proxies:
            raise ValueError("池文本没有可用的 proxies 段")
        self.proxies = proxies
        self.slot_names = [str(n) for n in slot_names if n]
        self.exit_by_node = dict(exit_by_node or {})
        self.criteria = criteria or CapacityCriteria()
        self.rounds = max(1, int(rounds))
        self.soak_seconds = max(0.0, float(soak_seconds))
        self.soak_interval_s = max(0.0, float(soak_interval_s))
        self.work_root = Path(work_root)
        self.keep_work = keep_work
        self.run_dir = self.work_root / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.target_url = target_url or (
            STEAM_TARGET_URL if mode == MODE_STEAM else EXIT_IP_TARGET_URL
        )
        # 出口归因只在目标确为出口 IP 回显端点时启用；自定义目标（如直连可达的
        # 轻量端点）没有 IP 语义，串线判定自然退化为不做
        self._ip_mode = self.target_url == EXIT_IP_TARGET_URL

    # ── 对外主流程 ────────────────────────────────────────────────

    async def run(self, ladder: Sequence[int], *, soak_tier: int | None = None) -> CapacityReport:
        tiers: list[RunMetrics] = []
        passing: list[int] = []
        baseline_p95: float | None = None
        theoretical: int | None = None
        fail_streak = 0

        if soak_tier is not None:
            # 跳过梯度，直接对指定档做 soak 确认（梯度结论已被冷启动突发等
            # 一过性因素污染时，用它单独验证稳态）
            soak = await self._run_tier(int(soak_tier), soak_seconds=self.soak_seconds)
            reasons = evaluate_run(
                soak, expected_lanes=int(soak_tier), criteria=self.criteria,
                latency_reference_ms=None,
            )
            soak.failure_reasons = [*soak.failure_reasons, *reasons]
            soak.passed = not soak.failure_reasons
            self._log_tier(soak)
            verified = int(soak_tier) if soak.passed else None
            theoretical = int(soak_tier) if soak.listener_ready == int(soak_tier) else None
            production_safe = (
                int(verified * self.criteria.safety_factor) if verified is not None else None
            )
            recommended = (
                min(len(self.slot_names), production_safe)
                if production_safe is not None and self.mode != MODE_DIRECT
                else production_safe
            )
            return CapacityReport(
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                mode=self.mode,
                mihomo_version=await asyncio.to_thread(kernel_version, self.exe_path),
                machine=_machine_info(),
                nodes_eligible=len(self.proxies),
                exits_available=len(self.slot_names),
                ladder=[],
                soak_seconds=self.soak_seconds,
                criteria=self.criteria.to_dict(),
                tiers=[],
                soak=soak.to_dict(),
                theoretical_capacity=theoretical,
                verified_capacity=verified,
                production_safe_capacity=production_safe,
                recommended_crawl_workers=recommended,
            )

        for n in ladder:
            metrics = await self._run_tier(int(n))
            reasons = evaluate_run(
                metrics, expected_lanes=int(n), criteria=self.criteria,
                latency_reference_ms=baseline_p95,
            )
            metrics.failure_reasons = [*metrics.failure_reasons, *reasons]
            metrics.passed = not metrics.failure_reasons
            tiers.append(metrics)
            self._log_tier(metrics)
            if metrics.started and metrics.listener_ready == int(n):
                theoretical = int(n)
            if metrics.passed:
                passing.append(int(n))
                fail_streak = 0
                if baseline_p95 is None and metrics.p95_ms:
                    baseline_p95 = metrics.p95_ms
            else:
                fail_streak += 1
                if not metrics.started or metrics.listener_ready == 0:
                    break  # 内核起不来属环境性故障，后续档位无意义
                if fail_streak >= 2:
                    break
            self._cleanup_tier(self._tier_dir(int(n), soak=False), keep=not metrics.passed)

        soak: RunMetrics | None = None
        verified: int | None = None
        if passing and self.soak_seconds > 0:
            by_lanes = {m.lanes: m for m in tiers}
            for candidate in reversed(passing[-2:]):
                soak = await self._run_tier(candidate, soak_seconds=self.soak_seconds)
                ref = by_lanes.get(candidate)
                reasons = evaluate_run(
                    soak, expected_lanes=candidate, criteria=self.criteria,
                    latency_reference_ms=ref.p95_ms if ref else None,
                )
                soak.failure_reasons = [*soak.failure_reasons, *reasons]
                soak.passed = not soak.failure_reasons
                self._log_tier(soak)
                self._cleanup_tier(
                    self._tier_dir(candidate, soak=True), keep=not soak.passed
                )
                if soak.passed:
                    verified = candidate
                    break
        elif passing:
            verified = passing[-1]

        production_safe = (
            int(verified * self.criteria.safety_factor) if verified is not None else None
        )
        recommended = (
            min(len(self.slot_names), production_safe)
            if production_safe is not None and self.mode != MODE_DIRECT
            else production_safe
        )
        return CapacityReport(
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mode=self.mode,
            mihomo_version=await asyncio.to_thread(kernel_version, self.exe_path),
            machine=_machine_info(),
            nodes_eligible=len(self.proxies),
            exits_available=len(self.slot_names),
            ladder=[int(n) for n in ladder],
            soak_seconds=self.soak_seconds,
            criteria=self.criteria.to_dict(),
            tiers=[m.to_dict() for m in tiers],
            soak=soak.to_dict() if soak else None,
            theoretical_capacity=theoretical,
            verified_capacity=verified,
            production_safe_capacity=production_safe,
            recommended_crawl_workers=recommended,
        )

    # ── 单档执行 ──────────────────────────────────────────────────

    def _tier_dir(self, lanes: int, soak: bool) -> Path:
        return self.run_dir / (f"soak-{lanes}" if soak else f"ladder-{lanes}")

    def _selections_for(self, lanes: int) -> list[str | None]:
        """第 N 档每条 lane 绑谁：出口槽按序占位，节点不够的尾部 lane 留空（共享默认项）。"""
        if self.mode == MODE_DIRECT:
            return ["DIRECT"] * lanes
        return list(plan_lane_assignment([], self.slot_names, lanes=lanes))

    async def _wait_listeners(
        self, tier_dir: Path, lanes: int
    ) -> tuple[list[int], int, float | None]:
        """等全部 lane listener 进入监听。返回 (就绪端口, 就绪数, 启动耗时 ms)。"""
        deadline = time.monotonic() + self.criteria.ready_timeout_s
        begin = time.perf_counter()
        ports: list[int] = []
        while True:
            ports = list(runtime_lane_ports(tier_dir))
            if len(ports) == lanes and all(_port_listening(p) for p in ports):
                return ports, lanes, (time.perf_counter() - begin) * 1000
            if time.monotonic() >= deadline:
                ready = [p for p in ports if _port_listening(p)]
                return ready, len(ready), None
            await asyncio.sleep(0.1)

    async def _run_tier(self, lanes: int, *, soak_seconds: float | None = None) -> RunMetrics:
        phase = "soak" if soak_seconds is not None else "ladder"
        metrics = RunMetrics(lanes=lanes, phase=phase)
        begin = time.perf_counter()
        tier_dir = self._tier_dir(lanes, soak=phase == "soak")
        tier_dir.mkdir(parents=True, exist_ok=True)
        # 档位目录内镜像生产布局（<dir>/proxypool/*.yaml）：runtime 的路径助手
        # （runtime_lane_ports / controller_endpoint_of 等）按该布局拼路径
        pool_file = tier_dir / "proxypool" / POOL_FILENAME
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text(
            yaml.safe_dump(
                {"proxies": [dict(p) for p in self.proxies]},
                allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        config = _render_tier_config(
            tier_dir, self.proxies, lanes=lanes, direct_mode=self.mode == MODE_DIRECT
        )

        runtime = ClashRuntime()
        try:
            await asyncio.to_thread(
                runtime.start, str(self.exe_path), str(config), inject_lanes=False
            )
        except Exception as e:  # noqa: BLE001 —— 启动失败本身即该档结果
            metrics.failure_reasons.append(f"内核启动失败：{type(e).__name__}: {e}")
            metrics.duration_s = time.perf_counter() - begin
            return metrics
        metrics.started = True
        metrics.kernel_pid = runtime.process.pid if runtime.process else None

        try:
            ports, ready, startup_ms = await self._wait_listeners(tier_dir, lanes)
            metrics.listener_ready = ready
            metrics.startup_ms = startup_ms
            controller_url, secret = controller_endpoint_of(tier_dir)
            selections = self._selections_for(lanes)
            metrics.unbound_lanes = sum(1 for s in selections if s is None)
            if ready:
                try:
                    applied = await assign_lanes(controller_url, secret, selections)
                    metrics.binding_ok = all(
                        a == s for a, s in zip(applied, selections) if s is not None
                    )
                except Exception as e:  # noqa: BLE001 —— 绑定失败照常测，记录原因
                    metrics.failure_reasons.append(
                        f"lane 绑定未成立：{type(e).__name__}: {e}"
                    )

            if not ready:
                metrics.duration_s = time.perf_counter() - begin
                metrics.failure_reasons.append("listener 无一就绪")
                return metrics

            sampler = ProcessResourceSampler(runtime.process.pid)
            sampler.start()
            try:
                if phase == "soak":
                    lane_stats = await self._drive_soak(ports, selections, soak_seconds or 0.0)
                else:
                    lane_stats = await self._drive_ladder(ports, selections)
            finally:
                summary = sampler.stop()
            metrics.cpu_pct_avg = summary["cpu_pct_avg"]
            metrics.cpu_pct_max = summary["cpu_pct_max"]
            metrics.rss_mb_start = summary["rss_mb_start"]
            metrics.rss_mb_end = summary["rss_mb_end"]
            metrics.rss_mb_max = summary["rss_mb_max"]

            metrics.kernel_alive = (
                runtime.process is not None and runtime.process.poll() is None
            )
            metrics.kernel_restarted = (
                metrics.kernel_pid is not None
                and (runtime.process is None or runtime.process.pid != metrics.kernel_pid)
            )
            metrics.controller_ok = await self._controller_alive(controller_url, secret)
            self._collect(metrics, lane_stats)
            metrics.duration_s = time.perf_counter() - begin
            return metrics
        finally:
            await asyncio.to_thread(runtime.stop)

    async def _controller_alive(self, controller_url: str, secret: str) -> bool:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=5.0, headers=headers
            ) as client:
                resp = await client.get(f"{controller_url}/version")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001 —— 探测性检查，异常即不可达
            return False

    def _make_client(self, port: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=self.criteria.request_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "holdexar-capacity-probe/1"},
        )

    async def _one_request(self, client: httpx.AsyncClient, stat: LaneStat) -> None:
        begin = time.perf_counter()
        stat.requests += 1
        try:
            resp = await client.get(self.target_url)
        except httpx.TimeoutException:
            stat.timeouts += 1
            return
        except httpx.ConnectError:
            stat.connect_errors += 1
            return
        except httpx.HTTPError:
            stat.other_errors += 1
            return
        elapsed_ms = (time.perf_counter() - begin) * 1000
        if resp.status_code == 429:
            stat.rate_limited += 1
            return
        if resp.status_code >= 300:
            stat.http_errors += 1
            return
        if not self._ip_mode:
            stat.ok += 1
            stat.latencies_ms.append(elapsed_ms)
            return
        try:
            ip = str((resp.json() or {}).get("ip") or "")
        except Exception:  # noqa: BLE001 —— 2xx 但解析不出出口 IP 按其他错误计
            stat.other_errors += 1
            return
        if not ip:
            stat.other_errors += 1
            return
        stat.ok += 1
        stat.latencies_ms.append(elapsed_ms)
        stat.ips[ip] = stat.ips.get(ip, 0) + 1
        if stat.baseline_ip is None:
            stat.baseline_ip = ip

    async def _drive_ladder(
        self, ports: Sequence[int], selections: Sequence[str | None]
    ) -> list[LaneStat]:
        """ladder 负载：rounds 轮，每轮全部 lane 并行各发一请求，lane 内串行。"""
        stats = [
            LaneStat(lane=i, port=port, node=selections[i] if i < len(selections) else None)
            for i, port in enumerate(ports)
        ]
        clients = [self._make_client(p) for p in ports]
        try:
            for _round in range(self.rounds):
                await asyncio.gather(
                    *(self._one_request(c, s) for c, s in zip(clients, stats))
                )
        finally:
            await asyncio.gather(*(c.aclose() for c in clients))
        return stats

    async def _drive_soak(
        self, ports: Sequence[int], selections: Sequence[str | None], seconds: float
    ) -> list[LaneStat]:
        """soak 负载：每 lane 独立循环（请求 → 间隔），跑到总时长为止。"""
        stats = [
            LaneStat(lane=i, port=port, node=selections[i] if i < len(selections) else None)
            for i, port in enumerate(ports)
        ]
        deadline = time.monotonic() + seconds

        async def _lane_loop(client: httpx.AsyncClient, stat: LaneStat) -> None:
            while time.monotonic() < deadline:
                await self._one_request(client, stat)
                if self.soak_interval_s:
                    await asyncio.sleep(self.soak_interval_s)

        clients = [self._make_client(p) for p in ports]
        try:
            await asyncio.gather(
                *(_lane_loop(c, s) for c, s in zip(clients, stats))
            )
        finally:
            await asyncio.gather(*(c.aclose() for c in clients))
        return stats

    def _collect(self, metrics: RunMetrics, lane_stats: Sequence[LaneStat]) -> None:
        latencies = [ms for s in lane_stats for ms in s.latencies_ms]
        metrics.requests = sum(s.requests for s in lane_stats)
        metrics.ok = sum(s.ok for s in lane_stats)
        metrics.timeouts = sum(s.timeouts for s in lane_stats)
        metrics.connect_errors = sum(s.connect_errors for s in lane_stats)
        metrics.rate_limited = sum(s.rate_limited for s in lane_stats)
        metrics.http_errors = sum(s.http_errors for s in lane_stats)
        metrics.other_errors = sum(s.other_errors for s in lane_stats)
        metrics.p50_ms = percentile(latencies, 0.50)
        metrics.p95_ms = percentile(latencies, 0.95)
        metrics.p99_ms = percentile(latencies, 0.99)
        if self._ip_mode:
            mismatch, rotating, hard = classify_lane_leaks(
                lane_stats, ratio_threshold=self.criteria.hard_leak_ratio
            )
            metrics.mismatch_count = mismatch
            metrics.rotating_lanes = rotating
            metrics.hard_leak_lanes = hard
        suspects: list[dict] = []
        for s in lane_stats:
            clean = (s.ok == s.requests)
            flagged = s.lane in metrics.hard_leak_lanes or s.lane in metrics.rotating_lanes
            if clean and not flagged:
                continue
            suspects.append({
                "lane": s.lane, "node": s.node,
                "requests": s.requests, "ok": s.ok,
                "timeouts": s.timeouts, "connect_errors": s.connect_errors,
                "rate_limited": s.rate_limited, "http_errors": s.http_errors,
                "other_errors": s.other_errors,
                "ips": sorted(s.ips),
            })
        metrics.suspect_lanes = suspects

    def _log_tier(self, metrics: RunMetrics) -> None:
        verdict = "未判定" if metrics.passed is None else ("PASS" if metrics.passed else "FAIL")
        reasons = "；".join(metrics.failure_reasons) if metrics.failure_reasons else ""
        p95 = f"{round(metrics.p95_ms)}ms" if metrics.p95_ms is not None else "-"
        cpu = f"{metrics.cpu_pct_max:.0f}%" if metrics.cpu_pct_max is not None else "-"
        rss = (
            f"{metrics.rss_mb_start:.0f}->{metrics.rss_mb_end:.0f}MB"
            if metrics.rss_mb_start is not None and metrics.rss_mb_end is not None else "-"
        )
        success_denom = max(1, metrics.requests - metrics.rate_limited)
        print(
            f"[capacity] {metrics.phase} lanes={metrics.lanes} "
            f"ready={metrics.listener_ready}/{metrics.lanes} "
            f"req={metrics.requests} ok={metrics.ok}"
            f"({metrics.ok * 100 // success_denom}%) "
            f"p95={p95} cpu={cpu} rss={rss} → {verdict} {reasons}",
            flush=True,
        )

    def _cleanup_tier(self, tier_dir: Path, *, keep: bool) -> None:
        if keep or self.keep_work:
            return
        shutil.rmtree(tier_dir, ignore_errors=True)
