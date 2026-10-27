"""userview 工具契约单测：隔离库直调真实 handler（不触网、不碰注册表）。"""
import asyncio
import json

from app.domains.agent.runtime import executor
from app.domains.agent.tools.builtin.userview import SPECS

_SPECS = {s.name: s for s in SPECS}


def _run(name, args=None):
    return asyncio.run(executor.execute(_SPECS[name], args or {}))


def test_specs_shape():
    assert [s.name for s in SPECS] == [
        "account_status", "notifications_status", "notification_history",
        "achievements_near_completion", "achievements_game_detail",
        "bills_spend_query", "alert_history", "update_status",
    ]
    assert len({s.step_label for s in SPECS}) == len(SPECS)
    for s in SPECS:
        assert s.group == "read" and s.risk == "low", s.name


def test_account_status_no_credential():
    result = _run("account_status")
    assert result.status in ("ok", "empty")
    assert result.data["kind"] in ("rows", "empty")
    assert "steamLoginSecure" not in json.dumps(result.data, ensure_ascii=False)


def test_notifications_status():
    result = _run("notifications_status")
    assert result.status == "ok"
    assert result.data["kind"] == "rows"
    assert "enabled" in result.data


def test_notification_history_empty():
    result = _run("notification_history")
    assert result.status == "empty"
    assert result.data == {"kind": "empty", "items": []}


def test_alert_history_empty():
    result = _run("alert_history")
    assert result.status == "empty"


def test_achievements_views_empty():
    assert _run("achievements_near_completion").status == "empty"
    assert _run("achievements_game_detail", {"appid": 123}).status == "empty"


def test_bills_query_missing_import():
    for args in ({"import_id": 999999},
                 {"import_id": 999999, "mode": "game_txs"}):
        result = _run("bills_spend_query", args)
        assert result.status == "empty", args
        assert result.data["kind"] == "empty"


def test_update_status_readonly():
    result = _run("update_status")
    assert result.status == "ok"
    assert result.data["currentVersion"]
    assert result.data["checkedAt"] is None
