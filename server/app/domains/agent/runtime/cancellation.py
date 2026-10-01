"""运行级取消原语：取消令牌 + 活跃运行注册表（进程内）。

取消语义（单进程口径）：
- cancel 是「请求停止」，不是直接改状态行——有活跃执行者的 run 由
  执行器在步边界观察到令牌后自行收敛到 cancelled（已完成步的事件
  保留原样，不伪造回滚）；
- 无活跃执行者的 run（queued / 进程重启后的孤儿）由 service 层
  直接收敛；
- 令牌幂等：重复 request 只生效一次，重复取消不产生重复事件。

跨进程取消不在本层（桌面单实例形态下单进程口径已完整）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


class CancelToken:
    """一次运行的取消令牌：可被多方置位、执行器侧轮询/等待。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    async def wait(self, timeout: float | None = None) -> bool:
        """等待取消请求；返回 True = 已请求（含进入前已置位），
        False = 超时未请求。执行器用它实现「可打断的工作延时」。"""
        if self._event.is_set():
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        return True


@dataclass
class ActiveRun:
    """注册表条目：令牌 + 执行任务引用（强持有防任务被回收）。"""

    token: CancelToken
    task: asyncio.Task | None = None


class ActiveRunRegistry:
    """进程内活跃 run 表：run_id → ActiveRun。

    service 层在启动执行器前 register、执行器 finally 中 pop；
    cancel_run 依据「run 是否在表中」分流：在表 → 置令牌交执行器
    收敛，不在表 → 直接收敛状态。"""

    def __init__(self) -> None:
        self._runs: dict[str, ActiveRun] = {}

    def register(self, run_id: str) -> CancelToken:
        entry = ActiveRun(token=CancelToken())
        self._runs[run_id] = entry
        return entry.token

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        entry = self._runs.get(run_id)
        if entry is not None:
            entry.task = task

    def token_of(self, run_id: str) -> CancelToken | None:
        entry = self._runs.get(run_id)
        return entry.token if entry else None

    def pop(self, run_id: str) -> ActiveRun | None:
        return self._runs.pop(run_id, None)

    def active_ids(self) -> list[str]:
        return list(self._runs)


REGISTRY = ActiveRunRegistry()
