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

import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import access, lan_access  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.core import scheduler as core_scheduler  # noqa: E402
from app.domains.games import boards as boards_mod  # noqa: E402
from app.domains.metadata import service as metadata_service  # noqa: E402
from app.domains.proxies import service as proxies_service  # noqa: E402
from app.domains.rates import service as rates_service  # noqa: E402
from app.domains.settings import service as settings_service  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.domains.settings.router import (  # noqa: E402
    LanAccessUpdate,
    ContentSchedule,
    FetchSettingsUpdate,
    SettingsUpdate,
    get_fetch_settings,
    get_settings as get_settings_payload,
    update_fetch_settings,
    update_settings,
)


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
        # 目录层默认随策略：默认直连下目录段不随轮出网
        "catalog_refresh": False,
        "queue_missing": True,
        "queue_pool": True,
        "queue_discounts": True,
        "hb_schedule": {"time": "02:30", "days": 1},
        "epic_schedule": {"time": "23:05", "days": 1},
    }

    await update_fetch_settings(FetchSettingsUpdate(epic_free=False, fx_auto=False))

    updated = await get_fetch_settings()
    assert updated.epic_free is False
    assert updated.fx_auto is False
    assert updated.boards is True

    # fx_auto 沿用历史 KV key（原闸无写入点，本端点是第一个写入口）
    assert await settings_service.get_value("crawl.auto_refresh_rates", None) is False
    assert await core_scheduler.content_fetch_enabled("fetch.epic_free") is False
    assert await core_scheduler.content_fetch_enabled("fetch.hb_choice") is True


@pytest.mark.asyncio
async def test_content_schedule_roundtrip_validation_and_reschedule(tmp_data_dir, monkeypatch):
    """内容源刷新节奏：读写回路 + 时刻校验/天数夹取 + 调度在跑时立即重排
    + 脏 KV 逐项回落默认 + 启动链应用同一重排入口。"""
    await init_db()
    rescheduled: list[tuple] = []
    monkeypatch.setattr(
        core_scheduler.scheduler,
        "reschedule_job",
        lambda job, **kw: rescheduled.append((job, kw)),
    )
    monkeypatch.setattr(type(core_scheduler.scheduler), "running", property(lambda self: True))

    updated = await update_fetch_settings(
        FetchSettingsUpdate(
            hb_schedule=ContentSchedule(time="04:00", days=3),
            epic_schedule=ContentSchedule(time="1:05"),
        )
    )
    assert updated.hb_schedule.time == "04:00" and updated.hb_schedule.days == 3
    assert updated.epic_schedule.time == "01:05" and updated.epic_schedule.days == 1
    assert await settings_service.get_value("fetch.hb_choice_schedule", None) == {
        "time": "04:00", "days": 3,
    }
    assert ("hb_choice", {"trigger": "cron", "hour": 4, "minute": 0, "day": "*/3"}) in rescheduled
    assert ("epic_free", {"trigger": "cron", "hour": 1, "minute": 5}) in rescheduled

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FetchSettingsUpdate(hb_schedule=ContentSchedule(time="2:5"))
    with pytest.raises(ValidationError):
        FetchSettingsUpdate(hb_schedule=ContentSchedule(time="24:00"))
    clamped = FetchSettingsUpdate(hb_schedule=ContentSchedule(time="04:00", days=99))
    assert clamped.hb_schedule.days == 30

    await settings_service.set_value(
        "fetch.epic_free_schedule", {"time": "99:99", "days": 0}
    )
    reread = await get_fetch_settings()
    assert reread.epic_schedule.time == "23:05" and reread.epic_schedule.days == 1

    rescheduled.clear()
    await settings_service.set_value(
        "fetch.hb_choice_schedule", {"time": "03:15", "days": 2}
    )
    await core_scheduler.apply_content_schedules_from_settings()
    assert ("hb_choice", {"trigger": "cron", "hour": 3, "minute": 15, "day": "*/2"}) in rescheduled
    assert ("epic_free", {"trigger": "cron", "hour": 23, "minute": 5}) in rescheduled


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
    assert calls == []

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
    assert strategy["strategy"] == "direct_only"


@pytest.mark.asyncio
async def test_health_summary_gate_respects_switch(tmp_data_dir):
    await init_db()
    await settings_service.set_value("proxy.health_auto", False)
    assert await proxies_service.refresh_subscription_health_summary() == "disabled"
    await settings_service.set_value("proxy.health_auto", True)
    assert await proxies_service.refresh_subscription_health_summary() == "refreshed"


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

    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=2))
    assert (await get_fetch_settings()).price_interval_hours == 2
    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=999))
    assert (await get_fetch_settings()).price_interval_hours == 72
    await update_fetch_settings(FetchSettingsUpdate(price_interval_hours=0))
    assert (await get_fetch_settings()).price_interval_hours == 1

    assert await core_scheduler.price_refresh_interval_hours() == 1
    await settings_service.set_value("crawl.price_interval_hours", "dirty")
    assert await core_scheduler.price_refresh_interval_hours() == 6


def test_next_grid_time_custom_step():
    """网格步长参数化：默认 6h 不变，自定义步长从锚点按步递进。"""
    from datetime import datetime, timedelta

    from app.core.external_time import BEIJING_TZ, next_grid_time

    now = datetime(2026, 9, 26, 3, 0, tzinfo=BEIJING_TZ)
    assert next_grid_time(now, 2) == datetime(2026, 9, 26, 8, 0, tzinfo=BEIJING_TZ)
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

    result = await metadata_service.epic_free_offers()
    assert result["ok"] is False
    assert calls == []

    snapshot = {"source": "epic-offers", "ok": True, "offers": [{"a": 1}], "mobile": None,
                "fetchedAt": "2026-01-01T00:00:00"}
    monkeypatch.setitem(metadata_service._epic_offers_cache, "payload", snapshot)
    cached = await metadata_service.epic_free_offers()
    assert cached["ok"] is True
    assert cached["cached"] is True
    assert calls == []




@pytest.mark.asyncio
async def test_set_values_publishes_settings_changed(tmp_data_dir):
    """KV 写口唯一收口处的变更广播：一次批量一条事件、键列表可订阅。"""
    from app.core.events import bus

    await init_db()
    sub = bus.subscribe()
    try:
        await settings_service.set_values({"ui.probe.a": 1, "ui.probe.b": 2})
        event = sub.critical.get_nowait()
        assert event.type == "settings.changed"
        assert event.payload["keys"] == ["ui.probe.a", "ui.probe.b"]
        assert sub.critical.empty()
    finally:
        bus.unsubscribe(sub)


def test_kick_job_ids_only_for_off_to_on():
    """补拍判定：只有 off→on 翻转才补；缺省=开 的键首存 on 不算翻转。"""
    from app.domains.settings.router import _kick_job_ids_for_enabled_source

    assert _kick_job_ids_for_enabled_source(
        {"fetch.epic_free": False, "fetch.boards": True},
        {"fetch.epic_free": True, "fetch.boards": True},
    ) == ["epic_free"]
    assert _kick_job_ids_for_enabled_source({"fetch.epic_free": True}, {"fetch.epic_free": True}) == []
    assert _kick_job_ids_for_enabled_source({"fetch.epic_free": True}, {"fetch.epic_free": False}) == []


class _StubScheduler:
    """kick 单测替身：记录 modify_job 调用。"""

    running = True

    def __init__(self, existing: bool = True):
        self.existing = existing
        self.modified: list[tuple[str, object]] = []

    def get_job(self, job_id):
        return object() if self.existing else None

    def modify_job(self, job_id, next_run_time):
        self.modified.append((job_id, next_run_time))


@pytest.mark.asyncio
async def test_kick_source_jobs_modifies_next_run(monkeypatch):
    stub = _StubScheduler()
    monkeypatch.setattr(core_scheduler, "scheduler", stub)
    await core_scheduler.kick_source_jobs(["epic_free", "fx_refresh"])
    assert [job for job, _ in stub.modified] == ["epic_free", "fx_refresh"]
    assert all(next_run is not None for _, next_run in stub.modified)

    gone = _StubScheduler(existing=False)
    monkeypatch.setattr(core_scheduler, "scheduler", gone)
    await core_scheduler.kick_source_jobs(["epic_free"])
    assert gone.modified == []


@pytest.mark.asyncio
async def test_update_fetch_settings_kicks_enabled_source(tmp_data_dir, monkeypatch):
    """off→on 补拍接线：调度在跑时提前下一拍；已开着再保存不补。"""
    import asyncio

    stub = _StubScheduler()
    monkeypatch.setattr(core_scheduler, "scheduler", stub)
    await init_db()
    await settings_service.set_value("fetch.epic_free", False)

    await update_fetch_settings(FetchSettingsUpdate(epic_free=True))
    await asyncio.sleep(0.01)
    assert [job for job, _ in stub.modified] == ["epic_free"]

    stub.modified.clear()
    await update_fetch_settings(FetchSettingsUpdate(epic_free=True))
    await asyncio.sleep(0.01)
    assert stub.modified == []




@pytest.mark.asyncio
async def test_lan_password_minimum_length(tmp_data_dir):
    await init_db()
    with pytest.raises(HTTPException) as short_exc:
        await update_settings(
            payload=SettingsUpdate(lan_access=LanAccessUpdate(password="1")),
        )
    assert short_exc.value.status_code == 422
    await update_settings(
        payload=SettingsUpdate(lan_access=LanAccessUpdate(password="x" * 8)),
    )
    # PHC 终态：哈希 KV 存验证器串，旧可逆密文键清账
    stored = await settings_service.get_value(settings_service.LAN_PASSWORD_HASH_KEY)
    assert isinstance(stored, str) and stored.startswith("$scrypt$")
    assert lan_access.verify_password("x" * 8, stored)
    assert await settings_service.get_secret_value(settings_service.LAN_PASSWORD_KEY) is None


@pytest.mark.asyncio
async def test_lan_password_change_revokes_old_sessions(tmp_data_dir):
    """改密即吊销：已设密码下再改，进程会话令牌轮换（旧解锁会话全失效）。

    测试口令运行时生成，与真实凭据无关。
    """
    await init_db()
    pw_a = "pw-" + secrets.token_hex(8)
    pw_b = "pw-" + secrets.token_hex(8)
    await update_settings(
        payload=SettingsUpdate(lan_access=LanAccessUpdate(password=pw_a))
    )
    old_token = access.session_token()
    await update_settings(
        payload=SettingsUpdate(lan_access=LanAccessUpdate(password=pw_b))
    )
    assert access.session_token() != old_token
