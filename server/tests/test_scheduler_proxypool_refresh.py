"""调度层：proxypool 刷新与旧链路「在跑哪条订阅」识别解耦。

旧链路 `running_subscription_is(url)` 依据内核启动文本，实例以 `-f <data>/clash/config.yaml`
启动时文本里没有订阅 URL → 状态长期是 `unknown_subscription`。该状态只属于旧链路自己的
重拉判断，不得阻止 proxypool 按库里的 subscription + admission_status 执行 sync。
"""
from __future__ import annotations

import os

from types import SimpleNamespace

import pytest

from app.core import scheduler as sched
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.domains.proxies import service as proxies_service
from app.domains.proxypool import bootstrap as bs


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则 settings 缓存会把临时目录带进下一个测试文件
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


async def _run_job(monkeypatch, legacy_state: str) -> list[dict]:
    """跑一次 `_job_subscription_refresh`，返回 proxypool 分流的调用参数列表。"""
    calls: list[dict] = []

    async def fake_legacy() -> dict:
        return {"state": legacy_state, "subscriptionId": None, "nodes": None,
                "restarted": False, "failed": False}

    async def fake_split(session, **kw):  # noqa: ANN001
        calls.append(kw)
        return SimpleNamespace(synced=True, pool_changed=False, action="none")

    monkeypatch.setattr(sched, "_crawler_idle", lambda: True)
    monkeypatch.setattr(
        proxies_service, "maybe_refresh_active_clash_subscription", fake_legacy
    )
    monkeypatch.setattr(bs, "handle_subscription_refresh", fake_split)

    await sched._job_subscription_refresh()
    return calls


@pytest.mark.asyncio
async def test_unknown_subscription_still_runs_proxypool_split(tmp_data_dir, monkeypatch):
    """旧链路认不出内核在跑哪条订阅时，proxypool 仍必须执行一次 sync 分流。"""
    calls = await _run_job(monkeypatch, "unknown_subscription")
    assert len(calls) == 1, "unknown_subscription 不得阻止 proxypool 刷新"


@pytest.mark.asyncio
async def test_refreshed_path_runs_proxypool_split_exactly_once(tmp_data_dir, monkeypatch):
    """正常 refreshed 路径不得造成重复 sync（同一拍只执行一次分流）。"""
    calls = await _run_job(monkeypatch, "refreshed")
    assert len(calls) == 1, "refreshed 路径应恰好执行一次 proxypool 分流"
