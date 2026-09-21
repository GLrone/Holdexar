"""crawler 执行占用：所有**生产**爬取路径共用一个占用语义。

占用落在执行入口 `run_crawl`：手动任务走 crawl 域、bundles 链尾直调、CLI 调试都从它进入，
而 `crawl_service._active` 只登记走 `start_job` 的那一条。占用是进程内状态、不落库——
它回答「此刻有没有爬取在执行」，不是历史。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_holder: str | None = None


class CrawlerBusyError(RuntimeError):
    """已有爬取任务在执行——同时只允许一个生产爬取。"""


def crawler_busy() -> bool:
    return _holder is not None


def current_holder() -> str | None:
    return _holder


def begin_crawl(tag: str) -> None:
    """取得爬取占用；已被占用即抛 `CrawlerBusyError`。"""
    global _holder
    with _lock:
        if _holder is not None:
            raise CrawlerBusyError(f"已有爬取任务在执行（{_holder}）")
        _holder = tag


def end_crawl() -> None:
    global _holder
    with _lock:
        _holder = None