"""crawler 执行占用：所有**生产**爬取路径共用一个占用语义。

占用语义落在执行入口而不是调用方的 job 表上：`run_crawl` 才是唯一的生产执行入口
（手动任务走 crawl 域、bundles 链尾**直调**、CLI 调试），而 `crawl_service._active`
只登记了走 `start_job` 的那一条——只看它，bundles 那条直调路径就是盲区，会出现
"手动 30 worker + bundles 4 worker 同时跑"。

占用与维护预约共用一把锁、互斥：爬取占用与分钟级维护（体检/重建）任一在位，
另一方即被拒。

占用是进程内状态，不落库：它要回答的是"此刻有没有爬取在执行"，不是历史。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_holder: str | None = None

# 维护预约（进程内）：体检 L1/L2 与 rebuild 全程持位并计入 crawler_busy()，
# 启动/让路门禁自动拒绝维护期间的爬取
MAINTENANCE_HEALTH = "health"
MAINTENANCE_REBUILD = "rebuild"
_MAINTENANCE_REASONS = {
    MAINTENANCE_HEALTH: "代理体检进行中",
    MAINTENANCE_REBUILD: "代理通道正在重建",
}
_maintenance_kind: str | None = None


class CrawlerBusyError(RuntimeError):
    """已有爬取任务在执行（或维护预约在位）——同时只允许一个生产爬取。"""


def crawler_busy() -> bool:
    return _holder is not None or _maintenance_kind is not None


def current_holder() -> str | None:
    return _holder


def maintenance_kind() -> str | None:
    """当前维护预约类型（health / rebuild）；无预约返回 None。"""
    return _maintenance_kind


def maintenance_reason() -> str | None:
    """维护预约的用户文案；无预约返回 None。"""
    with _lock:
        return _MAINTENANCE_REASONS.get(_maintenance_kind) if _maintenance_kind else None


def begin_crawl(tag: str) -> None:
    """取得爬取占用；维护预约或其它占用者在位即抛 `CrawlerBusyError`。

    同 tag 重入视为幂等：start_job 预取占用后 run_crawl 以同名 tag 复用。"""
    global _holder
    with _lock:
        if _maintenance_kind is not None:
            raise CrawlerBusyError(_MAINTENANCE_REASONS[_maintenance_kind])
        if _holder == tag:
            return
        if _holder is not None:
            raise CrawlerBusyError(f"已有爬取任务在执行（{_holder}）")
        _holder = tag


def begin_maintenance(kind: str) -> None:
    """取得维护预约；已有预约或爬取占用在位即抛 `CrawlerBusyError`（带用户文案）。"""
    global _maintenance_kind
    with _lock:
        if _maintenance_kind is not None:
            raise CrawlerBusyError(_MAINTENANCE_REASONS[_maintenance_kind])
        if _holder is not None:
            raise CrawlerBusyError(f"已有爬取任务在执行（{_holder}）")
        _maintenance_kind = kind


def end_maintenance() -> None:
    """清掉维护预约（与 begin_maintenance 同调用方 finally 配对）。"""
    global _maintenance_kind
    with _lock:
        _maintenance_kind = None


def end_crawl() -> None:
    global _holder
    with _lock:
        _holder = None
    _kick_rebuild_consume()


# 收尾重建消费任务强引用：create_task 无强引用会被 GC 半路回收
_after_crawl_tasks: set = set()


def _kick_rebuild_consume() -> None:
    """占用释放即消费待重建（任务结束后剔除已标记移除的节点）：有 pending
    才动手，无事件循环（同步调用方 / 测试夹具）静默跳过，交给周期拍。"""
    try:
        import asyncio

        from app.domains.proxypool import scheduling as _sched

        if not _sched.rebuild_pending():
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    except Exception:  # noqa: BLE001 —— 钩子自身故障不影响占用释放
        return
    task = loop.create_task(_sched.consume_rebuild_after_crawl())
    _after_crawl_tasks.add(task)
    task.add_done_callback(_after_crawl_tasks.discard)
