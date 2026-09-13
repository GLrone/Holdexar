"""任务路由分发器：按 task["scope"] 把任务派给对应 handler。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from app.crawler.db_writer import DbWriter
from app.crawler.http_client import SteamHttpClient

logger = logging.getLogger(__name__)


class CrawlerContext:
    """传递给 Handler 的上下文对象。"""

    def __init__(
        self,
        task: dict,
        http_client: SteamHttpClient,
        db_writer: DbWriter,
        session: aiohttp.ClientSession,
    ):
        self.task = task
        self.http_client = http_client
        self.db_writer = db_writer
        self.session = session
        self.queue: asyncio.Queue | None = None  # Scheduler 运行时注入
        # 停止信号（Scheduler 运行时注入）：断网等待等长阻塞环节的打断通道
        self.stop_event: asyncio.Event | None = None


class CrawlerRouter:
    """将不同类型的任务分发给对应的 Handler。"""

    def __init__(self):
        self._handlers: dict[str, Callable[[CrawlerContext], Awaitable[None]]] = {}

    def handle(self, task_type: str):
        def decorator(func: Callable[[CrawlerContext], Awaitable[None]]):
            if task_type in self._handlers:
                raise ValueError(f"Handler for '{task_type}' is already registered.")
            self._handlers[task_type] = func
            return func

        return decorator

    async def route(self, context: CrawlerContext) -> None:
        task_type = context.task.get("type")
        if not task_type:
            logger.error("任务缺少 type 字段: %s", context.task)
            return

        handler = self._handlers.get(task_type)
        if not handler:
            logger.error("找不到处理类型 '%s' 的 Handler", task_type)
            return

        await handler(context)
