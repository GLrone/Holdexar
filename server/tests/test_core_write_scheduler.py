"""写入调度：互斥、优先级、取消、跨进程锁与真实数据库连接收尾。"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.core.database import WritePriority, WriteScheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance", [False, True])
async def test_child_write_is_rejected_without_waiting_for_parent(maintenance):
    from app.core.database import write_gate, write_maintenance_barrier, write_scheduler_diagnostics

    async def child():
        context = write_maintenance_barrier() if maintenance else write_gate()
        with pytest.raises(RuntimeError, match="子任务"):
            async with context:
                pytest.fail("child must not acquire its parent's write lock")

    async with write_gate(label="parent"):
        await asyncio.wait_for(asyncio.create_task(child()), 0.2)
        assert write_scheduler_diagnostics()["depth"] == 1
    assert not write_scheduler_diagnostics()["busy"]


@pytest.mark.asyncio
async def test_single_writer_exclusion():
    """第二个过闸者在第一个释放前不得进入。"""
    s = WriteScheduler()
    inside = asyncio.Event()
    can_leave = asyncio.Event()

    async def holder():
        await s.acquire(WritePriority.BACKGROUND)
        inside.set()
        await can_leave.wait()
        s.release()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(inside.wait(), 1)

    got = asyncio.Event()

    async def second():
        await s.acquire(WritePriority.BACKGROUND)
        got.set()
        s.release()

    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.02)
    assert not got.is_set(), "闸未释放时第二个写者不得进入"
    assert s.snapshot()["waiting_background"] == 1
    can_leave.set()
    await asyncio.wait_for(got.wait(), 1)
    await task
    await t2


@pytest.mark.asyncio
async def test_write_wait_timeout_leaves_owner_and_queue_usable(monkeypatch, tmp_path):
    import app.core.database as dbmod

    monkeypatch.setattr(dbmod, "_WRITE_WAIT_TIMEOUT", 0.03)
    scheduler = WriteScheduler()
    await scheduler.acquire(WritePriority.BACKGROUND)

    async def waiter():
        with pytest.raises(TimeoutError):
            await scheduler.acquire(WritePriority.INTERACTIVE)

    try:
        await asyncio.wait_for(asyncio.create_task(waiter()), 0.3)
        assert scheduler.snapshot()["waiting_interactive"] == 0
        assert scheduler.owned_by_current_task()
    finally:
        scheduler.release()
    await scheduler.acquire(WritePriority.INTERACTIVE)
    scheduler.release()

    lock = await dbmod._CrossProcessWriteLock(tmp_path / "timeout.lock").acquire()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(dbmod._CrossProcessWriteLock(lock.path).acquire(), 0.3)
    finally:
        lock.release()
    retry = await dbmod._CrossProcessWriteLock(lock.path).acquire()
    retry.release()


@pytest.mark.asyncio
async def test_interactive_jumps_ahead_of_background():
    """写者位空出时交互队列先于后台队列拿到移交。"""
    s = WriteScheduler()
    order: list[str] = []

    async def holder():
        await s.acquire(WritePriority.BACKGROUND)
        order.append("hold")
        await asyncio.sleep(0.1)
        s.release()

    async def bg():
        await s.acquire(WritePriority.BACKGROUND)
        order.append("bg")
        s.release()

    async def inter():
        await asyncio.sleep(0.02)
        await s.acquire(WritePriority.INTERACTIVE)
        order.append("inter")
        s.release()

    await asyncio.gather(holder(), bg(), inter())
    assert order == ["hold", "inter", "bg"], f"交互应插到后台前，实际 {order}"


@pytest.mark.asyncio
async def test_same_task_reentry():
    """同任务嵌套过闸按重入放行（批量回退路径依赖），最外层释放后闸归位。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND)
    await s.acquire(WritePriority.INTERACTIVE)
    s.release()
    assert s.snapshot()["busy"]
    s.release()
    assert not s.snapshot()["busy"]
    await asyncio.wait_for(s.acquire(WritePriority.BACKGROUND), 1)
    s.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_stall_gate():
    """排队者被取消后不占队列位，写者位照常移交后续等待者。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND)

    async def waiter():
        await s.acquire(WritePriority.BACKGROUND)
        s.release()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)
    assert s.snapshot()["waiting_background"] == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert s.snapshot()["waiting_background"] == 0, "取消者应移出队列"
    s.release()
    await asyncio.wait_for(s.acquire(WritePriority.BACKGROUND), 1)
    s.release()


@pytest.mark.asyncio
async def test_cancelled_after_handoff_releases_gate():
    """移交已落、等待者尚未恢复时被取消：闸必须由取消路径代为释放。

    这是最危险的取消竞态窗口——fut 已 set_result（闸名下已写到该任务）、
    任务却再也不会从 acquire 恢复。代释放分支缺失会让写者位永久停在
    已消失的任务上，后续所有写者饿死。
    """
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND)

    async def waiter():
        await s.acquire(WritePriority.BACKGROUND)
        s.release()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)
    assert s.snapshot()["waiting_background"] == 1
    # 主任务主动释放：移交发生（fut 已写入结果），但等待者要等事件循环
    # 下一步才会恢复——在它恢复前取消，正好落进竞态窗口
    s.release()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert not s.snapshot()["busy"], "闸不能停在已消失的任务名下"
    await asyncio.wait_for(s.acquire(WritePriority.BACKGROUND), 1)
    s.release()


@pytest.mark.asyncio
async def test_label_flows_to_owner():
    """label 随排队移交落到持闸者（诊断日志按归属输出）。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND, "price_batch")
    assert s._owner_label == "price_batch"
    s.release()
    assert s._owner_label is None


@pytest.mark.asyncio
async def test_metrics_recorded():
    """每次过闸留下等待/持闸耗时观测。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND)
    s.release()
    m = s.metrics[WritePriority.BACKGROUND]
    assert m.count == 1
    assert m.wait_last_ms >= 0
    assert m.hold_last_ms >= 0


@pytest.mark.asyncio
async def test_hold_watchdog_reports_before_gate_is_released(monkeypatch):
    """持闸超过阈值时应在闸仍占用期间记录告警事件。"""
    import app.core.database as dbmod

    monkeypatch.setattr(dbmod, "_HOLD_WARN_MS", 10.0)
    scheduler = WriteScheduler()
    await scheduler.acquire(WritePriority.BACKGROUND, "watchdog_probe")
    try:
        await asyncio.sleep(0.05)
        events = scheduler.diagnostics()["events"]
        assert any(e["kind"] == "hold" and e["label"] == "watchdog_probe" for e in events)
        assert scheduler.snapshot()["busy"], "看门狗只告警，不擅自释放正在使用的事务闸"
    finally:
        scheduler.release()


@pytest.mark.asyncio
async def test_diagnostics_reports_owner_and_metrics():
    """诊断快照：占用归属（优先级+label）与分优先级指标同框输出。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.INTERACTIVE, "wallet_save")
    d = s.diagnostics()
    assert d["busy"] and d["owner_priority"] == "INTERACTIVE"
    assert d["owner_label"] == "wallet_save"
    assert d["metrics"]["INTERACTIVE"]["count"] >= 1
    assert d["metrics"]["BACKGROUND"]["count"] == 0
    s.release()
    d2 = s.diagnostics()
    assert not d2["busy"] and d2["owner_label"] is None


@pytest.mark.asyncio
async def test_gate_autolabels_unlabeled_caller():
    """未显式标注的闸自动取调用栈归属（auto:文件:行号）。

    owner_label=None 的观测盲区曾让 135-233s 级持闸无法点名——任何巨型
    持闸都必须能回答「是谁」。
    """
    from app.core.database import write_gate, write_scheduler_diagnostics

    async with write_gate(WritePriority.BACKGROUND):
        snap = write_scheduler_diagnostics()
        assert snap["busy"]
        assert snap["owner_label"].startswith("auto:"), (
            f"未标注闸应自动归属调用点，实际 {snap['owner_label']}"
        )
        assert "test_core_write_scheduler" in snap["owner_label"]
    assert write_scheduler_diagnostics()["owner_label"] is None


@pytest.mark.asyncio
async def test_gate_keeps_explicit_label():
    """显式 label 优先于自动归属。"""
    from app.core.database import write_gate, write_scheduler_diagnostics

    async with write_gate(WritePriority.BACKGROUND, label="price_batch"):
        assert write_scheduler_diagnostics()["owner_label"] == "price_batch"


@pytest.mark.asyncio
async def test_write_session_begins_immediate_and_holds_sqlite_lock(tmp_path, monkeypatch):
    """write_session 事务起点显式取写锁：持有期间外部连接拿不到 SQLite 写锁，
    提交后释放；begin 耗时记会话属性。"""
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.core.database as dbmod
    from app.core.database import write_session

    db = tmp_path / "ws.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: maker)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))

    async with write_session(WritePriority.BACKGROUND, label="ws_probe") as session:
        await session.execute(text("INSERT INTO t (v) VALUES ('a')"))
        assert session._hx_begin_ms >= 0
        raw = sqlite3.connect(str(db), timeout=0.2, isolation_level=None)
        try:
            raw.execute("BEGIN IMMEDIATE")
            raise AssertionError("write_session 持有期间外部连接不应拿到写锁")
        except sqlite3.OperationalError:
            pass
        finally:
            raw.close()
    raw = sqlite3.connect(str(db), timeout=0.2, isolation_level=None)
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.rollback()
    finally:
        raw.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_nested_write_session_is_rejected(tmp_path, monkeypatch):
    """同任务嵌套新建第二会话会造成 SQLite 自锁，必须在打开内层事务前拒绝。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.core.database as dbmod
    from app.core.database import write_session

    db = tmp_path / "ws2.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: maker)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))

    async with write_session(WritePriority.BACKGROUND, label="outer") as outer:
        assert hasattr(outer, "_hx_begin_ms")
        await outer.execute(text("INSERT INTO t (v) VALUES ('outer')"))
        with pytest.raises(RuntimeError, match="嵌套 write_session"):
            async with write_session(WritePriority.BACKGROUND, label="inner"):
                pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_write_session_under_manual_gate_starts_immediate(tmp_path, monkeypatch):
    """手动 write_gate 只提供调度互斥，内层唯一会话仍要主动取得 SQLite 写锁。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.core.database as dbmod
    from app.core.database import write_gate, write_session

    db = tmp_path / "manual-gate.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: async_sessionmaker(engine, expire_on_commit=False))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))

    async with write_gate(WritePriority.BACKGROUND, label="manual_outer"):
        async with write_session(WritePriority.BACKGROUND, label="inner_session") as session:
            assert hasattr(session, "_hx_begin_ms")
            await session.execute(text("INSERT INTO t DEFAULT VALUES"))
            await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_write_session_autolabels_real_caller(tmp_path, monkeypatch):
    """无标签 write_session 的归属 = 真实调用方（不是 database.py 自己）。

    归属帧链要跳过 contextlib 包装帧与 write_session/write_gate 自身帧——
    否则观测退化为所有无标签写事务都归到 database.py（盲区回归）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.core.database as dbmod
    from app.core.database import write_scheduler_diagnostics, write_session

    db = tmp_path / "ws3.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: async_sessionmaker(engine, expire_on_commit=False))

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))

    async with write_session(WritePriority.BACKGROUND) as session:
        await session.commit()
        label = write_scheduler_diagnostics()["owner_label"]
    assert label.startswith("auto:test_core_write_scheduler"), label
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_cross_process_write_lock_serializes_a_second_process(tmp_path, sync):
    """跨进程写者必须等现持锁进程释放 OS 锁后才进入。"""
    from pathlib import Path

    import app.core.database as dbmod

    lock_path = tmp_path / "shared-write.lock"
    current = await dbmod._CrossProcessWriteLock(lock_path).acquire()
    child_code = (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from app.core.database import _CrossProcessWriteLock\n"
        "async def main():\n"
        "    lock = _CrossProcessWriteLock(Path(sys.argv[1]))\n"
        "    lock.acquire_sync() if sys.argv[2] == 'sync' else await lock.acquire()\n"
        "    print('ACQUIRED', flush=True)\n"
        "    lock.release()\n"
        "asyncio.run(main())\n"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        str(lock_path),
        "sync" if sync else "async",
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.sleep(0.2)
        assert child.returncode is None
    finally:
        current.release()
    stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=5)
    assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert b"ACQUIRED" in stdout


@pytest.mark.asyncio
async def test_many_database_writers_wait_without_borrowing_connections():
    from sqlalchemy import func, select
    from sqlalchemy.dialects.sqlite import insert

    from app.core.database import get_engine, get_session_factory, write_scheduler_diagnostics, write_session
    from app.domains.settings.models import AppSetting

    entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with write_session(label="concurrent_holder") as session:
            entered.set()
            await release.wait()
            await session.commit()

    async def write_batch(index):
        async with write_session(label=f"concurrent_{index}") as session:
            await session.execute(insert(AppSetting), [
                {"key": f"concurrent.{index}.{row}", "value_json": row} for row in range(5)
            ])
            await session.commit()

    owner = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 2)
    writers = [asyncio.create_task(write_batch(index)) for index in range(24)]
    try:
        async with asyncio.timeout(2):
            while write_scheduler_diagnostics()["waiting_background"] != 24:
                await asyncio.sleep(0)
        assert get_engine().pool.checkedout() == 1
    finally:
        release.set()
        await asyncio.gather(owner, *writers)

    async with get_session_factory()() as session:
        count = await session.scalar(select(func.count()).select_from(AppSetting).where(
            AppSetting.key.like("concurrent.%")
        ))
    assert count == 120
    assert get_engine().pool.checkedout() == 0
    assert not write_scheduler_diagnostics()["busy"]


@pytest.mark.asyncio
async def test_database_wait_timeout_preserves_owner_connection_and_allows_retry(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.dialects.sqlite import insert

    import app.core.database as database
    from app.domains.settings.models import AppSetting

    monkeypatch.setattr(database, "_WRITE_WAIT_TIMEOUT", 0.05)
    entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with database.write_session(label="timeout_holder") as session:
            await session.execute(insert(AppSetting).values(key="timeout.owner", value_json=1))
            entered.set()
            await release.wait()
            await session.commit()

    async def waiter():
        with pytest.raises(TimeoutError):
            async with database.write_session(label="timeout_waiter"):
                pytest.fail("blocked writer must not open a transaction")

    owner = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.gather(*(waiter() for _ in range(8)))
        assert database.get_engine().pool.checkedout() == 1
        diagnostics = database.write_scheduler_diagnostics()
        assert diagnostics["busy"]
        assert diagnostics["waiting_background"] == 0
        assert diagnostics["pool"]["checkedOut"] == 1
        assert diagnostics["pool"]["type"] == "AsyncAdaptedQueuePool"
        assert diagnostics["pool"]["size"] >= 1
    finally:
        release.set()
        await owner

    async with database.write_session(label="timeout_retry") as session:
        await session.execute(insert(AppSetting).values(key="timeout.retry", value_json=2))
        await session.commit()
    async with database.get_session_factory()() as session:
        rows = (await session.execute(select(AppSetting.key).where(
            AppSetting.key.like("timeout.%")
        ))).scalars().all()
    assert set(rows) == {"timeout.owner", "timeout.retry"}
    assert database.get_engine().pool.checkedout() == 0
    assert not database.write_scheduler_diagnostics()["busy"]
    assert database.write_scheduler_diagnostics()["pool"]["checkedOut"] == 0
