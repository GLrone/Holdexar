"""asyncio 生产者-消费者任务调度器。

要点：
- 计数器周期日志
- asyncio.Event 停止信号
- 每个任务完成向 SSE 事件总线发布 crawl.progress
- Per-AppID Session：每个 app 任务创建独立 aiohttp session，处理完销毁——迫使
  Clash（loadbalance）在下一个任务换出口 IP，规避 429 风控
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import aiohttp

from app.core.events import bus

from .router import CrawlerContext, CrawlerRouter

logger = logging.getLogger(__name__)

_PROGRESS_LOG_EVERY = 25

# Per-AppID Session 连接预算（对齐原版 TCPConnector 参数）
_PER_TASK_CONNECTOR_LIMIT = 20
_PER_TASK_DNS_CACHE = 60


class CrawlerScheduler:
    """基于 asyncio.Queue 的高可用任务调度器（生产者-消费者模型）。"""

    def __init__(
        self, router, http_client, db_writer, worker_count=30, stop_event=None,
        failure_ledger: list | None = None,
    ):
        self.router = router
        self.http_client = http_client
        self.db_writer = db_writer
        self.worker_count = worker_count
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stop_event = stop_event or asyncio.Event()

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

        # ── 捆绑包单协程 lane ──
        # bundle 任务与 app 任务双队列分道：app 走主队列 N worker 并发；
        # bundle 全程单 worker 串行（整区抓取 42 请求量级，节省 Steam
        # 配额）。懒启动（首个任务到达才拉起），队列排干由 watcher 投
        # 毒丸收尾——"捆绑包结束线程自动结束"语义；排干后再次到达可重启
        self.bundle_queue: asyncio.Queue = asyncio.Queue()
        self._bundle_tasks: list[asyncio.Task] = []
        self.bundle_processed = 0

    def _ensure_bundle_lane(self, session) -> None:
        """懒启动捆绑包 lane；已收尾（排干退出）则重启。

        注意调用方必须先把任务 put 进 bundle_queue 再 ensure——
        watcher 的 join() 以队列为空即完成，先启动后投递会在投递前
        被毒丸关停（任务滞留无人处理）。
        """
        if any(not t.done() for t in self._bundle_tasks):
            return
        self._bundle_tasks = [
            asyncio.create_task(self._bundle_worker(session)),
            asyncio.create_task(self._bundle_drain_watch()),
        ]

    async def _bundle_drain_watch(self) -> None:
        """捆绑包队列排干 → 投毒丸收尾单协程（排干即关，不占常驻）。"""
        await self.bundle_queue.join()
        await self.bundle_queue.put(None)
        logger.info(
            "[捆绑包lane] 队列排干，单协程自动收尾（本轮处理 %d 个）",
            self.bundle_processed,
        )

    async def _bundle_worker(self, session) -> None:
        """捆绑包单协程 worker：串行处理 bundle 任务，收到毒丸自动退出。"""
        while not self.stop_event.is_set():
            try:
                task = await self.bundle_queue.get()
            except asyncio.CancelledError:
                break

            if task is None:  # 排干毒丸
                self.bundle_queue.task_done()
                break

            task_id = task.get("id", "unknown")
            try:
                context = CrawlerContext(task, self.http_client, self.db_writer, session)
                context.queue = self.queue
                context.stop_event = self.stop_event
                await self.router.route(context)
                async with self._counter_lock:
                    self.total_processed += 1
                    self.success_count += 1
                    self.bundle_processed += 1
                    self._completed_ids.add(str(task_id))
            except Exception as e:
                async with self._counter_lock:
                    self.total_processed += 1
                    self.fail_count += 1
                    self._completed_ids.add(str(task_id))
                logger.error("[捆绑包lane] bundle:%s 异常: %s", task_id, e)
            finally:
                self.bundle_queue.task_done()
                await self._record_speed()
                self._publish_progress()

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
        while not self.stop_event.is_set():
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break

            if task is None:  # 毒丸信号
                self.queue.task_done()
                break

            # bundle 任务不占 app worker：转投单协程 lane（先投递再 ensure——
            # watcher 以 join() 判完成，先启动后投递会被毒丸提前关停）
            if task.get("type") == "bundle":
                await self.bundle_queue.put(task)
                self._ensure_bundle_lane(session)
                self.queue.task_done()
                continue

            task_type = task.get("type", "unknown")
            task_id = task.get("id", "unknown")
            try:
                # ── Per-AppID Session：每个任务独立 session，处理完销毁 ──
                # 迫使 Clash 换出口 IP 规避 429（原版 process_app 同款语义；
                # Connection: close 由 http_client 头统一附加）
                if task_type == "app":
                    connector = aiohttp.TCPConnector(
                        limit=_PER_TASK_CONNECTOR_LIMIT, ttl_dns_cache=_PER_TASK_DNS_CACHE
                    )
                    async with aiohttp.ClientSession(connector=connector) as task_session:
                        context = CrawlerContext(
                            task, self.http_client, self.db_writer, task_session
                        )
                        context.queue = self.queue
                        context.stop_event = self.stop_event
                        await self.router.route(context)
                else:
                    context = CrawlerContext(task, self.http_client, self.db_writer, session)
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
        # 双队列分道：bundle 任务直投单协程 lane（先投递再 ensure，防止
        # watcher 以空队列 join 立即收尾）；其余进主队列由 N worker 处理
        lane_initial = [t for t in initial_tasks if t.get("type") == "bundle"]
        for task in initial_tasks:
            if task.get("type") != "bundle":
                await self.queue.put(task)
        if lane_initial:
            for t in lane_initial:
                await self.bundle_queue.put(t)
            self._ensure_bundle_lane(session)

        logger.info(
            "启动 %d 个 Worker%s，队列初始任务数: %d",
            self.worker_count,
            f" + 捆绑包单协程 lane（{len(lane_initial)} 个）" if lane_initial else "",
            total_initial,
        )

        workers = [
            asyncio.create_task(self._worker(i, session)) for i in range(self.worker_count)
        ]
        start_time = time.time()

        stop_wait = asyncio.ensure_future(self.stop_event.wait())
        join_task = asyncio.ensure_future(self.queue.join())
        # 只等主队列/停止信号——bundle_queue.join() 不能进此等待集：
        # lane 未启动时队列为空，join 立即完成会把整个 run() 提前掐断
        # （worker 未跑任务即被取消）。lane 终态在主队列收尾后由 drain
        # watcher 自动收束，此处 gather 兜底等待
        await asyncio.wait({join_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)

        # 捆绑包 lane 终态：正常路径由 watcher 投毒丸自动退出（排干即关）。
        # 主队列 join 先完成时 lane 可能仍有任务（app worker 刚转投）：
        # 先等 lane 队列真正排干（join），再 cancel 兜底停止路径
        if any(not t.done() for t in self._bundle_tasks):
            if self.stop_event.is_set():
                for t in self._bundle_tasks:
                    t.cancel()
            else:
                bundle_join = asyncio.ensure_future(self.bundle_queue.join())
                stop_check = asyncio.ensure_future(self.stop_event.wait())
                await asyncio.wait(
                    {bundle_join, stop_check}, return_when=asyncio.FIRST_COMPLETED
                )
                bundle_join.cancel()
                stop_check.cancel()
                for t in self._bundle_tasks:
                    t.cancel()
        if self._bundle_tasks:
            await asyncio.gather(*self._bundle_tasks, return_exceptions=True)
        while True:
            try:
                self.bundle_queue.get_nowait()
                self.bundle_queue.task_done()
            except asyncio.QueueEmpty:
                break

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
