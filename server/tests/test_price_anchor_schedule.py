"""池价格爬取锚点网格调度测试。

规则：请求外部时间判定太平洋夏令时（timeapi.io dstActive；请求失败
回落本地 zoneinfo）→ 夏令时锚点北京 01:00 / 冬令时 02:00 → 锚点 + 6h
步进网格；job 每轮触发先 modify_job 自续约下一格（APScheduler 实证：
job 内手改的 next_run_time 不被触发器覆盖），interval 6h 仅兜底。

隔离（对齐 test_rates_schedule.py）：不打真实网络（fetch_pacific_dst /
_http_get_json / _resolve_proxy 打桩）、不动生产库
（run_sequential 打桩），调度用真实
AsyncIOScheduler（内存 jobstore，测试毕 shutdown 复原模块单例）。
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import external_time
from app.core.external_time import BEIJING_TZ, next_grid_time


def _bj(*args) -> datetime:
    return datetime(*args, tzinfo=BEIJING_TZ)


# ── 纯函数：锚点映射与网格推算 ──


def test_anchor_hour_mapping():
    """夏令时 → 1 点锚；冬令时 → 2 点锚（对齐 Steam 每日折扣刷新）。"""
    assert external_time.anchor_hour(True) == 1
    assert external_time.anchor_hour(False) == 2


def test_grid_summer_points():
    """夏令时网格 1/7/13/19：锚前 → 当日锚点；触发后 → 下一格。"""
    assert next_grid_time(_bj(2026, 9, 7, 0, 58), 1) == _bj(2026, 9, 7, 1)
    assert next_grid_time(_bj(2026, 9, 7, 1, 0), 1) == _bj(2026, 9, 7, 7)
    assert next_grid_time(_bj(2026, 9, 7, 13, 30), 1) == _bj(2026, 9, 7, 19)
    assert next_grid_time(_bj(2026, 9, 7, 23, 30), 1) == _bj(2026, 9, 8, 1)


def test_grid_winter_points():
    """冬令时网格 2/8/14/20：含当日末格过完 → 次日锚点。"""
    assert next_grid_time(_bj(2026, 12, 24, 4, 30), 2) == _bj(2026, 12, 24, 8)
    assert next_grid_time(_bj(2026, 12, 24, 20, 0), 2) == _bj(2026, 12, 25, 2)


def test_grid_fall_transition_day():
    """2026-11-01 17:00 北京 DST 结束（秋季回拨）：13:00 触发仍夏令时
    → 19:00；19:00 触发已切冬令时 → 20:00（切换日一次性短间隔）；
    之后恢复 6h → 次日 02:00 锚。"""
    assert next_grid_time(_bj(2026, 11, 1, 13), 1) == _bj(2026, 11, 1, 19)
    assert next_grid_time(_bj(2026, 11, 1, 19), 2) == _bj(2026, 11, 1, 20)
    assert next_grid_time(_bj(2026, 11, 1, 20), 2) == _bj(2026, 11, 2, 2)


def test_grid_spring_transition_day():
    """2027-03-14 18:00 北京 DST 开始（春季前跳）：20:00 触发已切夏令时
    → 新网格首格 = 次日 01:00（间隔 5h，切换日一次性短间隔）。"""
    assert next_grid_time(_bj(2027, 3, 14, 20), 1) == _bj(2027, 3, 15, 1)


# ── 外部时间判定：通道矩阵与兜底 ──


@pytest.mark.asyncio
async def test_fetch_dst_external_wins(monkeypatch):
    """外部 API 返回 dstActive → 权威采纳。"""
    async def _no_proxy():
        return None

    async def _fake_get(url, proxy):
        assert "timeapi.io" in url
        return {"dstActive": True, "dateTime": "2026-09-06T16:38:15.5585457",
                "timeZone": "America/Los_Angeles"}

    monkeypatch.setattr(external_time, "_resolve_proxy", _no_proxy)
    monkeypatch.setattr(external_time, "_http_get_json", _fake_get)
    assert await external_time.fetch_pacific_dst() == (True, "timeapi.io")


@pytest.mark.asyncio
async def test_fetch_dst_proxy_then_direct(monkeypatch):
    """代理通道失败 → 直连通道重试成功（策略代理优先 + 直连兜底矩阵）。"""
    tried = []

    async def _proxy():
        return "http://127.0.0.1:7897"

    async def _flaky(url, proxy):
        tried.append(proxy)
        return None if proxy is not None else {"dstActive": False}

    monkeypatch.setattr(external_time, "_resolve_proxy", _proxy)
    monkeypatch.setattr(external_time, "_http_get_json", _flaky)
    assert await external_time.fetch_pacific_dst() == (False, "timeapi.io")
    assert tried == ["http://127.0.0.1:7897", None]


@pytest.mark.asyncio
async def test_fetch_dst_rejects_wrong_zone(monkeypatch):
    """响应时区与请求不符（服务端异常）→ 不采信，继续走兜底。"""
    async def _no_proxy():
        return None

    async def _wrong_zone(url, proxy):
        return {"dstActive": True, "timeZone": "Europe/Berlin"}

    monkeypatch.setattr(external_time, "_resolve_proxy", _no_proxy)
    monkeypatch.setattr(external_time, "_http_get_json", _wrong_zone)
    _, source = await external_time.fetch_pacific_dst()
    assert source == "zoneinfo"


@pytest.mark.asyncio
async def test_fetch_dst_garbage_falls_back(monkeypatch):
    """响应缺 dstActive / 全链失联 → 回落本地 zoneinfo（永不抛异常）。"""
    async def _no_proxy():
        return None

    async def _garbage(url, proxy):
        return {"error": "boom"}

    monkeypatch.setattr(external_time, "_resolve_proxy", _no_proxy)
    monkeypatch.setattr(external_time, "_http_get_json", _garbage)
    is_dst, source = await external_time.fetch_pacific_dst()
    assert source == "zoneinfo"
    assert is_dst == (datetime.now(external_time.PACIFIC_TZ).dst() != timedelta(0))


@pytest.mark.asyncio
async def test_probe_next_grid_composes(monkeypatch):
    """probe = DST 判定 + 锚点 + 网格推算的组合出口（调度自续约单一入口）。"""
    async def _dst_on():
        return (True, "stub")

    monkeypatch.setattr(external_time, "fetch_pacific_dst", _dst_on)
    nxt, hour, is_dst, source = await external_time.probe_next_grid()
    assert (hour, is_dst, source) == (1, True, "stub")
    now = datetime.now(BEIJING_TZ)
    assert (nxt.minute, nxt.second) == (0, 0)
    assert nxt.hour in {1, 7, 13, 19}
    assert now < nxt <= now + timedelta(hours=6)


# ── 调度器集成：自续约机制（真 AsyncIOScheduler，协作方全打桩）──


async def _fire_price_refresh_once(monkeypatch, is_dst: bool) -> datetime:
    """注册→立即触发→返回续约后的 next_run_time；毕 shutdown 复原单例。"""
    from app.core import scheduler as sched_mod
    import app.domains.crawl.service as crawl_service_mod

    async def _dst():
        return (is_dst, "stub")

    fired: list[bool] = []

    async def _spy_run(specs, **kw):
        fired.append(True)
        return []

    monkeypatch.setattr(external_time, "fetch_pacific_dst", _dst)
    monkeypatch.setattr(crawl_service_mod, "run_sequential", _spy_run)

    # 链尾捆绑包存量刷新打桩（真实实现出网 + 触生产库）
    from app.domains.bundles import refresh as bundles_refresh_mod

    async def _no_bundles():
        return {"ok": True, "updated": 0, "total": 0}

    monkeypatch.setattr(bundles_refresh_mod, "refresh_bundles", _no_bundles)

    sched = sched_mod.scheduler
    sched.add_job(
        sched_mod._job_price_refresh, "interval", hours=6, id="price_refresh",
        next_run_time=datetime.now(BEIJING_TZ), coalesce=True, misfire_grace_time=None,
    )
    try:
        sched.start()
        for _ in range(100):  # 全打桩，首轮亚秒级完成
            await asyncio.sleep(0.05)
            if fired:
                break
        assert fired, "price_refresh 未在 next_run_time 触发"
        await asyncio.sleep(0.05)  # 让 job body 完成写回
        job = sched.get_job("price_refresh")
        assert job is not None and job.next_run_time is not None
        return job.next_run_time
    finally:
        sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_price_refresh_self_renews_to_summer_grid(monkeypatch):
    """自续约实证：触发后 next_run_time 落到锚点网格（整点 0 分 0 秒），
    而非 interval 兜底的 now+6h（带任意分钟秒）——可判别 modify 生效。"""
    nxt = await _fire_price_refresh_once(monkeypatch, is_dst=True)
    assert (nxt.minute, nxt.second, nxt.microsecond) == (0, 0, 0)
    assert nxt.hour in {1, 7, 13, 19}


@pytest.mark.asyncio
async def test_price_refresh_self_renews_to_winter_grid(monkeypatch):
    """DST 判定冬令时 → 网格切 02/08/14/20（重新计算）。"""
    nxt = await _fire_price_refresh_once(monkeypatch, is_dst=False)
    assert (nxt.minute, nxt.second, nxt.microsecond) == (0, 0, 0)
    assert nxt.hour in {2, 8, 14, 20}
