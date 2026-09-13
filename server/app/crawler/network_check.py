"""断网检测：把「本机没有网」与「代理/目标不可用」区分开。

**为什么非要做这个区分**：两者的正确反应是相反的，判错任何一个都亏。
- 本机断网 → 换代理毫无意义（所有出口都不通），正确动作是停止消耗、等网络恢复；
- 代理/目标不可用 → 本机网络是好的，正确动作是换出口重试，等下去只是浪费时间。

**判错的具体代价**（本模块存在的唯一理由）：爬虫用 `missing` 账本 + `retries`
预算表达「抓不到」。网络抖动若被当成数据缺失写进账本，补抓层就再也不会回头
补这一批——真实数据的缺口被永久固化成一个「已知为空」的记录。所以断网期间
任务必须整体推回队列：不记账、不烧重试次数（见 browse_store 的各处守卫）。

**判定方式**：不看单次请求的结论，只看连续失败的形状。单次失败说明不了任何事
（代理抖动、目标偶发超时都会产生），但连续 FAILURE_THRESHOLD 次**传输层**失败
就值得怀疑；此时绕过全部代理，直连国内可达目标探测本机网络是否真的还活着。
探测只要拿到响应就算活着（429/404 同样证明链路通），据此推翻怀疑，把失败
归因到代理/目标侧。

**为什么探测必须绕开代理**：若探测本身也走代理，「代理挂了」会让探测一起失败，
于是永远判定本机断网、永远不去换出口——恰好把两种情况的处置都做反。因此
`_ping_once` 显式 `trust_env=False`（httpx 默认会读 HTTP_PROXY/ALL_PROXY 等
环境变量，那样探测会跟着走代理）且不传 proxy。
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3  # 连续传输失败达此次数 → 值得怀疑，转入核实
PING_ATTEMPTS = 3  # 核实阶段的探测轮数（任一轮通即否决断网）
PING_TIMEOUT = 5.0
PING_RETRY_GAP = 2.0  # 探测轮之间的间隔：留出瞬时抖动自行恢复的窗口
POLL_INTERVAL = 30  # 等待模式下重新探测本机网络的间隔（秒）

# 直连可达的国内目标，不经代理；拿到任意响应即证明本机链路通
PING_URLS = ["http://www.baidu.com", "https://www.qq.com"]


class NetworkChecker:
    """事件循环内的网络状态判定器。

    只有两个状态：正常 / 已确认断网。中间的失败计数不算状态，只是怀疑度——
    它到线只触发一次核实，本身不改变任何行为。
    """

    def __init__(self) -> None:
        self._failure_count = 0
        self._is_offline = False
        self._confirm_lock = asyncio.Lock()

    @property
    def is_offline(self) -> bool:
        return self._is_offline

    def report_success(self) -> None:
        """一次真实 HTTP 交换完成 —— 网络还活着的直接证据。

        同步方法：只改本对象的两个属性，事件循环内无竞态。任何探测结论都不如
        「刚刚真的收发成功过」可信，所以这一发直接推翻断网判定。
        """
        self._failure_count = 0
        self._is_offline = False

    async def report_failure(self) -> None:
        """上报一次传输层失败（连接拒绝 / 超时 / DNS 失败）。

        传输层失败才调用这里。HTTP 层面的 4xx/5xx 是「网络通、目标答了」，
        属于另一类问题，不应计入。
        """
        if self._is_offline:
            # 已确认断网期间不再累计：计数没有消费者，只会在长时间断网里无限增长
            return
        self._failure_count += 1
        if self._failure_count >= FAILURE_THRESHOLD:
            await self._confirm_offline()

    async def _confirm_offline(self) -> None:
        """怀疑到线后的一次性核实：抢确认权 → 复检 → 绕代理探测本机网络。"""
        async with self._confirm_lock:
            # 排队等锁期间可能已被别的 worker 核实过，或计数已被成功请求清零
            if self._is_offline or self._failure_count < FAILURE_THRESHOLD:
                return
            count = self._failure_count
            if await self._probe_with_retries():
                self._failure_count = 0
                logger.info(
                    "[断网检测] 连续 %d 次传输失败，但本机可直连外网 —— 归为代理/目标侧问题，"
                    "交由换出口重试处理",
                    count,
                )
                return
            self._is_offline = True
            logger.error(
                "[断网检测] 本机网络不可达（连续 %d 次传输失败 + %d 轮直连探测全败），"
                "转入等待模式：暂停抓取，网络恢复后自动继续",
                count, PING_ATTEMPTS,
            )

    async def _probe_with_retries(self) -> bool:
        """最多探测 PING_ATTEMPTS 轮，任一轮通即返回 True。

        单轮失败不足以定论：DNS 抖动、某个探测目标临时不可用都会造成单轮失败，
        所以给抖动留几轮窗口再下结论。
        """
        for attempt in range(PING_ATTEMPTS):
            if await self._ping_once():
                return True
            if attempt < PING_ATTEMPTS - 1:
                await asyncio.sleep(PING_RETRY_GAP)
        return False

    async def _ping_once(self) -> bool:
        """直连探测本机网络是否可达；任一目标给出响应即算通。"""
        for url in PING_URLS:
            try:
                async with httpx.AsyncClient(
                    timeout=PING_TIMEOUT,
                    follow_redirects=True,
                    trust_env=False,  # 关键：不被 HTTP_PROXY 等环境变量拽回代理
                ) as client:
                    await client.get(url)
                    # 拿到响应即证明链路可达——状态码无关紧要，429/404/503 都算通
                    return True
            except Exception:  # noqa: BLE001 —— 这一个目标没通不代表本机断网，换下一个再试
                continue
        return False

    async def wait_until_online(self, stop_event: asyncio.Event | None = None) -> bool:
        """等待本机网络恢复：每 POLL_INTERVAL 秒直连探测一次。

        返回 True = 已恢复（状态清零，调用方可继续）；False = 收到停止信号。

        停止信号用 Event.wait 与超时竞争，而不是把间隔切成小片轮询：等待期间
        用户唯一的操作就是「停止」，没有理由让它最多晚一秒才生效。
        """
        logger.warning(
            "[断网等待] 本机网络不可达，每 %d 秒探测一次，恢复后自动继续抓取",
            POLL_INTERVAL,
        )
        while not _is_set(stop_event):
            if await self._ping_once():
                self.reset()
                logger.info("[断网等待] 本机网络已恢复，继续抓取")
                return True
            if await _sleep_or_stop(stop_event, POLL_INTERVAL):
                return False
        return False

    def reset(self) -> None:
        """清零状态：网络恢复后复位，测试中用于隔离用例间影响。"""
        self._failure_count = 0
        self._is_offline = False


def _is_set(event: asyncio.Event | None) -> bool:
    return event is not None and event.is_set()


async def _sleep_or_stop(stop_event: asyncio.Event | None, seconds: float) -> bool:
    """睡满 seconds，或被停止信号提前打断。返回 True 表示被打断。"""
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False  # 正常睡满：超时才是常态
    return True


# 事件循环级单例：爬虫各 worker 共享同一份网络状态判定
network_checker = NetworkChecker()
