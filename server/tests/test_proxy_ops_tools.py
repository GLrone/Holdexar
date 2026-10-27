"""proxy_ops 工具单测：四工具在隔离库上的真实 handler 行为与订阅链接脱敏红线。"""
import asyncio
import json

from app.domains.agent.runtime import executor
from app.domains.agent.tools.builtin import proxy_ops

SPECS = {s.name: s for s in proxy_ops.SPECS}


def _run(name, args=None):
    return asyncio.run(executor.execute(SPECS[name], args or {}))


def test_specs_contract():
    meta = [(s.name, s.group, s.risk, s.step_label) for s in proxy_ops.SPECS]
    assert meta == [
        ("subscriptions_list", "read", "low", "subsList"),
        ("subscription_sync", "read", "high", "subSync"),
        ("pool_health_history", "read", "low", "healthHistory"),
        ("pool_job_runs", "read", "low", "jobRuns"),
    ]


def test_subscriptions_list_empty_db():
    result = _run("subscriptions_list")
    assert result.status in ("ok", "empty")
    assert "http" not in json.dumps(result.data, ensure_ascii=False)


def test_subscription_row_never_leaks_url():
    """红线：即便上游传入明文订阅链接，行里也只允许域名级摘要。"""
    row = proxy_ops._sub_row({
        "id": 7,
        "kind": "clash",
        "url": "https://sub.example.com/link/SECRET_TOKEN?x=1",
        "label": "主订阅",
        "lastStats": {"alive": 3, "total": 12},
        "lastImportedAt": "2026-10-25T10:00:00",
    })
    text = json.dumps(row, ensure_ascii=False)
    assert "SECRET_TOKEN" not in text
    assert "/link/" not in text and "?" not in text
    assert "sub.example.com" in text


def test_subscription_sync_missing():
    result = _run("subscription_sync", {"sub_id": 999999})
    assert result.status == "empty"
    assert result.data.get("kind") == "empty"


def test_pool_health_history_empty_db():
    result = _run("pool_health_history")
    assert result.status in ("ok", "empty")


def test_pool_job_runs_empty_db():
    result = _run("pool_job_runs")
    assert result.status in ("ok", "empty")
    assert result.data.get("kind") in ("rows", "empty")
