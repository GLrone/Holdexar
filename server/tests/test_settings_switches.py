"""自动抓取 / 备份 / 代理维护开关的读写回路与闸门行为。

覆盖：
- settings 路由：/settings/fetch 六源开关默认全开、PUT 局部更新、与
  scheduler.content_fetch_enabled 的 KV 读取一致（fx_auto 沿用历史 key
  `crawl.auto_refresh_rates`）；
- /settings 的 backup_auto 字段回路；
- /proxies/strategy 的 autostart / healthAuto 字段回路；
- 闸门行为：榜单 job、备份 job 到点让路；体检 force 手动路径不受闸；
  汇率启动兜底、Epic 请求路径在开关关闭时不触网。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.core import scheduler as core_scheduler  # noqa: E402
from app.domains.games import boards as boards_mod  # noqa: E402
from app.domains.metadata import service as metadata_service  # noqa: E402
from app.domains.proxies import service as proxies_service  # noqa: E402
from app.domains.rates import service as rates_service  # noqa: E402
from app.domains.settings import service as settings_service  # noqa: E402
from app.domains.settings.router import (  # noqa: E402
    FetchSettingsUpdate,
    SettingsUpdate,
    get_fetch_settings,
    get_settings as get_settings_payload,
    update_fetch_settings,
    update_settings,
)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """临时数据目录 + 建表（teardown 清三重 lru_cache，防跨文件外溢）。"""
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.mark.asyncio
async def test_fetch_settings_defaults_and_roundtrip(tmp_data_dir):
    await init_db()
    defaults = await get_fetch_settings()
    assert defaults.model_dump() == {
        "epic_free": True,
        "hb_choice": True,
        "boards": True,
        "bundle_counts": True,
        "fx_auto": True,
        "fx_history": True,
        "price_interval_hours": 6,
        "catalog_refresh": False,
    }

    await update_fetch_settings(FetchSettingsUpdate(epic_free=False, fx_auto=False))

    updated = await get_fetch_settings()
    assert updated.epic_free is False
    assert updated.fx_auto is False
    assert updated.boards is True  # 未传字段保持不变

    # fx_auto 沿用历史 KV key（原闸无写入点，本端点是第一个写入口）
    assert await settings_service.get_value("crawl.auto_refresh_rates", None) is False
    # 调度器闸门读到同一份 KV
    assert await core_scheduler.content_fetch_enabled("fetch.epic_free") is False
    assert await core_scheduler.content_fetch_enabled("fetch.hb_choice") is True


@pytest.mark.asyncio
async def test_backup_auto_roundtrip_and_job_gate(tmp_data_dir, monkeypatch):
    await init_db()
    payload = await update_settings(SettingsUpdate(backup_auto=False))
    assert payload.backup_auto is False
    assert (await get_settings_payload()).backup_auto is False

    calls: list[str] = []

    async def _fake_create_backup():
        calls.append("backup")
        return {"name": "x", "sizeBytes": 1, "games": 0}

    monkeypatch.setattr("app.core.backup.create_backup", _fake_create_backup)

    await core_scheduler._job_backup()
    assert calls == []  # 开关关闭：定时与启动补备（内部调用 _job_backup）一并让路

    await settings_service.set_value("backup.auto", True)
    await core_scheduler._job_backup()
    assert calls == ["backup"]


@pytest.mark.asyncio
async def test_board_job_gate(tmp_data_dir, monkeypatch):
    await init_db()
    calls: list[str] = []

    async def _fake_refresh_board(key):
        calls.append(key)
        return []

    monkeypatch.setattr(boards_mod, "refresh_board", _fake_refresh_board)

    await settings_service.set_value("fetch.boards", False)
    await core_scheduler._make_board_job("specials")()
    assert calls == []

    await settings_service.set_value("fetch.boards", True)
    await core_scheduler._make_board_job("specials")()
    assert calls == ["specials"]


@pytest.mark.asyncio
async def test_strategy_switches_roundtrip(tmp_data_dir):
    await init_db()
    await proxies_service.set_strategy(autostart=False, health_auto=False)
    strategy = await proxies_service.get_strategy()
    assert strategy["autostart"] is False
    assert strategy["healthAuto"] is False
    assert strategy["strategy"] == "proxy_first"  # 策略字段不受本次更新影响


@pytest.mark.asyncio
async def test_health_check_gate_respects_force(tmp_data_dir):
    await init_db()
    await settings_service.set_value("proxy.health_auto", False)
    assert await proxies_service.maybe_run_clash_health_check() == "disabled"
    # force=True（页面手动检测）不受闸；此处内核未运行，落到 not_running 分支
    state = await proxies_service.maybe_run_clash_health_check(force=True)
    assert state == "clash_not_running"


@pytest.mark.asyncio
async def test_fx_startup_fallback_gate(tmp_data_dir, monkeypatch):
    await init_db()
    await settings_service.set_value("crawl.auto_refresh_rates", False)
    calls: list[str] = []

    async def _fake_refresh_rates():
        calls.append("fx")

    monkeypatch.setattr(rates_service, "refresh_rates", _fake_refresh_rates)
    assert await rates_service.refresh_if_stale() is False
    assert calls == []


@pytest.mark.asyncio
async def test_price_interval_roundtrip_and_clamp(tmp_data_dir):
    await init_db()
    defaults = await get_fetch_settings()
    assert defaults.price_interval_hours == 6

    # 正常写入 + 夹取：超界值落到 1..72
    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=2))
    assert (await get_fetch_settings()).price_interval_hours == 2
    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=999))
    assert (await get_fetch_settings()).price_interval_hours == 72
    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=0))
    assert (await get_fetch_settings()).price_interval_hours == 1

    # 调度器 helper 读到同一份 KV，脏值回落默认
    assert await core_scheduler.price_refresh_interval_hours() == 1
    await settings_service.set_value("crawl.price_interval_hours", "dirty")
    assert await core_scheduler.price_refresh_interval_hours() == 6


def test_next_grid_time_custom_step():
    """网格步长参数化：默认 6h 不变，自定义步长从锚点按步递进。"""
    from datetime import datetime, timedelta

    from app.core.external_time import BEIJING_TZ, next_grid_time

    now = datetime(2026, 9, 26, 3, 0, tzinfo=BEIJING_TZ)
    # 默认步长 6h：锚点 02:00 ≤ now → +6h = 08:00
    assert next_grid_time(now, 2) == datetime(2026, 9, 26, 8, 0, tzinfo=BEIJING_TZ)
    # 自定义步长 12h：锚点 02:00 ≤ now → +12h = 14:00
    assert next_grid_time(now, 2, timedelta(hours=12)) == datetime(
        2026, 9, 26, 14, 0, tzinfo=BEIJING_TZ
    )


@pytest.mark.asyncio
async def test_epic_offers_request_path_gate(tmp_data_dir, monkeypatch):
    await init_db()
    await settings_service.set_value("fetch.epic_free", False)
    calls: list[str] = []

    async def _fake_fetch():
        calls.append("net")
        return None

    monkeypatch.setattr(metadata_service, "_fetch_offers_payload", _fake_fetch)
    monkeypatch.setitem(metadata_service._epic_offers_cache, "payload", None)

    # 无快照 + 开关关：直接空态返回，不触网
    result = await metadata_service.epic_free_offers()
    assert result["ok"] is False
    assert calls == []

    # 有快照 + 开关关：回已有快照（stale 语义），仍不触网
    snapshot = {"source": "epic-offers", "ok": True, "offers": [{"a": 1}], "mobile": None,
                "fetchedAt": "2026-01-01T00:00:00"}
    monkeypatch.setitem(metadata_service._epic_offers_cache, "payload", snapshot)
    cached = await metadata_service.epic_free_offers()
    assert cached["ok"] is True
    assert cached["cached"] is True
    assert calls == []
