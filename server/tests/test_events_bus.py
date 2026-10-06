"""事件总线双通道分级测试。

crawl.progress 高频可丢走 progress 通道；关键状态事件走 critical 通道——
满时挤最旧保最新，不静默丢新；get() 关键事件优先。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.events import EventBus


@pytest.mark.asyncio
async def test_critical_delivered_before_progress():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("crawl.progress", done=1)
    bus.publish("job.status", status="running")
    first = await sub.get()
    assert first.type == "job.status"  # 关键事件优先
    second = await sub.get()
    assert second.type == "crawl.progress"
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_progress_queue_full_drops_progress_only():
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(200):  # 超过 progress 通道容量 64
        bus.publish("crawl.progress", done=i)
    assert sub.progress.qsize() == 64  # 满即丢，不阻塞发布方
    for i in range(64):
        ev = await sub.get()
        assert ev.type == "crawl.progress"
    # 关键事件不受 progress 洪泛影响
    bus.publish("job.started", job_id=1)
    ev = await sub.get()
    assert ev.type == "job.started"
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_critical_full_evicts_oldest_keeps_newest():
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(256):
        bus.publish("job.status", seq=i)
    bus.publish("job.status", seq=999)  # 挤掉 seq=0
    seen = []
    while not sub.critical.empty():
        seen.append((await sub.get()).payload["seq"])
    assert seen[0] == 1 and seen[-1] == 999
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_get_wakes_on_first_event():
    """挂起的 get() 在任一通道有事件时被唤醒（交付不丢，先到先出）。"""
    bus = EventBus()
    sub = bus.subscribe()
    getter = asyncio.create_task(sub.get())
    await asyncio.sleep(0.01)
    bus.publish("crawl.progress", done=1)
    ev = await asyncio.wait_for(getter, timeout=1)
    assert ev.type == "crawl.progress"
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_critical_not_lost_when_progress_arrives_first():
    """get() 挂起时 progress 先满足、critical 随后到达：关键事件不得丢。"""
    bus = EventBus()
    sub = bus.subscribe()
    getter = asyncio.create_task(sub.get())
    await asyncio.sleep(0.01)
    bus.publish("crawl.progress", done=1)
    await asyncio.sleep(0.01)  # 保证 progress getter 已消费
    bus.publish("job.started", job_id=7)
    first = await asyncio.wait_for(getter, timeout=1)
    second = await asyncio.wait_for(asyncio.create_task(sub.get()), timeout=1)
    types = {first.type, second.type}
    assert types == {"crawl.progress", "job.started"}
    bus.unsubscribe(sub)
