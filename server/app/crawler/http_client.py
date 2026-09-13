"""Steam HTTP 客户端：全局 429 熔断 + 指数退避 + 幻觉 429 清洗。

要点：
- 内置静态 UA 池轮换
- `Connection: close`（Per-AppID Session 配套：连接即用即断，
  促使 Clash loadbalance 在下一个任务/请求换出口 IP，规避 429 风控）
- 支持 per-request 代理注入（proxy_url）
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import aiohttp

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]


class SteamRateLimitError(Exception):
    """当 Steam API 返回真正的 429 且超出局部重试次数时抛出。"""


class Global429CircuitBreaker:
    """全局 429 熔断器：

    - 任意 worker 收到真 429 时 trip() 设置冷却时间
    - 所有 worker 发请求前 wait_if_tripped() 阻塞等待
    - 冷却指数递增（5s → 10s → 20s → 40s → 60s）
    - 成功请求后 reset()
    """

    def __init__(self) -> None:
        self._tripped = False
        self._trip_time = 0.0
        self._cooldown = 5.0
        self._max_cooldown = 60.0
        self._consecutive_429 = 0
        self._lock = asyncio.Lock()

    async def trip(self, status_code: int | None = None, response_body: str | None = None, url: str | None = None) -> None:
        async with self._lock:
            self._consecutive_429 += 1
            self._cooldown = min(self._max_cooldown, 5.0 * (2 ** (self._consecutive_429 - 1)))
            self._tripped = True
            self._trip_time = time.monotonic()
            logger.warning(
                "[熔断器] 触发全局 429 冷却 %.0fs（连续第 %d 次）status=%s url=%s",
                self._cooldown,
                self._consecutive_429,
                status_code,
                url,
            )
            if response_body:
                preview = (response_body or "<空>")[:500]
                logger.debug("[熔断器] 响应原文: %s", preview)

    async def wait_if_tripped(self) -> None:
        if not self._tripped:
            return
        async with self._lock:
            if not self._tripped:
                return
            remaining = self._cooldown - (time.monotonic() - self._trip_time)
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def reset(self) -> None:
        async with self._lock:
            if self._tripped:
                self._tripped = False
                self._consecutive_429 = 0


# 全局单例
global_429_breaker = Global429CircuitBreaker()


class SteamHttpClient:
    """带拦截器和重试机制的 HTTP 客户端。"""

    def __init__(self, timeout: int = 12, max_retries: int = 4, proxy_url: str | None = None,
                 failover_proxy_resolver=None):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.proxy_url = proxy_url
        # direct_first 失败换代理：直连失败/429 时调用，返回代理 URL（None=无可用代理）
        self.failover_proxy_resolver = failover_proxy_resolver
        self._ua_index = random.randrange(len(_USER_AGENTS))

    def _get_headers(self, appid=None) -> dict[str, str]:
        self._ua_index = (self._ua_index + 1) % len(_USER_AGENTS)
        return {
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": _USER_AGENTS[self._ua_index],
            # 断连语义：配合 Per-AppID Session（每游戏独立 session 用完即毁），
            # 不留 keep-alive，让 Clash 在下一个连接重新选出口
            "Connection": "close",
        }

    async def get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        appid: str | int | None = None,
        params: dict | None = None,
    ):
        """GET 并解析 JSON。内置拦截：

        1. 全局 429 熔断 → 所有请求阻塞等待冷却
        2. 真 429 → 指数退避重试（3s, 6s, 12s, 24s）
        3. 幻觉 429 → 状态码 429 但 body 含有效 JSON → 直接清洗返回
        4. 网络超时/错误 → 重试（direct_first 配了 failover 时换代理再试）
        5. 断网检测：传输层成败上报 NetworkChecker（达阈值 ping 确认，
           app_handler 守卫据此暂停而非写 missing 污染账本）
        """
        from .network_check import network_checker

        for attempt in range(self.max_retries):
            await global_429_breaker.wait_if_tripped()

            try:
                async with session.get(
                    url,
                    params=params,
                    headers=self._get_headers(appid),
                    timeout=self.timeout,
                    proxy=self.proxy_url,
                ) as response:
                    network_checker.report_success()
                    if response.status == 200:
                        try:
                            data = await response.json()
                            await global_429_breaker.reset()
                            return data
                        except aiohttp.ContentTypeError:
                            # 200 但返回了 HTML 错误页
                            if attempt < self.max_retries - 1:
                                await asyncio.sleep(1 + random.uniform(0, 1))
                                continue
                            raise Exception("Invalid JSON Response")

                    elif response.status == 429:
                        # ── 幻觉 429 检测：状态码 429 但 body 是有效 JSON ──
                        #
                        # ⚠️ 形状限定为 **appdetails 形状**（`{appid: {success, data}}`
                        # 或裸 `{success, data}`）。只认 `success: true` 是不够的：
                        # `success` 是「这次请求处理成功」的标记，它出现在任何 JSON
                        # API 的常规成功体里——一旦某个调用方的成功体恰好带顶层
                        # `success: true`，真 429 就会被当成成功返回，上层拿到空数据
                        # 且不会重试，缺口静默固化。所以额外要求 `data` 是对象。
                        # 当前唯一的调用方 StoreBrowse 走 `{response: {...}}` 形状，
                        # 两条分支都不匹配 → 真 429 照常熔断，不受此启发式影响。
                        body_text_429 = None
                        try:
                            body_text_429 = await response.text()
                            if body_text_429 and body_text_429.strip().startswith("{"):
                                phantom_data = json.loads(body_text_429)
                                if appid and str(appid) in phantom_data:
                                    phantom_app = phantom_data[str(appid)]
                                    if (phantom_app.get("success")
                                            and isinstance(phantom_app.get("data"), dict)):
                                        await global_429_breaker.reset()
                                        return phantom_data
                                elif not appid and isinstance(phantom_data.get("data"), dict):
                                    await global_429_breaker.reset()
                                    return phantom_data
                        except Exception:
                            pass

                        # 真 429 → 触发全局熔断
                        await global_429_breaker.trip(
                            status_code=response.status,
                            response_body=body_text_429 or "<无法读取响应体>",
                            url=str(response.url),
                        )

                        # direct_first 语义：真 429 说明出口被风控，换代理
                        # 重试比干等冷却更快见效（Clash/池在跑时）
                        if self.failover_proxy_resolver is not None and attempt < self.max_retries - 1:
                            try:
                                failover = await self.failover_proxy_resolver()
                            except Exception:  # noqa: BLE001 —— 换代理失败回落退避
                                failover = None
                            if failover and failover != self.proxy_url:
                                self.proxy_url = failover
                                logger.info("[429换代理] 切换出口重试: %s", failover)
                                await asyncio.sleep(1 + random.uniform(0, 1))
                                continue

                        if attempt < self.max_retries - 1:
                            backoff = 3.0 * (2**attempt) + random.uniform(0, 1)
                            await asyncio.sleep(backoff)
                            continue
                        raise SteamRateLimitError(
                            f"API Rate Limit Exceeded after {self.max_retries} attempts"
                        )

                    elif response.status in (403, 404):
                        # 永久性封锁，不重试
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"HTTP {response.status}",
                        )

                    else:
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(1 + random.uniform(0, 1))
                            continue
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"HTTP {response.status}",
                        )

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 传输层失败上报断网检测（达阈值触发 ping 确认——
                # 本机断网时换代理无意义，等恢复才对）
                await network_checker.report_failure()
                # direct_first 语义：直连传输失败 → 换代理重试
                # （解析出口与当前相同 = 无处可换，沿用退避）
                if self.failover_proxy_resolver is not None and attempt < self.max_retries - 1:
                    try:
                        failover = await self.failover_proxy_resolver()
                    except Exception:  # noqa: BLE001
                        failover = None
                    if failover and failover != self.proxy_url:
                        self.proxy_url = failover
                        logger.info("[直连失败换代理] 切换出口重试: %s（%s）", failover, type(e).__name__)
                        continue
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1 + random.uniform(0, 1))
                    continue
                raise e

        raise Exception("Max retries exceeded")
