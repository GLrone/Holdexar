"""CDK 查价的按版本缓存：同一 (appid, sub) 在 TTL 内只打一次外部接口。

不触真实网络——两个平台的抓取函数与代理解析全部 monkeypatch；sub 解析
（缺省 sub_id 时读库）也换成桩，本文件不建库。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import cdk_fetcher


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """桩掉平台抓取，返回按 sub 区分的价格，并记录每次外部调用。"""
    cdk_fetcher.invalidate_cdk_cache()
    calls: list[tuple[str, int, int | None]] = []

    async def fake_steampy(appid, sub_id, proxy_url=None):
        calls.append(("steampy", appid, sub_id))
        return {"listed": True, "price": f"¥{sub_id}", "url": "u"}

    async def fake_cici(appid, sub_id, proxy_url=None):
        calls.append(("cici", appid, sub_id))
        return {"listed": False, "price": None, "url": "u"}

    async def no_proxy():
        return None

    monkeypatch.setattr(cdk_fetcher, "fetch_steampy", fake_steampy)
    monkeypatch.setattr(cdk_fetcher, "fetch_steamcici", fake_cici)
    monkeypatch.setattr(cdk_fetcher, "resolve_proxy_for_request", no_proxy)
    yield calls
    cdk_fetcher.invalidate_cdk_cache()


@pytest.mark.asyncio
async def test_same_sub_reuses_cache(_stub_external):
    """同一版本连查两次：第二次走缓存，外部接口不再被调用。"""
    first = await cdk_fetcher.fetch_cdk(898750, sub_id=510898)
    second = await cdk_fetcher.fetch_cdk(898750, sub_id=510898)

    assert first == second
    assert second["subId"] == 510898
    assert _stub_external == [("steampy", 898750, 510898), ("cici", 898750, 510898)]


@pytest.mark.asyncio
async def test_each_sub_has_own_cache_entry(_stub_external):
    """换版本另算一档；切回原版本仍命中它自己的缓存。"""
    await cdk_fetcher.fetch_cdk(898750, sub_id=510898)
    await cdk_fetcher.fetch_cdk(898750, sub_id=510899)
    await cdk_fetcher.fetch_cdk(898750, sub_id=510898)

    assert [c for c in _stub_external if c[0] == "steampy"] == [
        ("steampy", 898750, 510898),
        ("steampy", 898750, 510899),
    ]


@pytest.mark.asyncio
async def test_default_key_distinct_from_explicit_sub(_stub_external, monkeypatch):
    """不传 sub_id（标准版，键 = appid）与显式传同一 sub（键 = (appid, sub)）互不顶替。"""

    async def fake_resolve(appid):
        return 999

    monkeypatch.setattr(cdk_fetcher, "resolve_sub_id", fake_resolve)

    await cdk_fetcher.fetch_cdk(898750)
    await cdk_fetcher.fetch_cdk(898750, sub_id=999)

    assert [c for c in _stub_external if c[0] == "steampy"] == [
        ("steampy", 898750, 999),
        ("steampy", 898750, 999),
    ]