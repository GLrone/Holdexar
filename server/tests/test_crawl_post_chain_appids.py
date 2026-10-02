"""收尾下游链输入回归：补抓段批次标签不得再被当对象喂给下游。

旧故障：_execute 曾把 pre_tasks 的任务 id（「kz:补1」这类批次标签）
拼进 crawled 传给提醒/史低/永降/排序/脱池链——全按 appid 消费，
字符串标签让 refresh_pp_flags 直接 ValueError，其余链段静默空转。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.crawl.service import _crawled_appids


def test_crawled_appids_from_missing_batches_are_real_appids():
    """补抓段真实对象在每发的 appids 列表里，任务 id 只是批次标签。"""
    crawled = _crawled_appids(
        [(730, ""), (570, "")],
        [
            {"type": "app", "id": "kz:补1", "region": "kz", "appids": [101, 102]},
            {"type": "app", "id": "tr:补1", "region": "tr", "appids": [103]},
        ],
    )
    assert crawled == [730, 570, 101, 102, 103]


def test_crawled_appids_empty_segments():
    assert _crawled_appids(None, None) == []
    assert _crawled_appids([], [{"id": "kz:补1", "appids": []}]) == []
