"""爬取调度器核心行为验收（app 任务主队列）。

纯内存：假 router / 假 http_client，不触库不出网。覆盖：
- N worker 并发消费 + 队列排干后 run() 正常收尾、计数口径正确；
- handler 动态重推（context.queue.put）会被继续消费，done 按任务 id 去重；
- stop_event 置位：取消 worker、排干剩余队列、run() 不挂起；
- failure_ledger 并入「失败」（browse 层重试耗尽的账本不计入就是假数字）；
- 岗位池（Coordinator）：worker 逐任务租借 lane、坏 lane 隔离换岗、待用池顶替。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.router import CrawlerContext, CrawlerRouter  # noqa: E402
from app.crawler.scheduler import CrawlerScheduler  # noqa: E402


class _FakeClient:
    """scheduler 只透传 http_client，行为由 handler 自定，占位即可。"""


def _make_scheduler(handler, *, workers: int = 3, ledger=None) -> CrawlerScheduler:
    router = CrawlerRouter()
    router.handle("app")(handler)
    return CrawlerScheduler(
        router, _FakeClient(), None, worker_count=workers,
        stop_event=asyncio.Event(), failure_ledger=ledger,
    )


@pytest.mark.asyncio
async def test_workers_consume_all_tasks_and_drain():
    """6 个任务 × 3 worker：全部消费、队列排干、成功计数 = 任务数。"""
    record: list = []

    async def _app(context: CrawlerContext) -> None:
        await asyncio.sleep(0.01)
        record.append(context.task["id"])

    sched = _make_scheduler(_app)
    await sched.run([{"type": "app", "id": i} for i in range(6)], session=None)

    assert sorted(record) == list(range(6))
    # 收尾会投毒丸后 cancel（worker 可能来不及消费）——残留的是 None 而非任务；
    # 调度器对象一次运行即弃，无需清空，这里只断言无未处理任务残留
    leftovers = []
    while not sched.queue.empty():
        leftovers.append(sched.queue.get_nowait())
    assert all(t is None for t in leftovers)
    assert sched.counts() == (6, 6, 0)


@pytest.mark.asyncio
async def test_handler_repush_is_consumed_and_done_deduped():
    """handler 重推（同 id 二次投递）会被继续消费，done 按 id 去重不虚高。"""
    record: list = []

    async def _app(context: CrawlerContext) -> None:
        record.append((context.task["id"], context.task.get("retries", 0)))
        if context.task["id"] == 1 and "retries" not in context.task:
            await context.queue.put({**context.task, "retries": 1})

    sched = _make_scheduler(_app, workers=2)
    await sched.run([{"type": "app", "id": 1}, {"type": "app", "id": 2}], session=None)

    assert (1, 0) in record and (1, 1) in record  # 重推被消费
    assert sched.total_processed == 3  # 原始处理次数含重推
    assert sched.counts()[0] == 2  # 去重口径：id 1 只计一次 done


@pytest.mark.asyncio
async def test_stop_event_cancels_workers_and_drains_queue():
    """停止信号：worker 取消、剩余队列排干、run() 立即返回不挂起。"""
    started = asyncio.Event()

    async def _slow(context: CrawlerContext) -> None:
        started.set()
        await asyncio.sleep(30)

    sched = _make_scheduler(_slow, workers=2)
    run_task = asyncio.create_task(
        sched.run([{"type": "app", "id": i} for i in range(10)], session=None)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    sched.stop_event.set()

    await asyncio.wait_for(run_task, timeout=5)  # 不挂起即过
    assert sched.queue.empty()


@pytest.mark.asyncio
async def test_failure_ledger_merged_into_fail_count():
    """browse 层失败账本并入对外口径：success = done − fail，不虚报全成。"""
    async def _app(context: CrawlerContext) -> None:
        return None

    sched = _make_scheduler(_app, ledger=["cn:1", "ru:2"])
    await sched.run([{"type": "app", "id": 1}], session=None)

    done, ok, fails = sched.counts()
    assert (done, fails) == (1, 2)
    assert ok == 0  # max(0, done - fails)


@pytest.mark.asyncio
async def test_progress_events_all_published_before_run_returns():
    """run() 返回时全部 crawl.progress 必须已发布到总线。

    收尾方（_finish_job 的 job.status）在 run() 返回之后才发布——末条进度
    若压在它后面到达，客户端会把已收束的任务重新置回运行态，进度条卡死
    100% 直到下一个任务的事件来纠正。worker 数 = 任务数时末条进度与收尾
    最容易同拍，是最紧的排序窗口。
    """
    from app.core.events import bus

    total = 6

    async def _app(context: CrawlerContext) -> None:
        await asyncio.sleep(0.01)

    queue = bus.subscribe()
    try:
        sched = _make_scheduler(_app, workers=total)
        await sched.run([{"type": "app", "id": i} for i in range(total)], session=None)
    finally:
        bus.unsubscribe(queue)

    progress = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.type == "crawl.progress":
            progress.append(event.payload)
    assert progress, "run() 返回前应已发布进度事件"
    assert progress[-1]["done"] == total, "run() 返回时末条进度必须已发布"


@pytest.mark.asyncio
async def test_worker_switches_to_spare_lane_on_dead_neighbour(monkeypatch):
    """岗位池换岗：一条 lane 坏掉时 worker 换到待用 lane 继续干活，岗位数不掉。

    worker_count=1、lane_count=3（2 条待用）：首个租到的 lane 立即标记不健康，
    后续任务必须由待用 lane 完成——不得因为坏 lane 卡住队列。
    """
    from app.crawler import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_LANE_COOLDOWN_SECONDS", 999.0)

    class _LaneClient:
        def __init__(self, unhealthy: bool) -> None:
            self.unhealthy = unhealthy

        @property
        def lane_unhealthy(self) -> bool:
            return self.unhealthy

    dead, spare = _LaneClient(True), _LaneClient(False)
    clients = [dead, spare]
    used: list = []

    async def _app(context: CrawlerContext) -> None:
        used.append(getattr(context.http_client, "unhealthy", None))

    router = CrawlerRouter()
    router.handle("app")(_app)
    sched = CrawlerScheduler(
        router, _FakeClient(), None, worker_count=1,
        stop_event=asyncio.Event(), client_factory=lambda i: clients[i],
        lane_count=2,
    )
    await sched.run([{"type": "app", "id": "A"}, {"type": "app", "id": "B"}],
                    session=None)

    assert used == [False, False], "坏 lane 被隔离后由待用 lane 完成全部任务"
    assert dead in sched._cooldown_lanes, "坏 lane 进冷却区等待探测复活"
    assert spare not in sched._cooldown_lanes, "健康 lane 仍在岗位池"


@pytest.mark.asyncio
async def test_unhealthy_lane_worker_parks_then_recovers(monkeypatch):
    """死通道冷却：lane_unhealthy 的 worker 暂停拉任务，冷却到期放一发探测，
    通道恢复后队列照常消化（只降速，不停摆）。"""
    from app.crawler import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_LANE_COOLDOWN_SECONDS", 0.05)

    class _LaneClient:
        """lane_unhealthy 可控的假客户端（真实标记由 SteamHttpClient 维护）。"""

        def __init__(self) -> None:
            self.unhealthy = True

        @property
        def lane_unhealthy(self) -> bool:
            return self.unhealthy

    client = _LaneClient()
    record: list = []

    async def _app(context: CrawlerContext) -> None:
        record.append(context.task["id"])
        client.unhealthy = False  # 探测任务成功 → 通道恢复（真实路径是请求成功复位计数）

    router = CrawlerRouter()
    router.handle("app")(_app)
    sched = CrawlerScheduler(
        router, _FakeClient(), None, worker_count=1,
        stop_event=asyncio.Event(), client_factory=lambda _wid: client,
    )
    await sched.run([{"type": "app", "id": "A"}], session=None)
    assert record == ["A"], "冷却到期探测放行，任务最终被消费"
    assert sched.counts() == (1, 1, 0)
