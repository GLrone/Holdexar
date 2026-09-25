"""自动更新订阅开关（订阅级）。

用途：限时订阅（只能在窗口内下载、下载后可长期使用）关掉自动更新后，
定时刷新不再每轮撞一次注定失败的抓取；手动重拉不受影响。

边界：开关只决定"定时刷新是否抓这条订阅"，不改变已有节点与来源——
关掉不等于退出生产池（那是准入/下架的事）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
from app.domains.proxypool import bootstrap as bs
import app.domains.proxypool.models  # noqa: F401 —— 注册表结构

NOW = datetime(2026, 9, 22, 12, 0, 0)


async def _factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'auto.db').as_posix()}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(factory, *, auto_refresh: bool, sub_id: int = 1) -> None:
    async with factory() as session:
        session.add(ProxySubscription(
            id=sub_id, kind="clash", url=f"http://example.invalid/{sub_id}",
            label=f"s{sub_id}", created_at=NOW, admission_status=ADMISSION_ACTIVE,
            auto_refresh=auto_refresh))
        await session.commit()


@pytest.mark.asyncio
async def test_auto_off_subscription_skipped_by_schedule(tmp_path, monkeypatch) -> None:
    factory = await _factory(tmp_path)
    await _seed(factory, auto_refresh=True, sub_id=1)
    await _seed(factory, auto_refresh=False, sub_id=2)

    fetched: list[str] = []

    async def _fetch(url, _channels):
        fetched.append(url)
        raise bs.FetchFailedError("boom")

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)
    monkeypatch.setattr(bs, "default_kernel_proxy", lambda: None)

    async with factory() as session:
        result = await bs.sync_subscriptions(
            session, data_dir=tmp_path, now=NOW, only_auto=True)

    assert fetched == ["http://example.invalid/1"], (
        "关闭自动更新的订阅不得被定时刷新抓取"
    )
    assert 2 not in result.failures


@pytest.mark.asyncio
async def test_manual_sync_ignores_switch(tmp_path, monkeypatch) -> None:
    """手动路径（only_auto=False）照常抓——开关只约束定时刷新。"""
    factory = await _factory(tmp_path)
    await _seed(factory, auto_refresh=False, sub_id=2)

    fetched: list[str] = []

    async def _fetch(url, _channels):
        fetched.append(url)
        raise bs.FetchFailedError("boom")

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)
    monkeypatch.setattr(bs, "default_kernel_proxy", lambda: None)

    async with factory() as session:
        await bs.sync_subscriptions(session, data_dir=tmp_path, now=NOW)

    assert fetched == ["http://example.invalid/2"]


@pytest.mark.asyncio
async def test_switch_off_does_not_drop_nodes_or_sources(tmp_path) -> None:
    """关掉开关不改变已有节点/来源——它不是退出生产池。"""
    from app.domains.proxies.service import list_subscriptions, update_subscription

    factory = await _factory(tmp_path)
    await _seed(factory, auto_refresh=True, sub_id=3)

    import app.domains.proxies.service as svc

    original = svc.get_session_factory
    svc.get_session_factory = lambda: factory
    try:
        items = await list_subscriptions("clash")
        assert items[0]["autoRefresh"] is True
        out = await update_subscription(3, auto_refresh=False)
        assert out["autoRefresh"] is False
        items = await list_subscriptions("clash")
        assert items[0]["autoRefresh"] is False
        # 行本身与链接、准入都还在
        assert items[0]["admissionStatus"] == ADMISSION_ACTIVE
        assert items[0]["url"] == "http://example.invalid/3"
    finally:
        svc.get_session_factory = original