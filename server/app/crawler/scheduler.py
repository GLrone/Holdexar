"""asyncio 生产者-消费者任务调度器。

要点：
- 计数器周期日志
- asyncio.Event 停止信号
- 每个任务完成向 SSE 事件总线发布 crawl.progress
- **岗位池（Coordinator 的 lane 管理）**：`client_factory(i)` 物化全部 lane（含待用
  出口）；worker 每个任务前从池里租一条健康 lane，用完归还——worker 协程整个 run
  不销毁。lane 连续失败即隔离进冷却区（到期放一发探测任务验证通道），worker 立刻
  换下一条健康 lane 继续领任务：死出口拖不住岗位，岗位数由池中健康 lane 数托底。
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

# 死通道冷却秒数：绑在连续连接失败出口上的 worker 暂停拉任务的间隔，
# 到期放一发探测（成功即复位恢复，见 SteamHttpClient.lane_unhealthy）
_LANE_COOLDOWN_SECONDS = 60


class CrawlerScheduler:
    """基于 asyncio.Queue 的高可用任务调度器（生产者-消费者模型）。

    `client_factory(i) -> SteamHttpClient` 可选：给了就按 lane 序号取客户端（多入口
    形态的岗位池）；不给则全体共用 `http_client`（单入口形态，行为不变）。
    """

    def __init__(
        self, router, http_client, db_writer, worker_count=30, stop_event=None,
        failure_ledger: list | None = None,
        error_sink: Callable[[BaseException], None] | None = None,
        client_factory: Callable[[int], object] | None = None,
        lane_count: int | None = None,
    ):
        self.router = router
        self.http_client = http_client
        self.db_writer = db_writer
        self.worker_count = worker_count
        self.client_factory = client_factory
        # 出口兵源条数（默认 = worker 数）：可大于 worker_count——多出的即待用出口
        self.lane_count = lane_count
        # 岗位池（Coordinator 的 lane 管理）：run 时由 factory 物化全部 lane，
        # worker 逐任务租借；lane_unhealthy → 隔离进冷却区，到期放一条探测。
        # worker 协程不销毁——没有岗位就等，岗位回来立刻继续领任务。
        self._lanes: list = []
        self._cooldown_lanes: list = []
        self._cooldown_until: dict[int, float] = {}
        self._lanes_ready = False
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

    def _materialize_lanes(self) -> None:
        """run 开始时物化岗位池：一条 lane 一个客户端（出口预算随客户端走）。

        lane 数取 `lane_count`（默认 worker 数）：大于 worker 数时多出的入口即
        **待用出口池**——被隔离的 lane 由它们顶替，岗位数不掉。
        """
        if self.client_factory is not None and not self._lanes_ready:
            count = self.lane_count or self.worker_count
            self._lanes = [self.client_factory(i) for i in range(count)]
            self._lanes_ready = True

    def _sweep_unhealthy(self) -> None:
        """把池里已被标记不健康的 lane 扫进冷却区——否则它们既不派活也拿不到
        冷却复活探测，永久卡死在岗位池里。"""
        for lane in list(self._lanes):
            if getattr(lane, "lane_unhealthy", False):
                self._lanes.remove(lane)
                if lane not in self._cooldown_lanes:
                    self._cooldown_lanes.append(lane)
                    self._cooldown_until[id(lane)] = (
                        time.monotonic() + _LANE_COOLDOWN_SECONDS
                    )

    def _acquire_lane(self):
        """从岗位池取一条 lane，返回 (lane, is_probe)；无可用岗位返回 (None, False)。

        冷却到期的 lane 以**探路位**返回（`is_probe=True`）：它此刻仍带 unhealthy
        标记，但正因如此才要放一个真任务去验证通道——调用方不得立即再隔离它，
        否则形成「隔离→到期→再隔离」的空转死锁。
        """
        self._sweep_unhealthy()
        for lane in self._lanes:
            self._lanes.remove(lane)
            return lane, False
        now = time.monotonic()
        for lane in list(self._cooldown_lanes):
            if now >= self._cooldown_until.pop(id(lane), 0.0):
                self._cooldown_lanes.remove(lane)
                return lane, True
        return None, False

    def _quarantine_lane(self, lane) -> None:
        """lane 连续失败 → 移出岗位池进冷却区，到期自动回来接受探测。"""
        if lane in self._lanes:
            self._lanes.remove(lane)
        if lane not in self._cooldown_lanes:
            self._cooldown_lanes.append(lane)
            self._cooldown_until[id(lane)] = time.monotonic() + _LANE_COOLDOWN_SECONDS

    def _release_lane(self, lane) -> None:
        if lane not in self._lanes and lane not in self._cooldown_lanes:
            self._lanes.append(lane)
        self._sweep_unhealthy()

    async def _worker(self, worker_id: int, session) -> None:
        # Coordinator 岗位模型：worker 不再终身绑定 lane。每个任务前从岗位池
        # 租一条健康 lane；lane_unhealthy → 隔离进冷却区并立刻换下一条——
        # 死节点拖不住 worker，任务队列由健康岗位继续消化。
        http_client = (
            self.client_factory(worker_id) if self.client_factory is not None
            else self.http_client
        )
        while not self.stop_event.is_set():
            if self.client_factory is not None:
                http_client, is_probe = self._acquire_lane()
                if http_client is None:
                    # 全部岗位在隔离/租出：等岗位回来或停止（队列由别的岗位消化）
                    try:
                        await asyncio.wait_for(
                            self.stop_event.wait(), timeout=1.0
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
                if not is_probe and getattr(http_client, "lane_unhealthy", False):
                    self._quarantine_lane(http_client)
                    continue
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
                # 进度事件必须先于 task_done 发布：join() 一放行，run() 的收尾
                # 路径（job.status）就会出站；末条进度若压在它后面，客户端会把
                # 已收束的任务重新置回运行态，进度条卡死在 100%。
                if self.client_factory is not None:
                    self._release_lane(http_client)
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
                self.queue.task_done()

    async def run(self, initial_tasks: list[dict], session) -> None:
        """将初始任务打入队列，启动 workers，等待队列清空（含动态追加任务）。

        停止语义：stop_event 置位后立即取消全部 Worker 并清空队列——
        不能等 queue.join()（停止后剩余任务无人处理，join 永远挂起）。
        """
        total_initial = len(initial_tasks)
        self.total_target = total_initial
        self._materialize_lanes()
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
