"""proxypool 周期的写事务边界：L0 提交一次，维护（重建 + L1/L2）再提交一次。

L1/L2 要串行探完池内节点，整段落在一个未提交事务里会让 SQLite 写锁跨分钟被占住，
同拍的订阅刷新/账单等 job 撞满 `busy_timeout` 后整批失败。本文件钉住"两段提交"确实存在。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
import yaml
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import health as health_mod
from app.domains.proxypool import scheduling as sched
from app.domains.proxypool.models import HealthObservation, ProxyNode

NOW = datetime(2026, 9, 20, 22, 0, 0)


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


class _EmptyResult:
    """空查询结果：对齐步骤的只读查询在假 session 上按空表处理。"""

    def scalars(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter([])


class _Recorder:
    """只记录 commit 顺序的假 session（各阶段都被替换掉，不需要真 DB）。

    周期开头还有两账本对齐的只读查询（来源补回 / 体检账本），假 session 按空表
    返回，使这些步骤以零写入的空转形态通过而不改变 commit 序列。
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def execute(self, *args, **kwargs) -> _EmptyResult:
        return _EmptyResult()

    async def scalars(self, *args, **kwargs) -> list:
        return []

    def add(self, *args, **kwargs) -> None:
        return None


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
    """池内 L0 按块提交：写锁窗口 = 一块的耗时，不随池规模线性增长。

    两账本对齐自身也是写事务（过写调度器提交一次），其后每块各提交一次。
    """
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
    assert rec.events == ["commit"] * 4, f"对齐 1 次 + 每块 1 次提交，实际 {rec.events}"


def _write_lock_free(timeout: float = 2.0) -> bool:
    """另一条连接能否立即拿到写锁（外部 sqlite3 连接，2 秒 busy_timeout）。"""
    db_path = get_settings().db_url.removeprefix("sqlite+aiosqlite:///")
    conn = sqlite3.connect(db_path, timeout=timeout)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_maintenance_probe_holds_no_write_lock_across_probes(
    tmp_data_dir, monkeypatch
):
    """维护探测的写锁不得跨网络探测段：探针进行中与恢复 GLOBAL 时，
    另一条连接必须能立即写（否则同拍写者排队甚至撞满 busy_timeout）。"""
    await init_db()

    during: dict[str, bool] = {}

    async def fake_l1(session, **kw):  # noqa: ANN001
        during["l1"] = _write_lock_free()
        return ()

    async def fake_l2(session, **kw):  # noqa: ANN001
        during["l2"] = _write_lock_free()
        return ()

    async def fake_current(*a, **kw):
        return "1|node-a"

    async def fake_apply(controller_url, secret, chosen):
        during["apply"] = _write_lock_free()

    async def fake_names(*a, **kw):
        return ("1|node-a",)

    async def fake_snapshot(*a, **kw):
        return ()

    monkeypatch.setattr(sched, "crawler_busy", lambda: False)
    monkeypatch.setattr(sched, "exit_ip_check_pool", fake_l1)
    monkeypatch.setattr(sched, "business_check_pool", fake_l2)
    monkeypatch.setattr(sched, "current_global_selection", fake_current)
    monkeypatch.setattr(sched, "apply_global_selection", fake_apply)
    monkeypatch.setattr(sched, "eligible_runtime_names", fake_names)
    monkeypatch.setattr(sched, "exit_snapshot", fake_snapshot)

    async with get_session_factory()() as s:
        result = await sched.run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", now=NOW,
        )
        await s.commit()

    assert result is not None
    assert during["l1"], "L1 探测进行中写锁必须已放开（run 行已先行提交）"
    assert during["l2"], "L2 探测进行中写锁必须已放开（L1 返回前已提交）"
    assert during["apply"], "恢复 GLOBAL（网络调用）前写锁必须已放开（收尾已提交）"


@pytest.mark.asyncio
async def test_probe_pools_return_committed_session(tmp_data_dir, monkeypatch):
    """两个探测池函数返回前必须提交：会话零脏对象交还，调用方后续只读查询
    不会 autoflush 出横跨网络等待的写事务。"""
    await init_db()
    (tmp_data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    (tmp_data_dir / "proxypool" / "crawl-pool.yaml").write_text(
        yaml.safe_dump({"proxies": [
            {"name": "1|n1", "type": "http", "server": "10.0.0.1", "port": 1},
        ]}),
        encoding="utf-8",
    )
    async with get_session_factory()() as s:
        s.add(ProxyNode(
            node_id="fp-n1", fingerprint="fp-n1", runtime_name="1|n1",
            proxy_type="http", server="10.0.0.1",
            normalized_config={"name": "n1"},
            state="NEW", first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
        ))
        await s.commit()

    async def fake_probe_exit_ip(controller_url, secret, mixed_port, name, **kw):
        return health_mod.ExitIpResult(
            runtime_name=name, ok=True, exit_ip="203.0.113.9",
            detail="ok", latency_ms=1,
        )

    async def fake_probe_business(controller_url, secret, mixed_port, name, **kw):
        return health_mod.BusinessResult(
            runtime_name=name, ok=True, http_status=200,
            final_price_in_cents=999, detail="ok", latency_ms=1,
        )

    monkeypatch.setattr(health_mod, "mixed_port_of", lambda data_dir: 0)
    monkeypatch.setattr(health_mod, "probe_exit_ip", fake_probe_exit_ip)
    monkeypatch.setattr(health_mod, "probe_business", fake_probe_business)

    async with get_session_factory()() as s:
        l1 = await health_mod.exit_ip_check_pool(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", now=NOW, names=("1|n1",),
        )
        assert l1 and not s.in_transaction(), "L1 池函数返回前必须提交"
        l2 = await health_mod.business_check_pool(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", now=NOW,
        )
        assert l2 and not s.in_transaction(), "L2 池函数返回前必须提交"