"""P1.7 接线契约（7 条）：启动链 + 订阅刷新链如何消费 bootstrap 原语。

分工不变：`ensure_pool_runtime()` 只负责"没有 Runtime 时建起来"；重建仍归 P1.6-C。
本批只把原语接到两个生产调用点，不扩展 bootstrap 本身。

判定"池变了"用的是 **Runtime Pool signature**（eligible 节点的 `runtime_name` +
配置指纹），**不是 Snapshot SHA**：快照变了但合格节点集没变，不需要重启内核。
"""
from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxies.models import (  # noqa: E402
    ADMISSION_ACTIVE,
    ProxySubscription,
)
from app.domains.proxypool import bootstrap as bs  # noqa: E402
from app.domains.proxypool import scheduling as sched  # noqa: E402
from app.domains.proxypool.subscription import (  # noqa: E402
    CHANNEL_DIRECT, FORMAT_YAML, FetchResult,
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


@pytest.fixture(autouse=True)
def _clean_signals(monkeypatch):
    monkeypatch.setattr(bs, "build_channels", lambda *a, **k: [])
    sched.take_rebuild_pending()
    yield
    sched.take_rebuild_pending()


def _node(name: str, server: str) -> dict:
    return {"name": name, "type": "http", "server": server, "port": 1}


def _script(nodes: list[dict], monkeypatch) -> None:
    async def _fetch(url, channels, **kw):
        raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
        return FetchResult(raw=raw, http_status=200, content_type="text/yaml",
                           channel=CHANNEL_DIRECT, fmt=FORMAT_YAML)

    monkeypatch.setattr(bs, "fetch_subscription", _fetch)


async def _sub() -> int:
    """ACTIVE 订阅：本文件验的是签名/重建编排，前提是来源已在生产里。"""
    async with get_session_factory()() as s:
        row = ProxySubscription(kind="clash", url="https://sub.invalid/x",
                                created_at=NOW, admission_status=ADMISSION_ACTIVE)
        s.add(row)
        await s.commit()
        return row.id


async def _triage(data_dir, runtime, exe):
    async with get_session_factory()() as s:
        result = await bs.handle_subscription_refresh(
            s, data_dir=data_dir, runtime=runtime, exe_path=str(exe), now=NOW,
        )
        await s.commit()
        return result


# ══ 1. 启动链会调用 ensure_pool_runtime ═════════════════════════
@pytest.mark.asyncio
async def test_startup_chain_invokes_bootstrap(tmp_data_dir, monkeypatch) -> None:
    from app.core import scheduler as core_scheduler

    await init_db()
    called: list[str] = []

    async def _fake_ensure(session, **kw):
        called.append("ensure")
        return bs.BootstrapResult(True, True, "http://127.0.0.1:1", None, (), "stub")

    monkeypatch.setattr(bs, "ensure_pool_runtime", _fake_ensure)
    await core_scheduler._startup_pool_runtime()
    assert called == ["ensure"]


# ══ 2. bootstrap 失败不阻塞应用启动 / 调度器 ════════════════════
@pytest.mark.asyncio
async def test_bootstrap_failure_does_not_block_startup(
    tmp_data_dir, monkeypatch
) -> None:
    from app.core import scheduler as core_scheduler

    await init_db()

    async def _boom(session, **kw):
        raise RuntimeError("bootstrap 爆炸")

    monkeypatch.setattr(bs, "ensure_pool_runtime", _boom)
    # 不抛异常 = 应用与 scheduler 都能继续
    await core_scheduler._startup_pool_runtime()


# ══ 3. 无 Runtime + 同步成功 → 直接 bootstrap，不产生额外 rebuild ══
@pytest.mark.asyncio
async def test_first_sync_bootstraps_without_pending_rebuild(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()
    _script([_node("A", "127.0.0.1")], monkeypatch)

    result = await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.action == "bootstrapped"
    assert result.bootstrap is not None and result.bootstrap.ready is True
    assert sched.rebuild_pending() is False, "首次 bootstrap 不该再产生一次多余重建"


# ══ 4. 已有 Runtime + 快照变但 Pool signature 不变 → 不 rebuild ══
@pytest.mark.asyncio
async def test_signature_unchanged_does_not_rebuild(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()
    _script([_node("A", "127.0.0.1")], monkeypatch)
    await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)
    pid = pool_runtime.process.pid

    # 换个名字但**配置指纹相同**（name 不参与指纹）→ 合格集与签名都不变
    _script([_node("A-改名", "127.0.0.1")], monkeypatch)
    second = await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert second.synced is True
    assert second.pool_changed is False
    assert second.action == "none"
    assert sched.rebuild_pending() is False, "快照变了但池没变 → 不该重建"
    assert pool_runtime.process.pid == pid


# ══ 5. 已有 Runtime + Pool signature 改变 → 只 request_rebuild ═══
@pytest.mark.asyncio
async def test_signature_changed_requests_rebuild_only(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()
    _script([_node("A", "127.0.0.1")], monkeypatch)
    await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)
    pid = pool_runtime.process.pid

    _script([_node("A", "127.0.0.1"), _node("B", "127.0.0.2")], monkeypatch)
    result = await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.pool_changed is True
    assert result.action == "rebuild_requested"
    assert sched.rebuild_pending() is True
    assert pool_runtime.process.pid == pid, "刷新路径绝不自己 stop/start"


# ══ 6. 无 Runtime + Pool 变化 → bootstrap（不是 rebuild）══════════
@pytest.mark.asyncio
async def test_pool_change_without_runtime_bootstraps(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    """订阅第一次成功拉取、此前一直没有可用 Runtime —— 生产第一次进闭环。"""
    await init_db()
    await _sub()
    _script([_node("A", "127.0.0.1")], monkeypatch)
    async with get_session_factory()() as s:      # 先有 Registry/Pool，但**没有** Runtime
        await bs.sync_subscriptions(s, data_dir=tmp_data_dir, now=NOW)
        await s.commit()

    _script([_node("A", "127.0.0.1"), _node("B", "127.0.0.2")], monkeypatch)
    result = await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.pool_changed is True
    assert result.action == "bootstrapped", "没 Runtime 时该 bootstrap，而不是 request_rebuild"
    assert sched.rebuild_pending() is False
    assert result.bootstrap is not None and result.bootstrap.ready is True


# ══ 7. 订阅同步失败 → 不碰 Runtime、不产生 rebuild ═══════════════
@pytest.mark.asyncio
async def test_sync_failure_leaves_runtime_untouched(
    tmp_data_dir, kernel_exe_path, pool_runtime, monkeypatch
) -> None:
    await init_db()
    await _sub()
    _script([_node("A", "127.0.0.1")], monkeypatch)
    await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)
    pid = pool_runtime.process.pid

    async def _boom(url, channels, **kw):
        raise RuntimeError("订阅下载失败")

    monkeypatch.setattr(bs, "fetch_subscription", _boom)
    result = await _triage(tmp_data_dir, pool_runtime, kernel_exe_path)

    assert result.synced is False
    assert result.action == "none"
    assert sched.rebuild_pending() is False
    assert pool_runtime.process.pid == pid, "已有 Runtime 必须继续工作"