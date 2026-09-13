"""进程内事件总线：爬取进度 / 任务状态 -> SSE（app/api 或 domains 订阅）。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_sse(self) -> str:
        data = json.dumps({"ts": self.ts, **self.payload}, ensure_ascii=False)
        return f"event: {self.type}\ndata: {data}\n\n"


class EventBus:
    """发布/订阅。慢消费者队列满时丢弃事件，绝不阻塞爬虫主流程。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, **payload) -> None:
        event = Event(event_type, payload)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue


bus = EventBus()
