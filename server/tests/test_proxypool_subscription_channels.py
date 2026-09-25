"""订阅抓取通道链：直连 → 内核 → 池 → 本机混合端口。

只留直连一条通道等于把订阅刷新交给运气：面板域名多被墙、直连失败是常态，
一旦整链失败，后果不是"这一次没抓到"，而是：快照停止更新 → 该订阅提供的节点
失去"新的健康证据" → 判定退休 → 池再也建不起来。链尾的本机混合端口借道口
（用户自启 Clash / Verge 常在听）就是为此兜底。

本组用例钉住链的**构成与顺序**，以及"调用方漏传也必须拿到内核通道"这条防线。
边界不变：这些通道只用于**订阅获取**，不是 crawler 的代理，也不构成 Runtime 回退。
"""
from __future__ import annotations

import socket

import pytest

from app.domains.proxies import clash_manager
from app.domains.proxypool import bootstrap as bs
from app.domains.proxypool import subscription as sub_mod
from app.domains.proxypool.subscription import (
    CHANNEL_DIRECT,
    CHANNEL_KERNEL,
    CHANNEL_LOCAL,
    CHANNEL_POOL,
    FetchFailedError,
    build_channels,
)


def _alive_port() -> tuple[int, socket.socket]:
    """占一个真实在听的端口，冒充"本机混合端口"。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return int(sock.getsockname()[1]), sock


def test_local_channel_appended_when_port_listening(monkeypatch) -> None:
    """本机混合端口在听 → 作为**最后**一条通道加入。"""
    port, holder = _alive_port()
    try:
        monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", (port,))
        channels = build_channels(
            kernel_proxy="http://127.0.0.1:7890",
            pool_proxy="http://127.0.0.1:1234",
        )
    finally:
        holder.close()
    assert [(c.name, c.proxy) for c in channels] == [
        (CHANNEL_DIRECT, None),
        (CHANNEL_KERNEL, "http://127.0.0.1:7890"),
        (CHANNEL_POOL, "http://127.0.0.1:1234"),
        (CHANNEL_LOCAL, f"http://127.0.0.1:{port}"),
    ], "自管出口（内核/池）必须排在借道口之前"


def test_local_channel_absent_when_nothing_listening(monkeypatch) -> None:
    """没有本机混合端口在听 → 不凭空加通道（借道口是加成，不是必需）。"""
    monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", ())
    names = [c.name for c in build_channels(kernel_proxy="http://127.0.0.1:7890")]
    assert names == [CHANNEL_DIRECT, CHANNEL_KERNEL]


def test_kernel_port_not_duplicated_as_local_channel(monkeypatch) -> None:
    """内核自己的端口已经在链上 → 不再作为"本地混合端口"重复一条。"""
    port, holder = _alive_port()
    try:
        kernel = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", (port,))
        channels = build_channels(kernel_proxy=kernel)
    finally:
        holder.close()
    assert [c.proxy for c in channels] == [None, kernel]


def test_explicit_local_proxies_override_probe() -> None:
    """显式指定借道口：不探活、原样入链（测试与诊断用）。"""
    channels = build_channels(local_proxies=["http://127.0.0.1:9"])
    assert [c.name for c in channels] == [CHANNEL_DIRECT, CHANNEL_LOCAL]


@pytest.mark.asyncio
async def test_sync_subscriptions_defaults_kernel_channel(tmp_path, monkeypatch) -> None:
    """调用方**漏传** kernel_proxy 时，仍必须现取老内核入口——链不能只剩直连。

    定时刷新那条调用没有传通道参数：缺这条防线，链上只剩直连一条。
    """
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
    import app.domains.proxypool.models  # noqa: F401 —— 注册表结构

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'sub.db').as_posix()}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    captured: list[list[str]] = []
    monkeypatch.setattr(bs, "default_kernel_proxy", lambda: "http://127.0.0.1:7890")
    monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", ())

    real_build = sub_mod.build_channels

    def _spy(**kw):
        channels = real_build(**kw)
        captured.append([c.name for c in channels])
        return channels

    monkeypatch.setattr(bs, "build_channels", _spy)

    async def _fetch(_url, _channels):
        raise FetchFailedError("boom")

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)

    now = datetime(2026, 9, 22, 12, 0, 0)
    async with session_factory() as session:
        session.add(ProxySubscription(
            kind="clash", url="http://example.invalid/sub", label="t",
            created_at=now, admission_status=ADMISSION_ACTIVE))
        await session.commit()
        result = await bs.sync_subscriptions(session, data_dir=tmp_path, now=now)

    assert captured, "sync_subscriptions 必须真的调用 build_channels"
    assert captured[0] == [CHANNEL_DIRECT, CHANNEL_KERNEL], (
        "漏传 kernel_proxy 时链上只剩直连一条，兜底通道全部缺席"
    )
    assert result.failures, "抓取失败仍要如实登记"