from datetime import datetime

import pytest

from app.core.database import get_session_factory
from app.domains.crawl import cycle, service
from app.domains.games.models import Game
from app.domains.monitoring import service as monitoring


@pytest.mark.asyncio
async def test_manual_all_includes_catalog_when_automatic_catalog_is_off(monkeypatch):
    from app.domains.settings import service as settings

    async def value(key, default=None):
        return {"proxy.strategy": "direct_only", "crawl.catalog_refresh": False}.get(key, default)

    monkeypatch.setattr(settings, "get_value", value)
    automatic = await service.default_queue_specs()
    manual = await service.default_queue_specs(full_library=True)
    assert "catalog" not in [s.get("scope") for s in automatic]
    assert "catalog" in [s.get("scope") for s in manual]
    assert "discounts" not in [s.get("scope") for s in manual]


@pytest.mark.asyncio
async def test_execution_uses_objects_and_regions_frozen_at_planning(monkeypatch):
    calls = []

    async def regions(requested=None):
        return requested or ["CN", "US"]

    async def resolve(scope, appids=None):
        calls.append(scope)
        return [101, 102]

    monkeypatch.setattr(service, "effective_regions", regions)
    monkeypatch.setattr(service, "plan_scope_appids", resolve)
    specs = [{"scope": "catalog"}]
    cid = await cycle.create("manual")
    await cycle.freeze_expected(cid, specs)

    async def start(**kwargs):
        assert kwargs["scope"] == "appids"
        assert kwargs["appids"] == [101, 102]
        assert kwargs["regions"] == ["CN", "US"]
        return {"id": 999}

    monkeypatch.setattr(service, "start_job", start)
    monkeypatch.setattr(service, "_active", None)
    await service.run_sequential(specs)
    assert calls == ["catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["discounts", "specials"])
async def test_board_scopes_respect_explicit_exclusion(scope, monkeypatch):
    from app.domains.games import boards

    async def board(_key):
        return [101, 102, 102]

    monkeypatch.setattr(boards, "get_board", board)
    await monitoring.set_exclusion("game", 101, True)
    assert await service.resolve_scope_appids(scope, None) == [(102, "")]


@pytest.mark.asyncio
async def test_full_library_preserves_free_removed_and_monitoring_rules():
    now = datetime.now()
    async with get_session_factory()() as session:
        session.add_all([
            Game(appid=101, name="catalog", updated_at=now),
            Game(appid=102, name="free", updated_at=now, free_kind="permanent"),
            Game(appid=103, name="removed", updated_at=now, removed_at=now),
            Game(appid=104, name="excluded", updated_at=now),
        ])
        await session.commit()
    await monitoring.set_exclusion("game", 104, True)
    assert await service.resolve_scope_appids("catalog", None) == [(101, "")]


@pytest.mark.asyncio
async def test_exact_cancel_is_owned_and_idempotent(monkeypatch):
    import asyncio
    from app.domains.crawl.models import CrawlJob

    old_id = await cycle.create("manual")
    live_id = await cycle.create("manual")
    chain, stopped = asyncio.Event(), asyncio.Event()
    service.register_chain_stop(chain, cycle_id=live_id)
    async with get_session_factory()() as session:
        job = CrawlJob(kind="manual", status="running", cycle_id=live_id)
        session.add(job)
        await session.commit()
        job_id = job.id
    monkeypatch.setattr(service, "_active", service.JobHandle(
        id=job_id, task=asyncio.current_task(), stop_event=stopped, cycle_id=live_id,
    ))
    assert not await service.stop_job(job_id + 1)
    assert not await service.stop_job(cycle_id=old_id)
    assert not chain.is_set() and not stopped.is_set()
    assert await service.stop_job(cycle_id=live_id)
    assert not await service.stop_job(cycle_id=live_id)
    assert chain.is_set() and stopped.is_set()
    assert (await cycle.get(old_id))["status"] == cycle.PLANNING
    assert (await cycle.get(live_id))["status"] == cycle.CANCELLED
    service.unregister_chain_stop(chain)
    assert service.active_cycle_id() is None


@pytest.mark.asyncio
async def test_cycle_identity_is_bound_during_planning(monkeypatch):
    from app.domains.crawl import cycle_run
    calls = []

    async def freeze(cycle_id, specs):
        assert service.active_cycle_id() == cycle_id
        calls.append(cycle_id)
        assert await service.stop_job(cycle_id=cycle_id)
        return ["CN"], 0

    monkeypatch.setattr(cycle, "freeze_expected", freeze)
    await cycle_run.run_price_cycle([])
    assert len(calls) == 1
    assert (await cycle.get(calls[0]))["status"] == cycle.CANCELLED
    assert service.active_cycle_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "failed", "stopped"])
async def test_finish_race_preserves_terminal_result_and_merges_stats(terminal, monkeypatch):
    from app.domains.crawl.models import CrawlJob

    events = []
    monkeypatch.setattr(service.bus, "publish", lambda *args, **kwargs: events.append((args, kwargs)))
    ended = datetime(2026, 1, 1)
    async with get_session_factory()() as session:
        job = CrawlJob(kind="manual", status=terminal, finished_at=ended,
                       error="original", stats_json={"trigger": "manual", "total": 2})
        session.add(job)
        await session.commit()
        job_id = job.id
    await service._finish_job(job_id, "stopped", error="用户已停止")
    await service._finish_job(job_id, "done", stats={"processed": 1}, error=None)
    row = await service._load_job(job_id)
    assert (row.status, row.finished_at, row.error) == (terminal, ended, "original")
    assert row.stats_json == {"trigger": "manual", "total": 2, "processed": 1}
    assert events == []
