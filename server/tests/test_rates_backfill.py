"""汇率历史缺口补齐（backfill_history）行为验收。

用合成币种 XTS/XTC 在真实开发库验证（夹具清理——遵循合成探针规约，
XTS 为 ISO 测试币种不在白名单，误留也会被启动清洗掉）：
1. 工作日缺口按上一已有行值延续补行（forward-fill），周末不造行
2. 同日多行锚点取 id 最后一行（与日线查询的日收语义一致）
3. 幂等：二跑零写入
4. dry_run 只算不写
5. 白名单外币种跳过
"""
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import get_session_factory, init_db
from app.domains.rates import service as rates_service
from app.domains.rates.models import FxRateHistory

XTS = "XTS"
XTC = "XTC"  # 第二合成币种（白名单外用例）


def _fake_today(y: int, m: int, d: int):
    return lambda: datetime(y, m, d, 12, 0, 0)


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch):
    await init_db()
    async with get_session_factory()() as session:
        await session.execute(
            text("DELETE FROM fx_rate_history WHERE currency_code IN (:a, :b)"),
            {"a": XTS, "b": XTC},
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            text("DELETE FROM fx_rate_history WHERE currency_code IN (:a, :b)"),
            {"a": XTS, "b": XTC},
        )
        await session.commit()


async def _add_rows(rows: list[tuple[str, float, str]]) -> None:
    async with get_session_factory()() as session:
        for code, rate, fa in rows:
            session.add(
                FxRateHistory(
                    currency_code=code, rate_to_cny=rate, source="manual",
                    fetched_at=datetime.fromisoformat(fa),
                )
            )
        await session.commit()


async def _xts_rows() -> list[tuple[str, float, str]]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT fetched_at, rate_to_cny, source FROM fx_rate_history "
                    "WHERE currency_code = :c ORDER BY fetched_at"
                ),
                {"c": XTS},
            )
        ).all()
    return [(str(r[0])[:19], float(r[1]), r[2]) for r in rows]


@pytest.mark.asyncio
async def test_fill_weekday_gap_forward_fill(monkeypatch):
    """周五→下周四之间的工作日缺口补行：值延续、周末跳过、source=backfill。"""
    monkeypatch.setattr(rates_service, "get_beijing_time_obj", _fake_today(2026, 9, 11))
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    await _add_rows(
        [
            (XTS, 6.5, "2026-09-04T12:00:00"),  # 周五
            (XTS, 6.6, "2026-09-10T12:00:00"),  # 下周四
        ]
    )

    stats = await rates_service.backfill_history()

    assert stats["inserted"] == 4
    assert stats["currencies"] == {
        XTS: {"count": 4, "first": "2026-09-07", "last": "2026-09-11"}
    }
    rows = await _xts_rows()
    by_day = {r[0][:10]: (r[1], r[2]) for r in rows}
    assert by_day["2026-09-07"] == (6.5, "backfill")  # 延续周五值
    assert by_day["2026-09-08"] == (6.5, "backfill")
    assert by_day["2026-09-09"] == (6.5, "backfill")
    assert by_day["2026-09-11"] == (6.6, "backfill")  # 延续周四值
    assert "2026-09-05" not in by_day  # 周六不造行
    assert "2026-09-06" not in by_day  # 周日不造行


@pytest.mark.asyncio
async def test_anchor_last_row_of_day_wins(monkeypatch):
    """同日多行锚点取 id 最后一行（日收语义），forward-fill 用它延续。"""
    monkeypatch.setattr(rates_service, "get_beijing_time_obj", _fake_today(2026, 9, 7))
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    await _add_rows(
        [
            (XTS, 6.5, "2026-09-04T10:00:00"),
            (XTS, 6.55, "2026-09-04T18:00:00"),  # 同日更晚行
        ]
    )

    stats = await rates_service.backfill_history()

    assert stats["inserted"] == 1  # 只缺 09-07（周一）一天
    rows = await _xts_rows()
    filled = [r for r in rows if r[0].startswith("2026-09-07")]
    assert filled == [("2026-09-07 00:00:00", 6.55, "backfill")]


@pytest.mark.asyncio
async def test_idempotent_second_run(monkeypatch):
    """补过的日期已有行，二跑零写入。"""
    monkeypatch.setattr(rates_service, "get_beijing_time_obj", _fake_today(2026, 9, 11))
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    await _add_rows(
        [
            (XTS, 6.5, "2026-09-04T12:00:00"),
            (XTS, 6.6, "2026-09-10T12:00:00"),
        ]
    )

    first = await rates_service.backfill_history()
    assert first["inserted"] == 4
    second = await rates_service.backfill_history()
    assert second == {"inserted": 0, "currencies": {}}


@pytest.mark.asyncio
async def test_dry_run_no_write(monkeypatch):
    """dry_run 返回完整计划但一行不写。"""
    monkeypatch.setattr(rates_service, "get_beijing_time_obj", _fake_today(2026, 9, 16))
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    await _add_rows(
        [
            (XTS, 6.5, "2026-09-04T12:00:00"),
            (XTS, 6.6, "2026-09-10T12:00:00"),
        ]
    )

    stats = await rates_service.backfill_history(dry_run=True)

    assert stats["inserted"] == 7  # 09-07..09, 09-11, 09-14..16 全是工作日缺口
    assert stats["currencies"][XTS] == {
        "count": 7, "first": "2026-09-07", "last": "2026-09-16"
    }
    rows = await _xts_rows()
    assert len(rows) == 2  # 库里还是只有原两行


@pytest.mark.asyncio
async def test_skips_currency_outside_allowlist(monkeypatch):
    """白名单外币种不补不碰（清洗由启动链专门负责）。"""
    monkeypatch.setattr(rates_service, "get_beijing_time_obj", _fake_today(2026, 9, 11))
    monkeypatch.setattr(rates_service, "ALLOWED_CURRENCIES", frozenset({XTS}))
    await _add_rows(
        [
            (XTC, 9.9, "2026-09-04T12:00:00"),
            (XTS, 6.5, "2026-09-04T12:00:00"),
            (XTS, 6.6, "2026-09-10T12:00:00"),
        ]
    )

    stats = await rates_service.backfill_history()

    assert stats["inserted"] == 4  # 只补 XTS
    assert XTC not in stats["currencies"]
    async with get_session_factory()() as session:
        n = (
            await session.execute(
                text("SELECT COUNT(*) FROM fx_rate_history WHERE currency_code = :c"),
                {"c": XTC},
            )
        ).scalar_one()
    assert n == 1  # XTC 原行原样
