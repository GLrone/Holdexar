"""出口账本：按 (出口 IP × endpoint) 累计本次作业的请求结果。

目的不是审计日志，而是回答「这条线的限制到底按出口 IP、按 endpoint，还是别的维度」——
所以最小事实是「哪个出口、打哪个端点、发了多少、成没成、怎么没成」。

两条边界：
- **不按请求写库**：累计发生在内存里，一次作业结束时汇总成一行行聚合记录；
- **不存自由文本**：`outcome` 是固定枚举，原始异常串只进日志（异常串可能带 URL 与凭据，
  且长度无界）。

出口键取的是出口 IP（`exits.ExitSlot.key`）：同一个出口上放几个 worker，账都记在同一个
出口上——否则"多开一个 worker"会凭空多出一份统计口径。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# 结果枚举（固定；增删都要同步改表注释与前端/报表口径）
OUTCOMES = ("ok", "e429", "e4xx", "e5xx", "timeout", "connect_error", "other")

# 端点标签：把 URL 收敛成稳定短名，避免把带查询串的完整 URL 存进库
_ENDPOINT_LABELS = (
    ("IStoreBrowseService", "store_browse"),
    ("appdetails", "app_details"),
    ("appreviews", "app_reviews"),
)


def endpoint_of(url: str) -> str:
    """URL → 端点标签（认不出记 `other`）。"""
    text = str(url or "")
    for needle, label in _ENDPOINT_LABELS:
        if needle in text:
            return label
    return "other"


@dataclass
class _Bucket:
    requests: int = 0
    ok: int = 0
    e429: int = 0
    e4xx: int = 0
    e5xx: int = 0
    timeout: int = 0
    connect_error: int = 0
    other: int = 0
    duration_ms: int = 0


@dataclass
class ExitStatsCollector:
    """按 (出口键, endpoint) 累计；线程/协程安全由「单事件循环内同步累加」保证。"""

    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def record(self, *, exit_key, url: str, outcome: str, duration_ms: int) -> None:
        key = (str(exit_key or "direct"), endpoint_of(url))
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket
        bucket.requests += 1
        bucket.duration_ms += max(0, int(duration_ms))
        if outcome in OUTCOMES:
            setattr(bucket, outcome, getattr(bucket, outcome) + 1)
        else:
            bucket.other += 1

    def rows(self) -> list[dict]:
        """序列化成可落库/可断言的聚合行，按出口键与端点排序（顺序稳定）。"""
        out: list[dict] = []
        for (exit_key, endpoint) in sorted(self._buckets):
            bucket = self._buckets[(exit_key, endpoint)]
            out.append({
                "exit_ip": exit_key,
                "endpoint": endpoint,
                "requests": bucket.requests,
                "success": bucket.ok,
                "e429": bucket.e429,
                "e4xx": bucket.e4xx,
                "e5xx": bucket.e5xx,
                "timeout": bucket.timeout,
                "connect_error": bucket.connect_error,
                "other": bucket.other,
                "duration_ms": bucket.duration_ms,
            })
        return out

    @property
    def total_requests(self) -> int:
        return sum(b.requests for b in self._buckets.values())

    @property
    def exit_count(self) -> int:
        return len({key for key, _endpoint in self._buckets})

    def merge_into(self, summary: dict) -> None:
        """把出口账本并入作业台账的 `error_summary`（固定枚举，长度有界）。"""
        by_exit: dict[str, int] = defaultdict(int)
        for (exit_key, _endpoint), bucket in self._buckets.items():
            by_exit[exit_key] += bucket.requests
        summary["by_exit"] = dict(sorted(by_exit.items()))