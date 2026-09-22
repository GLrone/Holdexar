"""asyncio 生产者-消费者任务调度器。

要点：
- 计数器周期日志
- asyncio.Event 停止信号
- 每个任务完成向 SSE 事件总线发布 crawl.progress
- **worker ↔ 入口绑定**：多入口形态下每个 worker 在整个 run 内固定使用一条 lane
  （`client_factory(worker_id)` 决定），不再所有 worker 挤同一个入口。Session 只
  负责连接池生命周期，不承担"换出口"职责。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

import aiohttp

from app.core.events import bus

from .router import CrawlerContext, CrawlerRouter

logger = logging.getLogger(__name__)

_PROGRESS_LOG_EVERY = 25

# 每任务连接预算（TCPConnector 参数）
_PER_TASK_CONNECTOR_LIMIT = 20
_PER_TASK_DNS_CACHE = 60


class CrawlerScheduler:
    """基于 asyncio.Queue 的高可用任务调度器（生产者-消费者模型）。

    `client_factory(worker_id) -> SteamHttpClient` 可选：给了就每个 worker 用它取
    自己的客户端（多入口形态，一 worker 一 lane）；不给则全体共用 `http_client`
    （单入口形态，行为不变）。
    """

    def __init__(
        self, router, http_client, db_writer, worker_count=30, stop_event=None,
        failure_ledger: list | None = None,
        error_sink: Callable[[BaseException], None] | None = None,
        client_factory: Callable[[int], object] | None = None,
    ):
        self.router = router
        self.http_client = http_client
        self.db_writer = db_writer
        self.worker_count = worker_count
        self.client_factory = client_factory
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stop_event = stop_event or asyncio.Event()
        # 错误分类回调（生产作业台账用）：worker 捕获到的异常在这里分类计数。
        # **None = 不记账**（CLI/测试不必知道台账）。browse 层重试耗尽的失败不抛异常、
        # 只进 failure_ledger，见 run_crawl 侧的补记。
        self.error_sink = error_sink

        # 统计
        self.success_count = 0
        self.skip_count = 0
        self.fail_count = 0
        self.total_processed = 0
        self.skip_no_discount_count = 0
        self.discount_ended_count = 0
        self._counter_lock = asyncio.Lock()

        # 进度发布口径（crawl.progress 的 done/ok/fail/total 都从这里取）：
        # · failure_ledger = browse 层的 FAILED_TASKS 账本（重试耗尽的批次从不
        #   向调度器抛异常，只记账本——不看账本「失败」恒为 0，是假数字）；
        # · _completed_ids 按任务 id 去重：断网守卫/退避重推会把同一任务再投
        #   递处理，原始 total_processed 会把同一批计两次（done 虚高）；
        # · total_target 在 run() 里定死为初始任务量，不随队列/重推波动。
        self._failure_ledger = failure_ledger
        self._completed_ids: set[str] = set()
        self.total_target = 0

        # 速率统计（10s 窗口）
        self._speed_window: deque[float] = deque()
        self._speed_lock = asyncio.Lock()

    async def _record_speed(self) -> None:
        async with self._speed_lock:
            now = time.time()
            self._speed_window.append(now)
            cutoff = now - 10
            while self._speed_window and self._speed_window[0] < cutoff:
                self._speed_window.popleft()

    def _get_speed(self) -> float:
        return len(self._speed_window) / 10.0

    def _derived_counts(self) -> tuple[int, int, int]:
        """对外口径 (done, ok, fail)：done 按 id 去重，失败并入爬取账本。"""
        fails = self.fail_count + (
            len(self._failure_ledger) if self._failure_ledger is not None else 0
        )
        done = len(self._completed_ids)
        return done, max(0, done - fails), fails

    def counts(self) -> tuple[int, int, int]:
        """任务收尾统计（run_crawl 返回值用）：与进度事件同一口径。"""
        return self._derived_counts()

    def _publish_progress(self) -> None:
        done, ok, fails = self._derived_counts()
        bus.publish(
            "crawl.progress",
            done=done,
            ok=ok,
            fail=fails,
            total=self.total_target or done,
            qsize=self.queue.qsize(),
            speed=round(self._get_speed(), 2),
        )

    async def _worker(self, worker_id: int, session) -> None:
        # 本 worker 固定使用的入口客户端：多入口形态下一 worker 一条 lane，
        # 整个 run 不换（换入口由 Runtime 的重建/重绑负责，不在这里发生）
        http_client = (
            self.client_factory(worker_id) if self.client_factory is not None
            else self.http_client
        )
        while not self.stop_event.is_set():
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break

            if task is None:  # 毒丸信号
                self.queue.task_done()
                break

            task_type = task.get("type", "unknown")
            task_id = task.get("id", "unknown")
            try:
                if task_type == "app":
                    connector = aiohttp.TCPConnector(
                        limit=_PER_TASK_CONNECTOR_LIMIT, ttl_dns_cache=_PER_TASK_DNS_CACHE
                    )
                    async with aiohttp.ClientSession(connector=connector) as task_session:
                        context = CrawlerContext(
                            task, http_client, self.db_writer, task_session
                        )
                        context.queue = self.queue
                        context.stop_event = self.stop_event
                        await self.router.route(context)
                else:
                    context = CrawlerContext(task, http_client, self.db_writer, session)
                    context.queue = self.queue
                    context.stop_event = self.stop_event
                    await self.router.route(context)
                async with self._counter_lock:
                    self.total_processed += 1
                    self.success_count += 1
                    self._completed_ids.add(str(task_id))
            except Exception as e:
                async with self._counter_lock:
                    self.total_processed += 1
                    self.fail_count += 1
                    self._completed_ids.add(str(task_id))
                if self.error_sink is not None:
                    try:
                        self.error_sink(e)
                    except Exception:  # noqa: BLE001 —— 记账回调绝不能拖垮 worker
                        logger.exception("[Worker-%d] 错误分类回调异常", worker_id)
                logger.error("[Worker-%d] %s:%s 异常: %s", worker_id, task_type, task_id, e)
            finally:
                self.queue.task_done()
                await self._record_speed()
                if self.total_processed % _PROGRESS_LOG_EVERY == 0:
                    done, ok, fails = self._derived_counts()
                    logger.info(
                        "进度: %d 完成 (成功 %d / 失败 %d), 队列剩余 %d, 速度 %.1f t/s",
                        done,
                        ok,
                        fails,
                        self.queue.qsize(),
                        self._get_speed(),
                    )
                self._publish_progress()

    async def run(self, initial_tasks: list[dict], session) -> None:
        """将初始任务打入队列，启动 workers，等待队列清空（含动态追加任务）。

        停止语义：stop_event 置位后立即取消全部 Worker 并清空队列——
        不能等 queue.join()（停止后剩余任务无人处理，join 永远挂起）。
        """
        total_initial = len(initial_tasks)
        self.total_target = total_initial
        for task in initial_tasks:
            await self.queue.put(task)

        logger.info(
            "启动 %d 个 Worker，队列初始任务数: %d", self.worker_count, total_initial
        )

        workers = [
            asyncio.create_task(self._worker(i, session)) for i in range(self.worker_count)
        ]
        start_time = time.time()

        stop_wait = asyncio.ensure_future(self.stop_event.wait())
        join_task = asyncio.ensure_future(self.queue.join())
        await asyncio.wait({join_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)

        if self.stop_event.is_set() and not join_task.done():
            logger.info("收到停止信号：取消 %d 个 Worker 并清空队列", len(workers))
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # 排干剩余队列，保持计数器一致
            while True:
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break
        else:
            for _ in workers:
                await self.queue.put(None)
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        stop_wait.cancel()
        join_task.cancel()

        elapsed = time.time() - start_time
        extras = []
        if self.skip_no_discount_count:
            extras.append(f"未打折跳过 {self.skip_no_discount_count}")
        if self.discount_ended_count:
            extras.append(f"打折结束 {self.discount_ended_count}")
        done, ok, fails = self._derived_counts()
        speed = (done / elapsed) if elapsed > 0 else 0.0
        logger.info(
            "爬取完成：初始 %d | 总处理 %d | 成功 %d | 失败 %d | %s耗时 %.1fs | %.1f task/s",
            total_initial,
            self.total_processed,
            ok,
            fails,
            (" | ".join(extras) + " | ") if extras else "",
            elapsed,
            speed,
        )
