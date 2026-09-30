"""池内核 lane 容量探测（手动运维工具）。

阶梯压测本机 mihomo + lane 架构的并发容量，产出容量报告（JSON）：

    python scripts/capacity_probe.py                        # nodes 模式全梯度 + 600s soak
    python scripts/capacity_probe.py --mode direct          # 排除代理链路的内核裸容量
    python scripts/capacity_probe.py --mode steam --ladder 96,128 --rounds 3

节点来源：缺省读当前数据目录的节点台账（合格节点 → 出口槽，与生产同一套口径）；
`--pool-file` 可改用指定池文件（无出口身份信息，串线判定退化为 lane 自身基线一致性）。
报告落在 `<data_dir>/proxypool/capacity/`，属本机数据，不入库。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import yaml  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_session_factory  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxypool.capacity import (  # noqa: E402
    DEFAULT_LADDER,
    MODE_NODES,
    MODE_STEAM,
    MODES,
    CapacityCriteria,
    KernelCapacityProbe,
)
from app.domains.proxypool.exits import select_exit_slots  # noqa: E402
from app.domains.proxypool.health import (  # noqa: E402
    L2_LEVEL,
    latest_l0_delays,
)
from app.domains.proxypool.models import HealthObservation  # noqa: E402
from app.domains.proxypool.pool import eligible_nodes, render_pool  # noqa: E402

SOURCES = ("all", "l2", "l2-recent")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=MODES, default=MODE_NODES)
    parser.add_argument(
        "--ladder", default=",".join(str(n) for n in DEFAULT_LADDER),
        help="档位序列（逗号分隔，升序）",
    )
    parser.add_argument(
        "--rounds", type=int, default=None,
        help="ladder 每档每 lane 请求轮数（缺省 nodes/direct=10，steam=3）",
    )
    parser.add_argument("--soak", type=float, default=600.0, help="确认 soak 秒数（0 跳过）")
    parser.add_argument(
        "--soak-tier", type=int, default=None,
        help="跳过梯度，直接对指定 lane 数做 soak 确认（稳态验证）",
    )
    parser.add_argument("--soak-interval", type=float, default=1.0, help="soak 每 lane 请求间隔秒")
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--ready-timeout", type=float, default=20.0)
    parser.add_argument("--min-success", type=float, default=0.99)
    parser.add_argument("--safety-factor", type=float, default=0.8)
    parser.add_argument("--latency-factor", type=float, default=3.0)
    parser.add_argument("--latency-floor-ms", type=float, default=500.0)
    parser.add_argument("--data-dir", default=None, help="数据目录（缺省按环境解析）")
    parser.add_argument("--pool-file", default=None, help="节点来源改用指定池文件")
    parser.add_argument(
        "--source", choices=SOURCES, default="all",
        help="节点口径：all=全部合格节点；l2=最新一次 Steam 业务体检通过；"
             "l2-recent=时间窗内有过 L2 通过（窗口见 --l2-window-h）。"
             "「最新」口径会被体检机器的单次故障扫污染（如控制器 401 批量误记失败）",
    )
    parser.add_argument(
        "--l2-window-h", type=float, default=24.0,
        help="l2-recent 口径的时间窗（小时）",
    )
    parser.add_argument(
        "--target-url", default=None,
        help="覆盖请求目标（direct 模式须给本机直连可达的轻量端点，"
             "如 https://www.msftconnecttest.com/connecttest.txt）",
    )
    parser.add_argument("--out", default=None, help="报告输出路径")
    parser.add_argument("--keep-work", action="store_true", help="保留每档工作目录（排查内核日志）")
    return parser.parse_args()


def _find_kernel(data_dir: Path) -> Path | None:
    candidates = [
        clash_manager.kernel_exe(data_dir),
        clash_manager.bundled_clash_dir() / kernel_filename(),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.extend(sorted(Path(local).glob(f"holdexar*/clash/{kernel_filename()}")))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


async def _l2_latest_ok_node_ids(session) -> set[str]:
    """「最新一次 L2 通过」的节点集：每节点取最新 L2 观测，仅收 ok=True。"""
    latest = (
        select(
            HealthObservation.node_id.label("node_id"),
            func.max(HealthObservation.observed_at).label("mx"),
        )
        .where(HealthObservation.level == L2_LEVEL)
        .group_by(HealthObservation.node_id)
        .subquery()
    )
    rows = await session.execute(
        select(HealthObservation.node_id)
        .join(
            latest,
            (latest.c.node_id == HealthObservation.node_id)
            & (latest.c.mx == HealthObservation.observed_at),
        )
        .where(HealthObservation.level == L2_LEVEL, HealthObservation.ok.is_(True))
    )
    return {node_id for (node_id,) in rows.all()}


async def _l2_recent_ok_node_ids(session, *, hours: float) -> set[str]:
    """时间窗内有过 L2 通过的节点集（单次故障扫不会抹掉窗口内的历史通过）。"""
    since = datetime.now() - timedelta(hours=hours)
    rows = await session.execute(
        select(HealthObservation.node_id)
        .where(
            HealthObservation.level == L2_LEVEL,
            HealthObservation.ok.is_(True),
            HealthObservation.observed_at >= since,
        )
        .distinct()
    )
    return {node_id for (node_id,) in rows.all()}


async def _load_nodes(args: argparse.Namespace) -> tuple[str, list[str]]:
    """节点来源：返回 (池文本, 出口槽节点名列表，按生产同款排序)。"""
    if args.pool_file:
        if args.source != "all":
            raise ValueError("--source 口径需要读节点台账，不能与 --pool-file 同用")
        text = Path(args.pool_file).read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        names = [
            str(p["name"]) for p in (doc.get("proxies") or []) if p.get("name")
        ]
        return text, names
    async with get_session_factory()() as session:
        nodes = await eligible_nodes(session)
        delays = await latest_l0_delays(session)
        if args.source == "l2":
            keep = await _l2_latest_ok_node_ids(session)
            nodes = [n for n in nodes if n.node_id in keep]
        elif args.source == "l2-recent":
            keep = await _l2_recent_ok_node_ids(session, hours=args.l2_window_h)
            nodes = [n for n in nodes if n.node_id in keep]
    text, _names = render_pool(nodes)
    slots = select_exit_slots(nodes, max_workers=10**9, delays=delays)
    return text, [slot.runtime_name for slot in slots]


def _print_summary(report, out: Path) -> None:
    machine = report.machine
    print("\n===== lane 容量探测报告 =====")
    print(
        f"模式 {report.mode} ｜ 内核 {report.mihomo_version or '?'}"
        f" ｜ 池节点 {report.nodes_eligible} ｜ 出口槽 {report.exits_available}"
    )
    print(
        f"机器 {machine.get('cpu_count')} 核 / {machine.get('total_mem_gb')}GB"
        f" ｜ 采样时并存 mihomo 进程 {machine.get('concurrent_mihomo_processes')}"
    )
    for t in report.tiers:
        verdict = "PASS" if t["passed"] else "FAIL"
        reasons = "；".join(t["failure_reasons"]) if t["failure_reasons"] else ""
        p95 = f"{round(t['p95_ms'])}ms" if t.get("p95_ms") is not None else "-"
        print(
            f"  lanes={t['lanes']:>3}  ready={t['listener_ready']}/{t['lanes']}"
            f"  ok={t['ok']}/{t['requests']}  p95={p95}"
            f"  rss={t['rss_mb_start'] and round(t['rss_mb_start']) or '-'}"
            f"→{t['rss_mb_end'] and round(t['rss_mb_end']) or '-'}MB"
            f"  → {verdict} {reasons}"
        )
        for s in (t.get("suspect_lanes") or [])[:5]:
            print(
                f"      lane {s['lane']} ({s['node']}) req={s['requests']}"
                f" ok={s['ok']} 连接错误={s['connect_errors']} 超时={s['timeouts']}"
                f" 429={s['rate_limited']} http错误={s['http_errors']}"
                f" ips={s['ips'] or '-'}"
            )
        if len(t.get("suspect_lanes") or []) > 5:
            print(f"      …另有 {len(t['suspect_lanes']) - 5} 条可疑 lane，见报告 JSON")
    if report.soak:
        s = report.soak
        verdict = "PASS" if s["passed"] else "FAIL"
        reasons = "；".join(s["failure_reasons"]) if s["failure_reasons"] else ""
        print(
            f"  soak lanes={s['lanes']} {report.soak_seconds:.0f}s"
            f"  ok={s['ok']}/{s['requests']}  → {verdict} {reasons}"
        )
    print(f"理论容量（listener 全就绪最高档）：{report.theoretical_capacity}")
    print(f"验证容量（ladder + soak 双过）：{report.verified_capacity}")
    if report.production_safe_capacity is not None:
        print(
            f"建议生产上限（验证 × {report.criteria['safety_factor']}）："
            f"{report.production_safe_capacity}"
        )
    if report.recommended_crawl_workers is not None:
        print(
            f"建议 crawl worker 上限 = min(出口槽 {report.exits_available},"
            f" 生产上限 {report.production_safe_capacity})"
            f" = {report.recommended_crawl_workers}"
        )
    print(f"报告：{out}")


async def _async_main(args: argparse.Namespace) -> int:
    if args.data_dir:
        os.environ["HOLDEXAR_DATA_DIR"] = str(Path(args.data_dir).resolve())
    data_dir = get_settings().data_dir

    exe = _find_kernel(data_dir)
    if exe is None:
        print(
            "找不到可用的 mihomo 内核（数据目录 clash/、随包 assets、"
            "%LOCALAPPDATA%/holdexar*/clash/ 均无）。先运行一次应用或 fetch_kernel 脚本。",
            file=sys.stderr,
        )
        return 2

    try:
        pool_text, slot_names = await _load_nodes(args)
    except Exception as e:  # noqa: BLE001 —— 来源不可用要给出路
        print(
            f"节点来源读取失败：{type(e).__name__}: {e}"
            "\n（数据目录台账不可用时，用 --pool-file 指定池文件）",
            file=sys.stderr,
        )
        return 2
    if not slot_names:
        print("没有可用节点（台账为空、池文件为空或所选口径无通过记录）。", file=sys.stderr)
        return 2
    print(
        f"节点来源 {args.source}"
        + (f"（{args.l2_window_h:.0f}h 窗口）" if args.source == "l2-recent" else "")
        + f"：{len(slot_names)} 个独立出口槽",
        flush=True,
    )

    rounds = (
        args.rounds if args.rounds is not None
        else (3 if args.mode == MODE_STEAM else 10)
    )
    criteria = CapacityCriteria(
        request_timeout_s=args.request_timeout,
        ready_timeout_s=args.ready_timeout,
        min_success_rate=args.min_success,
        latency_factor=args.latency_factor,
        latency_floor_ms=args.latency_floor_ms,
        safety_factor=args.safety_factor,
    )
    work_root = data_dir / "proxypool" / "capacity"
    probe = KernelCapacityProbe(
        exe_path=exe, pool_text=pool_text, mode=args.mode,
        slot_names=slot_names, criteria=criteria, rounds=rounds,
        soak_seconds=args.soak, soak_interval_s=args.soak_interval,
        work_root=work_root, keep_work=args.keep_work,
        target_url=args.target_url,
    )
    ladder = [int(x) for x in str(args.ladder).split(",") if x.strip()]
    if not ladder and args.soak_tier is None:
        print("ladder 为空且未指定 --soak-tier。", file=sys.stderr)
        return 2

    report = await probe.run(ladder, soak_tier=args.soak_tier)
    out = (
        Path(args.out) if args.out
        else work_root / f"{probe.run_dir.name.replace('run-', 'report-')}.json"
    )
    report.dump(out)
    _print_summary(report, out)
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("\n已中断（已启动的内核实例会随档位结束而停止）。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
