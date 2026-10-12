"""本机网络被接管（疑似虚拟网卡模式）时的让路防呆。

三处接线各一例：环境判定本身（假地址段命中/未命中/解析失败）、订阅配置剥离
虚拟网卡段、体检与价格周期的让路。判据与拒因文案同源，改一处三处都动。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.proxies import clash_manager, env_check
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import cycle_run
from app.domains.crawl import service as crawl_service
from app.domains.crawl.models import CrawlJob


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()


# ── 环境判定：假地址段命中才算接管 ────────────────────────────────


def test_fake_ip_resolution_is_treatment(monkeypatch):
    monkeypatch.setattr(
        env_check, "_resolves_to_fake_ip", lambda host=env_check.PROBE_HOST: True
    )
    env_check.reset_cache()
    assert env_check.tun_mode_suspected() is True


def test_real_ip_resolution_is_not_treatment(monkeypatch):
    monkeypatch.setattr(
        env_check, "_resolves_to_fake_ip", lambda host=env_check.PROBE_HOST: False
    )
    env_check.reset_cache()
    assert env_check.tun_mode_suspected() is False


def test_resolution_failure_is_not_treatment(monkeypatch):
    """解析报错不是接管证据（可能只是断网）：误停全局抓取代价远大于多跑一轮。"""

    def _boom(host, _proto=None):
        raise OSError("dns fail")

    monkeypatch.setattr("socket.getaddrinfo", _boom)
    assert env_check._resolves_to_fake_ip() is False


def test_result_is_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _probe(host=env_check.PROBE_HOST):
        calls["n"] += 1
        return True

    monkeypatch.setattr(env_check, "_resolves_to_fake_ip", _probe)
    env_check.reset_cache()
    assert env_check.tun_mode_suspected() is True
    assert env_check.tun_mode_suspected() is True
    assert calls["n"] == 1, "判定短缓存：一拍内不重复做域名解析"


# ── 订阅配置：虚拟网卡段剥离（幂等）─────────────────────────────

_FULL = """mixed-port: 7890
tun:
  enable: true
  stack: system
  dns-hijack:
    - 198.18.0.2:53
proxies:
  - name: n1
    type: ss
    server: 1.2.3.4
"""


def test_strip_removes_tun_only():
    out = clash_manager.strip_tun_section(_FULL)
    doc = yaml.safe_load(out)
    assert "tun" not in doc
    assert doc["mixed-port"] == 7890
    assert doc["proxies"][0]["name"] == "n1"


def test_strip_is_idempotent():
    once = clash_manager.strip_tun_section(_FULL)
    assert clash_manager.strip_tun_section(once) == once
    assert clash_manager.strip_tun_section("mixed-port: 7890\n") == "mixed-port: 7890\n"


def test_strip_passthrough_on_broken_yaml():
    bad = "a: [unclosed\n"
    assert clash_manager.strip_tun_section(bad) == bad


# ── 体检让路：整轮不跑，不留台账不占预约位 ──────────────────────


@pytest.mark.asyncio
async def test_proxypool_cycle_skipped_when_hijacked(monkeypatch):
    from app.core import scheduler as sched_mod
    from app.domains.proxypool import scheduling as _sched

    calls: list[str] = []

    async def _no_run(*a, **kw):
        calls.append("ran")

    monkeypatch.setattr(env_check, "tun_mode_suspected", lambda: True)
    monkeypatch.setattr(_sched, "run_proxypool_cycle", _no_run)

    await sched_mod._job_proxypool_cycle()
    assert calls == [], "环境不可信时整轮体检（含 L0 与恢复探测）都不跑"


# ── 价格周期让路：当前段跑完后不再推进 ──────────────────────────


@pytest.mark.asyncio
async def test_sequential_starts_nothing_when_hijacked_from_start(monkeypatch):
    """接管在先：一个段都不启动（爬了也只会堆失败与限流）。"""
    started: list[dict] = []

    async def _fake_start_job(**kw):
        started.append(kw)
        return {"id": len(started)}

    monkeypatch.setattr(crawl_service, "start_job", _fake_start_job)
    monkeypatch.setattr(crawl_service, "_tun_mode_suspected", lambda: True)

    chain = asyncio.Event()
    skipped: list[str] = []
    results = await crawl_service.run_sequential(
        [{"scope": "pool"}, {"scope": "missing"}], skip_reasons=skipped,
        stop_event=chain,
    )
    assert started == [] and results == []
    assert chain.is_set() is True, "置位链级停止位：整轮按停止语义收敛"
    assert len(skipped) == 1 and env_check.USER_MESSAGE in skipped[0]


@pytest.mark.asyncio
async def test_sequential_stops_after_current_segment_when_hijacked_later(monkeypatch):
    """接管在后：已启动的当前段跑完（数据照常落库），下一段不再启动。"""
    started: list[dict] = []

    async def _fake_start_job(**kw):
        started.append(kw)
        return {"id": len(started)}

    def _hijacked_after_first():
        return len(started) >= 1

    monkeypatch.setattr(crawl_service, "start_job", _fake_start_job)
    monkeypatch.setattr(crawl_service, "_tun_mode_suspected", _hijacked_after_first)

    chain = asyncio.Event()
    skipped: list[str] = []
    results = await crawl_service.run_sequential(
        [{"scope": "pool"}, {"scope": "missing"}], skip_reasons=skipped,
        stop_event=chain,
    )
    assert len(started) == 1 and len(results) == 1, "当前段跑完即停，不启动下一段"
    assert chain.is_set() is True
    assert any(env_check.USER_MESSAGE in s for s in skipped)


@pytest.mark.asyncio
async def test_hijacked_cycle_records_rejection_reason(db, monkeypatch):
    async def _regions(regions=None):
        return ["CN"]

    monkeypatch.setattr(crawl_service, "effective_regions", _regions)

    async def _stopped_only(specs, **kw):
        async with db() as session:
            session.add(CrawlJob(
                kind="scheduled", status="stopped", cycle_id=kw.get("cycle_id"),
                started_at=datetime.now(), finished_at=datetime.now(),
            ))
            await session.commit()
        kw["skip_reasons"].append(f"pool：{env_check.USER_MESSAGE}")
        kw["stop_event"].set()
        return [{"id": 1}]

    monkeypatch.setattr(crawl_service, "run_sequential", _stopped_only)

    calls = {"detect": 0}
    monkeypatch.setattr(cycle_run, "detect_events", lambda cid: _noop(cid, calls))
    monkeypatch.setattr(cycle_run, "notify_events", lambda cid: None)

    assert await cycle_run.run_price_cycle([{"scope": "pool"}]) is True
    rows = await cycle_mod.list_cycles(5)
    assert rows and rows[0]["status"] == cycle_mod.CANCELLED
    assert rows[0]["error"] == env_check.USER_MESSAGE, "账本要能看出是让路停的，不是爬挂了"
    assert calls["detect"] == 0, "半轮事实不产生价格事件"


async def _noop(cycle_id, calls):
    calls["detect"] += 1
