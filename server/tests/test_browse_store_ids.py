"""IStoreBrowseService URL 构造与分批的行为验收（app 与 bundle 两条链路共用出口）。

- build_url(appids) 与通用 build_ids_url([{"appid": ...}]) 必须完全等价（app 链路既有行为）；
- plan_batches 保序去重、按条数分批，返回纯 appid 列表；
- plan_id_batches 支持 bundleid/packageid，与 URL 长度上限兜底一致；
- 编码坑：`{ } "` 不得被二次百分号编码（encoded=True，否则 400 条 URL 会超限被 Steam 拒）。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.browse_store import MAX_URL_LEN, StoreBrowseAPI  # noqa: E402


def _payload(url) -> dict:
    qs = urllib.parse.urlparse(str(url)).query
    return json.loads(urllib.parse.parse_qs(qs)["input_json"][0])


def test_build_url_equals_generic_ids_url() -> None:
    appids = [728880, 858240, 909720]
    assert str(StoreBrowseAPI.build_url(appids, "us", "english")) == str(
        StoreBrowseAPI.build_ids_url([{"appid": a} for a in appids], "us", "english")
    )


def test_bundle_and_package_ids_accepted() -> None:
    url = StoreBrowseAPI.build_ids_url(
        [{"bundleid": 13608}, {"packageid": 1066582}], "cn", "english"
    )
    body = _payload(url)
    assert body["ids"] == [{"bundleid": 13608}, {"packageid": 1066582}]
    assert body["context"]["country_code"] == "CN"


def test_url_keeps_minimal_escaping() -> None:
    """`{ } "` 原样保留：yarl 二次编码会把 400 条 URL 撑过上限。"""
    raw = str(StoreBrowseAPI.build_ids_url([{"appid": 728880}], "us", "english"))
    assert '"ids"' in raw and "%7B" not in raw and "%22" not in raw


def test_plan_batches_order_and_dedup() -> None:
    appids = [5, 3, 5, 1, 3, 2]
    batches = StoreBrowseAPI.plan_batches(appids, "us", "english", True, 2)
    assert [a for b in batches for a in b] == [5, 3, 1, 2]
    assert all(len(b) <= 2 for b in batches)


def test_plan_batches_splits_by_url_length() -> None:
    """9 位级 id 会把 URL 撑长：兜底再切后每发仍在上限内。"""
    appids = [900_000_000 + i for i in range(400)]
    batches = StoreBrowseAPI.plan_batches(appids, "us", "english", True, 400)
    assert len(batches) > 1
    for batch in batches:
        url = StoreBrowseAPI.build_url(batch, "us", "english")
        assert len(str(url)) <= MAX_URL_LEN


def test_plan_id_batches_shapes() -> None:
    batches = StoreBrowseAPI.plan_id_batches(
        [{"bundleid": 1}, {"bundleid": 2}, {"packageid": 3}, {"bundleid": 1}],
        "us", "english", True, 10,
    )
    assert batches == [[{"bundleid": 1}, {"bundleid": 2}, {"packageid": 3}]]


def test_cis_cache_roundtrip(tmp_path) -> None:
    """CIS 基线缓存：save → load 跨进程回环（基线现由 kz/ua 批次顺路采集，
    缓存只覆盖「ru 先于 kz/ua 落库」窗口与冷启动）。"""
    from app.crawler import browse_store as bs

    try:
        bs.CIS_SUBS[620] = {111, 222}
        bs.CIS_SUBS_TS[620] = 12345.0
        bs.save_cis_cache(tmp_path)
        assert (tmp_path / bs.CIS_CACHE_FILENAME).is_file()

        bs.CIS_SUBS.clear()
        bs.CIS_SUBS_TS.clear()
        bs.load_cis_cache(tmp_path)
        assert bs.CIS_SUBS[620] == {111, 222}
        assert bs.CIS_SUBS_TS[620] == 12345.0

        # 损坏文件 = 无基线：不抛、不污染现有状态
        (tmp_path / bs.CIS_CACHE_FILENAME).write_text("{broken", encoding="utf-8")
        bs.load_cis_cache(tmp_path)
        assert bs.CIS_SUBS[620] == {111, 222}
    finally:
        bs.CIS_SUBS.clear()
        bs.CIS_SUBS_TS.clear()


def test_cis_gate_lifecycle() -> None:
    """RU 基线门：登记 kz/ua 批次后关门，批次逐个释放、清空即放行；
    无 kz/ua 批次的运行默认放行。"""
    import asyncio

    from app.crawler import browse_store as bs

    try:
        bs.reset_run_state()
        assert bs.CIS_GATE.is_set(), "无 kz/ua 批次时默认放行"

        bs.register_cis_pending(["kz:1", "ua:1", "ua:2"])
        assert not bs.CIS_GATE.is_set(), "登记后关门"
        bs.cis_batch_fetched("ua:1")
        assert not bs.CIS_GATE.is_set(), "还有批次未采完"
        bs.cis_batch_fetched("kz:1")
        bs.cis_batch_fetched("ua:2")
        assert bs.CIS_GATE.is_set(), "全部采完（含穷尽失败）即放行"
    finally:
        bs.CIS_PENDING.clear()
        bs.CIS_GATE.set()
