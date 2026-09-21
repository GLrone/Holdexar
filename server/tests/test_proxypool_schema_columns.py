"""真实生产暴露的 schema 缺列修复：登记 + 老库自愈。

真实 holdexar-dev 库的 `subscription_snapshots` 由更早的停放期模型建成，
缺 `http_status` / `content_type` / `source_channel`，而这三列**从未登记**进
`_TABLE_EXTRA_COLUMNS` → 首次真实同步的 INSERT 直接报
`table subscription_snapshots has no column named http_status`。

本文件钉两件事：
1. 这 3 列确实登记在 `_TABLE_EXTRA_COLUMNS["subscription_snapshots"]`（防被误删）；
2. 老形状的库跑一次 `init_db()` 就能被补齐，且补齐后 `persist_snapshot` 可成功写入。
"""
from __future__ import annotations

import os

import sqlite3
from datetime import datetime

import pytest
import yaml

from app.core.config import get_settings  # noqa: E402
from app.core.database import (  # noqa: E402
    _TABLE_EXTRA_COLUMNS,
    Base,
    get_engine,
    get_session_factory,
    init_db,
)

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


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则「缓存库 ≠ 当前配置库」判据判定相等，临时引擎会留给后续文件
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_snapshot_extra_columns_are_registered():
    """真实生产缺的 3 列必须登记在册（未登记 → 启动不会补，首次同步必失败）。"""
    reg = _TABLE_EXTRA_COLUMNS["subscription_snapshots"]
    for col in ("http_status", "content_type", "source_channel"):
        assert col in reg, f"{col} 未登记进 _TABLE_EXTRA_COLUMNS"
    # 登记口径与模型一致（只登记加列；不碰 SCHEMA_VERSION / 迁移链）
    model_cols = {c.name for c in Base.metadata.tables["subscription_snapshots"].columns}
    assert set(reg) <= model_cols


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
    assert row == [("OK", 200, "text/yaml", "direct", "https://heal.invalid/x", 1)]
