"""运行账本桥语义测试：job 入账 / 异常归因 / 段位流水 / 保留窗。

隔离口径与 test_agent_task_runner 一致（独立 tmp SQLite + 外键开启 + 真实
write_gate）。job 本体全部打桩——本文件证明的是「账本桥语义」。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.agent import service as agent_service
from app.domains.agent.models import AgentEvent, AgentRun, AgentSession
from app.domains.agent.runtime import scheduler_bridge
from app.domains.agent.runtime.cancellation import REGISTRY


@pytest.fixture
def db():
    """完整库（隔离数据目录，外键与生产同开）；url 供重启模拟另开引擎。"""
    from app.core.database import get_session_factory, get_settings

    yield {"factory": get_session_factory(), "url": get_settings().db_url}
    for rid in REGISTRY.active_ids():
        entry = REGISTRY.pop(rid)
        if entry and entry.task and not entry.task.done():
            entry.task.cancel()


class TestRunAccounted:
    @pytest.mark.asyncio
    async def test_success_run_done_with_meta(self, db):
        async def _job():
            return {"did": "work"}

        result = await scheduler_bridge.run_accounted("some_job", _job, ref={"k": 1})
        assert result == {"did": "work"}

        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "some_job")
        assert run["status"] == "done"
        assert run["runner"] == "job"
        assert run["trigger"] == "scheduled"
        meta = run["meta"]
        assert meta["entry"] == "scheduled" and meta["ref"] == {"k": 1}
        # 挂靠会话幂等建且唯一
        async with db["factory"]() as session:
            sessions = (await session.execute(AgentSession.__table__.select())).fetchall()
        assert [s.id for s in sessions] == [agent_service.SYSTEM_SESSION_ID]

    @pytest.mark.asyncio
    async def test_exception_fails_run_and_reraises(self, db):
        async def _boom():
            raise ValueError("业务配置缺失")

        with pytest.raises(ValueError):
            await scheduler_bridge.run_accounted("boom_job", _boom)

        runs = await agent_service.list_runs(status="failed")
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "boom_job")
        assert run["error_code"] == "job_error"
        events = await agent_service.list_events(run["run_id"])
        failed = [e for e in events if e["event_type"] == "task.failed"]
        assert failed and "业务配置缺失" in failed[0]["payload"]["reason"]

    @pytest.mark.asyncio
    async def test_ledger_failure_does_not_break_job(self, db, monkeypatch):
        """建账失败 = 业务照常执行、返回值原样（fail-soft 边界）。"""
        async def _accounting_boom(*a, **kw):
            raise RuntimeError("账本库不可用")

        monkeypatch.setattr(agent_service, "ensure_system_session", _accounting_boom)

        async def _job():
            return "fine"

        assert await scheduler_bridge.run_accounted("j", _job) == "fine"

    def test_bridge_has_no_domain_sampling_interface(self):
        import inspect

        assert "task_kind" not in inspect.signature(scheduler_bridge.run_accounted).parameters
        assert not hasattr(scheduler_bridge, "_sample_outcome")

    @pytest.mark.asyncio
    async def test_result_is_not_reinterpreted_as_domain_verdict(self, db):
        async def job():
            return {"status": "failed", "outcome": "failed"}

        result = await scheduler_bridge.run_accounted("generic_job", job)
        assert result == {"status": "failed", "outcome": "failed"}
        run = (await agent_service.list_runs())[0]
        assert run["status"] == "done" and run["error_code"] is None
        assert "task" not in run["meta"]
        assert run["meta"]["ref"] == {}


class TestNoteStep:
    @pytest.mark.asyncio
    async def test_note_step_inside_run_context(self, db):
        async def _job():
            noted = await scheduler_bridge.note_step("tail", {"skipped": "无欠账"})
            assert noted is True
            return None

        await scheduler_bridge.run_accounted("note_job", _job)
        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "note_job")
        events = await agent_service.list_events(run["run_id"])
        steps = [e for e in events if e["event_type"] == "step.completed"]
        assert steps and steps[0]["payload"] == {"step": "tail", "summary": {"skipped": "无欠账"}}

    @pytest.mark.asyncio
    async def test_note_step_without_context_is_noop(self, db):
        assert await scheduler_bridge.note_step("x", {}) is False


class TestSkipNotes:
    """门禁跳过静默分支接 skip 流水：原因用户语言、run 保持 done。"""

    async def _last_skip(self, job_id: str) -> tuple[dict, list[dict]]:
        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == job_id)
        events = await agent_service.list_events(run["run_id"])
        steps = [e for e in events if e["event_type"] == "step.completed"]
        return run, steps

    @pytest.mark.asyncio
    async def test_wallet_sync_no_cookie_notes_skip(self, db, monkeypatch):
        from app.core import scheduler as sched
        from app.domains.account import service as account_service

        async def no_cookie():
            return {"ok": False, "status": "no_cookie"}

        monkeypatch.setattr(account_service, "sync_wallets_rotational", no_cookie)
        await scheduler_bridge.run_accounted("wallet_sync", sched._job_wallet_sync)
        run, steps = await self._last_skip("wallet_sync")
        assert run["status"] == "done"
        assert steps, "no_cookie 分支必须落 skip 流水"
        assert steps[-1]["payload"] == {
            "step": "wallet_sync", "summary": {"skipped": "未绑定 Steam 账号"},
        }

    @pytest.mark.asyncio
    async def test_bills_sync_no_cookie_and_busy_notes_skip(self, db, monkeypatch):
        from app.core import scheduler as sched
        from app.domains.bills import service as bills_service

        for status, wording in (
            ("no_cookie", "未绑定 Steam Cookie"),
            ("busy", "账单同步进行中"),
        ):
            async def fake(force=False, _status=status):
                return {"ok": False, "status": _status}

            monkeypatch.setattr(bills_service, "sync_bills", fake)
            await scheduler_bridge.run_accounted("bills_sync", sched._job_bills_sync)
            run, steps = await self._last_skip("bills_sync")
            assert run["status"] == "done"
            assert steps[-1]["payload"]["summary"] == {"skipped": wording}

    @pytest.mark.asyncio
    async def test_fx_refresh_switch_off_notes_skip(self, db, monkeypatch):
        from app.core import scheduler as sched
        from app.domains.settings import service as settings_service

        async def off(key, default=None):
            return False if key == "crawl.auto_refresh_rates" else default

        monkeypatch.setattr(settings_service, "get_value", off)
        await scheduler_bridge.run_accounted("fx_refresh", sched._job_fx_refresh)
        run, steps = await self._last_skip("fx_refresh")
        assert run["status"] == "done"
        assert steps[-1]["payload"]["summary"] == {"skipped": "汇率自动更新已关闭"}

    @pytest.mark.asyncio
    async def test_proxy_health_switch_off_notes_skip(self, db, monkeypatch):
        from app.core import scheduler as sched

        async def disabled(key):
            return False

        monkeypatch.setattr(sched, "content_fetch_enabled", disabled)
        await scheduler_bridge.run_accounted("proxy_health", sched._job_proxy_health)
        run, steps = await self._last_skip("proxy_health")
        assert run["status"] == "done"
        assert steps[-1]["payload"]["summary"] == {"skipped": "自动节点体检已关闭"}

    @pytest.mark.asyncio
    async def test_board_gate_skip_notes_with_board_key(self, db, monkeypatch):
        from app.core import scheduler as sched

        async def disabled(key):
            return False

        monkeypatch.setattr(sched, "content_fetch_enabled", disabled)
        await scheduler_bridge.run_accounted(
            "board_topsellers", sched._make_board_job("topsellers")
        )
        run, steps = await self._last_skip("board_topsellers")
        assert run["status"] == "done"
        assert steps[-1]["payload"] == {
            "step": "board_topsellers", "summary": {"skipped": "Steam 排行榜自动更新已关闭"},
        }

    @pytest.mark.asyncio
    async def test_fx_history_repair_no_gaps_notes(self, db, monkeypatch):
        from app.core import scheduler as sched
        from app.domains.rates import history as rates_history

        async def enabled(key):
            return True

        async def no_gaps(**kwargs):
            return {"status": "no_gaps"}

        monkeypatch.setattr(sched, "content_fetch_enabled", enabled)
        monkeypatch.setattr(rates_history, "repair_history_gaps", no_gaps)
        await scheduler_bridge.run_accounted("fx_history_repair", sched._job_fx_history_repair)
        run, steps = await self._last_skip("fx_history_repair")
        assert run["status"] == "done"
        assert steps[-1]["payload"]["summary"] == {"skipped": "历史汇率没有缺漏，暂不补齐"}

    @pytest.mark.asyncio
    async def test_comingsoon_and_free_promo_empty_notes(self, db, monkeypatch):
        from app.core import scheduler as sched

        async def on():
            return True

        monkeypatch.setattr(sched, "price_auto_enabled", on)

        async def no_pairs(self, **kw):
            return []

        from app.crawler.db_writer import DbWriter
        monkeypatch.setattr(DbWriter, "coming_soon_retry_pairs", no_pairs)
        await scheduler_bridge.run_accounted("comingsoon_retry", sched._job_coming_soon_retry)
        run, steps = await self._last_skip("comingsoon_retry")
        assert run["status"] == "done"
        assert steps[-1]["payload"]["summary"] == {"skipped": "暂无需要重新检查的游戏"}

    @pytest.mark.asyncio
    async def test_gate_open_no_skip_note(self, db, monkeypatch):
        """正向对照：开关全开时 run 无任何 skipped step 事件。"""
        from app.core import scheduler as sched
        from app.domains.rates import service as rates_service
        from app.domains.settings import service as settings_service

        async def on(key, default=None):
            return True

        async def ok_refresh():
            return {"ok": True}

        monkeypatch.setattr(settings_service, "get_value", on)
        monkeypatch.setattr(rates_service, "refresh_rates", ok_refresh)
        await scheduler_bridge.run_accounted("fx_refresh", sched._job_fx_refresh)
        run, steps = await self._last_skip("fx_refresh")
        assert run["status"] == "done"
        assert not steps, "开关全开不得落 skip 流水"


class TestNoteJobSkipped:
    """调度跳拍事件按 job_id 投给在飞 run（监听器拿不到 ContextVar，靠映射）。"""

    @pytest.mark.asyncio
    async def test_note_job_skipped_targets_inflight_run(self, db):
        async def _job():
            noted = await scheduler_bridge.note_job_skipped(
                "evt_job", "evt_job", {"skipped": "上一轮还在进行，本拍让路"}
            )
            assert noted is True
            return None

        await scheduler_bridge.run_accounted("evt_job", _job)
        runs = await agent_service.list_runs()
        run = next(r for r in runs if (r.get("meta") or {}).get("job") == "evt_job")
        events = await agent_service.list_events(run["run_id"])
        steps = [e for e in events if e["event_type"] == "step.completed"]
        assert steps and steps[0]["payload"] == {
            "step": "evt_job", "summary": {"skipped": "上一轮还在进行，本拍让路"},
        }

    @pytest.mark.asyncio
    async def test_note_job_skipped_without_inflight_run_is_noop(self, db):
        assert await scheduler_bridge.note_job_skipped("ghost", "ghost", {}) is False

    @pytest.mark.asyncio
    async def test_run_accounted_clears_inflight_mapping(self, db):
        async def _job():
            return None

        await scheduler_bridge.run_accounted("map_job", _job)
        assert "map_job" not in scheduler_bridge._RUNNING_JOB_RUNS

    @pytest.mark.asyncio
    async def test_run_accounted_keeps_foreign_mapping_value(self, db):
        """收口只清自己登记的值，不误删理论重入时他者写入的同键值。"""
        async def _job():
            scheduler_bridge._RUNNING_JOB_RUNS["map_job2"] = "foreign_run"
            return None

        try:
            await scheduler_bridge.run_accounted("map_job2", _job)
            assert scheduler_bridge._RUNNING_JOB_RUNS["map_job2"] == "foreign_run"
        finally:
            scheduler_bridge._RUNNING_JOB_RUNS.pop("map_job2", None)


class TestPruneCrawlJobs:
    @pytest.mark.asyncio
    async def test_prune_removes_only_old_terminal_rows(self, db):
        from app.domains.crawl import service as crawl_service
        from app.domains.crawl.cycle import PriceCycle
        from app.domains.crawl.models import CrawlJob

        factory = db["factory"]
        old_ts = datetime.now() - timedelta(days=91)
        async with factory() as session:
            session.add_all([
                CrawlJob(id=1, kind="manual", status="done", finished_at=old_ts),
                CrawlJob(id=2, kind="manual", status="failed", finished_at=old_ts),
                CrawlJob(id=3, kind="manual", status="pending"),
                CrawlJob(id=4, kind="manual", status="running"),
                CrawlJob(id=5, kind="manual", status="done", finished_at=datetime.now()),
            ])
            # price_cycles 整行归周期保留窗：过窗行不得被本函数碰
            session.add(PriceCycle(
                kind="scheduled", status="done", started_at=old_ts,
                finished_at=old_ts,
            ))
            await session.commit()

        pruned = await crawl_service.prune_crawl_jobs(days=90)
        assert pruned == 2

        async with factory() as session:
            jobs = {j.id: j.status for j in (
                await session.execute(CrawlJob.__table__.select())).fetchall()}
            assert jobs == {3: "pending", 4: "running", 5: "done"}
            cycles = (await session.execute(PriceCycle.__table__.select())).fetchall()
            assert len(cycles) == 1, "price_cycles 整行不碰"

    @pytest.mark.asyncio
    async def test_prune_chunk_cap_truncates(self, db):
        from app.domains.crawl import service as crawl_service
        from app.domains.crawl.models import CrawlJob

        factory = db["factory"]
        old_ts = datetime.now() - timedelta(days=91)
        async with factory() as session:
            session.add_all([
                CrawlJob(id=i, kind="manual", status="done", finished_at=old_ts)
                for i in range(1, 6)
            ])
            await session.commit()

        pruned = await crawl_service.prune_crawl_jobs(days=90, chunk=2, max_chunks=2)
        assert pruned == 4, "单轮块上限截断，剩余 1 行留下一轮"
        pruned = await crawl_service.prune_crawl_jobs(days=90, chunk=2, max_chunks=2)
        assert pruned == 1, "续删幂等清零"


class TestPruneJobRuns:
    @pytest.mark.asyncio
    async def test_prune_removes_only_old_job_runs(self, db):
        factory = db["factory"]
        old_ts = datetime.now() - timedelta(days=40)
        async with factory() as session:
            session.add(AgentSession(id=agent_service.SYSTEM_SESSION_ID,
                                     created_at=old_ts, updated_at=old_ts))
            await session.flush()
            session.add(AgentRun(id="old_job", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="job", status="done", created_at=old_ts))
            session.add(AgentRun(id="old_task", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="task", status="done", created_at=old_ts))
            session.add(AgentRun(id="fresh_job", session_id=agent_service.SYSTEM_SESSION_ID,
                                 runner="job", status="done", created_at=datetime.now()))
            await session.flush()
            session.add(AgentEvent(run_id="old_job", seq=1, event_type="run.state",
                                   payload={"from": "queued", "to": "running"}))
            await session.commit()

        pruned = await scheduler_bridge.prune_job_runs(days=30)
        assert pruned == 1

        async with factory() as session:
            ids = {r.id for r in (await session.execute(AgentRun.__table__.select())).fetchall()}
        assert ids == {"old_task", "fresh_job"}
        # 事件随行级联删除（外键开启）
        async with factory() as session:
            rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
        assert all(r.run_id != "old_job" for r in rows)
