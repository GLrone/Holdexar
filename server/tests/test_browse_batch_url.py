"""StoreBrowse 批量切分：按**构造出来的 URL 实际长度**切，而不是按条数。

服务端按请求行长度拒绝（超限回 414 / 400），同一条数在不同 appid 位数下长度差很多，
所以判据只能是最终 URL 长度。本组用例断言：

1. 切出来的每一发 URL 都不超过 `MAX_URL_LEN`；
2. 400 条 7 位 appid 会被切成多发布，而不是一发必然被拒；
3. 不丢 id、不重复 id（切分只改边界，不改集合）；
4. 全部放得下时**不切**（不为切而切）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import browse_store as bs  # noqa: E402


def _specs(appids: list[int]) -> list[dict]:
    return [{"appid": int(a)} for a in appids]


def _url_len(specs: list[dict]) -> int:
    return bs.StoreBrowseAPI._url_len(specs, "us", "english", True)


def test_every_batch_url_within_limit() -> None:
    appids = list(range(5000000, 5000400))  # 7 位 appid
    batches = bs.StoreBrowseAPI.plan_batches(appids, "us", "english", True, 400)
    assert len(batches) > 1, "400 条 7 位 appid 一发装不下，必须切"
    for batch in batches:
        assert _url_len(_specs(batch)) <= bs.MAX_URL_LEN
    assert sum(len(b) for b in batches) == len(appids), "切分不得丢 id"
    assert sorted(a for b in batches for a in b) == appids, "切分不得改集合"


def test_no_split_when_everything_fits() -> None:
    appids = list(range(220, 240))
    batches = bs.StoreBrowseAPI.plan_batches(appids, "us", "english", True, 400)
    assert batches == [appids], "装得下就不切"


def test_short_appids_yield_shorter_url_but_same_rule() -> None:
    """同样 400 条，5 位 appid 的 URL 更短——批边界只能按 URL 定，不能按条数定。

    两者都超过单发上限，所以都会切；差别只在切出的批大小（短 appid 每发装得更多）。
    """
    short = _specs(list(range(10000, 10400)))
    long = _specs(list(range(5000000, 5000400)))
    assert _url_len(short) < _url_len(long)
    assert _url_len(short) > bs.MAX_URL_LEN and _url_len(long) > bs.MAX_URL_LEN

    short_first = bs.StoreBrowseAPI.plan_batches(
        [int(s["appid"]) for s in short], "us", "english", True, 400)[0]
    long_first = bs.StoreBrowseAPI.plan_batches(
        [int(s["appid"]) for s in long], "us", "english", True, 400)[0]
    assert len(short_first) > len(long_first), "短 appid 每发能装更多条"


def test_single_id_url_is_never_dropped() -> None:
    assert bs.StoreBrowseAPI.plan_batches([220], "us", "english", True, 400) == [[220]]


def test_split_uses_longest_prefix_not_halving() -> None:
    """切点取最长可行前缀：首发条数必须大于对半折的 200（否则白多发一倍请求）。"""
    appids = list(range(5000000, 5000400))
    batches = bs.StoreBrowseAPI.plan_batches(appids, "us", "english", True, 400)
    assert len(batches[0]) > 200, f"首发只有 {len(batches[0])} 条，切点退化成对半折"


def test_limit_is_below_measured_server_rejection() -> None:
    """上限必须低于服务端真实拒绝线（6834 字节回 414）。"""
    assert bs.MAX_URL_LEN < 6834