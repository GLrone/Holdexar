"""P1.7 缺口修复：订阅抓取的完整通道链 + 失败原因透出（5 条）。

真实运行暴露的两个最小缺口：
1. `sync_subscriptions` 调 `build_channels()` 不传参数 → **只有 direct**，直连一超时
   bootstrap 就永远建不起来，哪怕现有 Mihomo 内核本可以拉下这条订阅；
2. `detail` 只说"无可用节点/无订阅"，把"没订阅"和"抓取失败"混成一句模糊话。

边界不变：kernel_proxy 只是**订阅获取的辅助通道**，不是 crawler 的代理，
也不构成 proxypool Runtime 的回退。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxies.models import ProxySubscription  # noqa: E402
from app.domains.proxypool import bootstrap as bs  # noqa: E402
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, CHANNEL_KERNEL, FORMAT_YAML, FetchResult,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def kernel_exe_path(tmp_data_dir) -> Path:
    for candidate in (
        clash_manager.kernel_exe(tmp_data_dir),
        clash_manager.bundled_clash_dir() / kernel_filename(),
        *(
            sorted(Path(os.environ["LOCALAPPDATA"]).glob(f"holdexar*/clash/{kernel_filename()}"))
            if os.environ.get("LOCALAPPDATA") else []
        ),
    ):
        if candidate.is_file():
            return candidate
    pytest.skip("本机没有可用的 mihomo 内核资产（assets/clash/ 不随仓库分发）")


@pytest.fixture
def pool_runtime():
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


def _result(nodes: list[dict], channel: str = CHANNEL_KERNEL) -> FetchResult:
    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    return FetchResult(raw=raw, http_status=200, content_type="text/yaml",
                       channel=channel, fmt=FORMAT_YAML)


def _node(name: str, server: str) -> dict:
    return {"name": name, "type": "http", "server": server, "port": 1}


async def _sub() -> int:
    async with get_session_factory()() as s:
        row = ProxySubscription(kind="clash", url="https://sub.invalid/x",
                                created_at=NOW)
        s.add(row)
        await s.commit()
        return row.id


async def _ensure(data_dir, runtime, exe, **kw):
    async with get_session_factory()() as s:
        r = await bs.ensure_pool_runtime(
            s, data_dir=data_dir, runtime=runtime, exe_path=str(exe), now=NOW, **kw
        )
        await s.commit()
        return r


# ══ 1. 直连失败、kernel 通道成功 → 整链通 ═══════════════════════
@pytest.mark.asyncio
async def test_direct_fails_kernel_channel_succeeds(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()
    seen: list[list[str]] = []

    async def _fetch(url, channels, **kw):
        seen.append([c.name for c in channels])
        names = {c.name for c in channels}
        if CHANNEL_KERNEL not in names:
            raise RuntimeError("订阅抓取失败（1 通道）：direct:ConnectTimeout")
        return _result([_node("A", "127.0.0.1")], CHANNEL_KERNEL)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)

    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path,
                           kernel_proxy="http://127.0.0.1:7890")

    assert seen and CHANNEL_KERNEL in seen[0], "必须把 kernel 通道真的传下去"
    assert CHANNEL_DIRECT in seen[0], "direct 仍是第一通道"
    assert result.ready is True and result.bootstrapped is True
    assert result.pool_names == ("1|A",)
    assert result.proxy_url is not None


# ══ 2. 所有通道都失败 → 不 ready + detail 带真实原因 ════════════
@pytest.mark.asyncio
async def test_all_channels_fail_surfaces_reason(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    sub_id = await _sub()

    async def _boom(url, channels, **kw):
        raise RuntimeError("订阅抓取失败（2 通道）：direct:ConnectTimeout / "
                           "kernel:ConnectError")

    monkeypatch.setattr(bs, "fetch_subscription", _boom)

    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path,
                           kernel_proxy="http://127.0.0.1:7890")

    assert result.ready is False
    assert "订阅抓取失败" in result.detail, "detail 必须能直接看到抓取失败"
    assert "ConnectTimeout" in result.detail, "并带出真实原因（通道级）"
    from app.domains.proxypool.runtime import require_runtime_proxy_url

    with pytest.raises(Exception, match="代理运行时不可用"):
        require_runtime_proxy_url(tmp_data_dir)
    # 持久化诊断事实仍在
    from sqlalchemy import select
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxySubscription).where(ProxySubscription.id == sub_id)
        )).scalar_one()
    assert row.last_fetch_status == "FAILED" and "ConnectTimeout" in (row.last_error or "")


# ══ 3. 没有订阅 → 与"抓取失败"区分开 ═══════════════════════════
@pytest.mark.asyncio
async def test_no_subscription_has_distinct_reason(
    tmp_data_dir, kernel_exe_path, pool_runtime
) -> None:
    await init_db()
    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.ready is False
    assert "没有可 bootstrap 的订阅" in result.detail
    assert "订阅抓取失败" not in result.detail, "两种情况不能混成同一句模糊话"


# ══ 4. 默认 kernel_proxy 取自现有内核（老 Clash）它自己不是 crawler 代理 ══
@pytest.mark.asyncio
async def test_default_kernel_proxy_from_existing_runtime(monkeypatch) -> None:
    class _Status:
        def status(self):
            return {"running": True, "port": 7890}

    monkeypatch.setattr(clash_manager, "runtime", _Status())
    assert bs.default_kernel_proxy() == "http://127.0.0.1:7890"

    class _Stopped:
        def status(self):
            return {"running": False, "port": None}

    monkeypatch.setattr(clash_manager, "runtime", _Stopped())
    assert bs.default_kernel_proxy() is None, "内核没跑就没有第二通道（不影响直连）"


# ══ 5. kernel 通道只用于"取订阅"：crawler 仍然只认 proxypool proxy_url ══
@pytest.mark.asyncio
async def test_kernel_channel_never_becomes_crawler_proxy(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()

    async def _fetch(url, channels, **kw):
        return _result([_node("A", "127.0.0.1")], CHANNEL_KERNEL)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)
    result = await _ensure(tmp_data_dir, pool_runtime, kernel_exe_path,
                           kernel_proxy="http://127.0.0.1:7890")

    from app.domains.proxypool.runtime import require_runtime_proxy_url

    crawler_proxy = require_runtime_proxy_url(tmp_data_dir)
    assert crawler_proxy == result.proxy_url
    assert crawler_proxy != "http://127.0.0.1:7890", (
        "老 Clash 只是订阅获取辅助通道，绝不能变 crawler 的代理"
    )
    assert "7890" not in crawler_proxy