"""出网请求频率闸门：滑动窗口 200 发 / 5 分钟，进程级全局单例。

为什么限流是新的主闸：browse 接口按 country_code 参数返回各区价格，
出口 IP 不再参与数据判定（直连与代理拿到的是同一份数据）——「多出口
轮换 IP 规避风控」的整套机制（Per-AppID Session、429 换代理、断网
探测分流）随之退役，取而代之的是**把请求频率压进 Steam 接受的窗口**
（实测定线 200 发/5 分钟）：请求匀速发出，任何网络环境下（直连或
加速器）都不触发风控。

覆盖面：全部打 Steam 的出网请求——爬虫主轮批量 / 元数据预取 / 补抓 /
修复 / 孤儿回补（SteamHttpClient.get_json 收口）+ 捆绑包刷新
（refresh_bundles / 单包导入）。账户同步、CDK 查价等非 browse 通道
不在 Steam 爬取预算内，不受此闸管辖。

实现：asyncio.Lock 保护的 monotonic 时间戳队列。超窗的旧时间戳在
每次取号时清理；窗口满时按「最旧一条出窗时刻 + 窗口长」计算等待，
不轮询不忙等。异常安全：任何内部错误都放行请求（限流器故障不能
拖垮爬取链路）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# 窗口参数（实测定线，见 module docstring）
RATE_LIMIT_MAX_REQUESTS = 200
RATE_LIMIT_WINDOW_SECONDS = 300


class SlidingWindowRateLimiter:
    """滑动窗口限流器：窗口内最多 max_requests 次，超出即等到最旧请求出窗。"""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._waited_total = 0.0  # 累计限流等待秒数（收尾日志观测用）

    async def acquire(self) -> None:
        """取一个请求名额；窗口满时阻塞到有名额释放。

        异常安全：限流器内部出错（时钟异常等）直接放行——它只负责
        匀速，故障时宁可让请求发出去交给既有重试/熔断兜底。
        """
        try:
            async with self._lock:
                now = time.monotonic()
                # 清掉已出窗的时间戳（窗口滑动）
                cutoff = now - self.window_seconds
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                # 窗口满：最旧一条出窗的时刻 = 最早可发的时刻
                wait = self._timestamps[0] + self.window_seconds - now
                self._waited_total += max(wait, 0.0)
            if wait > 0:
                logger.info(
                    "[限流] 窗口满（%d 发/%ds），等待 %.1fs 后放行",
                    self.max_requests, self.window_seconds, wait,
                )
                await asyncio.sleep(wait)
            # 睡醒后递归重取：等待期间其他 worker 可能已把名额抢走
            await self.acquire()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 限流器故障不得拖垮爬取
            logger.exception("[限流] 限流器异常，本请求放行")

    def reset(self) -> None:
        """清空窗口（测试隔离用）。"""
        self._timestamps.clear()
        self._waited_total = 0.0

    @property
    def waited_total(self) -> float:
        return self._waited_total


# 进程级单例：单入口（GLOBAL 形态）下，爬虫各 worker + 捆绑包刷新共享同一份窗口预算
steam_rate_limiter = SlidingWindowRateLimiter(
    RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
)


class LaneRateLimits:
    """多入口（lane）形态下的限流账本：**每条 lane 一份预算，同 lane 的 worker 共享**。

    总额度不随出口数放大：`RATE_LIMIT_MAX_REQUESTS` 按 lane 数均分（向下取整，至少 1），
    所有 lane 加起来仍等于单入口时的那条线。多出口在这一层换到的是**故障隔离**
    （一条 lane 撞窗口不再拖住其它 lane）与出口分散，而不是更多总配额——总配额要放大，
    得先有真实作业数据回答"这条线按出口算还是按 endpoint 算"。

    按 lane 序号取用（`for_lane`），所以 worker 数多于 lane 数时它们共享同一份预算，
    不会因为多开一个 worker 就凭空多出一份额度。
    """

    def __init__(
        self,
        lane_count: int,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        count = max(0, int(lane_count))
        self.per_lane = max(1, int(max_requests) // count) if count else int(max_requests)
        self._limits = [
            SlidingWindowRateLimiter(self.per_lane, window_seconds) for _ in range(count)
        ]

    def for_lane(self, index: int) -> SlidingWindowRateLimiter:
        """第 `index` 条 lane 的预算；lane 数为 0（单入口形态）时退回进程级单例。"""
        if not self._limits:
            return steam_rate_limiter
        return self._limits[index % len(self._limits)]

    @property
    def lane_count(self) -> int:
        return len(self._limits)
