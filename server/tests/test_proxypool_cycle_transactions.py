"""proxypool 周期的写事务边界：L0 提交一次，维护（重建 + L1/L2）再提交一次。

L1/L2 要串行探完池内节点，整段落在一个未提交事务里会让 SQLite 写锁跨分钟被占住，
同拍的订阅刷新/账单等 job 撞满 `busy_timeout` 后整批失败。本文件钉住"两段提交"确实存在。
"""
from __future__ import annotations

import os

from datetime import datetime

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import scheduling as sched
from app.domains.proxypool.models import HealthObservation

NOW = datetime(2026, 9, 20, 22, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则「缓存库 ≠ 当前配置库」判据判定相等，临时引擎会留给后续文件
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


class _Recorder:
    """只记录 commit 顺序的假 session（各阶段都被替换掉，不需要真 DB）。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")


@pytest.mark.asyncio
async def test_cycle_commits_after_l0_before_maintenance(tmp_data_dir, monkeypatch):
    """顺序必须是 L0 → commit → 重建 → 维护 → commit（调用方那次）。"""
    rec = _Recorder()

    async def _l0(session, **kw):  # noqa: ANN001
        rec.events.append("l0")
        return ("l0",)

    async def _rebuild(session, **kw):  # noqa: ANN001
        rec.events.append("rebuild")
        return None

    async def _maint(session, **kw):  # noqa: ANN001
        rec.events.append("maintenance")
        return "m"

    monkeypatch.setattr(sched, "run_l0_cycle", _l0)
    monkeypatch.setattr(sched, "run_pending_rebuild", _rebuild)
    monkeypatch.setattr(sched, "run_maintenance_cycle", _maint)
    monkeypatch.setattr(sched, "crawler_busy", lambda: False)

    result = await sched.run_proxypool_cycle(
        rec, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
        secret="s", runtime=object(), exe_path="x", now=NOW,
    )
    await rec.commit()          # 调度器 job 里那次收尾提交

    assert rec.events == ["l0", "commit", "rebuild", "maintenance", "commit"]
    assert result.busy is False


@pytest.mark.asyncio
async def test_l0_writes_visible_to_other_connection_during_maintenance(
    tmp_data_dir, monkeypatch
):
    """维护开始时，L0 的写入必须已对**另一条连接**可见（= 已提交、写锁已放开）。"""
    await init_db()
    seen: dict[str, int] = {}

    async def _l0(session, **kw):  # noqa: ANN001
        session.add(HealthObservation(node_id="probe-node", level="L0", ok=True,
                                      observed_at=NOW))
        await session.flush()
        return ("l0",)

    async def _maint(session, **kw):  # noqa: ANN001
        async with get_session_factory()() as other:
            seen["l0_rows"] = int(await other.scalar(
                select(func.count()).select_from(HealthObservation)
                .where(HealthObservation.level == "L0")) or 0)
        session.add(HealthObservation(node_id="probe-node", level="L1", ok=True,
                                      observed_at=NOW))
        return "m"

    async def _no_rebuild(session, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(sched, "run_l0_cycle", _l0)
    monkeypatch.setattr(sched, "run_pending_rebuild", _no_rebuild)
    monkeypatch.setattr(sched, "run_maintenance_cycle", _maint)
    monkeypatch.setattr(sched, "crawler_busy", lambda: False)

    async with get_session_factory()() as s:
        await sched.run_proxypool_cycle(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", runtime=object(), exe_path="x", now=NOW,
        )
        await s.commit()

    assert seen["l0_rows"] == 1, "维护开始时 L0 必须已提交（写锁已放开）"

    async with get_session_factory()() as s:
        levels = dict((await s.execute(
            select(HealthObservation.level, func.count())
            .group_by(HealthObservation.level))).all())
    assert levels == {"L0": 1, "L1": 1}, "两段写入都必须最终落库"


@pytest.mark.asyncio
async def test_l0_commits_in_chunks(tmp_data_dir, monkeypatch):
    """池内 L0 按块提交：写锁窗口 = 一块的耗时，不随池规模线性增长。"""
    rec = _Recorder()
    chunks: list[tuple[str, ...]] = []

    async def _fake_pool(session, **kw):  # noqa: ANN001
        chunks.append(tuple(kw["names"]))
        return ()

    async def _empty(*a, **kw):
        return ()

    monkeypatch.setattr(sched, "health_check_pool", _fake_pool)
    monkeypatch.setattr(sched, "eligible_runtime_names", _empty)
    monkeypatch.setattr(sched, "request_rebuild", lambda: None)

    names = [f"1|n{i}" for i in range(23)]
    (tmp_data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    (tmp_data_dir / "proxypool" / "crawl-pool.yaml").write_text(
        yaml.safe_dump({"proxies": [{"name": n, "type": "http",
                                     "server": "10.0.0.1", "port": 1} for n in names]}),
        encoding="utf-8",
    )

    await sched.run_l0_cycle(rec, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
                             secret="s", now=NOW)

    assert [len(c) for c in chunks] == [10, 10, 3], f"分块应为 10/10/3，实际 {[len(c) for c in chunks]}"
    assert rec.events == ["commit", "commit", "commit"], "每块后都要提交（写锁窗口受块约束）"
