"""proxypool 遥测保留：删观测、**不删身份**。

保留期按实际写入速率定（推导见
`docs/PROXYPOOL_HANDOVER_P1.7_NEXT.md` §14.2）：

| 表 | 速率 | 保留 |
|---|---|---|
| `proxy_job_runs` | 十几~几十行/天 | 90 天，且至少留最近 200 行 |
| `health_observations` | **L0 每 5min × 池内每个节点** → 68 节点 ≈ 19.6k 行/天 | 30 天 |
| `orchestration_events` | 事件驱动，低频 | 90 天 |
| `subscription_snapshots` | 每次抓取一行（行小） | 180 天，且每订阅最新 3 条永不删 |
| `proxy_nodes` / `proxy_node_sources` | 节点身份与来源 | **永不清理** |
| `pool_generations` | 每次重建一行（审计链） | 不清理 |
| 快照原始 `.bin` | 内容寻址、只增不减 | 本模块不 GC（P1.9 既有决定） |

**分块删除是硬要求**：一次删几十万行会长时间持写锁，与爬取/调度抢锁。这里每块
`chunk` 行一个事务（`session.commit()` 在循环内），并有一轮最多 `max_chunks` 块的
上限——删不完留给下一轮，日志记差额。

删行**不会**让 SQLite 文件立刻变小（只把页标空、之后复用）：本模块的效果是
「增长封顶」，不是缩盘，**不要**为此加 VACUUM。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool.models import SubscriptionSnapshot

logger = logging.getLogger(__name__)

DEFAULT_JOB_DAYS = 90
DEFAULT_MIN_JOB_RUNS = 200
DEFAULT_OBS_DAYS = 30
DEFAULT_EVENT_DAYS = 90
DEFAULT_SNAPSHOT_DAYS = 180
DEFAULT_CHUNK = 5000
DEFAULT_MAX_CHUNKS = 20
# 每个订阅保留的快照行数：最新 3 条覆盖「最新 OK + 最新 FAILED + 1 条余量」，
# `latest_snapshot()` 依赖的那条永远不会被删。
KEEP_SNAPSHOTS_PER_SUB = 3


@dataclass(frozen=True)
class PruneResult:
    job_runs: int
    health_observations: int
    orchestration_events: int
    snapshots: int
    # 有一轮撞到 max_chunks 上限（还有剩）——下一轮继续，不是失败
    truncated: bool

    def as_dict(self) -> dict[str, int]:
        return {
            "job_runs": self.job_runs,
            "health_observations": self.health_observations,
            "orchestration_events": self.orchestration_events,
            "snapshots": self.snapshots,
        }


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=int(days))


async def _delete_chunked(
    session: AsyncSession, sql: str, params: dict, *, chunk: int, max_chunks: int
) -> tuple[int, bool]:
    """按块删除，每块一个事务。返回 (删除总数, 是否还有剩)。"""
    deleted = 0
    for _ in range(max(1, int(max_chunks))):
        result = await session.execute(
            text(sql), {**params, "chunk": int(chunk)}
        )
        await session.commit()
        affected = int(result.rowcount or 0)
        deleted += affected
        if affected < int(chunk):
            return deleted, False
    return deleted, True


async def _keep_ids_per_subscription(session: AsyncSession) -> list[int]:
    """每个订阅保留最新 `KEEP_SNAPSHOTS_PER_SUB` 条快照行的 id。

    在 Python 里算而不是写窗口函数：订阅只有个位数，可读性优先；`ROW_NUMBER`
    也能做，但为一个「保留几行」的规则引入窗口函数不值得。
    """
    sub_ids = (
        await session.execute(
            select(SubscriptionSnapshot.subscription_id).distinct()
        )
    ).scalars().all()
    keep: list[int] = []
    for sub_id in sub_ids:
        rows = (
            await session.execute(
                select(SubscriptionSnapshot.id)
                .where(SubscriptionSnapshot.subscription_id == sub_id)
                .order_by(SubscriptionSnapshot.id.desc())
                .limit(KEEP_SNAPSHOTS_PER_SUB)
            )
        ).scalars().all()
        keep.extend(int(v) for v in rows if v is not None)
    return keep


async def prune_telemetry(
    session: AsyncSession,
    now: datetime,
    *,
    job_days: int = DEFAULT_JOB_DAYS,
    min_job_runs: int = DEFAULT_MIN_JOB_RUNS,
    obs_days: int = DEFAULT_OBS_DAYS,
    event_days: int = DEFAULT_EVENT_DAYS,
    snapshot_days: int = DEFAULT_SNAPSHOT_DAYS,
    chunk: int = DEFAULT_CHUNK,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> PruneResult:
    """一次保留轮次：幂等、分块、fail-soft 由调用方兜（步骤异常只留日志）。"""
    truncated = False

    job_runs, more = await _delete_chunked(
        session,
        """
        DELETE FROM proxy_job_runs WHERE id IN (
            SELECT id FROM proxy_job_runs
            WHERE started_at IS NOT NULL AND started_at < :cutoff
              AND id NOT IN (
                  SELECT id FROM proxy_job_runs ORDER BY id DESC LIMIT :min_runs
              )
            ORDER BY id LIMIT :chunk
        )
        """,
        {"cutoff": _cutoff(now, job_days), "min_runs": int(min_job_runs)},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    truncated = truncated or more

    observations, more = await _delete_chunked(
        session,
        """
        DELETE FROM health_observations WHERE id IN (
            SELECT id FROM health_observations
            WHERE observed_at IS NOT NULL AND observed_at < :cutoff
            ORDER BY id LIMIT :chunk
        )
        """,
        {"cutoff": _cutoff(now, obs_days)},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    truncated = truncated or more

    events, more = await _delete_chunked(
        session,
        """
        DELETE FROM orchestration_events WHERE id IN (
            SELECT id FROM orchestration_events
            WHERE ts IS NOT NULL AND ts < :cutoff
            ORDER BY id LIMIT :chunk
        )
        """,
        {"cutoff": _cutoff(now, event_days)},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    truncated = truncated or more

    keep = await _keep_ids_per_subscription(session)
    keep_sql = ",".join(str(int(v)) for v in keep) if keep else "NULL"
    snapshots, more = await _delete_chunked(
        session,
        f"""
        DELETE FROM subscription_snapshots WHERE id IN (
            SELECT id FROM subscription_snapshots
            WHERE fetched_at IS NOT NULL AND fetched_at < :cutoff
              AND id NOT IN ({keep_sql})
            ORDER BY id LIMIT :chunk
        )
        """,
        {"cutoff": _cutoff(now, snapshot_days)},
        chunk=chunk,
        max_chunks=max_chunks,
    )
    truncated = truncated or more

    result = PruneResult(job_runs, observations, events, snapshots, truncated)
    if any(result.as_dict().values()):
        logger.info(
            "[proxypool保留] 已清理：作业 %d / 健康观测 %d / 编排事件 %d / 快照 %d%s",
            result.job_runs, result.health_observations,
            result.orchestration_events, result.snapshots,
            "（本轮达块上限，剩余下轮继续）" if truncated else "",
        )
    return result


async def count_old_observations(session: AsyncSession, now: datetime, days: int) -> int:
    """测试/排查用：当前超出保留期的健康观测行数。"""
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM health_observations "
            "WHERE observed_at IS NOT NULL AND observed_at < :cutoff"
        ),
        {"cutoff": _cutoff(now, days)},
    )
    return int(result.scalar() or 0)
