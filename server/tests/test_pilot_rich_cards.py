"""领航台富卡：走势序列 / 地区对比卡 / 多游戏对比的数据层契约。"""
from __future__ import annotations

import asyncio

from app.domains.pilot import service as pilot_service
from app.domains.pilot import tools as pilot_tools


def _pts(rows):
    return [{"timestamp": f"{d}T08:00:00", "cnyFen": fen} for d, fen in rows]


def test_trend_series_keeps_only_change_points():
    series = pilot_tools._trend_series(_pts([
        ("2026-01-01", 10000), ("2026-01-02", 10000), ("2026-01-03", 5000),
        ("2026-01-04", 5000), ("2026-01-05", 10000), ("2026-01-06", 0),
    ]))
    assert series == [["2026-01-01", 10000], ["2026-01-03", 5000], ["2026-01-05", 10000]]


def test_trend_series_same_day_takes_last_value():
    series = pilot_tools._trend_series(_pts([
        ("2026-01-01", 29800), ("2026-01-01", 36800), ("2026-01-01", 56800),
        ("2026-01-02", 56800), ("2026-01-03", 100),
    ]))
    assert series == [["2026-01-01", 56800], ["2026-01-03", 100]]


def test_trend_series_downsample_keeps_lowest_and_last():
    rows = []
    for i in range(300):
        rows.append((f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", 10000 + i if i != 137 else 1))
    series = pilot_tools._trend_series(_pts(rows))
    assert len(series) <= pilot_tools._TREND_MAX_POINTS + 2
    assert min(v for _, v in series) == 1
    assert series[-1][1] == 10000 + 299
    assert [d for d, _ in series] == sorted(d for d, _ in series)


def test_model_view_drops_trend_but_card_keeps_it():
    result = {"kind": "price", "appid": 1, "name": "A", "trend": [["2026-01-01", 100]]}
    assert "trend" not in pilot_service._model_view(result)
    assert pilot_service._card_of("get_price_briefing", result)["trend"] == [["2026-01-01", 100]]


def test_regions_card_keeps_account_region_and_cheapest_sorted():
    items = [
        {"region": f"R{i}", "display": "", "cnyFen": 1000 + i * 100, "discount": 0} for i in range(12)
    ]
    items.append({"region": "CN", "display": "", "cnyFen": 9999, "discount": 0})
    card = pilot_service._card_of("get_region_prices", {
        "kind": "region_prices", "appid": 7, "name": "G", "accountRegion": "cn",
        "items": items, "count": len(items),
    })
    assert card["kind"] == "regions"
    fens = [r["cnyFen"] for r in card["items"]]
    assert fens == sorted(fens)
    assert any(r["region"] == "CN" for r in card["items"])
    assert len(card["items"]) == pilot_service._REGION_CARD_TOP + 1
    assert card["count"] == 13


def test_regions_card_empty_has_no_card():
    assert pilot_service._card_of("get_region_prices", {"kind": "region_prices", "items": []}) is None


def test_compare_games_projects_price_facts(monkeypatch):
    facts = {
        1: {"kind": "price", "appid": 1, "name": "A", "positiveRate": 0.9, "reviewCount": 100,
            "cn": {"cnyFen": 5000, "discount": 50}, "alt": None,
            "lowest": {"cnyFen": 4000, "snapshotAt": None},
            "year": {"minFen": 4000, "maxFen": 10000, "medianFen": 8000, "count": 5}},
        2: {"kind": "price", "appid": 2, "name": "B", "positiveRate": 0.7, "reviewCount": 10,
            "cn": {"cnyFen": None, "discount": 0}, "alt": {"region": "TR", "cnyFen": 3000, "discount": 20},
            "lowest": None, "year": None},
        3: None,
    }

    async def fake_price_facts(appid):
        return facts[appid]

    monkeypatch.setattr("app.domains.agent.tools.builtin.prices.price_facts", fake_price_facts)
    out = asyncio.run(pilot_tools.compare_games([1, "2", 3, 2, "x"]))
    assert out["kind"] == "compare"
    a, b = out["items"]
    assert (a["cnyFen"], a["discount"], a["region"], a["lowestFen"], a["medianFen"]) == (5000, 50, None, 4000, 8000)
    assert (b["cnyFen"], b["region"], b["lowestFen"]) == (3000, "TR", None)
    assert pilot_tools.tool_step("compare_games", out)["status"] == "ok"
    assert pilot_service._card_of("compare_games", out) == {"kind": "compare", "items": out["items"]}


def test_compare_games_needs_two_distinct_targets():
    assert asyncio.run(pilot_tools.compare_games([5, 5]))["note"] == "need_two"
    assert asyncio.run(pilot_tools.compare_games("nope"))["note"] == "need_two"


def test_compare_tool_registered():
    names = [s["function"]["name"] for s in pilot_tools.tool_specs()]
    assert "compare_games" in names
    assert pilot_tools.TOOL_META["compare_games"]["label"] == "compare"
