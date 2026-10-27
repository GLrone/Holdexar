import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory, write_session
from app.crawler import browse_store as bs
from app.crawler.db_writer import DbWriter
from app.crawler.utils import get_beijing_time_obj
from app.domains.crawl import service as crawl
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice


@pytest.mark.asyncio
@pytest.mark.parametrize("rate_limited", [False, True])
async def test_request_retries_share_one_budget_and_record_missing_once(monkeypatch, rate_limited):
    bs.reset_run_state()
    requests, delays, missing = [], [], []

    async def fetch(*args):
        requests.append(1)
        raise bs.SteamRateLimitError() if rate_limited else TimeoutError()

    async def sleep(delay):
        delays.append(delay)

    async def mark(pairs):
        missing.append(pairs)
        return len(pairs)

    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    monkeypatch.setattr(bs.asyncio, "sleep", sleep)
    monkeypatch.setattr(bs, "network_checker", SimpleNamespace(is_offline=False))
    context = SimpleNamespace(task={"type": "app", "id": "cn:1", "region": "cn", "appids": [991001]},
        queue=asyncio.Queue(), db_writer=SimpleNamespace(mark_region_status_missing_batch=mark))
    for attempt in range(bs.MAX_PARTIAL_RETRIES + 1):
        await bs.handle_browse_price_task(context)
        if attempt < bs.MAX_PARTIAL_RETRIES:
            context.task = context.queue.get_nowait()
            assert context.task["retries"] == attempt + 1
    assert len(requests) == bs.MAX_PARTIAL_RETRIES + 1
    assert delays == ([5.0, 10.0, 20.0] if rate_limited else [1.0, 1.0, 1.0])
    assert missing == [[(991001, "cn")]]
    assert bs.FAILED_TASKS == ["cn:1"]
    assert context.queue.empty()


@pytest.mark.asyncio
async def test_browse_batch_saves_extras_and_discovers_bundles_once(monkeypatch):
    from app.domains.games.models import GamePriceHistory

    bs.reset_run_state()
    ids = [991001, 991002]
    calls = []
    items = {aid: {"appid": aid, "item_type": 0, "type": 0, "success": 1, "visible": True,
        "name": str(aid), "purchase_options": [
            {"packageid": aid, "purchase_option_name": str(aid), "final_price_in_cents": "1000",
             "original_price_in_cents": "2000", "discount_pct": 50, "bundle_discount_pct": 10,
             "active_discounts": [{"discount_end_date": 1792688400, "discount_description": "special"}]},
            {"bundleid": 123, "purchase_option_name": "Collection"},
        ]} for aid in ids}

    async def fetch(*args):
        return items, set(), 100

    writer = DbWriter()
    record = writer.record_bundle_discoveries

    async def discoveries(rows):
        calls.append(rows)
        return await record(rows)

    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    monkeypatch.setattr(bs, "PRESERVED", {aid: {"name": str(aid), "type": "GAME"} for aid in ids})
    monkeypatch.setattr(writer, "record_bundle_discoveries", discoveries)
    context = SimpleNamespace(task={"type": "app", "id": "cn:1", "region": "cn", "appids": ids},
                              db_writer=writer, http_client=None, session=None)
    await bs.handle_browse_price_task(context)
    assert bs.FAILED_TASKS == []
    assert len(calls) == 1
    assert [row["bundle_id"] for row in calls[0]] == [123]
    async with get_session_factory()() as session:
        rows = (await session.execute(select(GamePriceHistory))).scalars().all()
        assert len(rows) == 2
        assert {(r.discount_end_ts, r.discount_desc, r.bundle_discount_pct) for r in rows} == {
            (1792688400, "special", 10)}


def now():
    return get_beijing_time_obj().replace(tzinfo=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_region_status_commit_failure_is_not_swallowed(monkeypatch, batch):
    async def reject(_):
        raise RuntimeError("status commit rejected")

    monkeypatch.setattr(AsyncSession, "commit", reject)
    with pytest.raises(Exception) as failure:
        if batch:
            await DbWriter().mark_region_status_missing_batch([(991001, "cn"), (991001, "CN")])
        else:
            await DbWriter().mark_region_status(991001, "cn", "locked")
    assert failure.value.pairs == [(991001, "CN")]
    async with get_session_factory()() as session:
        assert await session.get(GameCurrentPrice, (991001, "CN")) is None


@pytest.mark.asyncio
async def test_persistence_failure_counts_once_without_network_retry(monkeypatch):
    from app.crawler.router import CrawlerRouter
    from app.crawler.scheduler import CrawlerScheduler

    bs.reset_run_state()
    requests = []

    async def fetch(*args):
        requests.append(1)
        return {}, [991001], 0

    async def reject(_):
        raise RuntimeError("status commit rejected")

    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    monkeypatch.setattr(AsyncSession, "commit", reject)
    router = CrawlerRouter()
    router.handle("app")(bs.handle_browse_price_task)
    errors = []
    scheduler = CrawlerScheduler(router, SimpleNamespace(), DbWriter(), worker_count=1,
                                 failure_ledger=bs.FAILED_TASKS, error_sink=errors.append)
    async with aiohttp.ClientSession() as session:
        await scheduler.run([{"type": "app", "id": "cn:1", "region": "cn", "appids": [991001]}], session)
    assert scheduler.counts() == (1, 0, 1)
    assert requests == [1]
    assert bs.FAILED_TASKS == []
    assert [error.pairs for error in errors] == [[(991001, "CN")]]


async def seed_job(*, status="failed", stats=None, age=10):
    stamp = now() - timedelta(minutes=age)
    async with write_session() as session:
        job = CrawlJob(kind="manual", status=status, mode="app", regions_json=["CN", "US"],
                       started_at=stamp, finished_at=stamp, stats_json=stats if stats is not None else {
                           "total": 2, "targets": {"appids": [991001, 991002], "regions": ["CN", "US"]},
                           "targetStartedAt": stamp.isoformat(),
                       })
        session.add(job)
        await session.commit()
        return job.id


def units(tasks):
    return {(int(aid), task["region"].upper()) for task in tasks for aid in task["appids"]}


def target_units(stats):
    targets = stats.get("targets") or {}
    return units(targets["tasks"]) if "tasks" in targets else {
        (int(aid), str(cc).upper()) for aid in targets.get("appids", []) for cc in targets.get("regions", [])}


@pytest.mark.asyncio
async def test_failed_targets_reenter_existing_missing_batches():
    await seed_job()
    assert await crawl.has_pending_missing()
    assert units(await crawl._missing_tasks(cooldown_minutes=4)) == {
        (991001, "CN"), (991002, "CN"), (991001, "US"), (991002, "US")}


@pytest.mark.asyncio
async def test_only_later_persisted_attempts_or_explicit_skips_resolve_targets():
    jid = await seed_job()
    async with write_session() as session:
        session.add_all([Game(appid=aid, name=str(aid)) for aid in [991001, 991002]])
        session.add_all([
            GameCurrentPrice(appid=991001, region_code="CN", currency="CNY", price_status="locked", updated_at=now()),
            GameCurrentPrice(appid=991001, region_code="US", currency="USD", price_status="missing", fail_count=1, updated_at=now()),
            GameCurrentPrice(appid=991002, region_code="CN", currency="CNY", price_status="ok", updated_at=now()-timedelta(days=1)),
        ])
        await session.commit()
    await seed_job(status="done", stats={"skipUnits": ["991002:US"], "targetStartedAt": now().isoformat()}, age=0)
    assert units(await crawl._missing_tasks(cooldown_minutes=4)) == {(991002, "CN")}
    await DbWriter().mark_region_status(991002, "CN", "blocked")
    assert not await crawl.has_pending_missing()
    await crawl._missing_tasks(cooldown_minutes=4)
    async with get_session_factory()() as session:
        assert "targets" not in (await session.get(CrawlJob, jid)).stats_json


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "stopped", "cancelled", "done"])
async def test_nonrecoverable_jobs_do_not_reenter_missing(status):
    await seed_job(status=status)
    assert units(await crawl._missing_tasks(cooldown_minutes=0)) == set()


@pytest.mark.asyncio
async def test_legacy_statistics_do_not_invent_region_targets():
    await seed_job(stats={"total": 42, "processed": 0})
    assert not await crawl.has_pending_missing()


@pytest.mark.asyncio
async def test_finish_preserves_initial_targets_and_none_statistics():
    jid = await seed_job(status="running")
    await crawl._finish_job(jid, "failed", {"processed": 1, "failed": 1})
    await crawl._finish_job(jid, "failed", None, "failure")
    async with get_session_factory()() as session:
        row = await session.get(CrawlJob, jid)
        assert row.stats_json["targets"]["appids"] == [991001, 991002]
        assert row.stats_json["total"] == 2
        assert row.stats_json["failed"] == 1


@pytest.mark.asyncio
async def test_job_persistence_errors_do_not_report_done(monkeypatch):
    jid = await seed_job(status="running")

    async def run(*args, **kwargs):
        return {"total": 1, "processed": 1, "success": 0, "failed": 1, "unpersistedUnits": ["991001:CN"]}

    monkeypatch.setattr(crawl, "run_crawl", run)
    await crawl._execute(jid, [], SimpleNamespace(occupancy_tag=None), asyncio.Event())
    async with get_session_factory()() as session:
        assert (await session.get(CrawlJob, jid)).status == "failed"


@pytest.mark.asyncio
async def test_targets_cannot_bypass_existing_missing_cooldown_or_blocked_exhaustion():
    await seed_job(stats={"targets": {"appids": [991001], "regions": ["CN", "US"]},
                         "targetStartedAt": (now()-timedelta(minutes=10)).isoformat()})
    async with write_session() as session:
        session.add(Game(appid=991001, name="existing missing"))
        session.add_all([
            GameCurrentPrice(appid=991001, region_code="CN", currency="CNY", price_status="missing", fail_count=4,
                             updated_at=now()-timedelta(minutes=11)),
            GameCurrentPrice(appid=991001, region_code="US", currency="USD", price_status="blocked", fail_count=5,
                             updated_at=now()),
        ])
        await session.commit()
    assert not await crawl.has_pending_missing(cooldown_minutes=24*60)
    assert units(await crawl._missing_tasks(cooldown_minutes=24*60)) == set()
    assert units(await crawl._missing_tasks(cooldown_minutes=4)) == {(991001, "CN")}


@pytest.mark.asyncio
async def test_retention_keeps_unresolved_failed_jobs_but_releases_settled_and_stopped():
    stamp = now()-timedelta(days=100)
    stats = {"targets": {"appids": [991001], "regions": ["CN"]}, "targetStartedAt": stamp.isoformat()}
    failed = await seed_job(stats=stats, age=100*24*60)
    stopped = await seed_job(status="stopped", stats=stats, age=100*24*60)
    done = await seed_job(status="done", stats=stats, age=100*24*60)
    assert await crawl.prune_crawl_jobs() == 2
    async with get_session_factory()() as session:
        assert await session.get(CrawlJob, failed) is not None
        assert await session.get(CrawlJob, stopped) is None
        assert await session.get(CrawlJob, done) is None
    await DbWriter().mark_region_status(991001, "CN", "locked")
    assert await crawl.prune_crawl_jobs() == 1


async def start_env(monkeypatch):
    from app.domains.proxies import service as proxies
    from app.domains.settings import service as settings

    async def regions(_):
        return ["CN", "US"]

    async def workers():
        return 2

    async def value(key, default=None):
        return default

    async def no_proxy():
        return None

    monkeypatch.setattr(crawl, "effective_regions", regions)
    monkeypatch.setattr(crawl, "_resolve_worker_count", workers)
    monkeypatch.setattr(settings, "get_value", value)
    monkeypatch.setattr(proxies, "resolve_system_proxy_url", no_proxy)


@pytest.mark.asyncio
async def test_targets_are_committed_before_execution_starts(monkeypatch):
    await start_env(monkeypatch)
    executed = asyncio.Event()
    captured = []

    async def execute(jid, *args, **kwargs):
        from app.crawler.occupancy import end_crawl

        async with get_session_factory()() as session:
            row = await session.get(CrawlJob, jid)
            captured.append(row.stats_json)
        end_crawl()
        crawl._active = None
        executed.set()

    monkeypatch.setattr(crawl, "_execute", execute)
    result = await crawl.start_job(scope="appids", appids=[991001, 991002])
    await asyncio.wait_for(executed.wait(), 2)
    assert result["id"]
    assert captured[0]["targets"] == {"appids": [991001, 991002], "regions": ["CN", "US"]}
    assert abs((now()-datetime.fromisoformat(captured[0]["targetStartedAt"])).total_seconds()) < 5


@pytest.mark.asyncio
async def test_target_commit_failure_releases_occupancy_and_never_executes(monkeypatch):
    from app.crawler.occupancy import crawler_busy

    await start_env(monkeypatch)
    executions = []

    async def reject(_):
        raise RuntimeError("target commit rejected")

    async def execute(*args, **kwargs):
        executions.append(1)

    monkeypatch.setattr(AsyncSession, "commit", reject)
    monkeypatch.setattr(crawl, "_execute", execute)
    with pytest.raises(RuntimeError, match="target commit"):
        await crawl.start_job(scope="appids", appids=[991001])
    assert not crawler_busy()
    assert executions == []
    async with get_session_factory()() as session:
        assert list((await session.execute(select(CrawlJob))).scalars()) == []


@pytest.mark.asyncio
async def test_interrupted_job_retains_precise_prebuilt_targets(monkeypatch):
    stamp = now()-timedelta(minutes=10)
    stats = {"targets": {"tasks": [
        {"region": "CN", "appids": [991001, 991002]},
        {"region": "US", "appids": [991002]},
    ]}, "targetStartedAt": stamp.isoformat()}
    jid = await seed_job(status="running", stats=stats)
    monkeypatch.setattr(crawl, "_PROCESS_STARTED_AT", now())
    await crawl.cleanup_orphan_jobs()
    assert units(await crawl._missing_tasks(cooldown_minutes=4)) == {
        (991001, "CN"), (991002, "CN"), (991002, "US")}
    async with get_session_factory()() as session:
        row = await session.get(CrawlJob, jid)
        assert row.status == "failed"
        assert row.stats_json == stats


@pytest.mark.asyncio
async def test_runner_returns_exact_unpersisted_and_skipped_units(monkeypatch):
    from app.crawler import runner

    async def fetch(*args):
        return {}, [991001], 0

    async def reject(_):
        raise RuntimeError("status commit rejected")

    monkeypatch.setattr(runner.bs, "load_preserved_rows", lambda _: {})
    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    monkeypatch.setattr(AsyncSession, "commit", reject)
    errors = []
    result = await runner._run_crawl_locked([], config=runner.CrawlRunConfig(regions=["CN"], workers=1),
        pre_tasks=[{"type": "app", "id": "cn:1", "region": "cn", "appids": [991001]}],
        error_sink=errors.append)
    assert result["unpersistedUnits"] == ["991001:CN"]
    assert result["skipUnits"] == []
    assert (result["processed"], result["success"], result["failed"]) == (1, 0, 1)
    assert errors[0].pairs == [(991001, "CN")]


@pytest.mark.asyncio
async def test_non_game_skip_result_settles_target_without_inventing_price(monkeypatch):
    from app.crawler import runner

    async def fetch(*args):
        return {}, [], 0

    monkeypatch.setattr(runner.bs, "load_preserved_rows", lambda _: {991001: {
        "appid": 991001, "name": "tool", "type": "TOOL"}})
    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    result = await runner._run_crawl_locked([], config=runner.CrawlRunConfig(regions=["CN"], workers=1),
        pre_tasks=[{"type": "app", "id": "cn:1", "region": "cn", "appids": [991001]}])
    assert result["skipUnits"] == ["991001:CN"]
    assert result["unpersistedUnits"] == []
    async with get_session_factory()() as session:
        assert await session.get(GameCurrentPrice, (991001, "CN")) is None


@pytest.mark.asyncio
async def test_partial_skip_resolution_survives_cleanup_of_later_skip_job():
    await seed_job(stats={"targets": {"appids": [991001, 991002], "regions": ["CN"]},
                         "targetStartedAt": (now()-timedelta(days=101)).isoformat()}, age=101*24*60)
    await seed_job(status="done", stats={"skipUnits": ["991001:CN"],
                                        "targetStartedAt": (now()-timedelta(days=100)).isoformat()}, age=100*24*60)
    assert await crawl.prune_crawl_jobs() == 1
    assert units(await crawl._missing_tasks(cooldown_minutes=0)) == {(991002, "CN")}


@pytest.mark.asyncio
async def test_older_resolver_cannot_restore_targets_or_overwrite_new_statistics(monkeypatch):
    from contextlib import asynccontextmanager

    jid = await seed_job(stats={"targets": {"appids": [991001, 991002, 991003], "regions": ["CN"]},
                               "targetStartedAt": (now()-timedelta(minutes=10)).isoformat()})
    await DbWriter().mark_region_status(991001, "CN", "locked")
    queued = asyncio.Event()
    resume = asyncio.Event()
    old_resolver = None
    original_session = crawl.write_session

    @asynccontextmanager
    async def delayed_session(*args, **kwargs):
        if kwargs.get("label") == "crawl_targets_resolved" and asyncio.current_task() is old_resolver:
            queued.set()
            await resume.wait()
        async with original_session(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(crawl, "write_session", delayed_session)
    old_resolver = asyncio.create_task(crawl._unobserved_job_targets(0, settle=True))
    await asyncio.wait_for(queued.wait(), 2)
    await DbWriter().mark_region_status(991002, "CN", "locked")
    async with write_session() as session:
        row = await session.get(CrawlJob, jid)
        row.stats_json = {**row.stats_json, "newStatistic": 17}
        await session.commit()
    await crawl._unobserved_job_targets(0, settle=True)
    resume.set()
    await old_resolver
    async with get_session_factory()() as session:
        row = await session.get(CrawlJob, jid)
        assert target_units(row.stats_json) == {(991003, "CN")}
        assert row.stats_json["newStatistic"] == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["missing", "blocked"])
async def test_old_failure_state_cannot_resolve_a_new_failed_refresh(status):
    jid = await seed_job(stats={"targets": {"appids": [991001], "regions": ["CN"]},
                               "targetStartedAt": (now()-timedelta(minutes=10)).isoformat()})
    async with write_session() as session:
        session.add(Game(appid=991001, name="old state"))
        session.add(GameCurrentPrice(appid=991001, region_code="CN", currency="CNY", price_status=status,
                                    fail_count=5, updated_at=now()-timedelta(days=1)))
        await session.commit()
    assert await crawl._unobserved_job_targets(0, settle=True) == {(991001, "CN")}
    async with get_session_factory()() as session:
        assert target_units((await session.get(CrawlJob, jid)).stats_json) == {(991001, "CN")}


@pytest.mark.asyncio
async def test_missing_cleanup_failure_does_not_record_a_successful_skip(monkeypatch):
    from app.crawler import runner

    async with write_session() as session:
        session.add(Game(appid=991001, name="tool", type="TOOL"))
        session.add(GameCurrentPrice(appid=991001, region_code="CN", currency="CNY", price_status="missing",
                                    updated_at=now()-timedelta(days=1)))
        await session.commit()

    async def fetch(*args):
        return {}, [], 0

    async def reject(_):
        raise RuntimeError("cleanup commit rejected")

    monkeypatch.setattr(runner.bs, "load_preserved_rows", lambda _: {991001: {"name": "tool", "type": "TOOL"}})
    monkeypatch.setattr(bs.StoreBrowseAPI, "fetch_batch", fetch)
    monkeypatch.setattr(AsyncSession, "commit", reject)
    result = await runner._run_crawl_locked([], config=runner.CrawlRunConfig(regions=["CN"], workers=1),
        pre_tasks=[{"type": "app", "id": "cn:1", "region": "cn", "appids": [991001]}])
    assert result["skipUnits"] == []
    assert result["unpersistedUnits"] == ["991001:CN"]
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_unobserved_target_query_obeys_requested_limit_without_shrinking_plan():
    jid = await seed_job(stats={"targets": {"appids": list(range(991001, 992001)), "regions": ["CN", "US"]},
                               "targetStartedAt": (now()-timedelta(minutes=10)).isoformat()})
    assert len(await crawl._unobserved_job_targets(0, limit_rows=3)) == 3
    assert len(units(await crawl._missing_tasks(cooldown_minutes=0, limit_rows=5))) == 5
    async with get_session_factory()() as session:
        assert len(target_units((await session.get(CrawlJob, jid)).stats_json)) == 2000
