"""生产作业台账（`proxy_job_runs`）：行为测试。

覆盖的是**事实层**的三件事：
1. 一次作业两笔写入（开始 `running` / 结束终态），字段按核实的真实语义落库；
2. `error_summary` 是固定枚举计数、`NULL ≠ 0`、终态由后端一处判定；
3. 真链路：`run_crawl()`（唯一生产入口）确实在库里留下一行。

不做（本文件不得出现）：健康度、健康等级、判死阈值、切换规则、每个 HTTP 请求一行。

隔离：`tmp_data_dir` 模式（同域其它测试）——临时数据目录 + 建表 + lru_cache 收尾。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.crawler.occupancy import end_crawl  # noqa: E402
from app.crawler.runner import CrawlRunConfig, run_crawl  # noqa: E402
from app.domains.proxypool import jobruns  # noqa: E402
from app.domains.proxypool import runtime as runtime_module  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyJobRun,
    ProxyNode,
    ProxyNodeSource,
    SubscriptionSnapshot,
)
from app.domains.proxypool.pool import pool_path  # noqa: E402
from app.domains.proxypool.state import NODE_ACTIVE  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则「缓存库 ≠ 当前配置库」判据判定相等，临时引擎会留给后续文件
    import os

    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    end_crawl()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _no_occupancy():
    """任何用例都不该被上一个用例的占用语义污染。"""
    end_crawl()
    yield
    end_crawl()


async def _add_node(
    session, runtime_name: str, *, sub_id: int = 1, exit_ip: str | None = None
) -> ProxyNode:
    node = ProxyNode(
        node_id=runtime_name,
        fingerprint=runtime_name,
        runtime_name=runtime_name,
        proxy_type="ss",
        server="10.0.0.1",
        normalized_config={
            "name": runtime_name, "type": "ss", "server": "10.0.0.1", "port": 443
        },
        state=NODE_ACTIVE,
        exit_ip=exit_ip,
    )
    session.add(node)
    await session.flush()
    session.add(
        ProxyNodeSource(
            node_id=node.node_id, subscription_id=sub_id, original_name=runtime_name
        )
    )
    await session.flush()
    return node


# ══ 1. 两笔写入 ═══════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_start_run_records_pool_facts_and_selected_node(tmp_data_dir, monkeypatch):
    """开始那笔：池事实（池 SHA / 节点数 / 出口 IP 数 / 来源订阅 / 快照）+ 选中节点。

    选中节点读控制器——这里打桩成真实返回，验证「一轮只有一个节点」这个事实
    确实被记成一等列（而不是塞进 by_node 字典）。
    """
    await init_db()
    async with get_session_factory()() as session:
        await _add_node(session, "1|A", exit_ip="203.0.113.7")
        await _add_node(session, "1|B")
        await _add_node(session, "2|C", sub_id=2, exit_ip="203.0.113.7")
        session.add(
            SubscriptionSnapshot(
                subscription_id=1, sha256="a" * 64, format="yaml", node_count=2,
                status="OK",
            )
        )
        session.add(
            SubscriptionSnapshot(
                subscription_id=1, sha256="b" * 64, format="yaml", node_count=2,
                status="FAILED",
            )
        )
        await session.commit()

    path = pool_path(tmp_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("proxies:\n- name: 1|A\n", encoding="utf-8")

    async def _fake_selection(base, secret):
        return "1|A"

    monkeypatch.setattr(
        runtime_module, "controller_endpoint_of",
        lambda data_dir: ("http://127.0.0.1:1", "s"),
    )
    monkeypatch.setattr(runtime_module, "current_global_selection", _fake_selection)

    async with get_session_factory()() as session:
        run_id = await jobruns.start_run(
            session,
            proxy_url="http://127.0.0.1:45678",
            regions=["us"],
            workers=30,
            data_dir=tmp_data_dir,
            now=NOW,
        )
        await session.commit()
        row = await session.get(ProxyJobRun, run_id)
        assert row is not None
        assert row.status == jobruns.STATUS_RUNNING
        assert row.proxy_url == "http://127.0.0.1:45678"
        assert row.pool_sha256 is not None and len(row.pool_sha256) == 64
        assert row.pool_node_count == 3
        # 两个节点共享同一出口 IP → 去重后 1
        assert row.pool_exit_ip_count == 1
        assert row.subscription_ids_json == [1, 2]
        # 只取每个订阅 latest status=OK 的快照
        assert row.snapshot_ids_json == [1]
        assert row.selected_node == "1|A"
        assert row.node_exit_ip == "203.0.113.7"
        # Active/Candidate 尚未实现：这一列必须留空，不伪造生产订阅
        assert row.active_subscription_id is None
        assert row.regions_json == ["us"]
        assert row.workers == 30
        assert row.finished_at is None


@pytest.mark.asyncio
async def test_pool_exit_ip_count_null_when_never_probed(tmp_data_dir):
    """NULL ≠ 0：池里没有任何已知出口 IP 时记 NULL（还没测过），不是 0 个出口。"""
    await init_db()
    async with get_session_factory()() as session:
        await _add_node(session, "1|A")
        run_id = await jobruns.start_run(
            session, proxy_url=None, regions=["us"], workers=1,
            data_dir=tmp_data_dir, now=NOW,
        )
        await session.commit()
        row = await session.get(ProxyJobRun, run_id)
        assert row.pool_node_count == 1
        assert row.pool_exit_ip_count is None
        assert row.pool_sha256 is None  # 没有池文件 → 无该事实
        assert row.selected_node is None  # 控制器读不到 → NULL，不阻塞


@pytest.mark.asyncio
async def test_finish_run_writes_terminal_state(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        run_id = await jobruns.start_run(
            session, proxy_url=None, regions=["us"], workers=4,
            data_dir=tmp_data_dir, now=NOW,
        )
        summary = jobruns.new_error_summary()
        jobruns.note_error(summary, TimeoutError("timed out"))
        jobruns.note_ledger(summary, 3)
        ok = await jobruns.finish_run(
            session, run_id, status=jobruns.STATUS_PARTIAL,
            now=NOW + timedelta(seconds=42), task_count=40, success_count=36,
            error_count=4, error_summary=summary, duration_ms=42000,
        )
        await session.commit()
        assert ok is True
        row = await session.get(ProxyJobRun, run_id)
        assert row.status == jobruns.STATUS_PARTIAL
        assert row.finished_at == NOW + timedelta(seconds=42)
        assert row.duration_ms == 42000
        assert row.task_count == 40 and row.success_count == 36 and row.error_count == 4
        assert row.error_summary == {"by_error": {"timeout": 1, "ledger": 3}}

    async with get_session_factory()() as session:
        assert await jobruns.finish_run(
            session, 999999, status=jobruns.STATUS_FAILED, now=NOW
        ) is False


# ══ 2. 终态与错误分类 ═════════════════════════════════════════════
@pytest.mark.parametrize(
    ("success", "error", "stopped", "expected"),
    [
        (0, 0, False, jobruns.STATUS_SUCCESS),
        (240, 0, False, jobruns.STATUS_SUCCESS),
        (231, 9, False, jobruns.STATUS_PARTIAL),
        (0, 240, False, jobruns.STATUS_FAILED),
        (0, 240, True, jobruns.STATUS_INTERRUPTED),
        (0, 0, True, jobruns.STATUS_INTERRUPTED),
    ],
)
def test_classify_outcome_matrix(success, error, stopped, expected):
    assert jobruns.classify_outcome(success, error, stopped=stopped) == expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timed out"), "timeout"),
        (ConnectionResetError("connection reset by peer"), "reset"),
        (KeyError("missing"), "internal"),
        (ValueError("任务缺少区服配置"), "internal"),
        (RuntimeError("已有爬取任务在执行"), "internal"),
        (OSError("getaddrinfo failed"), "dns"),
        (ConnectionRefusedError("connection refused"), "connect"),
    ],
)
def test_classify_error_enum(exc, expected):
    assert jobruns.classify_error(exc) in jobruns.ERROR_KINDS
    assert jobruns.classify_error(exc) == expected


def test_classify_error_uses_http_status_and_names():
    class FakeHttpError(Exception):
        def __init__(self, status):
            super().__init__(f"HTTP {status}")
            self.status = status

    class FakeTlsError(Exception):
        pass

    FakeTlsError.__name__ = "SSLCertVerificationError"
    assert jobruns.classify_error(FakeHttpError(503)) == "http_5xx"
    assert jobruns.classify_error(FakeHttpError(404)) == "http_4xx"
    assert jobruns.classify_error(FakeHttpError(0)) == "other"  # 非 4xx/5xx 不硬塞
    assert jobruns.classify_error(FakeTlsError("certificate verify failed")) == "tls"

    class SteamRateLimitError(Exception):
        pass

    assert jobruns.classify_error(SteamRateLimitError("429 too many")) == "rate_limit"
    assert jobruns.classify_error(Exception("weird")) == "other"


def test_error_summary_is_fixed_enum_no_free_text():
    """自由文本绝不进 error_summary：异常原文只进日志。"""
    summary = jobruns.new_error_summary()
    jobruns.note_error(
        summary,
        RuntimeError("GET http://user:secret@example.com/sub?token=abc 失败"),
    )
    out = jobruns.finalize_summary(summary)
    assert out == {"by_error": {"internal": 1}}
    assert "secret" not in str(out)


def test_error_summary_finalize_drops_zero_unknown_and_truncates(monkeypatch):
    assert jobruns.finalize_summary({"by_error": {"timeout": 0, "whatever": 5}}) == {
        "by_error": {}
    }
    assert jobruns.finalize_summary(None) == {"by_error": {}}

    # 固定枚举下正常不可能超 8KB：这里把预算压小，验证截断分支真的存在且可用
    # （将来加子映射时它就是那道防线）
    heavy = {"by_error": {kind: 1 for kind in jobruns.ERROR_KINDS}}
    assert jobruns.finalize_summary(heavy)["by_error"] == heavy["by_error"]
    monkeypatch.setattr(jobruns, "_SUMMARY_MAX_BYTES", 64)
    out = jobruns.finalize_summary(heavy)
    assert out["truncated"] is True
    assert len(out["by_error"]) == 6


@pytest.mark.asyncio
async def test_by_error_null_not_zero(tmp_data_dir):
    """没有汇总的行返回 None（前端显示 —），不是 `{}`、更不是 0。"""
    await init_db()
    async with get_session_factory()() as session:
        row = ProxyJobRun(status=jobruns.STATUS_INTERRUPTED, started_at=NOW)
        session.add(row)
        await session.commit()
        payload = await jobruns.get_run(session, row.id)
        assert payload is not None
        assert payload["byError"] is None
        assert payload["taskCount"] is None

        # 有汇总但全 0 的行：仍是 {}（明确记过"这一次失败分类为空"）
        row2 = ProxyJobRun(
            status=jobruns.STATUS_SUCCESS, started_at=NOW,
            error_summary={"by_error": {}},
        )
        session.add(row2)
        await session.commit()
        payload2 = await jobruns.get_run(session, row2.id)
        assert payload2["byError"] == {}


# ══ 3. 读取（今日概览 / 分页 / 详情）═════════════════════════════
@pytest.mark.asyncio
async def test_list_runs_paging_and_day_summary(tmp_data_dir):
    await init_db()
    noon = datetime(2026, 9, 19, 12, 0, 0)
    async with get_session_factory()() as session:
        session.add_all([
            # 前一天：不该进"今日"
            ProxyJobRun(status=jobruns.STATUS_FAILED, started_at=noon - timedelta(days=1),
                        duration_ms=9000, task_count=5, success_count=0, error_count=5,
                        pool_exit_ip_count=40),
            # 今日三次
            ProxyJobRun(status=jobruns.STATUS_SUCCESS, started_at=noon - timedelta(hours=3),
                        duration_ms=30000, task_count=10, success_count=10, error_count=0,
                        pool_exit_ip_count=44),
            ProxyJobRun(status=jobruns.STATUS_PARTIAL, started_at=noon - timedelta(hours=1),
                        duration_ms=50000, task_count=10, success_count=9, error_count=1,
                        pool_exit_ip_count=45),
            ProxyJobRun(status=jobruns.STATUS_INTERRUPTED, started_at=noon,
                        duration_ms=None, task_count=None, success_count=None,
                        error_count=None, pool_exit_ip_count=47),
        ])
        await session.commit()

        payload = await jobruns.list_runs(session, limit=2, offset=0, now=noon)
        summary = payload["summary"]
        assert payload["total"] == 4
        assert len(payload["items"]) == 2
        assert summary["day"] == "2026-09-19"
        assert summary["runs"] == 3
        assert summary["success"] == 1
        assert summary["partial"] == 1
        assert summary["failed"] == 0
        assert summary["interrupted"] == 1
        # 平均耗时只对有 duration 的行取（30000 + 50000）/ 2
        assert summary["avgDurationMs"] == 40000
        # 取当日**最后一次**作业的池出口 IP 数（47），不是最大值也不是 45
        assert summary["poolExitIpCount"] == 47

        page2 = await jobruns.list_runs(session, limit=2, offset=2, now=noon)
        assert [item["id"] for item in page2["items"]] == [2, 1]


@pytest.mark.asyncio
async def test_day_summary_nulls_when_no_samples(tmp_data_dir):
    await init_db()
    noon = datetime(2026, 9, 19, 12, 0, 0)
    async with get_session_factory()() as session:
        session.add(
            ProxyJobRun(status=jobruns.STATUS_FAILED, started_at=noon,
                        duration_ms=None, pool_exit_ip_count=None)
        )
        await session.commit()
        summary = (await jobruns.list_runs(session, now=noon))["summary"]
        assert summary["avgDurationMs"] is None
        assert summary["poolExitIpCount"] is None


@pytest.mark.asyncio
async def test_detail_payload_shape(tmp_data_dir):
    await init_db()
    async with get_session_factory()() as session:
        await _add_node(session, "1|A", exit_ip="203.0.113.7")
        run_id = await jobruns.start_run(
            session, proxy_url="http://127.0.0.1:45678", regions=["us", "de"],
            workers=30, data_dir=tmp_data_dir, now=NOW,
        )
        summary = jobruns.new_error_summary()
        jobruns.note_ledger(summary, 2)
        await jobruns.finish_run(
            session, run_id, status=jobruns.STATUS_FAILED, now=NOW,
            task_count=2, success_count=0, error_count=2, error_summary=summary,
            duration_ms=15000,
        )
        await session.commit()
        payload = await jobruns.get_run(session, run_id)
        assert payload["id"] == run_id
        assert payload["status"] == "failed"
        assert payload["byError"] == {"ledger": 2}
        assert payload["node"] is None and payload["nodeExitIp"] is None
        assert payload["regions"] == ["us", "de"]
        assert payload["poolNodeCount"] == 1
        assert payload["activeSubscriptionId"] is None
        assert payload["startedAt"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_router_serves_real_rows_and_validates_params(tmp_data_dir):
    """HTTP 层：列表（含今日概览）/ 详情 / 404 / 参数越界 422。

    真正挂一遍路由（最小 FastAPI 应用，不起 app.main 避免 lifespan 副作用）——
    直调 service 绕过 Query 校验层，那类回归只有真 HTTP 调用拦得住。
    """
    import httpx
    from fastapi import FastAPI

    from app.domains.proxypool.router import router as proxypool_router

    await init_db()
    # 走 HTTP 的一轮：概览按**真实 now** 取"今日"，所以数据必须落在真实今天，
    # 不能用模块级固定日期（否则跨过午夜就假红——时间依赖的测试脆弱点）。
    today = datetime.now()
    async with get_session_factory()() as session:
        row = ProxyJobRun(
            status=jobruns.STATUS_SUCCESS, started_at=today, finished_at=today,
            duration_ms=1234, task_count=4, success_count=4, error_count=0,
            proxy_url="http://127.0.0.1:45678", pool_node_count=3,
            pool_exit_ip_count=2, error_summary={"by_error": {}},
        )
        session.add(row)
        await session.commit()
        run_id = row.id

    app = FastAPI()
    app.include_router(proxypool_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/proxypool/job-runs", params={"limit": 20})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert body["summary"]["runs"] == 1
        assert body["summary"]["success"] == 1
        assert body["summary"]["day"] == today.date().isoformat()
        assert body["items"][0]["taskCount"] == 4
        assert body["items"][0]["poolExitIpCount"] == 2

        detail = await client.get(f"/api/v1/proxypool/job-runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == run_id

        missing = await client.get("/api/v1/proxypool/job-runs/999999")
        assert missing.status_code == 404

        assert (
            await client.get("/api/v1/proxypool/job-runs", params={"limit": 500})
        ).status_code == 422


# ══ 4. 中断收尾 / fail-soft ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_mark_interrupted_runs(tmp_data_dir):
    """上次进程遗留的 running 行 → interrupted（duration 自 started_at 补）。"""
    await init_db()
    async with get_session_factory()() as session:
        session.add_all([
            ProxyJobRun(status=jobruns.STATUS_RUNNING, started_at=NOW - timedelta(minutes=5)),
            ProxyJobRun(status=jobruns.STATUS_RUNNING, started_at=NOW - timedelta(minutes=1)),
            ProxyJobRun(status=jobruns.STATUS_SUCCESS, started_at=NOW - timedelta(minutes=9),
                        finished_at=NOW - timedelta(minutes=8), duration_ms=60000),
        ])
        await session.commit()

        now = NOW + timedelta(minutes=10)
        marked = await jobruns.mark_interrupted_runs(session, now)
        await session.commit()
        assert marked == 2

        rows = (await session.execute(
            select(ProxyJobRun).order_by(ProxyJobRun.id)
        )).scalars().all()
        assert [r.status for r in rows] == [
            jobruns.STATUS_INTERRUPTED, jobruns.STATUS_INTERRUPTED, jobruns.STATUS_SUCCESS
        ]
        assert rows[0].duration_ms == 15 * 60 * 1000
        assert rows[0].error_summary["interrupted"] is True
        # 已收尾的行一个字节不动
        assert rows[2].duration_ms == 60000

        # 幂等：再来一次没有可标的行
        assert await jobruns.mark_interrupted_runs(session, now) == 0


@pytest.mark.asyncio
async def test_record_start_is_fail_soft(tmp_data_dir, monkeypatch):
    """观测失败绝不影响爬取：记录入口返回 None，不抛。"""
    class _Boom:
        def __call__(self, *a, **kw):
            raise RuntimeError("库里写不进去")

    monkeypatch.setattr(jobruns, "get_session_factory", lambda: _Boom())
    run_id = await jobruns.record_start(
        proxy_url=None, regions=["us"], workers=1, data_dir=tmp_data_dir, now=NOW
    )
    assert run_id is None
    # 收尾同样 fail-soft（run_id=None 直接返回，不碰库）
    await jobruns.record_finish(
        None, status=jobruns.STATUS_FAILED, now=NOW, error_summary={}
    )


# ══ 5. 真链路：run_crawl 落一行 ══════════════════════════════════
@pytest.mark.asyncio
async def test_run_crawl_writes_one_row(tmp_data_dir):
    """**唯一生产入口**跑一轮（零任务、零网络）后，库里确实多出一行。

    这条是「接进真实生命周期」的最小证据：记录点在 `run_crawl` 里，
    bundles 直调与 CLI 两条路径同样经过它（写在 start_job 上会漏掉那两条）。
    """
    await init_db()
    stats = await run_crawl(None, config=CrawlRunConfig(regions=["us"], workers=2))
    assert stats["processed"] == 0 and stats["success"] == 0 and stats["failed"] == 0

    async with get_session_factory()() as session:
        rows = (await session.execute(select(ProxyJobRun))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == jobruns.STATUS_SUCCESS  # 0/0 不算失败
    assert row.task_count == 0
    assert row.finished_at is not None and row.duration_ms is not None
    assert row.proxy_url is None  # 直连形态（本轮没有池 Runtime）
    assert row.error_summary == {"by_error": {}}
    # 占用必须已释放（finally 里的 end_crawl）
    from app.crawler.occupancy import crawler_busy

    assert crawler_busy() is False


@pytest.mark.asyncio
async def test_run_crawl_records_failure_when_body_raises(tmp_data_dir, monkeypatch):
    """作业体抛异常：先落终态 failed 再抛——不影响原有异常语义。"""
    await init_db()

    import app.crawler.runner as runner_module

    async def _boom(*a, **kw):
        raise ValueError("任务缺少区服配置，拒绝全区回退")

    monkeypatch.setattr(runner_module, "_run_crawl_locked", _boom)
    with pytest.raises(ValueError):
        await run_crawl(None, config=CrawlRunConfig(regions=[], workers=1))

    async with get_session_factory()() as session:
        rows = (await session.execute(select(ProxyJobRun))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == jobruns.STATUS_FAILED
    assert rows[0].error_summary == {"by_error": {"internal": 1}}
    assert rows[0].finished_at is not None


@pytest.mark.asyncio
async def test_run_crawl_interrupted_status_when_stopped(tmp_data_dir):
    """停止信号置位 → interrupted（不是 failed）。"""
    await init_db()
    stop = asyncio.Event()
    stop.set()
    await run_crawl(None, config=CrawlRunConfig(regions=["us"], workers=1), stop_event=stop)
    async with get_session_factory()() as session:
        rows = (await session.execute(select(ProxyJobRun))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == jobruns.STATUS_INTERRUPTED
    # 「没跑完」同时落进行级标记（与启动清理写的 interrupted 同一语义）
    assert rows[0].error_summary == {"by_error": {}, "interrupted": True}
    assert rows[0].finished_at is not None and rows[0].duration_ms is not None


@pytest.mark.asyncio
async def test_task_count_is_planned_not_completed(tmp_data_dir, monkeypatch):
    """钉死 `task_count` 口径：**计划任务数**（`stats["total"]`），不是已完成数。

    中断场景：计划 2、完成 1 → 台账必须是 task_count=2 / success_count=1 /
    error_count=0。曾经取的是 `stats["processed"]`（done），会把它记成 1 ——
    「这次一共打算跑几个」这个事实就被悄悄改掉了。这条测试就是让那个错误再也进不来。

    这里刻意只钉 `run_crawl` 的**映射**（不做桩就是真跑）；「计划数 ≠ 完成数」这个
    输入事实本身由 `test_crawler_scheduler.py::test_total_target_is_planned_not_completed` 钉。
    """
    await init_db()

    import app.crawler.runner as runner_module

    half = {
        "total": 2,          # 计划 2
        "processed": 1,      # 只完成 1
        "success": 1,
        "failed": 0,
        "skipped_no_discount": 0,
        "discount_ended": 0,
        "elapsed_seconds": 3.0,
    }

    async def _stopped_after_first(*args, **kwargs):
        return half

    monkeypatch.setattr(runner_module, "_run_crawl_locked", _stopped_after_first)
    stop = asyncio.Event()
    stop.set()  # 完成 1 个之后被停止
    stats = await run_crawl(
        None, config=CrawlRunConfig(regions=["us"], workers=1), stop_event=stop
    )
    assert (stats["total"], stats["processed"]) == (2, 1), "输入事实：计划 2、完成 1"

    async with get_session_factory()() as session:
        row = (await session.execute(select(ProxyJobRun))).scalars().one()
    assert row.task_count == 2, "台账必须记计划数，不是完成数"
    assert row.success_count == 1
    assert row.error_count == 0
    assert row.status == jobruns.STATUS_INTERRUPTED
    # 计划数 ≠ 完成数：这正是中断作业应有的形状，不能被「凑平」
    assert row.task_count != row.success_count + row.error_count
