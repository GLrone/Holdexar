"""pilot 只读工具（清单与诊断）单测：属域服务全部替身注入，不触网不落库。"""
from app.domains.pilot import service as pilot_service
from app.domains.pilot import tools as pilot_tools


def _async_return(value):
    async def _fn(*a, **kw):
        return value
    return _fn


def _async_fn(fn):
    async def _fn2(*a, **kw):
        return fn(*a, **kw)
    return _fn2


def test_specs_contains_read_tools_without_required_params():
    specs = {s["function"]["name"]: s["function"] for s in pilot_tools.tool_specs()}
    for name in pilot_tools._READ_TOOLS:
        assert name in specs, name
        assert not specs[name].get("required"), name


def test_list_follows_merges_manual_and_favorite(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.monitoring_service, "ids_with_source",
        _async_return([10, 20]),
    )
    monkeypatch.setattr(
        pilot_tools.wishlist_follows, "followed_appids", _async_return([20, 30]),
    )
    monkeypatch.setattr(
        pilot_tools.games_service, "briefs_for",
        _async_return({10: {"name": "甲", "cnyFen": 9900, "discount": 0,
                            "positiveRate": 0.9, "reviewCount": 100}}),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("list_follows", {}))
    assert out["kind"] == "games"
    assert out["titleKey"] == "follows"
    assert out["total"] == 3
    notes = {it["appid"]: it["note"]["key"] for it in out["items"]}
    assert notes == {10: "manual", 20: "favorite", 30: "favorite"}
    assert out["items"][0]["cnyFen"] == 9900 and out["items"][0]["name"] == "甲"


def test_list_alerts_maps_types_and_tone(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.alerts_service, "list_alerts",
        _async_return([
            {"appid": 1, "gameName": "甲", "targetType": "price",
             "targetValue": 9900, "region": "CN", "active": True},
            {"appid": 2, "gameName": "乙", "targetType": "historic_low",
             "targetValue": None, "region": "CN", "active": False},
        ]),
    )
    out = pilot_tools.execute_tool("list_alerts", {})
    import asyncio
    out = asyncio.run(out) if asyncio.iscoroutine(out) else out
    assert [r["vKey"] for r in out["rows"]] == ["alertPrice", "alertLow"]
    assert out["rows"][0]["data"]["priceFen"] == 9900
    assert [r["tone"] for r in out["rows"]] == ["ok", "warn"]


def test_diagnose_price_projects_problems(monkeypatch):
    diag = {
        "appid": 7, "name": "甲",
        "cn": {"cnyFen": 5100, "discount": 0},
        "lowestPriceFen": 3100,
        "coverage": {
            "expectedUnits": 3, "success": 1,
            "regions": {
                "RU": {"outcome": "failed", "answer": "locked", "lastSuccessAt": "2026-10-03T14:00:00"},
                "TR": {"outcome": "notAttempted", "answer": None, "lastSuccessAt": None},
            },
        },
    }
    monkeypatch.setattr(pilot_tools.games_service, "price_diagnosis", _async_return(diag))
    out = pilot_tools.execute_tool("diagnose_price", {"appid": 7})
    import asyncio
    out = asyncio.run(out) if asyncio.iscoroutine(out) else out
    assert out["kind"] == "rows" and out["name"] == "甲"
    assert out["rows"][0]["vKey"] == "coverage"
    assert out["rows"][0]["data"] == {"ok": 1, "total": 3}
    ru = next(r for r in out["rows"] if r["k"] == "RU")
    assert ru["vKey"] == "failed" and ru["v"] == "locked" and ru["tone"] == "bad"


def test_diagnose_price_empty(monkeypatch):
    monkeypatch.setattr(pilot_tools.games_service, "price_diagnosis", _async_return(None))
    out = pilot_tools.execute_tool("diagnose_price", {"appid": 1})
    import asyncio
    out = asyncio.run(out) if asyncio.iscoroutine(out) else out
    assert out == {"kind": "empty", "note": "no_data"}


def test_recent_job_failures_filters_failed(monkeypatch):
    jobs = [
        {"id": 3, "kind": "app", "status": "failed", "error": "boom"},
        {"id": 2, "kind": "app", "status": "done", "error": None},
        {"id": 1, "kind": "manual", "status": "failed", "error": None},
    ]
    monkeypatch.setattr(pilot_tools.crawl_service, "list_jobs", _async_return(jobs))
    out = pilot_tools.execute_tool("recent_job_failures", {})
    import asyncio
    out = asyncio.run(out) if asyncio.iscoroutine(out) else out
    assert out["total"] == 2
    assert out["rows"][0]["k"] == "#3 app" and out["rows"][0]["v"] == "boom"


def test_proxy_and_gate_status_shapes(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.proxies_service, "pool_stats",
        _async_return({"pool": {"total": 4, "ok": 2},
                       "clash": {"running": True, "exitIps": 9, "okExitIps": 7}}),
    )
    monkeypatch.setattr(
        "app.domains.agent.tools.builtin.system.write_scheduler_diagnostics",
        lambda: {"busy": False, "owner_label": None,
                 "waiting_interactive": 0, "waiting_background": 2},
    )
    import asyncio

    proxy = asyncio.run(pilot_tools.execute_tool("proxy_pool_status", {}))
    assert proxy["rows"][0]["data"] == {"ok": 2, "total": 4}
    assert proxy["rows"][2]["vKey"] == "proxyRunning"

    gate = asyncio.run(pilot_tools.execute_tool("write_gate_status", {}))
    assert gate["rows"][0]["vKey"] == "gateIdle"
    assert gate["rows"][1]["data"] == {"n": 2}
    assert gate["rows"][1]["tone"] == "warn"


def test_read_tools_ignore_guard(monkeypatch):
    """只读工具不受守卫词影响：guarded=True 仍执行。"""
    monkeypatch.setattr(
        pilot_tools.monitoring_service, "ids_with_source", _async_return([]),
    )
    monkeypatch.setattr(
        pilot_tools.wishlist_follows, "followed_appids", _async_return([]),
    )
    monkeypatch.setattr(pilot_tools.games_service, "briefs_for", _async_return({}))
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("list_follows", {}, guarded=True))
    assert out["kind"] == "games" and out["items"] == []


def test_tool_step_and_card_for_games_list():
    result = {"kind": "games", "titleKey": "follows", "total": 2,
              "items": [{"appid": 10, "name": "甲", "cnyFen": None, "discount": 0,
                         "positiveRate": None, "reviewCount": None, "note": {"key": "manual"}},
                        {"appid": 20, "name": "乙", "cnyFen": None, "discount": 0,
                         "positiveRate": None, "reviewCount": None, "note": {"key": "favorite"}}]}
    step = pilot_tools.tool_step("list_follows", result)
    assert step == {"label": "follows", "status": "ok", "data": {"count": 2}}
    empty = pilot_tools.tool_step("list_follows", {"kind": "games", "titleKey": "follows", "items": []})
    assert empty["status"] == "empty"
    card = pilot_service._card_of("list_follows", result)
    assert card["kind"] == "games" and card["titleKey"] == "follows" and card["total"] == 2
    assert card["items"][0]["note"] == {"key": "manual"}


def test_hb_monthly_projects_price_and_low(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.metadata_service, "hb_choice_offers",
        _async_return({
            "ok": True, "label": "HB慈善包26年10月包",
            "games": [
                {"appid": 1, "name": "甲", "priceFen": 5100, "lowestCnyFen": 3100, "discount": 0},
                {"appid": 2, "name": "乙", "priceFen": 9900, "lowestCnyFen": 4900, "discount": 30},
            ],
        }),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("hb_monthly", {}))
    assert out["titleKey"] == "hb" and out["total"] == 2
    assert out["rows"][0]["vKey"] == "offerLow"
    assert out["rows"][0]["data"] == {"priceFen": 5100, "lowFen": 3100}
    assert out["rows"][1]["vKey"] == "offerDiscount" and out["rows"][1]["v"] == "-30%"


def test_hb_monthly_empty(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.metadata_service, "hb_choice_offers",
        _async_return({"ok": False, "error": "当月包尚未入库"}),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("hb_monthly", {}))
    assert out["rows"] == [] and out["total"] == 0


def test_epic_and_steam_free_rows(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.metadata_service, "epic_free_offers",
        _async_return({"ok": True, "offers": [
            {"title": "Alpha", "titleCn": "阿尔法", "upcoming": False, "end": "2026-10-09"},
            {"title": "Beta", "titleCn": None, "appid": 7, "upcoming": True, "start": "2026-10-10"},
        ]}),
    )
    monkeypatch.setattr(
        pilot_tools.metadata_service, "steam_free_offers",
        _async_return({"ok": True, "games": [
            {"appid": 3, "name": "丙", "priceFen": 9900},
        ]}),
    )
    import asyncio
    epic = asyncio.run(pilot_tools.execute_tool("epic_free", {}))
    assert [r["vKey"] for r in epic["rows"]] == ["epicFree", "epicUpcoming"]
    assert epic["rows"][0]["k"] == "阿尔法"
    free = asyncio.run(pilot_tools.execute_tool("steam_free", {}))
    assert free["rows"][0]["k"] == "丙" and free["rows"][0]["vKey"] == "offerPrice"


def test_price_drops_maps_names_and_types(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.crawl_events_service, "list_events",
        _async_return([
            {"appid": 1, "eventType": "new_historical_low", "region": "CN",
             "occurredAt": "2026-10-04T12:00:00"},
            {"appid": 2, "eventType": "region_locked", "region": "RU",
             "occurredAt": "2026-10-04T11:00:00"},
        ]),
    )
    monkeypatch.setattr(
        pilot_tools.games_service, "briefs_for",
        _async_return({1: {"name": "甲", "cnyFen": 3100, "discount": 50,
                           "positiveRate": None, "reviewCount": None}}),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("price_drops", {}))
    assert out["kind"] == "games" and out["titleKey"] == "drops"
    assert out["items"][0]["name"] == "甲" and out["items"][0]["discount"] == 50
    assert out["items"][0]["note"]["key"] == "ev_new_historical_low"
    assert out["items"][1]["note"]["key"] == "ev_region_locked"
    assert out["items"][1]["note"]["v"] == "RU"


def test_rates_and_calendar_rows(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.rates_service, "list_rates",
        _async_return({"rates": [{"currency": "USD", "rateToCny": 7.24, "fetchedAt": None}]}),
    )
    monkeypatch.setattr(
        pilot_tools.steam_events_service, "list_events",
        _async_return({
            "live": [{"nameZh": "秋促", "nameEn": None, "key": "autumn", "start": "2026-10-02", "end": "2026-10-09"}],
            "upcoming": [{"nameZh": None, "nameEn": "Winter", "key": "winter", "start": "2026-12-20"}],
        }),
    )
    import asyncio
    rates = asyncio.run(pilot_tools.execute_tool("rates_overview", {}))
    assert rates["rows"][0] == {"k": "USD", "vKey": "rateLine", "data": {"rate": 7.24}}
    cal = asyncio.run(pilot_tools.execute_tool("calendar_events", {}))
    assert cal["rows"][0]["vKey"] == "eventLive" and cal["rows"][0]["tone"] == "ok"
    assert cal["rows"][1]["k"] == "Winter" and cal["rows"][1]["vKey"] == "eventUpcoming"


def test_top_games_uses_board_and_names(monkeypatch):
    class _Boards:
        @staticmethod
        async def get_board(key):
            assert key == "topsellers"
            return [10, 20, 30]
    import sys
    monkeypatch.setitem(sys.modules, "app.domains.games.boards", _Boards)
    import importlib
    importlib.reload(pilot_tools)
    monkeypatch.setattr(
        pilot_tools.games_service, "briefs_for",
        _async_return({10: {"name": "甲", "cnyFen": 6200, "discount": 0,
                            "positiveRate": 0.97, "reviewCount": 660000}}),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("top_games", {}))
    assert out["kind"] == "games" and out["titleKey"] == "top"
    assert out["total"] == 3
    assert out["items"][0]["name"] == "甲" and out["items"][0]["appid"] == 10
    assert out["items"][2]["appid"] == 30 and out["items"][2]["name"] is None
    # 还原真实模块，避免影响后续用例
    import app.domains.games.boards as real_boards
    monkeypatch.setitem(sys.modules, "app.domains.games.boards", real_boards)
    importlib.reload(pilot_tools)


def test_list_accounts_projects_names_only(monkeypatch):
    """账号清单投影名称/好友码/统计，不带钱包与登录态字段。"""
    monkeypatch.setattr(
        pilot_tools.account_service, "list_accounts",
        _async_return([
            {"steam_id": "765", "persona_name": "雾渃", "is_primary": True,
             "friend_code": "493902940", "wishlist_count": 12, "game_count": 768,
             "wallet_frozen": True, "session_expired": False},
            {"steam_id": "766", "persona_name": None, "is_primary": False,
             "wishlist_count": 3, "game_count": 0},
        ]),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("list_accounts", {}))
    assert out["total"] == 2
    assert out["rows"][0]["k"] == "雾渃" and out["rows"][0]["vKey"] == "acctPrimary"
    assert out["rows"][0]["data"] == {"friend": "493902940", "wish": 12, "owned": 768}
    assert out["rows"][1]["k"] == "766" and out["rows"][1]["vKey"] == "acctActive"
    assert out["rows"][1]["data"]["friend"] == ""


def test_wishlist_and_owned_filter_by_source(monkeypatch):
    items = [
        {"appid": 1, "name": "甲", "wishlisted": True, "owned": False, "cnPriceFen": None},
        {"appid": 2, "name": "乙", "wishlisted": True, "owned": True, "cnPriceFen": 9900},
        {"appid": 3, "name": "丙", "wishlisted": False, "owned": True, "cnPriceFen": 0},
        {"appid": 4, "name": "丁", "wishlisted": False, "owned": False},
    ]
    monkeypatch.setattr(pilot_tools.wishlist_service, "list_items", _async_return(items))

    def _briefs(ids):
        table = {
            1: {"name": "甲", "cnyFen": None, "discount": 0, "positiveRate": None, "reviewCount": None},
            2: {"name": "乙", "cnyFen": 9900, "discount": 0, "positiveRate": None, "reviewCount": None},
            3: {"name": "丙", "cnyFen": 4800, "discount": 0, "positiveRate": None, "reviewCount": None},
        }
        return {i: table[i] for i in ids}

    import asyncio
    monkeypatch.setattr(pilot_tools.games_service, "briefs_for", _async_fn(_briefs))
    wish = asyncio.run(pilot_tools.execute_tool("list_wishlist", {}))
    assert [it["appid"] for it in wish["items"]] == [1, 2] and wish["total"] == 2
    owned = asyncio.run(pilot_tools.execute_tool("list_owned", {}))
    assert [it["appid"] for it in owned["items"]] == [2, 3]
    assert owned["items"][0]["cnyFen"] == 9900


def test_bills_and_redeem_rows(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.bills_service, "list_imports",
        _async_return([
            {"id": 1, "nickname": "雾渃", "sourceFile": "a.csv", "importedAt": "2026-10-01T00:00:00",
             "gameNetFen": 100, "gameSpendFen": 25000, "gameRefundFen": 0, "orders": 8, "fxMissing": 0},
        ]),
    )
    monkeypatch.setattr(
        pilot_tools.redeem_service, "quota_status",
        _async_return({"hasCookie": True, "used": 3, "limit": 10, "steamId": "765"}),
    )
    import asyncio
    bills = asyncio.run(pilot_tools.execute_tool("bills_summary", {}))
    assert bills["rows"][0]["k"] == "雾渃"
    assert bills["rows"][0]["data"] == {"spendFen": 25000}
    assert bills["rows"][0]["v"] == "8单"
    redeem = asyncio.run(pilot_tools.execute_tool("redeem_quota", {}))
    assert redeem["rows"][0]["vKey"] == "redeemQuota"
    assert redeem["rows"][0]["data"] == {"used": 3, "limit": 10}
    assert redeem["rows"][0]["tone"] == "ok"


def test_family_unbound_and_bound(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.family_service, "get_status",
        _async_return({"bound": False, "message": "尚未绑定"}),
    )
    import asyncio
    unbound = asyncio.run(pilot_tools.execute_tool("family_status", {}))
    assert unbound["kind"] == "family" and unbound["bound"] is False

    monkeypatch.setattr(
        pilot_tools.family_service, "get_status",
        _async_return({
            "bound": True, "primarySteamid": "765", "joined": True, "walletRegion": "CN",
            "groups": [{"accountName": "雾渃", "joined": True, "members": [
                {"steamid": "1", "role": "primary", "personaName": "雾渃", "avatarUrl": "", "region": "CN"},
                {"steamid": "2", "role": "member", "personaName": "成员乙", "avatarUrl": "", "region": None},
            ]}],
            "members": [],
        }),
    )
    bound = asyncio.run(pilot_tools.execute_tool("family_status", {}))
    assert bound["kind"] == "family" and bound["walletRegion"] == "CN"
    assert [m["name"] for m in bound["members"]] == ["雾渃", "成员乙"]
    assert bound["members"][0]["role"] == "primary" and bound["members"][0]["region"] == "CN"


def test_list_family_library_games_card(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.family_service, "cached_family_library",
        _async_return({"games": [{"appid": 10, "name": "甲"}, {"appid": 20, "name": "乙"}],
                       "sharedCount": 2}))
    monkeypatch.setattr(
        pilot_tools.games_service, "briefs_for",
        _async_return({10: {"name": "甲", "cnyFen": 4800, "discount": 0,
                            "positiveRate": None, "reviewCount": None}}),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("list_family_library", {}))
    assert out["kind"] == "games" and out["titleKey"] == "famLibrary"
    assert out["total"] == 2 and out["items"][0]["cnyFen"] == 4800


def test_achievements_card_shape(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.achievements_service, "get_summary",
        _async_return({
            "hasCredential": True, "platinum": 3,
            "unlockedAchievements": 29122, "totalAchievements": 58400,
            "completionRate": 49.9, "lastSyncedAt": "2026-10-04T10:00:00",
            "platinums": [{"appid": 413150, "name": "Stardew Valley", "headerImage": "", "playtimeMin": 1,
                           "total": 40, "date": 0, "source": "owned"}],
            "recentUnlocks": [{"appid": 1245620, "unlockTime": 1759500000, "name": "生态学家",
                               "imageName": None, "icon": "", "globalPercent": 8.0,
                               "gameName": "Dave the Diver"}],
        }))
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("achievements_summary", {}))
    assert out["kind"] == "achievements"
    assert out["platinum"] == 3 and out["unlocked"] == 29122
    assert out["platinums"][0]["appid"] == 413150
    assert out["recent"][0]["gameName"] == "Dave the Diver"


def test_list_bundles_rows(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.bundles_service, "list_bundles",
        _async_return([
            {"bundleId": 9, "name": "黑魂合集", "cnCnyFen": 19900, "lowestRegion": "AR", "lowestCnyFen": 8900, "diffFen": 11000},
        ]),
    )
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("list_bundles", {}))
    assert out["titleKey"] == "bundlesAll" and out["total"] == 1
    row = out["rows"][0]
    assert row["k"] == "黑魂合集" and row["vKey"] == "bundleLow"
    assert row["data"]["priceFen"] == 19900 and row["data"]["lowFen"] == 8900
    assert row["data"]["bundleId"] == 9 and row["v"] == "AR"


def test_update_price_alert_paths(monkeypatch):
    """改阈值 / 启停 / 不存在三分支；阈值元转分、名字回填。"""
    updated = {"appid": 292030, "targetValue": 5000, "id": 3, "active": True}
    calls = []

    async def fake_update(alert_id, **kw):
        calls.append((alert_id, kw))
        if alert_id == 404:
            raise ValueError("提醒不存在")
        return dict(updated, targetValue=kw.get("target_value") or updated["targetValue"])

    monkeypatch.setattr(pilot_tools.alerts_service, "update_alert", fake_update)
    monkeypatch.setattr(pilot_tools.games_service, "names_for", _async_return({292030: "巫师 3"}))
    import asyncio

    out = asyncio.run(pilot_tools.execute_tool(
        "update_price_alert", {"alert_id": 3, "target_value_yuan": 50}))
    assert calls[-1] == (3, {"active": None, "target_value": 5000})
    assert out["rows"][0]["k"] == "巫师 3" and out["rows"][0]["vKey"] == "alertUpdated"
    assert out["rows"][0]["data"]["priceFen"] == 5000

    out = asyncio.run(pilot_tools.execute_tool(
        "update_price_alert", {"alert_id": 3, "active": False}))
    assert out["rows"][0]["vKey"] == "alertOff" and out["rows"][0]["tone"] == "warn"

    out = asyncio.run(pilot_tools.execute_tool("update_price_alert", {"alert_id": 404}))
    assert out == {"kind": "empty", "note": "alert_not_found"}


def test_bundle_follow_toggle(monkeypatch):
    seen = []

    async def fake_follow(bundle_id):
        seen.append(("follow", bundle_id))

    async def fake_unfollow(bundle_id):
        seen.append(("unfollow", bundle_id))

    monkeypatch.setattr(pilot_tools.bundles_service, "follow_bundle", fake_follow)
    monkeypatch.setattr(pilot_tools.bundles_service, "unfollow_bundle", fake_unfollow)
    monkeypatch.setattr(
        pilot_tools.bundles_service, "names_for", _async_return({9: "黑魂合集"}))
    import asyncio

    out = asyncio.run(pilot_tools.execute_tool("bundle_follow", {"bundle_id": 9}))
    assert seen[-1] == ("follow", 9) and out["rows"][0]["vKey"] == "bundleFollowed"
    out = asyncio.run(pilot_tools.execute_tool(
        "bundle_follow", {"bundle_id": 9, "follow": False}))
    assert seen[-1] == ("unfollow", 9) and out["rows"][0]["vKey"] == "bundleUnfollowed"


def test_region_toggle_read_modify_write(monkeypatch):
    """启用集读改写：±1 后整集写回；全停被拒；未知区拒。"""
    current = {"codes": ["cn", "ru", "ua", "tr"]}

    async def fake_enabled():
        return list(current["codes"])

    async def fake_set(codes):
        current["codes"] = list(codes)

    monkeypatch.setattr(pilot_tools.regions_service, "enabled_regions", fake_enabled)
    monkeypatch.setattr(pilot_tools.regions_service, "set_enabled", fake_set)
    import asyncio

    out = asyncio.run(pilot_tools.execute_tool(
        "region_toggle", {"region_code": "UA", "enable": False}))
    assert current["codes"] == ["cn", "ru", "tr"]
    assert out["rows"][0]["k"] == "UA" and out["rows"][0]["vKey"] == "regionOff"

    out = asyncio.run(pilot_tools.execute_tool(
        "region_toggle", {"region_code": "ua", "enable": True}))
    assert "ua" in current["codes"] and out["rows"][0]["vKey"] == "regionOn"

    # 全停被拒：停到最后一个区时工具拒绝，写不发生
    for code in ["cn", "ru", "tr"]:
        asyncio.run(pilot_tools.execute_tool(
            "region_toggle", {"region_code": code, "enable": False}))
    assert current["codes"] == ["ua"]
    out = asyncio.run(pilot_tools.execute_tool(
        "region_toggle", {"region_code": "ua", "enable": False}))
    assert out["rows"][0]["vKey"] == "regionMinOne"
    assert current["codes"] == ["ua"]

    out = asyncio.run(pilot_tools.execute_tool(
        "region_toggle", {"region_code": "xx", "enable": True}))
    assert out["rows"][0]["vKey"] == "regionUnknown"


def test_sync_tools_rows(monkeypatch):
    import asyncio

    monkeypatch.setattr(
        pilot_tools.account_service, "sync_wallet",
        _async_return({"ok": True}))
    out = asyncio.run(pilot_tools.execute_tool("sync_wallet", {}))
    assert out["rows"][0]["vKey"] == "walletSynced"

    monkeypatch.setattr(
        pilot_tools.wishlist_service, "sync_account",
        _async_return({"ok": False, "error": "cookie 失效"}))
    out = asyncio.run(pilot_tools.execute_tool("sync_library", {}))
    assert out["rows"][0]["vKey"] == "syncFailed" and out["rows"][0]["v"] == "cookie 失效"

    async def _running(steamid):
        raise ValueError("running")

    monkeypatch.setattr(pilot_tools.achievements_service, "start_sync", _running)
    out = asyncio.run(pilot_tools.execute_tool("sync_achievements", {}))
    assert out["rows"][0]["vKey"] == "syncRunning"

    monkeypatch.setattr(
        pilot_tools.bills_service, "sync_bills",
        _async_return({"ok": False, "status": "no_cookie"}))
    out = asyncio.run(pilot_tools.execute_tool("sync_bills", {}))
    assert out["rows"][0]["vKey"] == "billNoCookie"

    monkeypatch.setattr(
        pilot_tools.rates_service, "refresh_rates", _async_return({"rates": []}))
    out = asyncio.run(pilot_tools.execute_tool("refresh_rates", {}))
    assert out["rows"][0]["vKey"] == "syncDone"


def test_write_tools_denied_on_guard_and_cap(monkeypatch):
    """守卫词拒写 + 单轮第三个写动作拒（上限由服务层传 guarded 实现）。"""
    async def _no_track(target_type, target_id, source):
        return "active"

    monkeypatch.setattr(pilot_tools.monitoring_service, "track", _no_track)
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool(
        "add_follow", {"appid": 1}, guarded=True))
    assert out == {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}
    out = asyncio.run(pilot_tools.execute_tool(
        "update_price_alert", {"alert_id": 1}, guarded=True))
    assert out == {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}
    out = asyncio.run(pilot_tools.execute_tool(
        "region_toggle", {"region_code": "ru", "enable": False}, guarded=True))
    assert out == {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}


# ─── Steam 检索与入库（search_steam / ingest_appids）───
import asyncio

def test_search_steam_parses_store_items(monkeypatch):
    monkeypatch.setattr(
        pilot_tools.proxies_service, "resolve_proxy_url", _async_return("http://127.0.0.1:7897"))
    monkeypatch.setattr("app.domains.agent.tools.builtin.catalog._steam_storesearch", _async_return({
        "items": [
            {"id": 2680010, "name": "The First Berserker: Khazan",
             "price": {"final": 2099, "initial": 3499, "currency": "USD"}},
            {"id": 2443510, "name": "DEMO",
             "price": {"final": 29800, "initial": 29800, "currency": "CNY"}},
            {"id": "bad", "name": "坏行"},
        ]}))
    out = asyncio.run(pilot_tools.execute_tool("search_steam", {"term": "Khazan"}))
    assert out["kind"] == "games"
    assert [it["appid"] for it in out["items"]] == [2680010, 2443510]
    # US 视图美元价不进 cnyFen；折扣照读；CNY 币种才带 cnyFen
    assert out["items"][0]["cnyFen"] is None and out["items"][0]["discount"] == 40
    assert out["items"][1]["cnyFen"] == 29800 and out["items"][1]["discount"] == 0


def test_search_steam_empty_term_and_failure(monkeypatch):
    out = asyncio.run(pilot_tools.execute_tool("search_steam", {"term": "  "}))
    assert out == {"kind": "empty", "note": "bad_term"}
    monkeypatch.setattr(
        pilot_tools.proxies_service, "resolve_proxy_url", _async_return(None))
    monkeypatch.setattr("app.domains.agent.tools.builtin.catalog._steam_storesearch", _async_return(None))
    out = asyncio.run(pilot_tools.execute_tool("search_steam", {"term": "khazan"}))
    assert out == {"kind": "empty", "note": "search_failed"}


def test_ingest_appids_imports_and_starts_first_crawl(monkeypatch):
    started = []

    async def _start_job(*, scope, appids, kind):
        started.append((scope, tuple(appids), kind))
        return {"id": 1, "count": len(appids)}

    monkeypatch.setattr(
        pilot_tools.crawl_service, "import_appids",
        _async_return({"results": [
            {"appid": 1794880, "status": "ok"},
            {"appid": 620, "status": "own"},
            {"appid": 99, "status": "fail"},
        ], "ok": 1, "own": 1, "fail": 1}))
    monkeypatch.setattr(pilot_tools.crawl_service, "start_job", _start_job)
    monkeypatch.setattr(
        pilot_tools.games_service, "names_for", _async_return({1794880: "卡赞"}))
    out = asyncio.run(pilot_tools.execute_tool(
        "ingest_appids", {"appids": [1794880, 620, 99, "x", 0, 1794880]}))
    assert started == [("appids", (1794880,), "import")]
    vkeys = {r["v"]: r["vKey"] for r in out["rows"]}
    assert vkeys == {"#1794880": "ingestQueued", "#620": "ingestOwned", "#99": "ingestFail"}
    assert out["titleKey"] == "ingest"


def test_ingest_appids_job_busy_still_imports(monkeypatch):
    async def _busy(*a, **kw):
        raise RuntimeError("已有任务在跑")

    monkeypatch.setattr(
        pilot_tools.crawl_service, "import_appids",
        _async_return({"results": [{"appid": 5, "status": "ok"}], "ok": 1, "own": 0, "fail": 0}))
    monkeypatch.setattr(pilot_tools.crawl_service, "start_job", _busy)
    monkeypatch.setattr(pilot_tools.games_service, "names_for", _async_return({}))
    out = asyncio.run(pilot_tools.execute_tool("ingest_appids", {"appids": [5]}))
    assert out["rows"][0]["vKey"] == "ingestImported"


def test_ingest_appids_caps_and_requires_targets(monkeypatch):
    out = asyncio.run(pilot_tools.execute_tool("ingest_appids", {"appids": []}))
    assert out == {"kind": "empty", "note": "no_target"}
    out = asyncio.run(pilot_tools.execute_tool(
        "ingest_appids", {"appids": list(range(1, 30))}))
    assert out["note"] == "too_many" and out["data"]["max"] == pilot_tools._INGEST_MAX


def test_specs_new_tools_registered():
    specs = {s["function"]["name"]: s["function"] for s in pilot_tools.tool_specs()}
    assert "search_steam" in specs and "ingest_appids" in specs
    assert specs["ingest_appids"]["parameters"]["required"] == ["appids"]
    assert "search_steam" in specs["search_games"]["description"]


def test_search_games_uses_catalog_search(monkeypatch):
    """search_games 走目录级检索：锁区/未爬价游戏可见（卡赞判例）。"""
    seen = {}

    async def _fake(term, limit=5):
        seen["term"] = term
        return [{"appid": 2680010, "name": "The First Berserker: Khazan",
                 "basePriceFen": None, "discount": 0,
                 "positiveRate": 0.92, "reviewCount": 30000}]

    monkeypatch.setattr(pilot_tools.games_service, "search_catalog", _fake)
    out = asyncio.run(pilot_tools.execute_tool("search_games", {"q": "卡赞"}))
    assert seen["term"] == "卡赞"
    assert out["kind"] == "games" and out["items"][0]["appid"] == 2680010


def test_search_games_full_sentence_passthrough(monkeypatch):
    """整句自然语言原样透传 search_catalog（bigram OR 通道在 service 层消解句子）。"""
    seen = {}

    async def _fake(term, limit=5):
        seen["term"] = term
        return []

    monkeypatch.setattr(pilot_tools.games_service, "search_catalog", _fake)
    out = asyncio.run(
        pilot_tools.execute_tool("search_games", {"q": "有没有类似只狼的动作游戏"})
    )
    assert seen["term"] == "有没有类似只狼的动作游戏"
    assert out["kind"] == "games" and out["items"] == []


def test_price_facts_offers_alt_region_when_cn_locked(monkeypatch):
    """国区锁区：cn 空 + alt=最低可购区（消费 detail 的 lowest 事实）。"""
    detail = {
        "appid": 2680010, "name": "The First Berserker: Khazan",
        "positiveRate": 0.92, "reviewCount": 30000,
        "priceMatrix": {"RU": ["RUB 2 147", 17212, 214700, 40],
                        "AR": ["ARS 2 099", 14074, 2099, 40]},
        "lowestCnyFen": 14074, "lowestRegionCode": "ar",
    }
    monkeypatch.setattr(pilot_tools.games_service, "get_game_detail", _async_return(detail))

    async def _ctx(appid, date, region="cn"):
        return {"at": None, "lowest": None}

    async def _hist(appid, region="cn", days=365):
        return {"points": []}

    monkeypatch.setattr(pilot_tools.games_service, "get_price_context", _ctx)
    monkeypatch.setattr(pilot_tools.games_service, "get_game_history", _hist)
    out = asyncio.run(pilot_tools.price_facts(2680010))
    assert out["cn"]["cnyFen"] is None
    assert out["alt"] == {"region": "AR", "cnyFen": 14074, "discount": 40}


def test_price_facts_no_alt_when_cn_ok(monkeypatch):
    detail = {
        "appid": 1, "name": "甲", "positiveRate": None, "reviewCount": None,
        "priceMatrix": {"CN": ["¥62", 6200, 6200, 0], "RU": ["RUB", 17212, 214700, 40]},
        "lowestCnyFen": 6200, "lowestRegionCode": "",
    }
    monkeypatch.setattr(pilot_tools.games_service, "get_game_detail", _async_return(detail))

    async def _ctx(appid, date, region="cn"):
        return {"at": {"cnyFen": 6200, "discount": 0}, "lowest": {"cnyFen": 3100, "snapshotAt": "2026-05-11"}}

    async def _hist(appid, region="cn", days=365):
        return {"points": [{"cnyFen": 6200}]}

    monkeypatch.setattr(pilot_tools.games_service, "get_price_context", _ctx)
    monkeypatch.setattr(pilot_tools.games_service, "get_game_history", _hist)
    out = asyncio.run(pilot_tools.price_facts(1))
    assert out["cn"]["cnyFen"] == 6200 and out["alt"] is None


def test_run_tool_quick_path(monkeypatch):
    """快捷直达：白名单内执行并返回步骤/卡片；白名单外拒绝。"""
    from app.domains.pilot import service as pilot_service

    monkeypatch.setattr(
        pilot_tools.rates_service, "list_rates",
        _async_return({"rates": [{"currency": "USD", "rateToCny": 7.24, "fetchedAt": None}]}),
    )
    import asyncio
    out = asyncio.run(pilot_service.run_tool("rates_overview", label="x", session_id=None))
    assert out["step"]["label"] == "rates"
    assert out["cards"][0]["kind"] == "rows"
    assert out["cards"][0]["titleKey"] == "rates"
    try:
        asyncio.run(pilot_service.run_tool("diagnose_price", label="x", session_id=None))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_parse_ddg_extracts_results():
    html = (
        '<div class="result results_links">'
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">'
        'Example <b>Title</b></a>'
        '<a class="result__snippet" href="">示例摘要内容</a>'
        '</div>'
    )
    out = pilot_tools._parse_ddg(html)
    assert len(out) == 1
    assert out[0]["title"] == "Example Title"
    assert out[0]["url"] == "https://example.com/a"
    assert out[0]["snippet"] == "示例摘要内容"


def test_refresh_price_busy_path(monkeypatch):
    async def _busy(**kw):
        raise RuntimeError("已有爬取任务在运行")

    monkeypatch.setattr(pilot_tools.crawl_service, "start_job", _busy)
    monkeypatch.setattr(pilot_tools.games_service, "names_for", _async_return({7: "甲"}))
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("refresh_game_price", {"appid": 7}))
    assert out["rows"][0]["vKey"] == "refreshBusy" and out["rows"][0]["tone"] == "warn"


def test_notify_test_failure_path(monkeypatch):
    async def _fail(**kw):
        raise ValueError("SMTP 535")

    monkeypatch.setattr("app.domains.agent.tools.builtin.system.get_smtp_config", _async_return({
        "host": "h", "port": 465, "user": "u", "password": "p", "to_addr": "t", "use_ssl": True,
    }))
    monkeypatch.setattr("app.domains.agent.tools.builtin.system.send_test_mail", _fail)
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("notify_test", {}))
    assert out["rows"][0]["vKey"] == "notifyFailed" and out["rows"][0]["tone"] == "bad"


def test_notify_test_no_config(monkeypatch):
    async def _empty_cfg():
        return {"host": "", "port": 0, "user": "", "password": "", "to_addr": "", "use_ssl": True}

    monkeypatch.setattr("app.domains.agent.tools.builtin.system.get_smtp_config", _empty_cfg)
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("notify_test", {}))
    assert out["rows"][0]["vKey"] == "notifyNoConfig" and out["rows"][0]["tone"] == "warn"


def test_find_deletables_creates_delete_proposal(monkeypatch):
    """清理检查：候选带原因码落删除提议；空候选直接说明无可清理。"""
    scanned = [
        {"key": "alert:3", "appid": 292030, "name": "巫师 3", "reason": "removed"},
        {"key": "bill_import:9", "name": "雾渃 10.2.csv", "reason": "dupImport"},
    ]
    monkeypatch.setattr("app.domains.agent.tools.builtin.tracking._scan_deletables", _async_return(scanned))
    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("find_deletables", {}, sid="del-s1"))
    assert out["kind"] == "proposal" and out["action"] == "delete"
    assert out["items"][0]["reason"] == "removed"
    assert out["items"][1]["key"] == "bill_import:9"

    monkeypatch.setattr("app.domains.agent.tools.builtin.tracking._scan_deletables", _async_return([]))
    empty = asyncio.run(pilot_tools.execute_tool("find_deletables", {}, sid="del-s1"))
    assert empty == {"kind": "empty", "note": "nothing_to_delete"}


def test_confirm_delete_executes_by_key(monkeypatch):
    """删除确认：按 key 类型分发到既有删除函数；逐项成败随结果返回。"""
    calls: list[str] = []

    async def _del_alert(alert_id):
        calls.append(f"alert:{alert_id}")
        return True

    async def _del_import(import_id):
        calls.append(f"bill_import:{import_id}")

    async def _unfollow(appid):
        calls.append(f"follow:{appid}")
        return True

    monkeypatch.setattr(pilot_tools.alerts_service, "delete_alert", _del_alert)
    monkeypatch.setattr(pilot_tools.bills_service, "delete_import", _del_import)
    monkeypatch.setattr(pilot_tools.wishlist_follows, "unfollow", _unfollow)

    items = [
        {"key": "alert:3", "name": "巫师 3", "reason": "removed", "appid": 292030},
        {"key": "bill_import:9", "name": "账单", "reason": "dupImport"},
        {"key": "follow:530", "name": "甲", "reason": "userAsk"},
        {"key": "bogus:1", "name": "坏项", "reason": "userAsk"},
    ]
    import asyncio
    out = asyncio.run(pilot_service.run_tool.__wrapped__ if False else pilot_tools.execute_tool(
        "propose_delete", {"items": items}, sid="del-s2"))
    assert out["kind"] == "proposal" and out["state"] == "pending"
    # 坏 key 在提议创建时即被白名单剔除（sanitize at creation），不进待定库
    assert [it["key"] for it in out["items"]] == ["alert:3", "bill_import:9", "follow:530"]
    # appid 透传（前端删除行渲染封面用）；无 appid 的项（账单）不造键
    assert out["items"][0]["appid"] == 292030
    assert "appid" not in out["items"][1]
    res = asyncio.run(pilot_service.confirm_proposal("del-s2", out["pid"], True))
    assert res["ok"] is True and res["total"] == 3
    assert res["done"] == 3 and res["failed"] == []
    assert calls == ["alert:3", "bill_import:9", "follow:530"]


def test_audit_follows_workflow_generates_stepper_and_proposal(monkeypatch):
    monkeypatch.setattr(pilot_tools.monitoring_service, "ids_with_source", _async_return([100, 200]))
    monkeypatch.setattr(pilot_tools.wishlist_follows, "followed_appids", _async_return([200, 300]))
    monkeypatch.setattr(
        pilot_tools.games_service, "briefs_for",
        _async_return({
            100: {"appid": 100, "name": "Game A", "cnyFen": 19900, "discount": 0},
            200: {"appid": 200, "name": "Game B", "cnyFen": 9900, "discount": 50},
            300: {"appid": 300, "name": "Game C", "cnyFen": 4500, "discount": 20},
        }),
    )
    monkeypatch.setattr(pilot_tools.alerts_service, "list_alerts", _async_return([{"appid": 100, "active": True}]))

    import asyncio
    out = asyncio.run(pilot_tools.execute_tool("audit_follows_workflow", {}, sid="wf-s1"))
    assert out["kind"] == "stepper"
    stepper = out["stepper"]
    assert stepper["kind"] == "stepper"
    assert len(stepper["steps"]) == 3
    assert stepper["steps"][0]["status"] == "ok"
    assert stepper["steps"][1]["status"] == "ok"
    assert "proposal" in out
    prop = out["proposal"]
    assert prop["kind"] == "proposal"
    assert prop["action"] == "create_price_alert"
    assert len(prop["items"]) >= 1

