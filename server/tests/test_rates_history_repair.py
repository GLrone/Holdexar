"""汇率历史缺口扫描与修复（rates.history）行为验收。

覆盖：
1. 缺口发现 = 缺行日 ∪ carried 日（扫描到昨天，不含今天）；
2. timeframe 窗口批量修复：缺失日与 carried 日写 observed、周末照常落行
   （数据由 Provider 决定，不维护工作日历）、已有 observed 永不重拉；
3. 幂等（二跑零写入）、dry-run（零触网零写入）；
4. Provider 失败即停（不重试风暴）；
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
from app.domains.rates import service as rates_service  # noqa: E402
from app.domains.rates.models import FxRateHistory  # noqa: E402
from app.domains.rates.providers.frankfurter import ProviderError  # noqa: E402

XTS = "XTS"  # 合成币种（ISO 测试码；白名单由夹具收窄为 {XTS}）
TODAY = date(2026, 9, 8)  # 周二；horizon = 9/7


@pytest_asyncio.fixture
async def db(monkeypatch):
    """完整库（隔离数据目录）+ canonical 唯一索引（幂等）。"""
    from app.core.database import get_engine, get_session_factory

    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_frh_currency_date"
                " ON fx_rate_history(currency_code, rate_date)"
            )
        )
    # 白名单收窄为合成币种（scan/repair 只处理白名单币种）
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    return get_session_factory()


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
    """生成打桩 Provider 实例：按合成数据返回窗口内行，记录调用区间。"""

    class _Fake:
        async def fetch_timeframe(self, start: date, end: date, currencies):
            calls.append((start, end))
            return {
                day: dict(row)
                for day, row in rows.items()
                if start <= day <= end
            }

    return _Fake()


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
async def test_scan_bootstraps_empty_library(db):
    """全库零历史行（全新安装）：以 Provider 全历史起点为白名单币种建窗。

    种子不再带汇率档案——若无锚点跳过，新装库永远建不出历史 CNY 换算；
    bootstrap 窗口交修复链现抓（bing 全历史档位自 2012 年起）。
    """
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert set(scan["currencies"]) == {XTS}
    info = scan["currencies"][XTS]
    assert info["first"] == "2012-01-01"
    assert info["carried"] == 0
    span_days = (date(2026, 9, 7) - date(2012, 1, 1)).days + 1
    assert info["missing"] == span_days
    assert scan["totalDays"] == span_days
    assert scan["windows"][0]["start"] == "2012-01-01"
    assert scan["windows"][-1]["end"] == "2026-09-07"
    assert all(w["days"] <= 365 for w in scan["windows"])


@pytest.mark.asyncio
async def test_normal_repair_handles_empty_library_with_batch_limit(db, monkeypatch):
    calls = []
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider({}, calls))
    result = await rates_history.repair_history_gaps(today=TODAY, max_windows=2)
    assert result["status"] == "ok" and result["requests"] == 2
    assert len(calls) == 2
    assert calls[0][0] == date(2012, 1, 1)


@pytest.mark.asyncio
async def test_normal_history_maintenance_respects_fetch_toggle(db, monkeypatch):
    from app.core import scheduler
    from app.domains.settings.service import set_value
    await set_value("fetch.fx_history", False)
    async def forbidden(**kwargs):
        raise AssertionError("disabled maintenance cannot consume upstream quota")
    monkeypatch.setattr(rates_history, "repair_history_gaps", forbidden)
    await scheduler._job_fx_history_repair()


# ── 全新安装 bootstrap 入口 ─────────────────────────────








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
async def test_scan_covers_legacy_rows_without_canonical_columns(db):
    """旧版本实例写入的行不带 canonical 列（rate_date 为空）：日期从 fetched_at
    推导、语义按 source 推导——这批发现在真实环境出现过，不得被静默漏掉。"""
    async with database_module.get_session_factory()() as session:
        session.add_all(
            [
                FxRateHistory(
                    currency_code=XTS, rate_to_cny=0.015, source="backfill",
                    source_kind=None, rate_date=None,
                    fetched_at=datetime(2026, 9, 1, 12, 0, 0),
                ),
                FxRateHistory(
                    currency_code=XTS, rate_to_cny=0.016, source="augmentedsteam",
                    source_kind=None, rate_date=None,
                    fetched_at=datetime(2026, 9, 2, 12, 0, 0),
                ),
            ]
        )
        await session.commit()
    scan = await rates_history.scan_history_gaps(today=TODAY)
    info = scan["currencies"][XTS]
    assert info["first"] == "2026-09-01"  # 无 canonical 列的行也被识别
    assert info["carried"] == 1  # backfill → carried（待修复）
    assert info["missing"] == 5  # 9/3 ~ 9/7 缺行


@pytest.mark.asyncio
async def test_scan_prefers_observed_over_carried_same_day(db):
    """同一 (币种, 日) 同时存在 canonical observed 与旧格式 carried 行 →
    取 observed：旧延续行不得把已有真实观测日重新判成缺口。"""
    await _add_history(
        XTS, [("2026-09-01", 6.50, "observed")], source="augmentedsteam"
    )
    async with database_module.get_session_factory()() as session:
        session.add(
            FxRateHistory(
                currency_code=XTS, rate_to_cny=9.99, source="backfill",
                source_kind=None, rate_date=None,
                fetched_at=datetime(2026, 9, 1, 23, 0, 0),
            )
        )
        await session.commit()
    scan = await rates_history.scan_history_gaps(today=TODAY)
    info = scan["currencies"][XTS]
    assert info["carried"] == 0
    assert info["missing"] == 6  # 9/2 ~ 9/7
    assert info["first"] == "2026-09-02"


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
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["requests"] == 1
    assert calls == [(date(2026, 9, 2), date(2026, 9, 7))]
    got = await _rows()
    assert got["2026-09-01"] == (6.50, "observed")  # 原 observed 不动
    assert got["2026-09-02"] == (6.51, "derived")  # carried → derived
    assert got["2026-09-03"] == (6.52, "derived")
    assert got["2026-09-04"] == (6.53, "derived")
    assert got["2026-09-05"] == (6.60, "observed")  # observed 永不重拉
    assert got["2026-09-06"] == (6.55, "derived")  # 周日也有行（Provider 语义）
    assert got["2026-09-07"] == (6.56, "derived")


@pytest.mark.asyncio
async def test_repair_idempotent_second_run(db, monkeypatch):
    await _seed_gap()
    calls: list = []
    rows = {d: {XTS: 6.5 + i * 0.01} for i, d in enumerate(
        [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
         date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)]
    )}
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

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
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider({}, calls))

    result = await rates_history.repair_history_gaps(today=TODAY, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["planned"][0]["start"] == "2026-09-02"
    assert calls == []  # 未触网
    got = await _rows()
    assert got["2026-09-02"] == (6.50, "carried")  # 未写库


@pytest.mark.asyncio
async def test_repair_keyless_primary_runs_without_key(db, monkeypatch):
    """主源免 Key：无任何 Key 也照常修复（不再有 no_key 短路）。"""
    await _seed_gap()
    calls: list = []
    rows = {
        d: {XTS: 6.5}
        for d in (date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
                  date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7))
    }
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["written"] == 5




@pytest.mark.asyncio
async def test_provider_error_stops(db, monkeypatch):
    """Provider 失败 → provider_error 即停（不重试风暴，缺口留到下一轮）。"""
    await _seed_gap()

    class _BingDown:
        async def fetch_timeframe(self, start: date, end: date, currencies):
            raise ProviderError("主源模拟故障")

    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _BingDown())

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "provider_error"
    assert result["written"] == 0






# ── 依赖方重估 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_repair_revalues_bills(db, monkeypatch):
    """修复后账单自动重估：缺汇率的交易补上 fx_rate/cny_fen，汇总同步。"""
    await _seed_gap()
    async with database_module.get_session_factory()() as session:
        session.add(
            BillImport(
                id=1, nickname="t",
            )
        )
        session.add(
            BillGameTx(
                import_id=1, date="2026-09-03", tx_type="购买", items_json="[]",
                currency=XTS, amount=100.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        await session.commit()

    # 完整性闸门要求：窗口内每天都要返回（含 USDCNY），故覆盖整个 [9/2, 9/7]
    rows = {
        day: {XTS: 0.05}
        for day in (
            date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
            date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7),
        )
    }
    calls: list = []
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["revalued"]["bills"] == 1
    async with database_module.get_session_factory()() as session:
        tx = (await session.execute(
            text("SELECT fx_rate, cny_fen FROM bill_game_txs WHERE import_id = 1")
        )).one()
        assert float(tx[0]) == pytest.approx(0.05)
        assert int(tx[1]) == round(100.0 * 0.05 * 100)  # 500 分
    from app.domains.bills.service import list_imports

    imp = (await list_imports())[0]
    assert imp["gameSpendFen"] == 500 and imp["gameNetFen"] == 500 and imp["fxMissing"] == 0


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
async def test_scheduler_job_runs_without_key(db, monkeypatch):
    """主源免 Key：无 Key 也运行（不再有 Key 门禁）。"""
    await _seed_gap()
    called: list = []

    async def _fake_repair(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "requests": 0, "written": 0, "windows": []}

    monkeypatch.setattr(rates_history, "repair_history_gaps", _fake_repair)
    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)

    await sched_mod._job_fx_history_repair()
    assert called and called[0]["max_windows"] == sched_mod._FX_REPAIR_MAX_WINDOWS


@pytest.mark.asyncio
async def test_scheduler_job_runs_when_all_gates_pass(db, monkeypatch):
    await _seed_gap()
    called: list = []

    async def _fake_repair(**kwargs):
        called.append(kwargs)
        return {"status": "ok", "requests": 1, "written": 3, "windows": [{"start": "x"}]}

    monkeypatch.setattr(rates_history, "repair_history_gaps", _fake_repair)
    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)

    await sched_mod._job_fx_history_repair()
    assert called and called[0]["max_windows"] == sched_mod._FX_REPAIR_MAX_WINDOWS


# ── 修复链的边界行为：缺日整批不写 / 单币缺失 / 旧格式行收敛 ──────




@pytest.mark.asyncio
async def test_missing_target_currency_writes_others_and_keeps_gap(db, monkeypatch):
    """某目标币种 Provider 未返回不判失败：其它币种照写 observed，
    缺失币种缺口保留（下一轮继续，不被静默吞掉）。"""
    await _add_history(XTS, [("2026-09-01", 6.5, "observed")])
    await _add_history("XTS2", [("2026-09-01", 0.5, "observed")])
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS, "XTS2"}))
    calls: list = []
    rows = {
        d: {XTS: 6.5}
        for d in (
            date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
            date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7),
        )
    }  # 全程只有 XTS，没有 XTS2
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    got = await _rows()
    assert got["2026-09-02"][1] == "derived"  # XTS 照写
    got2 = await _rows("XTS2")
    assert got2 == {"2026-09-01": (0.5, "observed")}  # XTS2 一个都没写
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert scan["currencies"]["XTS2"]["missing"] == 6  # 缺口保留
    assert "XTS" not in scan["currencies"]  # XTS 已修满


@pytest.mark.asyncio
async def test_legacy_rows_converge_to_observed_and_scan_clears(db, monkeypatch):
    """旧格式行（无 canonical 列 + source=backfill）语义闭环：scan 推导为
    carried → repair 写同日 observed → 同日多行取 observed，scan 不再列该日。
    （物理上旧 NULL 行仍留在表中——真实环境体检项，不影响语义收敛。）"""
    async with database_module.get_session_factory()() as session:
        session.add_all(
            [
                FxRateHistory(
                    currency_code=XTS, rate_to_cny=0.015, source="backfill",
                    source_kind=None, rate_date=None,
                    fetched_at=datetime(2026, 9, 2, 12, 0, 0),
                ),
                FxRateHistory(
                    currency_code=XTS, rate_to_cny=0.015, source="backfill",
                    source_kind=None, rate_date=None,
                    fetched_at=datetime(2026, 9, 3, 12, 0, 0),
                ),
            ]
        )
        await session.commit()
    scan1 = await rates_history.scan_history_gaps(today=TODAY)
    assert scan1["currencies"][XTS]["carried"] == 2  # 无 canonical 列也识别为待修复

    calls: list = []
    rows = {
        d: {XTS: 0.05}
        for d in (date(2026, 9, 2), date(2026, 9, 3))
    }
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)

    assert result["status"] == "ok"
    assert result["written"] == 2
    scan2 = await rates_history.scan_history_gaps(today=TODAY)
    info2 = scan2["currencies"].get(XTS, {})
    assert info2.get("carried", 0) == 0  # 旧行语义已被 observed 取代
    assert info2.get("missing", 0) == 4  # 9/4 ~ 9/7 仍缺（未修区间）


@pytest.mark.asyncio
async def test_bing_rows_upgraded_to_frankfurter_derived(db, monkeypatch):
    """旧 Bing 记录在扫描中识别为待修复，修复时被覆写为 frankfurter.v2 / derived。"""
    await _add_history(
        XTS,
        [("2026-09-02", 5.00, "observed")],
        source="bing.currencyapi",
    )
    scan = await rates_history.scan_history_gaps(today=TODAY)
    assert scan["currencies"][XTS]["carried"] == 1  # 识别为待修复

    calls: list = []
    rows = {date(2026, 9, 2): {XTS: 7.25}}
    monkeypatch.setattr(rates_history, "_new_bing_provider", lambda: _fake_provider(rows, calls))

    result = await rates_history.repair_history_gaps(today=TODAY)
    assert result["status"] == "ok"
    assert result["written"] == 1

    got = await _rows()
    assert got["2026-09-02"] == (7.25, "derived")


def test_pick_observed_rate_exact_15_days_boundary():
    """验证 pick_observed_rate 在 0、14、15、16 天的回溯边界。"""
    rows = [("2024-01-01", 7.0)]
    assert rates_history.pick_observed_rate(rows, "USD", "2024-01-01") == pytest.approx(7.0)  # 0 天
    assert rates_history.pick_observed_rate(rows, "USD", "2024-01-15") == pytest.approx(7.0)  # 14 天
    assert rates_history.pick_observed_rate(rows, "USD", "2024-01-16") == pytest.approx(7.0)  # 15 天（修复边界）
    assert rates_history.pick_observed_rate(rows, "USD", "2024-01-17") is None  # 16 天超窗


@pytest.mark.asyncio
async def test_game_history_fill_cny_fen_15_days_lookback(db, monkeypatch):
    """游戏历史价格 cny_fen 补算：支持最多 15 天向前回溯，已有值行不碰。"""
    from types import SimpleNamespace
    from app.domains.games.service import _fill_cny_fen_by_history

    await _add_history(XTS, [("2024-01-01", 2.0, "derived")], source="frankfurter.v2")

    rows = [
        SimpleNamespace(id=1, cny_fen=None, price=100, snapshot_at=datetime(2024, 1, 1, 10, 0), currency=XTS, region_code="US"),
        SimpleNamespace(id=2, cny_fen=None, price=200, snapshot_at=datetime(2024, 1, 3, 10, 0), currency=XTS, region_code="US"),
        SimpleNamespace(id=3, cny_fen=None, price=300, snapshot_at=datetime(2024, 1, 16, 10, 0), currency=XTS, region_code="US"),
        SimpleNamespace(id=4, cny_fen=None, price=400, snapshot_at=datetime(2024, 1, 17, 10, 0), currency=XTS, region_code="US"),
        SimpleNamespace(id=5, cny_fen=999, price=500, snapshot_at=datetime(2024, 1, 1, 10, 0), currency=XTS, region_code="US"),
        SimpleNamespace(id=6, cny_fen=None, price=600, snapshot_at=datetime(2024, 1, 5, 10, 0), currency="CNY", region_code="CN"),
    ]

    filled = await _fill_cny_fen_by_history(rows)
    assert filled[1] == 200
    assert filled[2] == 400
    assert filled[3] == 600
    assert 4 not in filled
    assert 5 not in filled
    assert filled[6] == 600


@pytest.mark.asyncio
async def test_bills_revalue_causal_window_and_no_inflation(db, monkeypatch):
    """账单重估因果窗口与币种隔离。"""
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({"USD", "EUR"}))
    async with database_module.get_session_factory()() as session:
        session.add(
            BillImport(
                id=10, nickname="t",
            )
        )
        session.add(
            BillGameTx(
                import_id=10, date="2024-01-05", tx_type="购买", items_json="[]",
                currency="USD", amount=10.0, sign=1, cny_fen=5000, fx_rate=5.0,
            )
        )
        session.add(
            BillGameTx(
                import_id=10, date="2024-01-12", tx_type="购买", items_json="[]",
                currency="USD", amount=10.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        session.add(
            BillGameTx(
                import_id=10, date="2024-01-25", tx_type="购买", items_json="[]",
                currency="USD", amount=10.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        session.add(
            BillGameTx(
                import_id=10, date="2024-01-26", tx_type="购买", items_json="[]",
                currency="USD", amount=10.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        session.add(
            BillGameTx(
                import_id=10, date="2024-06-05", tx_type="购买", items_json="[]",
                currency="USD", amount=10.0, sign=1, cny_fen=None, fx_rate=None,
            )
        )
        await session.commit()

    await _add_history("USD", [("2024-01-10", 7.0, "derived")], source="frankfurter.v2")
    await _add_history("EUR", [("2024-06-01", 8.0, "derived")], source="frankfurter.v2")

    touched = {
        "USD": {date(2024, 1, 10)},
        "EUR": {date(2024, 6, 1)},
    }
    await bills_service.revalue_affected(touched)

    async with database_module.get_session_factory()() as session:
        rows = (await session.execute(
            text("SELECT date, fx_rate, cny_fen FROM bill_game_txs WHERE import_id = 10 ORDER BY date")
        )).all()
    assert rows[0][0] == "2024-01-05" and float(rows[0][1]) == pytest.approx(5.0) and rows[0][2] == 5000
    assert rows[1][0] == "2024-01-12" and float(rows[1][1]) == pytest.approx(7.0) and rows[1][2] == 7000
    assert rows[2][0] == "2024-01-25" and float(rows[2][1]) == pytest.approx(7.0) and rows[2][2] == 7000
    assert rows[3][0] == "2024-01-26" and rows[3][1] is None
    assert rows[4][0] == "2024-06-05" and rows[4][1] is None
