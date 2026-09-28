"""steam_events 域验收：解析器语序覆盖、校验门、双语合并、event_key 稳定性、
价格观测行打标与窗口回贴。

解析 fixture 为手工构造的最小 HTML（覆盖官方页面出现过的全部日期语序与
标题命名形态），不粘真实页面片段。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.domains.games.models import GamePriceHistory  # noqa: F401
from app.domains.steam_events import parser, service
from app.domains.steam_events.models import SteamEvent  # noqa: F401


# ── 解析器：日期语序与标题命名漂移覆盖 ────────────────────────────────────


def test_parse_day_first_dates():
    """在线英文页形态：日在前（1 October, 2026）。"""
    raw = '<h2 class="bb_subsection"><strong>Autumn Sale 2026</strong> | 1 October, 2026 - 8 October, 2026</h2>'
    events = parser.parse_upcoming_events(raw)
    assert len(events) == 1
    assert events[0]["category"] == "seasonal_sale"
    assert events[0]["start"] == date(2026, 10, 1)
    assert events[0]["end"] == date(2026, 10, 8)


def test_parse_month_first_dates():
    """镜像历史快照形态：月在前（October 1, 2026）。"""
    raw = '<h2><strong>Winter Sale 2026</strong> | December 17, 2026 - January 4, 2027</h2>'
    events = parser.parse_upcoming_events(raw)
    assert events[0]["start"] == date(2026, 12, 17)
    assert events[0]["end"] == date(2027, 1, 4)


def test_parse_chinese_spaced_dates_without_strong():
    """中文页形态：无 strong 包裹、日期带空格、括号后缀。"""
    raw = "<h2>秋季特卖 | 2026 年 10 月 1 日 - 10 月 8 日（PT）</h2>"
    events = parser.parse_upcoming_events(raw)
    assert events[0]["category"] == "seasonal_sale"
    assert events[0]["start"] == date(2026, 10, 1)
    assert events[0]["end"] == date(2026, 10, 8)


def test_parse_nextfest_shared_month_and_zh_short_tail():
    """共享月年的短尾形式（英文 + 中文两形态）。"""
    en = parser.parse_upcoming_events("<h2>Next Fest | June 14 - 21, 2027</h2>")
    assert en[0]["start"] == date(2027, 6, 14)
    assert en[0]["end"] == date(2027, 6, 21)
    zh = parser.parse_upcoming_events("<h2>新品节 | 2026 年 10 月 19 日 - 26 日（PT）</h2>")
    assert zh[0]["start"] == date(2026, 10, 19)
    assert zh[0]["end"] == date(2026, 10, 26)


def test_parse_title_without_year_and_ended_suffix():
    """历史版本形态：标题无年份 + (ENDED) 后缀。"""
    raw = '<h2 class="bb_subsection"><a name="3"></a>Spring Sale | March 19 - 26, 2026 (ENDED)</h2>'
    events = parser.parse_upcoming_events(raw)
    assert events[0]["name"] == "Spring Sale"
    assert events[0]["start"] == date(2026, 3, 19)
    assert events[0]["end"] == date(2026, 3, 26)


def test_parse_fest_table_both_day_orders():
    """主题 Fest 表格：年份取自「YYYY Fests」标题，单元格两种语序都认。"""
    raw = (
        "<h2>2026 Fests</h2>"
        "<table><tr><td>Oct 12<br>Oct 19</td><td>Cooking Fest</td>"
        "<td><a href=\"https://partner.steamgames.com/doc/marketing/upcoming_events/themed_sales/cooking_2026\">More info</a></td></tr>"
        "<tr><td>12 Nov<br>19 Nov</td><td>Sample Fest</td><td>x</td></tr></table>"
    )
    events = parser.parse_upcoming_events(raw)
    assert len(events) == 2
    assert events[0]["start"] == date(2026, 10, 12)
    assert events[0]["end"] == date(2026, 10, 19)
    assert events[0]["info_slug"] == "themed_sales/cooking_2026"
    assert events[1]["start"] == date(2026, 11, 12)
    # 位置无关：标题行在页尾也一样解析
    tail = parser.parse_upcoming_events(
        "<table><tr><td>Oct 12<br>Oct 19</td><td>Cooking Fest</td><td>x</td></tr></table>"
        "<h2>2026 Fests</h2>"
    )
    assert not tail  # 年份标记在表格之后：无年份上下文不入结果


def test_parse_zh_fest_table_and_nav_names():
    """中文页：表格年份标题「2026 年各游戏节」+ 中文日期单元格可解析；
    导航锚文本按 slug 给出官方译名，「更多信息」等链接文本被过滤。"""
    raw = (
        "<h2><strong>2026 年各游戏节</strong></h2>"
        "<table><tr>"
        "<td>10 月 12 日 - 10 月 19 日（PT）</td><td>烹饪游戏节</td>"
        "<td><a href=\"https://partner.steamgames.com/doc/marketing/upcoming_events/themed_sales/cooking_2026\">更多信息</a></td>"
        "</tr></table>"
        "<a href=\"https://partner.steamgames.com/doc/marketing/upcoming_events/themed_sales/cooking_2026\">"
        "<span>2026 年 Steam 烹饪游戏节</span></a>"
    )
    events = parser.parse_upcoming_events(raw)
    assert len(events) == 1
    assert events[0]["name"] == "烹饪游戏节"
    assert events[0]["start"] == date(2026, 10, 12)
    assert events[0]["end"] == date(2026, 10, 19)
    names = parser.parse_zh_fest_names(raw)
    assert names["themed_sales/cooking_2026"] == "2026 年 Steam 烹饪游戏节"
    junk = parser.parse_zh_fest_names(
        '<a href="/doc/marketing/upcoming_events/themed_sales/x_2026">更多信息</a>'
    )
    assert junk == {}


# ── 校验门 ────────────────────────────────────────────────────────────────


def test_validation_gate_rejects_empty_seasonal():
    assert parser.validate_events([], today=date(2026, 9, 27))


def test_validation_gate_rejects_bad_duration_and_overlap():
    bad = [
        {"category": "seasonal_sale", "name": "A", "start": date(2026, 10, 1), "end": date(2026, 10, 2)},
        {"category": "seasonal_sale", "name": "B", "start": date(2026, 10, 1), "end": date(2026, 11, 30)},
    ]
    assert parser.validate_events(bad, today=date(2026, 9, 27))


def test_validation_gate_passes_reasonable_year():
    ok = [
        {"category": "seasonal_sale", "name": "Autumn Sale 2026",
         "start": date(2026, 10, 1), "end": date(2026, 10, 8)},
        {"category": "seasonal_sale", "name": "Winter Sale 2026",
         "start": date(2026, 12, 17), "end": date(2027, 1, 4)},
    ]
    assert parser.validate_events(ok, today=date(2026, 9, 27)) == []


# ── event_key 与双语合并 ──────────────────────────────────────────────────


def test_event_key_stable_across_languages():
    assert service._event_key("seasonal_sale", "Autumn Sale 2026", date(2026, 10, 1), None) == "seasonal_2026_autumn"
    assert service._event_key("seasonal_sale", "秋季特卖", date(2026, 10, 1), None) == "seasonal_2026_autumn"
    assert service._event_key("next_fest", "Steam Next Fest", date(2026, 10, 19), None) == "nextfest_2026_10"
    assert service._event_key("themed_fest", "Cooking Fest", date(2026, 10, 12), "themed_sales/cooking_2026") == "themed_cooking_2026"


def test_merge_bilingual_overlays_zh_names():
    en = [{"category": "seasonal_sale", "name": "Autumn Sale 2026",
           "start": date(2026, 10, 1), "end": date(2026, 10, 8)}]
    zh = [{"category": "seasonal_sale", "name": "秋季特卖",
           "start": date(2026, 10, 1), "end": date(2026, 10, 8)}]
    merged = service._merge_bilingual(en, zh, {})
    assert merged[0]["name_en"] == "Autumn Sale 2026"
    assert merged[0]["name_zh"] == "秋季特卖"
    merged_en_only = service._merge_bilingual(en, [], {})
    assert merged_en_only[0]["name_zh"] == "Autumn Sale 2026"


# ── 价格观测打标与窗口回贴 ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "_windows_cache", None)
    async with engine.begin() as conn:
        await conn.run_sync(database_module.Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_and_active_event_key(db):
    """回贴按窗口补齐 NULL 行（含窗口边界）；打标查询季节大促优先。"""
    from sqlalchemy import select

    async with db() as session:
        session.add(SteamEvent(
            event_key="seasonal_2026_autumn", category="seasonal_sale",
            name_en="Autumn Sale 2026", name_zh="秋季特卖",
            start_date=date(2026, 10, 1), end_date=date(2026, 10, 8),
        ))
        session.add(SteamEvent(
            event_key="themed_cooking_2026", category="themed_fest",
            name_en="Cooking Fest", start_date=date(2026, 10, 12),
            end_date=date(2026, 10, 19),
        ))
        for day, expect_key in [
            ("2026-09-30", None),
            ("2026-10-01", "seasonal_2026_autumn"),
            ("2026-10-05", "seasonal_2026_autumn"),
            ("2026-10-08", "seasonal_2026_autumn"),
            ("2026-10-09", None),
            ("2026-10-12", "themed_cooking_2026"),
        ]:
            session.add(GamePriceHistory(
                appid=1, region_code="CN", price=100,
                snapshot_at=datetime.fromisoformat(f"{day} 12:00:00"),
            ))
        await session.commit()

    rows = [{"event_key": "seasonal_2026_autumn",
             "start_date": date(2026, 10, 1), "end_date": date(2026, 10, 8)},
            {"event_key": "themed_cooking_2026",
             "start_date": date(2026, 10, 12), "end_date": date(2026, 10, 19)}]
    tagged = await service._backfill_price_history(rows)
    assert tagged == 4

    async with db() as session:
        tagged_rows = (await session.execute(select(GamePriceHistory))).scalars().all()
    by_snapshot = {r.snapshot_at.date().isoformat(): r.steam_event_key for r in tagged_rows}
    assert by_snapshot["2026-09-30"] is None
    assert by_snapshot["2026-10-01"] == "seasonal_2026_autumn"
    assert by_snapshot["2026-10-05"] == "seasonal_2026_autumn"
    assert by_snapshot["2026-10-08"] == "seasonal_2026_autumn"
    assert by_snapshot["2026-10-09"] is None
    assert by_snapshot["2026-10-12"] == "themed_cooking_2026"

    # 打标查询：窗口内命中 / 窗口外 None
    key = await service.active_event_key_at(
        datetime.fromisoformat("2026-10-05 12:00:00"))
    assert key == "seasonal_2026_autumn"
    assert await service.active_event_key_at(
        datetime.fromisoformat("2026-11-01 12:00:00")) is None

    # 同天同时落在季节大促与 Fest：季节大促优先（_load_windows 排序保证）
    async with db() as session:
        session.add(SteamEvent(
            event_key="themed_overlap_2026", category="themed_fest",
            name_en="Overlap Fest", start_date=date(2026, 10, 5),
            end_date=date(2026, 10, 20),
        ))
        await session.commit()
    service._windows_cache = None
    key_overlap = await service.active_event_key_at(
        datetime.fromisoformat("2026-10-05 12:00:00"))
    assert key_overlap == "seasonal_2026_autumn"
