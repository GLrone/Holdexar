"""proxypool 遥测保留（`retention.prune_telemetry`）：行为测试。

保留策略的三条硬要求：
1. **分块**删除（每块一个事务）——一次删几十万行会长时间持写锁；
2. **删观测不删身份**：`proxy_nodes` / `proxy_node_sources` / `pool_generations` 一行不碰；
3. 作业台账至少留最近 N 行、快照每订阅至少留最新几条（`latest_snapshot` 依赖的那条永不删）。

不做：VACUUM、快照原始 `.bin` 的 GC（P1.9 既有决定）、任何阈值判定。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxypool import retention  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


async def _add_observations(session, count: int, *, at: datetime) -> None:
    for i in range(count):
        await session.execute(
            text(
                "INSERT INTO health_observations (node_id, level, ok, latency_ms, detail, observed_at) "
                "VALUES (:n, 'L0', 1, 10, NULL, :at)"
            ),
            {"n": f"1|node{i}", "at": at},
        )


async def _add_events(session, count: int, *, at: datetime, kind: str = "test") -> None:
    for _ in range(count):
        await session.execute(
            text(
                "INSERT INTO orchestration_events (ts, kind, level, message, payload_json) "
                "VALUES (:at, :kind, 'INFO', 'm', NULL)"
            ),
            {"at": at, "kind": kind},
        )


async def _add_job_runs(session, count: int, *, at: datetime) -> None:
    for _ in range(count):
        await session.execute(
            text(
                "INSERT INTO proxy_job_runs (status, kind, started_at, finished_at, duration_ms, created_at) "
                "VALUES ('success', 'crawl', :at, :at, 1000, :at)"
            ),
            {"at": at},
        )


async def _add_snapshots(session, count: int, *, sub_id: int, at: datetime) -> None:
    for _ in range(count):
        await session.execute(
            text(
                "INSERT INTO subscription_snapshots "
                "(subscription_id, sha256, format, node_count, status, fetched_at) "
                "VALUES (:s, :sha, 'yaml', 1, 'OK', :at)"
            ),
            {"s": sub_id, "sha": "0" * 64, "at": at},
        )


async def _count(session, table: str) -> int:
    return int((await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0)


@pytest.mark.asyncio
async def test_prunes_by_age(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        await _add_observations(session, 3, at=NOW - timedelta(days=40))
        await _add_observations(session, 2, at=NOW - timedelta(days=10))
        await _add_events(session, 2, at=NOW - timedelta(days=100))
        await _add_events(session, 1, at=NOW - timedelta(days=1))
        await _add_job_runs(session, 2, at=NOW - timedelta(days=100))
        await _add_job_runs(session, 1, at=NOW - timedelta(days=1))
        # sub1：4 条过期 + 2 条新；每订阅保留最新 3 条 → 过期里只有最老的 3 条会被删
        await _add_snapshots(session, 4, sub_id=1, at=NOW - timedelta(days=200))
        await _add_snapshots(session, 2, sub_id=1, at=NOW - timedelta(days=1))
        await session.commit()

    async with get_session_factory()() as session:
        result = await retention.prune_telemetry(
            session, NOW, min_job_runs=0  # 本用例只验年龄，不验最小窗口
        )

    assert result.as_dict() == {
        "job_runs": 2,
        "health_observations": 3,
        "orchestration_events": 2,
        "snapshots": 3,
    }
    assert result.truncated is False

    async with get_session_factory()() as session:
        assert await _count(session, "health_observations") == 2
        assert await _count(session, "orchestration_events") == 1
        assert await _count(session, "proxy_job_runs") == 1
        assert await _count(session, "subscription_snapshots") == 3


@pytest.mark.asyncio
async def test_job_runs_keeps_minimum_window(tmp_data_dir):
    """90 天之外也要留最近 200 行（长期不开机的机器仍有历史可看）。"""
    await init_db()
    async with get_session_factory()() as session:
        await _add_job_runs(session, 3, at=NOW - timedelta(days=200))
        await session.commit()

    async with get_session_factory()() as session:
        result = await retention.prune_telemetry(session, NOW, min_job_runs=2)
    assert result.job_runs == 1

    async with get_session_factory()() as session:
        ids = [
            int(v) for v in (
                await session.execute(
                    text("SELECT id FROM proxy_job_runs ORDER BY id")
                )
            ).scalars()
        ]
    assert ids == [2, 3], "保留的必须是**最新**的那几行"


@pytest.mark.asyncio
async def test_snapshots_keep_newest_per_subscription(tmp_data_dir):
    """每个订阅最新 3 条永不删——`latest_snapshot()` 依赖的那条不能被清掉。"""
    await init_db()
    async with get_session_factory()() as session:
        await _add_snapshots(session, 10, sub_id=1, at=NOW - timedelta(days=400))
        await _add_snapshots(session, 2, sub_id=2, at=NOW - timedelta(days=400))
        await session.commit()

    async with get_session_factory()() as session:
        result = await retention.prune_telemetry(session, NOW)
    # sub1: 10-3=7；sub2: 2-2=0
    assert result.snapshots == 7

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT subscription_id, id FROM subscription_snapshots "
                    "ORDER BY subscription_id, id"
                )
            )
        ).all()
    by_sub: dict[int, list[int]] = {}
    for sub_id, sid in rows:
        by_sub.setdefault(int(sub_id), []).append(int(sid))
    assert by_sub[1] == [8, 9, 10]
    assert by_sub[2] == [11, 12]


@pytest.mark.asyncio
async def test_chunk_loop_and_truncation(tmp_data_dir):
    """分块：单轮不超 max_chunks × chunk，剩余留给下一轮（不是失败）。"""
    await init_db()
    async with get_session_factory()() as session:
        await _add_job_runs(session, 12, at=NOW - timedelta(days=200))
        await session.commit()

    async with get_session_factory()() as session:
        first = await retention.prune_telemetry(
            session, NOW, min_job_runs=0, chunk=5, max_chunks=1
        )
    assert (first.job_runs, first.truncated) == (5, True)

    async with get_session_factory()() as session:
        assert await _count(session, "proxy_job_runs") == 7

    async with get_session_factory()() as session:
        second = await retention.prune_telemetry(
            session, NOW, min_job_runs=0, chunk=5, max_chunks=5
        )
    # 剩 7 行：第一块 5、第二块 2（不足一块即收工）
    assert (second.job_runs, second.truncated) == (7, False)
    async with get_session_factory()() as session:
        assert await _count(session, "proxy_job_runs") == 0


@pytest.mark.asyncio
async def test_identity_tables_never_pruned(tmp_data_dir):
    """删观测不删身份：节点/来源/池代一行不碰（它们是历史本身）。"""
    await init_db()
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO proxy_nodes "
                "(node_id, fingerprint, runtime_name, proxy_type, server, normalized_config, "
                " state, first_seen, consecutive_failures) "
                "VALUES ('n1', 'f1', '1|A', 'ss', '10.0.0.1', '{}', 'ACTIVE', :at, 0)"
            ),
            {"at": NOW - timedelta(days=500)},
        )
        await session.execute(
            text(
                "INSERT INTO proxy_node_sources (node_id, subscription_id, original_name, first_seen, last_seen) "
                "VALUES ('n1', 1, 'A', :at, :at)"
            ),
            {"at": NOW - timedelta(days=500)},
        )
        await session.execute(
            text(
                "INSERT INTO pool_generations (generation, pool_sha256, expected_node_count, status, created_at) "
                "VALUES (1, :sha, 1, 'COMMITTED', :at)"
            ),
            {"sha": "1" * 64, "at": NOW - timedelta(days=500)},
        )
        await _add_observations(session, 4, at=NOW - timedelta(days=90))
        await session.commit()

    async with get_session_factory()() as session:
        await retention.prune_telemetry(session, NOW)

    async with get_session_factory()() as session:
        assert await _count(session, "proxy_nodes") == 1
        assert await _count(session, "proxy_node_sources") == 1
        assert await _count(session, "pool_generations") == 1
        assert await _count(session, "health_observations") == 0


@pytest.mark.asyncio
async def test_second_run_deletes_nothing(tmp_data_dir):
    """幂等：清干净之后再来一轮，零删除、零异常。"""
    await init_db()
    async with get_session_factory()() as session:
        await _add_observations(session, 5, at=NOW - timedelta(days=90))
        await session.commit()

    async with get_session_factory()() as session:
        first = await retention.prune_telemetry(session, NOW)
    async with get_session_factory()() as session:
        second = await retention.prune_telemetry(session, NOW)
    assert first.health_observations == 5
    assert second.as_dict() == {
        "job_runs": 0,
        "health_observations": 0,
        "orchestration_events": 0,
        "snapshots": 0,
    }


@pytest.mark.asyncio
async def test_retention_job_is_registered_next_to_wal_truncate(monkeypatch):
    """注册接线：start_scheduler 里真有 04:35 的 cron 清理任务（紧随 04:30 WAL 收缩）。

    只拦 add_job / start 与启动期异步探针，不起真调度器（不污染单例）。
    """
    import app.core.scheduler as sched_mod

    added: dict = {}

    def _fake_add_job(func, trigger, **kw):
        added[kw.get("id")] = (func, trigger, kw)

    async def _noop():
        return None

    monkeypatch.setattr(sched_mod.scheduler, "add_job", _fake_add_job)
    monkeypatch.setattr(sched_mod.scheduler, "start", lambda: None)
    monkeypatch.setattr(sched_mod, "_anchor_probe_after_start", _noop)
    monkeypatch.setattr(sched_mod, "_job_backup_catchup", _noop)
    monkeypatch.setattr(sched_mod, "_job_bartervg_catchup", _noop)

    sched_mod.start_scheduler()

    func, trigger, kw = added["proxypool_retention"]
    assert func is sched_mod._job_proxypool_retention
    assert trigger == "cron"
    assert (kw.get("hour"), kw.get("minute")) == (4, 35)
    assert kw.get("max_instances") == 1
    # 紧邻既有 WAL 收缩（04:30），不撞同一分钟
    _, wal_trigger, wal_kw = added["wal_truncate"]
    assert (wal_kw.get("hour"), wal_kw.get("minute")) == (4, 30)


@pytest.mark.asyncio
async def test_count_helper(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        await _add_observations(session, 3, at=NOW - timedelta(days=40))
        await _add_observations(session, 1, at=NOW - timedelta(days=1))
        await session.commit()
    async with get_session_factory()() as session:
        assert await retention.count_old_observations(session, NOW, 30) == 3
