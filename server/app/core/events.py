"""进程内事件总线：爬取进度 / 任务状态 -> SSE（app/api 或 domains 订阅）。

双通道分级：crawl.progress 高频可丢走 progress 队列；job.started / job.status /
price_cycle.completed 等关键状态走 critical 队列——满时挤掉最旧事件保留最新，
绝不静默丢新（丢一条进度只是界面少刷一次，丢一条状态前端会停在旧状态）。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

# 走 progress 通道（可丢）的事件类型；其余全部走 critical
PROGRESS_EVENT_TYPES = {"crawl.progress"}

_CRITICAL_MAX = 256
_PROGRESS_MAX = 64


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_sse(self) -> str:
        data = json.dumps({"ts": self.ts, **self.payload}, ensure_ascii=False)
        return f"event: {self.type}\ndata: {data}\n\n"


class Subscriber:
    """单订阅者的双通道收件箱；get() 关键事件优先，兼容单队列的 await .get() 用法。"""

    def __init__(self) -> None:
        self.critical: asyncio.Queue[Event] = asyncio.Queue(maxsize=_CRITICAL_MAX)
        self.progress: asyncio.Queue[Event] = asyncio.Queue(maxsize=_PROGRESS_MAX)
        self._pending: Event | None = None

    async def get(self) -> Event:
        if self._pending is not None:
            ev, self._pending = self._pending, None
            return ev
        if not self.critical.empty():
            return self.critical.get_nowait()
        if not self.progress.empty():
            return self.progress.get_nowait()
        cgetter = asyncio.ensure_future(self.critical.get())
        pgetter = asyncio.ensure_future(self.progress.get())
        try:
            done, _ = await asyncio.wait(
                {cgetter, pgetter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cgetter in done:
                # progress getter 可能已取走一条进度：进度可丢，直接取消
                pgetter.cancel()
                return cgetter.result()
            # progress 先到；critical getter 可能也已取走关键事件（不丢，缓存补投）
            if cgetter.done():
                self._pending = cgetter.result()
            else:
                cgetter.cancel()
            return pgetter.result()
        finally:
            for t in (cgetter, pgetter):
                if not t.done():
                    t.cancel()


class EventBus:
    """发布/订阅。progress 满即丢（不阻塞爬虫主流程）；critical 满挤最旧保最新。"""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, event_type: str, **payload) -> None:
        event = Event(event_type, payload)
        for sub in list(self._subscribers):
            if event_type in PROGRESS_EVENT_TYPES:
                try:
                    sub.progress.put_nowait(event)
                except asyncio.QueueFull:
                    continue
            else:
                self._put_critical(sub, event)

    @staticmethod
    def _put_critical(sub: Subscriber, event: Event) -> None:
        queue = sub.critical
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        # 满：挤掉最旧（job.status 类是状态语义，消费端只认最新），保新事件
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


bus = EventBus()
