"""汇率历史缺口扫描与修复（rates.history）行为验收。

覆盖：
1. 缺口发现 = 缺行日 ∪ carried 日（扫描到昨天，不含今天）；
2. timeframe 窗口批量修复：缺失日与 carried 日写 observed、周末照常落行
   （数据由 Provider 决定，不维护工作日历）、已有 observed 永不重拉；
3. 幂等（二跑零写入）、dry-run（零触网零写入）；
4. quota guard（账期用尽绝不出网）、Provider 失败即停；
5. 修复后账单重估（bills.revalue_affected：行级 fx 重算 + 汇总重算）；
6. 调度任务门禁（爬虫占线跳过）。

隔离：tmp 独立库（手工建 schema + canonical 唯一索引）+ Provider 打桩
（绝不出网）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.core.scheduler as sched_mod  # noqa: E402
from app.core import database as database_module  # noqa: E402
from app.domains.bills import service as bills_service  # noqa: E402
from app.domains.bills.models import BillGameTx, BillImport  # noqa: E402
from app.domains.rates import history as rates_history  # noqa: E402
from app.domains.rates import quota as rates_quota  # noqa: E402
from app.domains.rates import service as rates_service  # noqa: E402
from app.domains.rates.models import FxProviderUsage, FxRateHistory  # noqa: E402
from app.domains.rates.providers.exchangerate_host import (  # noqa: E402
    PROVIDER_NAME,
    ProviderError,
    ProviderQuotaError,
)

XTS = "XTS"  # 合成币种（ISO 测试码；白名单由夹具收窄为 {XTS}）
TODAY = date(2026, 9, 8)  # 周二；horizon = 9/7


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """tmp 独立库：建全量 schema + canonical 唯一索引；各模块工厂指向它。"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    for mod in (rates_history, rates_quota, rates_service, bills_service):
        monkeypatch.setattr(mod, "get_session_factory", lambda: factory)
    async with engine.begin() as conn:
        await conn.run_sync(database_module.Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_frh_currency_date"
                " ON fx_rate_history(currency_code, rate_date)"
            )
        )
    # 白名单收窄为合成币种（scan/repair 只处理白名单币种）
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    # 账期限额固定（真实限额另有 quota 断言；此处避免触 settings 表）
    async def _limit() -> int:
        return 100

    monkeypatch.setattr(rates_quota, "monthly_limit", _limit)
    yield factory
    await engine.dispose()


async def _add_history(
    code: str, rows: list[tuple[str, float, str | None]], source: str = "manual"
) -> None:
    """rows: (YYYY-MM-DD, rate, source_kind)；source_kind 传 None 模拟过渡期行。"""
    async with database_module.get_session_factory()() as session:
        for day, rate, kind in rows:
            session.add(
                FxRateHistory(
                    currency_code=code,
                    rate_to_cny=rate,
                    source=source,
                    source_kind=kind,
                    rate_date=date.fromisoformat(day),
                    fetched_at=datetime.fromisoformat(f"{day} 12:00:00"),
                )
            )
        await session.commit()


async def _rows(code: str = XTS) -> dict[str, tuple[float, str]]:
    async with database_module.get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT rate_date, rate_to_cny, source_kind FROM fx_rate_history"
                    " WHERE currency_code = :c ORDER BY rate_date"
                ),
                {"c": code},
            )
        ).all()
    return {str(r[0])[:10]: (float(r[1]), str(r[2])) for r in rows}


def _fake_provider(rows: dict[date, dict[str, float]], calls: list[tuple[date, date]]):
    """生成打桩 Provider 类：按合成数据返回窗口内行，记录调用区间。"""

    class _Fake:
        def __init__(self, api_key: str, **kwargs) -> None:
            pass

        async def fetch_timeframe(self, start: date, end: date, currencies):
            calls.append((start, end))
            return {
                day: dict(row)
                for day, row in rows.items()
                if start <= day <= end
            }

    return _Fake


async def _seed_gap() -> None:
    """造场景：9/1 observed、9/2 carried、9/5 observed；9/3、9/4、9/6、9/7 缺。"""
    await _add_history(
        XTS,
        [
            ("2026-09-01", 6.50, "observed"),
            ("2026-09-02", 6.50, "carried"),
            ("2026-09-05", 6.60, "observed"),
        ],
    )


# ── 缺口扫描 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_finds_missing_and_carried(db):
    await _seed_gap()
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert scan["horizon"] == "2026-09-07"
    info = scan["currencies"][XTS]
    assert info["missing"] == 4  # 9/3、9/4、9/6、9/7
    assert info["carried"] == 1  # 9/2
    assert info["first"] == "2026-09-02"
    assert info["last"] == "2026-09-07"
    assert scan["totalPairs"] == 5
    assert len(scan["windows"]) == 1
    assert scan["windows"][0]["start"] == "2026-09-02"
    assert scan["windows"][0]["end"] == "2026-09-07"


@pytest.mark.asyncio
async def test_scan_skips_currency_without_anchor(db):
    """完全无历史行的币种无锚点，跳过。"""
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert scan["currencies"] == {}
    assert scan["windows"] == []


@pytest.mark.asyncio
async def test_scan_treats_null_kind_by_source(db):
    """语义列为空的过渡期行按 source 推导：backfill 延续值列入待修复，
    实时源行视为观测——两类行都不得因语义列缺失而被误判。"""
    await _add_history(
        XTS,
        [("2026-09-01", 6.50, None), ("2026-09-02", 6.51, None)],
        source="backfill",
    )
    await _add_history(XTS, [("2026-09-03", 6.52, None)], source="augmentedsteam")
    scan = await rates_history.scan_history_gaps(today=TODAY)
    info = scan["currencies"][XTS]
    assert info["carried"] == 2  # 9/1、9/2 按 backfill 推导为 carried
    assert info["missing"] == 4  # 9/4 ~ 9/7 缺行
    assert info["first"] == "2026-09-01"


@pytest.mark.asyncio
async def test_scan_window_split_over_365_days(db):
    """超 365 天的缺口拆成多窗口。"""
    await _add_history(XTS, [("2024-01-01", 6.5, "observed")])
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert len(scan["windows"]) >= 2
    assert all(w["days"] <= 365 for w in scan["windows"])


# ── 修复 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repair_writes_observed_and_replaces_carried(db, monkeypatch):
    """缺失日与 carried 日写 observed；周末照常落行；已有 observed 永不重拉。"""
    await _seed_gap()
    calls: list = []
    rows = {
        date(2026, 9, 2): {XTS: 6.51},
        date(2026, 9, 3): {XTS: 6.52},
        date(2026, 9, 4): {XTS: 6.53},
        date(2026, 9, 5): {XTS: 9.99},  # Provider 值不该覆盖已有 observed
        date(2026, 9, 6): {XTS: 6.55},  # 周日（Provider 也返回）
        date(2026, 9, 7): {XTS: 6.56},
    }
    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _fake_provider(rows, calls))
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["requests"] == 1
    assert calls == [(date(2026, 9, 2), date(2026, 9, 7))]
    got = await _rows()
    assert got["2026-09-01"] == (6.50, "observed")  # 原 observed 不动
    assert got["2026-09-02"] == (6.51, "observed")  # carried → observed
    assert got["2026-09-03"] == (6.52, "observed")
    assert got["2026-09-04"] == (6.53, "observed")
    assert got["2026-09-05"] == (6.60, "observed")  # observed 永不重拉
    assert got["2026-09-06"] == (6.55, "observed")  # 周日也有行（Provider 语义）
    assert got["2026-09-07"] == (6.56, "observed")


@pytest.mark.asyncio
async def test_repair_idempotent_second_run(db, monkeypatch):
    await _seed_gap()
    calls: list = []
    rows = {d: {XTS: 6.5 + i * 0.01} for i, d in enumerate(
        [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
         date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)]
    )}
    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _fake_provider(rows, calls))
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    first = await rates_history.repair_history_gaps(today=TODAY)
    assert first["written"] == 5
    second = await rates_history.repair_history_gaps(today=TODAY)
    assert second["status"] == "no_gaps"
    assert second["written"] == 0
    assert len(calls) == 1  # 二跑未触网


@pytest.mark.asyncio
async def test_repair_dry_run_no_network_no_write(db, monkeypatch):
    await _seed_gap()
    calls: list = []
    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _fake_provider({}, calls))
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    result = await rates_history.repair_history_gaps(today=TODAY, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["planned"][0]["start"] == "2026-09-02"
    assert calls == []  # 未触网
    got = await _rows()
    assert got["2026-09-02"] == (6.50, "carried")  # 未写库


@pytest.mark.asyncio
async def test_repair_no_key_skips(db, monkeypatch):
    await _seed_gap()
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: None)
    result = await rates_history.repair_history_gaps(today=TODAY)
    assert result["status"] == "no_key"
    assert result["written"] == 0


@pytest.mark.asyncio
async def test_quota_guard_blocks_network(db, monkeypatch):
    """账期用尽：quota guard 在触网前拦截（剩余 0 绝不发请求）。"""
    await _seed_gap()
    calls: list = []
    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _fake_provider({}, calls))
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")
    fp = rates_quota.key_fingerprint("test-key")
    async with database_module.get_session_factory()() as session:
        session.add(
            FxProviderUsage(
                provider=PROVIDER_NAME,
                key_fingerprint=fp,
                period=rates_quota.current_period(),
                request_count=100,
                request_limit=100,
            )
        )
        await session.commit()

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "quota_exhausted"
    assert calls == []  # 绝不出网
    assert result["written"] == 0


@pytest.mark.asyncio
async def test_provider_error_stops_and_ledgers(db, monkeypatch):
    """Provider 失败即停（不重试风暴），失败记入账本 last_error。"""
    await _seed_gap()

    class _Boom:
        def __init__(self, api_key: str, **kwargs) -> None:
            pass

        async def fetch_timeframe(self, start, end, currencies):
            raise ProviderError("模拟网络故障")

    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _Boom)
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "provider_error"
    usage = await rates_quota.get_usage(PROVIDER_NAME, rates_quota.key_fingerprint("test-key"))
    assert usage["requestCount"] == 1
    assert "模拟网络故障" in (usage["lastError"] or "")


@pytest.mark.asyncio
async def test_repair_quota_error_from_provider(db, monkeypatch):
    await _seed_gap()

    class _Quota:
        def __init__(self, api_key: str, **kwargs) -> None:
            pass

        async def fetch_timeframe(self, start, end, currencies):
            raise ProviderQuotaError("HTTP 429 code=104")

    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _Quota)
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    result = await rates_history.repair_history_gaps(today=TODAY)
    assert result["status"] == "quota_exhausted"


# ── 依赖方重估 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_repair_revalues_bills(db, monkeypatch):
    """修复后账单自动重估：缺汇率的交易补上 fx_rate/cny_fen，汇总同步。"""
    await _seed_gap()
    async with database_module.get_session_factory()() as session:
        session.add(
            BillImport(
                id=1, nickname="t", game_net_fen=0, game_spend_fen=0,
                game_refund_fen=0, orders=0, fx_missing=1,
            )
        )
        session.add(
            BillGameTx(
                import_id=1, date="2026-09-03", tx_type="购买", items_json="[]",
                currency=XTS, amount=100.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        await session.commit()

    rows = {date(2026, 9, 3): {XTS: 0.05}}
    calls: list = []
    monkeypatch.setattr(rates_history, "ExchangerateHostProvider", _fake_provider(rows, calls))
    monkeypatch.setattr(rates_history, "resolve_api_key", lambda: "test-key")

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["revalued"]["bills"] == 1
    async with database_module.get_session_factory()() as session:
        tx = (await session.execute(
            text("SELECT fx_rate, cny_fen FROM bill_game_txs WHERE import_id = 1")
        )).one()
        assert float(tx[0]) == pytest.approx(0.05)
        assert int(tx[1]) == round(100.0 * 0.05 * 100)  # 500 分
        imp = (await session.execute(
            text("SELECT game_spend_fen, game_net_fen, fx_missing FROM bill_imports WHERE id = 1")
        )).one()
        assert imp[0] == 500 and imp[1] == 500 and imp[2] == 0


@pytest.mark.asyncio
async def test_revalue_does_not_consume_carried(db, monkeypatch):
    """carried 不是真实观测：重估只能命中 observed，命中不到就是缺失（不猜）。"""
    async with database_module.get_session_factory()() as session:
        session.add(
            BillGameTx(
                import_id=1, date="2026-09-03", tx_type="购买", items_json="[]",
                currency=XTS, amount=100.0, sign=1, cny_fen=999, fx_rate=9.99,
            )
        )
        await session.commit()
    await _add_history(XTS, [("2026-09-03", 0.05, "carried")])

    result = await bills_service.revalue_affected({XTS: {date(2026, 9, 3)}})

    assert result["transactions"] == 1
    async with database_module.get_session_factory()() as session:
        tx = (await session.execute(
            text("SELECT fx_rate, cny_fen FROM bill_game_txs WHERE import_id = 1")
        )).one()
        assert tx[0] is None and tx[1] is None  # carried 不参与 → 明示缺失


# ── 调度门禁 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_job_skips_when_crawler_busy(db, monkeypatch):
    await _seed_gap()
    called: list = []

    async def _fake_repair(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "requests": 0, "written": 0, "windows": []}

    monkeypatch.setattr(rates_history, "repair_history_gaps", _fake_repair)
    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: False)

    await sched_mod._job_fx_history_repair()
    assert called == []  # 爬虫占线 → 修复不启动


@pytest.mark.asyncio
async def test_scheduler_job_skips_without_key(db, monkeypatch):
    await _seed_gap()
    called: list = []

    async def _fake_repair(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "requests": 0, "written": 0, "windows": []}

    monkeypatch.setattr(rates_history, "repair_history_gaps", _fake_repair)
    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)
    import app.domains.rates.providers.exchangerate_host as erh_mod

    monkeypatch.setattr(erh_mod, "resolve_api_key", lambda: None)

    await sched_mod._job_fx_history_repair()
    assert called == []  # 未配置 Key → 静默跳过


@pytest.mark.asyncio
async def test_scheduler_job_runs_when_all_gates_pass(db, monkeypatch):
    await _seed_gap()
    called: list = []

    async def _fake_repair(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "requests": 1, "written": 3, "windows": [{"start": "x"}]}

    monkeypatch.setattr(rates_history, "repair_history_gaps", _fake_repair)
    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)
    import app.domains.rates.providers.exchangerate_host as erh_mod

    monkeypatch.setattr(erh_mod, "resolve_api_key", lambda: "k")

    await sched_mod._job_fx_history_repair()
    assert called and called[0]["max_windows"] == sched_mod._FX_REPAIR_MAX_WINDOWS
