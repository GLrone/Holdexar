"""browse 解析层行为验收：评测元数据装配 + 版本后缀提取的基准名回落。

- build_meta 必须把 reviews.summary_filtered 装进返回值（review_count /
  positive_rate 0-100 口径）——此前算了没返回，写库层永远拿 None 回落库内
  旧值，browse 首爬新建的行全库评测量成 0；
- parse_options 的版本后缀基准名回落条目自身英文名：补抓轮无预取、META
  全空时后缀提取不得失效（豪华版 sub 编号更小时会被 min(sub_id) 选成
  现价，女神异闻录４ 黄金版 即此错选）；
- build_game_data 零值护栏：meta 无真评测摘要（0 条/0%）时整体保留库内
  原值——API 对评测数不足的游戏不给 summary，拿到的 0 ≠ 真没有。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import browse_store as bs  # noqa: E402


def _item(name: str = "Persona 4 Golden", reviews=None, options=None) -> dict:
    item: dict = {"appid": 1113000, "success": 1, "visible": True, "name": name}
    if reviews is not None:
        item["reviews"] = reviews
    if options is not None:
        item["purchase_options"] = options
    return item


def test_build_meta_carries_review_summary() -> None:
    meta = bs.StoreBrowseAPI.build_meta(
        _item(
            name="女神异闻录４ 黄金版",
            reviews={"summary_filtered": {
                "review_count": "71207", "percent_positive": "97",
            }},
        ),
        None,
    )
    assert meta["review_count"] == 71207
    assert meta["positive_rate"] == 97


def test_build_meta_without_reviews_is_zero() -> None:
    meta = bs.StoreBrowseAPI.build_meta(_item(reviews=None), None)
    assert meta["review_count"] == 0
    assert meta["positive_rate"] == 0


def test_parse_options_suffix_falls_back_to_item_name() -> None:
    item = _item(options=[
        {"packageid": 447601, "purchase_option_name": "Persona 4 Golden",
         "final_price_in_cents": "12500", "original_price_in_cents": "12500"},
        {"packageid": 376686,
         "purchase_option_name": "Persona 4 Golden - Digital Deluxe Edition",
         "final_price_in_cents": "15600", "original_price_in_cents": "15600"},
    ])
    opts = bs.StoreBrowseAPI.parse_options(item, "")  # 补抓轮：META 全空
    by_sub = {o["sub_id"]: o for o in opts}
    assert by_sub[447601]["version_suffix"] in ("", None)  # 同名 = 标准版
    assert by_sub[376686]["version_suffix"] == "Digital Deluxe Edition"


def _with_preserved(appid: int, keep: dict):
    class _Restore:
        def __enter__(self):
            self._old = bs.PRESERVED.get(appid)
            bs.PRESERVED[appid] = keep
            return bs.PRESERVED

        def __exit__(self, *exc):
            if self._old is None:
                bs.PRESERVED.pop(appid, None)
            else:
                bs.PRESERVED[appid] = self._old
            return False

    return _Restore()


def test_build_game_data_keeps_reviews_when_meta_has_none() -> None:
    appid = 9_979_000
    keep = {"name": "旧名", "review_count": 71207, "positive_rate": 9700,
            "positive_reviews": 69071}
    with _with_preserved(appid, keep):
        meta = bs.StoreBrowseAPI.build_meta(_item(reviews=None), None)
        data = bs.build_game_data(appid, meta, "2026-09-13 00:00:00")
        assert data["review_count"] == 71207
        assert data["positive_rate"] == 9700
        assert data["positive_reviews"] == 69071


def test_build_game_data_overwrites_reviews_when_meta_real() -> None:
    appid = 9_979_001
    keep = {"name": "旧名", "review_count": 10, "positive_rate": 5000,
            "positive_reviews": 5}
    with _with_preserved(appid, keep):
        meta = bs.StoreBrowseAPI.build_meta(
            _item(reviews={"summary_filtered": {
                "review_count": 71207, "percent_positive": 97,
            }}),
            None,
        )
        data = bs.build_game_data(appid, meta, "2026-09-13 00:00:00")
        assert data["review_count"] == 71207
        assert data["positive_rate"] == 9700
        assert data["positive_reviews"] == round(71207 * 97 / 100)
