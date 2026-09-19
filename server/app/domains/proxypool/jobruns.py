"""生产作业台账（`proxy_job_runs`）：一次真实 `run_crawl` = 一行。

这个模块只做两件事：**把事实写下来**、**把事实读出来**。它不产出健康度、
健康等级，也不参与判死——阈值要等真实数据积累之后再谈（定稿见
`docs/PROXYPOOL_HANDOVER_P1.7_NEXT.md` §13/§14）。

四条边界（改这个文件前先读）：

1. **不按 HTTP 请求记账**：一轮的粒度是「一个 GLOBAL 选中节点上的 N 个批量任务」，
   所以列是 `task_count`。口径是**计划任务数**（`scheduler.total_target` = 本次初始
   任务量），不是已完成数——中断时 `task_count` 会大于 `success_count + error_count`，
   这正是「计划了多少、跑完多少」两个事实分开表达的地方。
2. **`error_summary` 只放固定枚举计数**（`ERROR_KINDS`）：异常原文只进日志——
   异常串里可能带 URL/凭据（生产层信息红线），且长度无界。
3. **两笔写入**：开始插 `running`、结束更新终态。进程被杀时库里因此留下
   「有一次作业没跑完」这个事实（启动清理把遗留 `running` 标 `interrupted`）。
4. **写入失败绝不影响爬取**：观测不得成为生产依赖，所有记录入口都是 fail-soft。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.domains.proxypool.models import (
    ProxyJobRun,
    ProxyNode,
    ProxyNodeSource,
    SubscriptionSnapshot,
)
from app.domains.proxypool.pool import eligible_nodes, pool_path

logger = logging.getLogger(__name__)

# ── 状态：由本模块一处定义，前端只渲染不推导 ─────────────────────────
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

# ── 错误分类：固定枚举（顺序即前端展示顺序）────────────────────────
# `ledger` = browse 层重试耗尽后只留了任务 id 的那类失败（原因在日志里，账本里没有），
# 单列一项比塞进 `other` 诚实：它恰恰是失败量最大的一类。
ERROR_KINDS: tuple[str, ...] = (
    "timeout",
    "connect",
    "reset",
    "proxy_error",
    "rate_limit",
    "http_4xx",
    "http_5xx",
    "tls",
    "dns",
    "ledger",
    "internal",
    "other",
)

# error_summary 单行大小上限（字节）。枚举键有界，正常不会碰到；留着防将来加子映射
# 时把一行写无界。
_SUMMARY_MAX_BYTES = 8192

DEFAULT_KEEP_JOB_DAYS = 90
DEFAULT_MIN_JOB_RUNS = 200


# ══ 错误分类 ═════════════════════════════════════════════════════
def classify_error(exc: BaseException) -> str:
    """异常 → `ERROR_KINDS` 之一。**按名字/消息判，不 import 上层的异常类**
    （crawler 的异常类型不该被 proxypool 域依赖）。"""
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)

    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return "rate_limit"
    if isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in text:
        return "timeout"
    if "ssl" in name or "certificate" in text:
        return "tls"
    if (
        "dns" in name
        or "resolver" in name
        or "getaddrinfo" in text
        or "name or service not known" in text
    ):
        return "dns"
    if "proxy" in text:
        return "proxy_error"
    if isinstance(status, int):
        if 400 <= status < 500:
            return "http_4xx"
        if 500 <= status < 600:
            return "http_5xx"
    if "reset" in text:
        return "reset"
    if "connect" in text or "refused" in text:
        return "connect"
    if isinstance(
        exc, (ValueError, RuntimeError, KeyError, TypeError, AttributeError, LookupError)
    ):
        return "internal"
    return "other"


def new_error_summary() -> dict:
    """一轮作业的错误汇总容器（写库前的形态）。"""
    return {"by_error": {}}


def _bump(summary: dict, kind: str, count: int = 1) -> None:
    by = summary.setdefault("by_error", {})
    by[kind] = int(by.get(kind, 0)) + int(count)


def note_error(summary: dict, exc: BaseException) -> None:
    """记一次异常（供 `CrawlerScheduler.error_sink` 回调）。"""
    _bump(summary, classify_error(exc))


def note_ledger(summary: dict, ledger_size: int) -> None:
    """记 browse 层账本里那些「重试耗尽、只留任务 id」的失败数。"""
    if ledger_size > 0:
        _bump(summary, "ledger", int(ledger_size))


def finalize_summary(summary: dict | None) -> dict:
    """收口：丢掉 0 与未知键、必要时截断并置 `truncated`。"""
    raw = (summary or {}).get("by_error") or {}
    by = {
        kind: int(count)
        for kind, count in raw.items()
        if kind in ERROR_KINDS and int(count) > 0
    }
    out: dict = {"by_error": by}
    if (summary or {}).get("interrupted"):
        out["interrupted"] = True
    if (summary or {}).get("truncated"):
        out["truncated"] = True
    if len(json.dumps(out, ensure_ascii=False)) > _SUMMARY_MAX_BYTES:
        top = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        out = {"by_error": dict(top), "truncated": True}
    return out


def classify_outcome(success_count: int, error_count: int, *, stopped: bool) -> str:
    """作业终态：**由后端一处定义**，前端不推导。

    `task_count == 0` 且正常结束记 `success`（列表里有任务列，`0/0` 不会被误读成
    「跑过一轮并成功」）。
    """
    if stopped:
        return STATUS_INTERRUPTED
    if error_count <= 0:
        return STATUS_SUCCESS
    if success_count > 0:
        return STATUS_PARTIAL
    return STATUS_FAILED


# ══ 写入 ═════════════════════════════════════════════════════════
def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def _pool_facts(session: AsyncSession, data_dir: Path) -> dict:
    """run 开始时的池事实：池文件哈希、合格节点数、已知出口 IP 数、来源订阅/快照。

    池 SHA 取的是**最近一次发布的池文件**（`build_pool` 的产物）；内核未必已重载它，
    那属于 rebuild 语义，不影响「这一轮用的是哪一版池产物」这个事实。
    """
    nodes = await eligible_nodes(session)
    node_ids = [n.node_id for n in nodes]
    sub_ids: list[int] = []
    if node_ids:
        rows = await session.execute(
            select(ProxyNodeSource.subscription_id)
            .where(ProxyNodeSource.node_id.in_(node_ids))
            .distinct()
        )
        sub_ids = sorted({int(v) for v in rows.scalars()})

    snapshots: list[int] = []
    if sub_ids:
        rows = await session.execute(
            select(func.max(SubscriptionSnapshot.id))
            .where(
                SubscriptionSnapshot.subscription_id.in_(sub_ids),
                SubscriptionSnapshot.status == "OK",
            )
            .group_by(SubscriptionSnapshot.subscription_id)
        )
        snapshots = sorted(int(v) for v in rows.scalars() if v is not None)

    exit_ips = {n.exit_ip for n in nodes if n.exit_ip}
    return {
        "pool_sha256": _file_sha256(pool_path(data_dir)),
        "pool_node_count": len(nodes),
        # NULL ≠ 0：一个出口 IP 都还没探到时是 None（尚未探测），不是「0 个出口」
        "pool_exit_ip_count": len(exit_ips) if exit_ips else None,
        "subscription_ids": sub_ids,
        "snapshot_ids": snapshots,
    }


async def _selected_node(
    session: AsyncSession, data_dir: Path
) -> tuple[str | None, str | None]:
    """GLOBAL 当前选中节点 + 它在 Registry 里已知的出口 IP。

    运行时是 `mode: global`：**一轮作业全程只有这一个节点**，所以它是归因的关键。
    读不到（配置缺失/控制器不可达）就记 NULL——**绝不阻塞爬取**。
    """
    try:
        from app.domains.proxypool.runtime import (
            controller_endpoint_of,
            current_global_selection,
        )

        base, secret = controller_endpoint_of(data_dir)
        name = await current_global_selection(base, secret)
    except Exception:  # noqa: BLE001 —— 读不到就是读不到，不是失败
        return None, None
    if not name:
        return None, None
    exit_ip = await session.scalar(
        select(ProxyNode.exit_ip).where(ProxyNode.runtime_name == name)
    )
    return str(name), exit_ip


async def start_run(
    session: AsyncSession,
    *,
    proxy_url: str | None,
    regions: list[str] | None,
    workers: int | None,
    data_dir: Path,
    now: datetime,
    kind: str = "crawl",
) -> int:
    """插入 `running` 行，返回作业 id（= 作业身份，没有第二个 run_id）。"""
    facts = await _pool_facts(session, data_dir)
    selected, exit_ip = await _selected_node(session, data_dir)
    row = ProxyJobRun(
        status=STATUS_RUNNING,
        kind=kind,
        started_at=now,
        regions_json=list(regions or []),
        workers=workers,
        proxy_url=proxy_url,
        pool_sha256=facts["pool_sha256"],
        pool_node_count=facts["pool_node_count"],
        pool_exit_ip_count=facts["pool_exit_ip_count"],
        selected_node=selected,
        node_exit_ip=exit_ip,
        subscription_ids_json=facts["subscription_ids"],
        snapshot_ids_json=facts["snapshot_ids"],
        # active_subscription_id 留空：Active/Candidate 尚未实现，不伪造生产订阅
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return int(row.id)


async def finish_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    now: datetime,
    task_count: int | None = None,
    success_count: int | None = None,
    error_count: int | None = None,
    error_summary: dict | None = None,
    duration_ms: int | None = None,
) -> bool:
    row = await session.get(ProxyJobRun, run_id)
    if row is None:
        return False
    row.status = status
    row.finished_at = now
    if duration_ms is not None:
        row.duration_ms = max(0, int(duration_ms))
    row.task_count = task_count
    row.success_count = success_count
    row.error_count = error_count
    if error_summary is not None:
        row.error_summary = finalize_summary(error_summary)
    return True


async def mark_interrupted_runs(session: AsyncSession, now: datetime) -> int:
    """把上次进程遗留的 `running` 标 `interrupted`（与 `cleanup_orphan_jobs` 同构）。

    只认「进程已经不在」这个事实：调用点在启动链上，此刻不可能有本进程的作业在跑。
    """
    rows = (
        await session.execute(
            select(ProxyJobRun).where(ProxyJobRun.status == STATUS_RUNNING)
        )
    ).scalars().all()
    for row in rows:
        row.status = STATUS_INTERRUPTED
        row.finished_at = now
        if row.duration_ms is None and row.started_at is not None:
            delta = now - row.started_at
            row.duration_ms = max(0, int(delta.total_seconds() * 1000))
        summary = dict(row.error_summary or {"by_error": {}})
        summary["interrupted"] = True
        row.error_summary = finalize_summary(summary)
    return len(rows)


# ── 自有会话入口（供 crawler / 启动链调用；一律 fail-soft）──────────
async def record_start(
    *,
    proxy_url: str | None,
    regions: list[str] | None,
    workers: int | None,
    data_dir: Path,
    now: datetime,
    kind: str = "crawl",
) -> int | None:
    try:
        async with get_session_factory()() as session:
            run_id = await start_run(
                session,
                proxy_url=proxy_url,
                regions=regions,
                workers=workers,
                data_dir=data_dir,
                now=now,
                kind=kind,
            )
            await session.commit()
        return run_id
    except Exception:  # noqa: BLE001 —— 观测失败不得影响爬取
        logger.exception("[作业台账] 开始记录失败（爬取照常进行）")
        return None


async def record_finish(
    run_id: int | None,
    *,
    status: str,
    now: datetime,
    task_count: int | None = None,
    success_count: int | None = None,
    error_count: int | None = None,
    error_summary: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    if run_id is None:
        return
    try:
        async with get_session_factory()() as session:
            await finish_run(
                session,
                run_id,
                status=status,
                now=now,
                task_count=task_count,
                success_count=success_count,
                error_count=error_count,
                error_summary=error_summary,
                duration_ms=duration_ms,
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[作业台账] 收尾记录失败（作业 %s）", run_id)


async def record_interrupted(now: datetime | None = None) -> int:
    """启动清理入口：返回被标记的作业数；失败只记日志。"""
    try:
        async with get_session_factory()() as session:
            marked = await mark_interrupted_runs(session, now or datetime.now())
            await session.commit()
        return marked
    except Exception:  # noqa: BLE001
        logger.exception("[作业台账] 中断作业标记失败（不阻塞启动）")
        return 0


# ══ 读取 ═════════════════════════════════════════════════════════
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _by_error(row: ProxyJobRun) -> dict | None:
    """`NULL ≠ 0`：没有汇总的行返回 None（前端显示 `—`），不是空字典。"""
    summary = row.error_summary
    if not isinstance(summary, dict) or "by_error" not in summary:
        return None
    return {str(k): int(v) for k, v in (summary.get("by_error") or {}).items()}


def _row_payload(row: ProxyJobRun) -> dict:
    summary = row.error_summary if isinstance(row.error_summary, dict) else {}
    return {
        "id": row.id,
        "status": row.status,
        "kind": row.kind,
        "startedAt": _iso(row.started_at),
        "finishedAt": _iso(row.finished_at),
        "durationMs": row.duration_ms,
        "taskCount": row.task_count,
        "successCount": row.success_count,
        "errorCount": row.error_count,
        "node": row.selected_node,
        "nodeExitIp": row.node_exit_ip,
        "poolNodeCount": row.pool_node_count,
        "poolExitIpCount": row.pool_exit_ip_count,
        "poolSha256": row.pool_sha256,
        "proxyUrl": row.proxy_url,
        "regions": list(row.regions_json or []),
        "workers": row.workers,
        "subscriptionIds": list(row.subscription_ids_json or []),
        "snapshotIds": list(row.snapshot_ids_json or []),
        "activeSubscriptionId": row.active_subscription_id,
        "byError": _by_error(row),
        "errorSummaryTruncated": bool(summary.get("truncated")),
        "interrupted": bool(summary.get("interrupted")),
    }


async def _day_summary(
    session: AsyncSession, now: datetime
) -> dict:
    """今日概览：**本地日历日**（与 crawl_jobs.started_at 同源）。

    爬虫内部另有北京时间（`crawler/utils.py`）——不混用两套时钟算日界。
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    rows = (
        await session.execute(
            select(ProxyJobRun)
            .where(ProxyJobRun.started_at >= day_start, ProxyJobRun.started_at < day_end)
            .order_by(ProxyJobRun.id.desc())
        )
    ).scalars().all()

    counts = {
        "runs": 0,
        "success": 0,
        "partial": 0,
        "failed": 0,
        "interrupted": 0,
        "running": 0,
    }
    durations: list[int] = []
    pool_exit_ip_count: int | None = None
    for row in rows:
        counts["runs"] += 1
        if row.status in counts:
            counts[row.status] += 1
        if row.duration_ms is not None:
            durations.append(int(row.duration_ms))
        if pool_exit_ip_count is None and row.pool_exit_ip_count is not None:
            # rows 已按 id 倒序：取当日**最后一次**作业的池出口 IP 数
            pool_exit_ip_count = int(row.pool_exit_ip_count)

    return {
        "day": day_start.date().isoformat(),
        **counts,
        # NULL ≠ 0：没有带时长的作业时是 None（前端显示 —）
        "avgDurationMs": round(sum(durations) / len(durations)) if durations else None,
        "poolExitIpCount": pool_exit_ip_count,
    }


async def list_runs(
    session: AsyncSession, *, limit: int = 20, offset: int = 0, now: datetime | None = None
) -> dict:
    """列表 + 今日概览一次返回（省一次往返）。"""
    now = now or datetime.now()
    total = int(
        await session.scalar(select(func.count()).select_from(ProxyJobRun)) or 0
    )
    rows = (
        await session.execute(
            select(ProxyJobRun)
            .order_by(ProxyJobRun.id.desc())
            .limit(max(1, int(limit)))
            .offset(max(0, int(offset)))
        )
    ).scalars().all()
    return {
        "summary": await _day_summary(session, now),
        "total": total,
        "items": [_row_payload(row) for row in rows],
    }


async def get_run(session: AsyncSession, run_id: int) -> dict | None:
    row = await session.get(ProxyJobRun, int(run_id))
    return _row_payload(row) if row is not None else None