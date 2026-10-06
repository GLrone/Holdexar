"""种子历史合并分块续跑（_merge_history_chunk / merge_history_seed）行为验收。

- 多块合并：appid 跨块边界时数据完整、最终 marker 落位、断点清除；
- 断点续跑：中断（前缀块已并入）后重跑只补剩余块，最终 marker 落位；
- 断点版本不符（新种子）：从头重并；
- 无历史表种子：静默 None（旧格式种子）。

隔离：tmp 独立库，不碰 assets/seed；_HISTORY_CHUNK_APPS 打桩为 1 强制多块。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.core import seed_assets
from app.domains.games.models import GamePriceHistory  # noqa: F401 —— 注册表结构
from app.domains.settings import service as settings_service
from app.domains.settings.models import AppSetting  # noqa: F401

SEED_VERSION = "2026-10-06 23:00:00"
APPS = [997401, 997402, 997403, 997404]

_GPH_ROW_SQL = (
    "INSERT INTO game_price_history "
    "(appid, region_code, currency, price, original_price, discount_percent, "
    "sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, "
    "snapshot_at, discount_end_ts, steam_event_key) VALUES "
    "(?, 'CN', 'CNY', 1000, 2000, 0, 1, 0, NULL, 0, 'ok', 1000, "
    "'2026-10-01 08:00:00', NULL, NULL)"
)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    db_file = tmp_path / "t.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(database_module, "sqlite_file_path", lambda: db_file)
    monkeypatch.setattr(
        settings_service, "get_session_factory", lambda: factory, raising=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(database_module.Base.metadata.create_all)
    # 每块一款：强制走多块路径（生产 200 款/块的语义由尺寸常量表达）
    monkeypatch.setattr(seed_assets, "_HISTORY_CHUNK_APPS", 1)
    yield db_file
    await engine.dispose()


def _make_seed(path: Path, appids: list[int], version: str = SEED_VERSION) -> Path:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE game_price_history (
                appid INTEGER, region_code TEXT, currency TEXT, price INTEGER,
                original_price INTEGER, discount_percent INTEGER, sub_id INTEGER,
                is_gold INTEGER, version_suffix TEXT, is_bundle INTEGER,
                price_status TEXT, cny_fen INTEGER, snapshot_at TEXT,
                discount_end_ts INTEGER, steam_event_key TEXT);
            CREATE INDEX ix_seed_gph_appid ON game_price_history (appid);
            """
        )
        con.execute("INSERT INTO seed_meta VALUES ('version', ?)", (version,))
        for aid in appids:
            con.execute(_GPH_ROW_SQL, (aid,))
        con.commit()
    finally:
        con.close()
    return path


def _main_state(db_file: Path) -> dict:
    con = sqlite3.connect(str(db_file))
    try:
        rows = con.execute(
            "SELECT appid, price FROM game_price_history ORDER BY appid"
        ).fetchall()
        marker = con.execute(
            "SELECT value_json FROM app_settings WHERE key=?",
            (seed_assets.HISTORY_MARKER_KEY,),
        ).fetchone()
        progress = con.execute(
            "SELECT value_json FROM app_settings WHERE key=?",
            (seed_assets.HISTORY_PROGRESS_KEY,),
        ).fetchone()
        return {
            "rows": rows,
            "marker": marker[0] if marker else None,
            "progress": progress[0] if progress else None,
        }
    finally:
        con.close()


@pytest.mark.asyncio
async def test_multi_chunk_merge_completes_and_clears_progress(env, tmp_path):
    seed = _make_seed(tmp_path / "seed.db", APPS)
    stats = await seed_assets.merge_history_seed(seed)
    assert stats == {"overwritten": 0, "inserted": len(APPS)}
    state = _main_state(env)
    assert [r[0] for r in state["rows"]] == APPS
    assert state["marker"] == json.dumps(SEED_VERSION)
    assert state["progress"] is None, "完成后断点必须清除"
    # 幂等：marker 命中直返 None
    assert await seed_assets.merge_history_seed(seed) is None


@pytest.mark.asyncio
async def test_resume_from_progress_skips_completed_range(env, tmp_path):
    seed = _make_seed(tmp_path / "seed.db", APPS)
    # 模拟中断：前两款已并入、断点指向 APPS[1]
    con = sqlite3.connect(str(env))
    try:
        con.execute("BEGIN IMMEDIATE")
        for aid in APPS[:2]:
            con.execute(_GPH_ROW_SQL, (aid,))
        con.execute(
            "INSERT INTO app_settings (key, value_json) VALUES (?, ?)",
            (
                seed_assets.HISTORY_PROGRESS_KEY,
                json.dumps({"version": SEED_VERSION, "last_appid": APPS[1]}),
            ),
        )
        con.commit()
    finally:
        con.close()

    stats = await seed_assets.merge_history_seed(seed)
    assert stats == {"overwritten": 0, "inserted": len(APPS) - 2}
    state = _main_state(env)
    assert [r[0] for r in state["rows"]] == APPS
    assert state["marker"] == json.dumps(SEED_VERSION)
    assert state["progress"] is None


@pytest.mark.asyncio
async def test_progress_from_other_version_restarts(env, tmp_path):
    """断点版本与当前种子不符（升级/重导）：忽略断点从头重并。"""
    seed = _make_seed(tmp_path / "seed.db", APPS)
    con = sqlite3.connect(str(env))
    try:
        con.execute(
            "INSERT INTO app_settings (key, value_json) VALUES (?, ?)",
            (
                seed_assets.HISTORY_PROGRESS_KEY,
                json.dumps({"version": "旧版本种子", "last_appid": APPS[2]}),
            ),
        )
        con.commit()
    finally:
        con.close()

    stats = await seed_assets.merge_history_seed(seed)
    assert stats == {"overwritten": 0, "inserted": len(APPS)}
    state = _main_state(env)
    assert [r[0] for r in state["rows"]] == APPS
    assert state["progress"] is None


@pytest.mark.asyncio
async def test_seed_without_history_table_is_none(env, tmp_path):
    """旧格式种子（无历史表）：静默 None，不留 marker 不动断点。"""
    seed = tmp_path / "seed.db"
    con = sqlite3.connect(str(seed))
    try:
        con.executescript(
            "CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        con.execute("INSERT INTO seed_meta VALUES ('version', ?)", (SEED_VERSION,))
        con.commit()
    finally:
        con.close()
    assert await seed_assets.merge_history_seed(seed) is None
    state = _main_state(env)
    assert state["rows"] == []
    assert state["marker"] is None
