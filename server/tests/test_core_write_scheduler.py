"""全局写调度器：单写者互斥、交互优先、同任务重入、取消安全、指标留痕。

SQLite 同一时刻只允许一个写事务；调度器把「谁下一个写」收敛到事件循环内
一处仲裁。这里钉住调度语义本身（不碰数据库）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.database import WritePriority, WriteScheduler


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
async def test_metrics_recorded():
    """每次过闸留下等待/持闸耗时观测。"""
    s = WriteScheduler()
    await s.acquire(WritePriority.BACKGROUND)
    s.release()
    m = s.metrics[WritePriority.BACKGROUND]
    assert m.count == 1
    assert m.wait_last_ms >= 0
    assert m.hold_last_ms >= 0
