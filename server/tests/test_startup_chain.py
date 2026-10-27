"""HTTP开门、兼容安全与正常调度入口的行为契约。"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import scheduler as sched
import app.main as main


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    from app.core import backup, layout, rekey, update_data
    from app.domains.account import service as accounts
    from app.domains.agent import service as agents
    from app.domains.crawl import service as crawl
    from app.domains.proxies import node_secret_files, service as proxies
    from app.domains.proxypool import jobruns

    calls, gates, failures = [], {}, set()
    started = asyncio.Event()
    def step(name):
        async def run(*args, **kwargs):
            calls.append(name)
            if name in gates:
                await gates[name].wait()
            if name in failures:
                raise RuntimeError("controlled failure")
        return run
    for module, attr, name in (
        (accounts, "migrate_legacy_kv", "account_migrate"),
        (accounts, "seal_credentials_at_rest", "account_seal"),
        (proxies, "migrate_legacy_subscription", "subscription_migrate"),
        (proxies, "maybe_revert_to_direct", "exit_policy"),
        (proxies, "seal_subscription_urls", "subscription_seal"),
        (proxies, "seal_proxy_credentials", "proxy_seal"),
        (crawl, "cleanup_orphan_jobs", "crawl_recovery"),
        (jobruns, "record_interrupted", "pool_recovery"),
        (agents, "reconcile_orphan_runs", "agent_recovery"),
        (rekey, "resume_pending_migration", "rekey"),
    ):
        monkeypatch.setattr(module, attr, step(name))
    for attr in ("tighten_all", "sweep_transient"):
        monkeypatch.setattr(node_secret_files, attr, lambda *args, name=attr: calls.append(name))
    @contextmanager
    def authorization(_):
        yield None
    monkeypatch.setattr(update_data, "startup_authorization", authorization)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(
        data_dir=tmp_path, db_filename="holdexar.db", version="test", port=12345))
    monkeypatch.setattr(main, "setup_logging", lambda *args: None)
    monkeypatch.setattr(main, "init_db", step("database"))
    monkeypatch.setattr(main, "start_scheduler", lambda: (calls.append("scheduler"), started.set()))
    monkeypatch.setattr(main, "stop_scheduler", lambda: calls.append("stopped"))
    monkeypatch.setattr(sched, "apply_content_schedules_from_settings", step("schedules"))
    return SimpleNamespace(calls=calls, gates=gates, failures=failures, started=started)


@pytest.mark.asyncio
async def test_http_opens_without_waiting_for_required_compatibility(prepared):
    prepared.gates["account_migrate"] = asyncio.Event()
    application = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(application):
        assert not prepared.started.is_set()
        await asyncio.sleep(0.02)
        assert "account_migrate" in prepared.calls
        prepared.gates["account_migrate"].set()
        await asyncio.wait_for(prepared.started.wait(), 2)
        assert prepared.calls.index("account_migrate") < prepared.calls.index("account_seal")
        assert prepared.calls.index("subscription_migrate") < prepared.calls.index("subscription_seal")
        assert "sweep_transient" in prepared.calls
    assert prepared.calls[-1] == "stopped"


@pytest.mark.asyncio
async def test_recoverable_preparation_failure_keeps_other_safety_work(prepared):
    prepared.failures.add("subscription_seal")
    async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
        await asyncio.wait_for(prepared.started.wait(), 2)
        assert {"account_seal", "proxy_seal", "crawl_recovery", "pool_recovery", "agent_recovery"} <= set(prepared.calls)


def test_scheduler_uses_normal_jobs_for_first_maintenance(monkeypatch):
    jobs = {}
    scheduler = SimpleNamespace(running=False,
        add_job=lambda fn, trigger, **kw: jobs.setdefault(kw["id"], kw),
        start=lambda: None, remove_listener=lambda *args: None, add_listener=lambda *args: None)
    monkeypatch.setattr(sched, "scheduler", scheduler)
    def untracked(coro, **kwargs):
        coro.close()
        raise AssertionError("maintenance must belong to registered jobs")
    monkeypatch.setattr(asyncio, "create_task", untracked)
    now = datetime.now()
    sched.start_scheduler()
    expected_staggers = {
        "proxypool_cycle": sched._STARTUP_STAGGER_PROXYPOOL_CYCLE,
        "price_repair": sched._STARTUP_STAGGER_PRICE_REPAIR,
        "subscription_refresh": sched._STARTUP_STAGGER_SUBSCRIPTION_REFRESH,
        "fx_refresh": sched._STARTUP_STAGGER_FX_REFRESH,
        "mini_seed": sched._STARTUP_STAGGER_MINI_SEED,
        "wishlist_sync": sched._STARTUP_STAGGER_WISHLIST_SYNC,
    }
    for key, stagger in expected_staggers.items():
        assert abs((jobs[key]["next_run_time"] - (now + stagger)).total_seconds()) <= 1.0
    assert jobs["auto_backup"]["next_run_time"] <= datetime.now() + timedelta(minutes=5, seconds=1)


@pytest.mark.asyncio
async def test_repair_maintains_views_and_pending_mail_when_collection_disabled(monkeypatch):
    from app.domains.games import derived
    from app.domains.notifications import service as notifications
    calls = []
    async def maintain(): calls.append("derived")
    async def dispatch(cycle_id=None): calls.append("notifications")
    async def disabled(): return False
    monkeypatch.setattr(derived, "maintain_derived", maintain)
    monkeypatch.setattr(notifications, "dispatch", dispatch)
    monkeypatch.setattr(sched, "price_auto_enabled", disabled)
    monkeypatch.setattr(sched, "_crawler_idle", lambda: True)
    await sched._job_price_repair()
    assert calls == ["derived", "notifications"]


@pytest.mark.asyncio
async def test_price_execution_does_not_wait_for_delivery(monkeypatch):
    from app.domains.crawl import service as crawl
    from app.domains.games import derived
    from app.domains.notifications import service as notifications
    calls = []
    async def maintain(): calls.append("derived")
    async def dispatch(cycle_id=None): calls.append("notifications")
    async def collect(**kwargs): calls.append("crawl")
    async def enabled(): return True
    async def noop(*args, **kwargs): pass
    monkeypatch.setattr(derived, "maintain_derived", maintain)
    monkeypatch.setattr(notifications, "dispatch", dispatch)
    monkeypatch.setattr(crawl, "start_job", collect)
    monkeypatch.setattr(sched, "price_auto_enabled", enabled)
    monkeypatch.setattr(sched, "_crawler_idle", lambda: True)
    monkeypatch.setattr(sched, "_reanchor_price_refresh", noop)
    monkeypatch.setattr(sched, "_kick_stale_price_refresh", noop)
    await sched._job_price_repair()
    assert calls == ["derived", "crawl", "notifications"]


@pytest.mark.asyncio
async def test_mini_seed_uses_normal_import_entry(monkeypatch):
    from app.core import mini_seed
    calls = []
    async def imported():
        calls.append("imported")
    monkeypatch.setattr(mini_seed, "import_mini_seed", imported)
    await sched._job_mini_seed()
    assert calls == ["imported"]
