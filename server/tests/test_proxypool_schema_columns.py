"""schema 缺列登记 + 老库自愈。

`subscription_snapshots` 的 HTTP、出口和尝试诊断字段必须登记进
`_TABLE_EXTRA_COLUMNS`：老形状的库缺列时，首次同步的
INSERT 直接报 `table subscription_snapshots has no column named http_status`。

本文件钉两件事：
1. 这些列确实登记在 `_TABLE_EXTRA_COLUMNS["subscription_snapshots"]`（防被误删）；
2. 老形状的库跑一次 `init_db()` 就能被补齐，且补齐后 `persist_snapshot` 可成功写入。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
import yaml

from app.core.database import _TABLE_EXTRA_COLUMNS, Base, get_session_factory, init_db

NOW = datetime(2026, 9, 20, 12, 0, 0)

# 真实产品库那张表的旧形状（= 缺 3 列时的实际列集）
LEGACY_DDL = """
CREATE TABLE subscription_snapshots (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    format VARCHAR(16) NOT NULL,
    raw_path VARCHAR(500),
    url VARCHAR(500),
    node_count INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    error TEXT,
    fetched_at DATETIME
)
"""


def test_snapshot_extra_columns_are_registered():
    """真实生产缺的 3 列必须登记在册（未登记 → 启动不会补，首次同步必失败）。"""
    reg = _TABLE_EXTRA_COLUMNS["subscription_snapshots"]
    for col in (
        "http_status", "content_type", "source_channel", "source_egress",
        "profile_title", "content_disposition", "userinfo", "duration_ms",
        "attempts_json", "provider_nodes_json",
    ):
        assert col in reg, f"{col} 未登记进 _TABLE_EXTRA_COLUMNS"
    # 登记口径与模型一致（只登记加列；不碰 SCHEMA_VERSION / 迁移链）
    model_cols = {c.name for c in Base.metadata.tables["subscription_snapshots"].columns}
    assert set(reg) <= model_cols
    subscription_reg = _TABLE_EXTRA_COLUMNS["proxy_subscriptions"]
    subscription_cols = {
        c.name for c in Base.metadata.tables["proxy_subscriptions"].columns
    }
    assert "cache_url_identity" in subscription_reg
    assert "cache_url_identity" in subscription_cols


@pytest.mark.asyncio
async def test_init_db_heals_legacy_snapshot_table(tmp_data_dir):
    """老形状建表 → init_db() 补齐缺列 → persist_snapshot 成功写入并可读回。"""
    db = tmp_data_dir / "holdexar.db"
    con = sqlite3.connect(db)
    con.execute(LEGACY_DDL)
    con.commit()
    con.close()

    await init_db()

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    actual = {r[1] for r in con.execute("PRAGMA table_info(subscription_snapshots)")}
    con.close()
    model_cols = {c.name for c in Base.metadata.tables["subscription_snapshots"].columns}
    assert model_cols <= actual, f"仍缺列：{sorted(model_cols - actual)}"

    from app.domains.proxypool.subscription import (
        FetchAttempt,
        FetchResult,
        build_snapshot,
        detect_format,
        persist_snapshot,
    )

    nodes = [{"name": "heal-node", "type": "http", "server": "10.1.1.1", "port": 1}]
    raw = yaml.safe_dump({"proxies": nodes}).encode("utf-8")
    res = FetchResult(raw=raw, http_status=200, content_type="text/yaml", channel="direct",
                      fmt=detect_format(raw),
                      attempts=[FetchAttempt("direct", True, 200, "yaml")])
    snap = build_snapshot(1, "https://heal.invalid/x", res, nodes, now=NOW)
    async with get_session_factory()() as s:
        await persist_snapshot(s, snap, data_dir=tmp_data_dir)
        await s.commit()

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    row = con.execute(
        "SELECT status, http_status, content_type, source_channel, url, node_count "
        "FROM subscription_snapshots"
    ).fetchall()
    con.close()
    from app.domains.proxies.subscription_secret import open_url

    assert [(r[0], r[1], r[2], r[3], open_url(r[4]), r[5]) for r in row] == [
        ("OK", 200, "text/yaml", "direct", "https://heal.invalid/x", 1)
    ]


@pytest.mark.asyncio
async def test_init_db_adds_subscription_cache_identity_to_existing_table(tmp_data_dir):
    from sqlalchemy import create_engine

    db = tmp_data_dir / "holdexar.db"
    sync_engine = create_engine(f"sqlite:///{db}")
    Base.metadata.tables["proxy_subscriptions"].create(sync_engine)
    sync_engine.dispose()
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE proxy_subscriptions DROP COLUMN cache_url_identity")
    con.commit()
    con.close()

    await init_db()

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    actual = {row[1] for row in con.execute("PRAGMA table_info(proxy_subscriptions)")}
    con.close()
    assert "cache_url_identity" in actual


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_init_db_preserves_retired_pool_tables_and_serves_history(tmp_data_dir, legacy):
    import httpx
    from fastapi import FastAPI

    from app.domains.proxypool.models import HealthRun
    from app.domains.proxypool.router import router

    tables = {
        "exit_groups": ("exit_ip VARCHAR(64)", "203.0.113.1"),
        "lanes": ("lane_index INTEGER", 2),
        "pool_generations": ("generation INTEGER", 3),
        "worker_leases": ("worker_id VARCHAR(64)", "legacy-worker"),
    }
    db = tmp_data_dir / "holdexar.db"
    with sqlite3.connect(db) as connection:
        if legacy:
            for table, (column, value) in tables.items():
                connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {column})")
                connection.execute(f"INSERT INTO {table} VALUES (1, ?)", (value,))
        before = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))

    await init_db()
    await init_db()
    async with get_session_factory()() as session:
        session.add(HealthRun(channel="manual", started_at=NOW, total=1, steam_ok=1))
        await session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/proxypool/health-runs")
    assert response.status_code == 200
    assert [(row["channel"], row["steamOk"]) for row in response.json()["runs"]] == [("manual", 1)]
    with sqlite3.connect(db) as connection:
        after = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
        for table, (_, value) in tables.items():
            if legacy:
                assert after[table] == before[table]
                assert connection.execute(f"SELECT * FROM {table}").fetchall() == [(1, value)]
            else:
                assert table not in after
