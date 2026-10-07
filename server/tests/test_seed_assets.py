"""资产种子包行为验收（瘦身后：种子不含汇率与价格历史）。

覆盖链路（种子文件合成于 tmp_path，不碰 assets/seed；写库用合成
appid，夹具清理——遵循 99xxxx 探针规约）：
1. export_seed.py 导出：白名单四块数据落种；汇率/价格历史**零带出**
   （fx 与历史归 rates Provider 现抓与 R2 云端分包）；games_curated 只收
   人工列非空行
2. 人工列种子优先：已存在 games 行补挂；爬虫重爬（白名单不含人工列）不丢
3. 爬虫联动：upsert_game_and_prices 自动贴标记（无需显式调用）
4. 现价快照 / 预设池 / 玩家标签：缺行落行、本地观测让位、marker 幂等
5. 旧 schema 种子（缺表）静默 no-op
"""
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, text

from app.core import seed_assets
from app.core.database import get_session_factory
from app.crawler.db_writer import DbWriter
from app.domains.games.models import (
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
    GamePriceHistory,
    GameTag,
)

# 本文件需要真实开发库：合并用例依赖「本地已有行让位 / 名单行落库」的存量形态，
# 种子 marker 的清理/重放操作 app_settings——隔离夹具的空临时库给不出这些形态。
HOLDEXAR_TEST_REAL_DB = True

APPID_A = 997_102
APPID_B = 997_103
APPID_C = 997_104  # 名单落行用例专用（本地无行 → 种子落完整 games 行）
APPID_D = 997_105  # 预设池合并用例专用
APPID_E = 997_106  # 现价快照合并用例专用（种子/本地行全合成）

SEED_VERSION = "2026-09-06 12:00:00"

_SCHEMA = """
CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE games_curated (
    appid INTEGER PRIMARY KEY, name TEXT, xgp_tier TEXT, epic_date TEXT,
    is_epic INTEGER, is_hb INTEGER, hb_data TEXT, series_id TEXT);
CREATE TABLE preset_games (
    appid INTEGER PRIMARY KEY, name TEXT, source TEXT, added_at TEXT);
CREATE TABLE games_catalog (
    appid INTEGER PRIMARY KEY, name TEXT, name_en TEXT, type TEXT,
    header_image TEXT, family_sharing INTEGER, trading_cards INTEGER,
    is_adult INTEGER, is_visual_novel INTEGER, release_date TEXT,
    positive_rate INTEGER, positive_reviews INTEGER,
    review_count INTEGER, view_count INTEGER, removed_at TEXT,
    free_kind TEXT);
CREATE TABLE game_current_prices (
    appid INTEGER, region_code TEXT, currency TEXT, price INTEGER,
    original_price INTEGER, discount_percent INTEGER, sub_id INTEGER,
    price_status TEXT, fail_count INTEGER, cny_fen INTEGER,
    discount_end_ts INTEGER, updated_at TEXT);
CREATE INDEX ix_seed_gcp_appid ON game_current_prices (appid);
CREATE TABLE game_tags (
    appid INTEGER, tagid INTEGER, weight INTEGER,
    PRIMARY KEY (appid, tagid)) WITHOUT ROWID;
CREATE TABLE game_price_history (
    appid INTEGER, region_code TEXT, currency TEXT, price INTEGER,
    original_price INTEGER, discount_percent INTEGER, sub_id INTEGER,
    is_gold INTEGER, version_suffix TEXT, is_bundle INTEGER,
    price_status TEXT, cny_fen INTEGER, snapshot_at TEXT,
    discount_end_ts INTEGER, steam_event_key TEXT);
CREATE INDEX ix_seed_gph_appid ON game_price_history (appid);
CREATE UNIQUE INDEX ux_seed_gph_snapshot ON game_price_history (
    appid, region_code, snapshot_at, COALESCE(sub_id, -1),
    COALESCE(price, 0), COALESCE(is_gold, 0));
CREATE TABLE bundles (
    bundle_id INTEGER PRIMARY KEY, name TEXT, must_purchase_as_set INTEGER,
    item_kind INTEGER, header_image TEXT, is_lowest INTEGER,
    min_cny_fen INTEGER, diff_fen INTEGER, smart_score REAL, url TEXT,
    app_ids TEXT, view_count INTEGER, updated_at TEXT);
CREATE TABLE bundle_region_prices (
    bundle_id INTEGER, region_code TEXT, currency TEXT, price INTEGER,
    original_price INTEGER, discount_percent INTEGER,
    bundle_base_discount INTEGER, price_status TEXT, cny_fen INTEGER,
    discount_end_ts INTEGER, app_ids TEXT, crawled_at TEXT,
    PRIMARY KEY (bundle_id, region_code));
"""


def _make_seed(
    path: Path,
    *,
    curated: list[tuple] | None = None,
    preset: list[tuple] | None = None,
    games_catalog: list[tuple] | None = None,
    current_prices: list[tuple] | None = None,
    game_tags: list[tuple] | None = None,
    price_history: list[tuple] | None = None,
    bundles: list[tuple] | None = None,
    bundle_prices: list[tuple] | None = None,
    version: str = SEED_VERSION,
) -> Path:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.executescript(_SCHEMA)
        con.executemany("INSERT INTO games_curated VALUES (?, ?, ?, ?, ?, ?, ?, ?)", curated or [])
        con.executemany("INSERT INTO preset_games VALUES (?, ?, ?, ?)", preset or [])
        con.executemany(
            "INSERT INTO games_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            games_catalog or [],
        )
        con.executemany(
            "INSERT INTO game_current_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            current_prices or [],
        )
        con.executemany(
            "INSERT INTO game_tags VALUES (?, ?, ?)",
            game_tags or [],
        )
        con.executemany(
            "INSERT INTO game_price_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            price_history or [],
        )
        con.executemany(
            "INSERT INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            bundles or [],
        )
        con.executemany(
            "INSERT INTO bundle_region_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            bundle_prices or [],
        )
        con.executemany(
            "INSERT INTO seed_meta VALUES (?, ?)",
            [
                ("schema_version", "9"),
                ("version", version),
                ("exported_at", version),
                ("rows_games_catalog", str(len(games_catalog or []))),
                ("rows_current_prices", str(len(current_prices or []))),
                ("rows_game_tags", str(len(game_tags or []))),
            ],
        )
        con.commit()
    finally:
        con.close()
    return path


def _game_data(appid: int, name: str) -> dict:
    return {
        "appid": appid,
        "name": name,
        "type": "game",
        "updated_at": datetime.now(),
    }


def _prices(appid: int) -> list[dict]:
    return [
        {
            "appid": appid,
            "region_code": "CN",
            "currency": "CNY",
            "price": 1000,
            "original_price": 1000,
            "discount_percent": 0,
            "sub_id": 1,
            "price_status": "ok",
        }
    ]


async def _game_row(appid: int) -> dict | None:
    cols = ", ".join(seed_assets.CURATED_COLS)
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(f"SELECT {cols} FROM games WHERE appid = :a"), {"a": appid}
            )
        ).first()
    return dict(zip(seed_assets.CURATED_COLS, row)) if row else None


_ALL_MARKER_KEYS = (
    seed_assets.CURATED_MARKER_KEY,
    seed_assets.PRESET_MARKER_KEY,
    seed_assets.CURRENT_MARKER_KEY,
    seed_assets.GAME_TAGS_MARKER_KEY,
    seed_assets.HISTORY_MARKER_KEY,
    seed_assets.BUNDLES_MARKER_KEY,
)

# 捆绑包用例专用合成 bundle_id（真库 bundles 是 Steam 真实 bundle id 空间，
# 合成段 997xxx 避让）
BUNDLE_11 = 997_011
BUNDLE_22 = 997_022


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    # setup：保存四个 marker 的真实值后清空——每个用例须从「无 marker」起步；
    # 但**绝不能只删不还**：本测试对真实开发库跑，marker 是种子通道的幂等锚，
    # 被删后下一次应用启动会整链重跑（名单合并、现价快照合并全量过一遍），
    # 日志表现为「每次启动都在执行种子合并」——正是曾经发生过的真实污染。
    # 无库环境（CI runner）先建全 schema 空库：本文件断言的「存量形态」用例
    # 各自 arrange 合成行，空 schema 即可承载；本机真实库存在时零行为变化
    import app.core.database as _database

    settings = _database.get_settings()
    if not (settings.data_dir / settings.db_filename).is_file():
        await _database.init_db()
        await _database.get_engine().dispose()
    keys_sql = ", ".join(f"'{k}'" for k in _ALL_MARKER_KEYS)
    async with get_session_factory()() as session:
        saved_markers: dict[str, str] = {
            k: v
            for k, v in await session.execute(
                text(f"SELECT key, value_json FROM app_settings WHERE key IN ({keys_sql})")
            )
        }
        await session.execute(
            text(f"DELETE FROM app_settings WHERE key IN ({keys_sql})")
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(Game).where(Game.appid.in_([APPID_A, APPID_B, APPID_C, APPID_D, APPID_E]))
        )
        await session.execute(
            text(f"DELETE FROM game_tags WHERE appid IN ({APPID_A}, {APPID_B}, {APPID_C}, {APPID_D}, {APPID_E})")
        )
        await session.execute(
            text(f"DELETE FROM game_current_prices WHERE appid IN ({APPID_A}, {APPID_B}, {APPID_C}, {APPID_D}, {APPID_E})")
        )
        await session.execute(
            text(f"DELETE FROM game_price_history WHERE appid IN ({APPID_A}, {APPID_B}, {APPID_C}, {APPID_D}, {APPID_E})")
        )
        await session.execute(
            text(f"DELETE FROM bundle_region_prices WHERE bundle_id IN ({BUNDLE_11}, {BUNDLE_22})")
        )
        await session.execute(
            text(f"DELETE FROM bundles WHERE bundle_id IN ({BUNDLE_11}, {BUNDLE_22})")
        )
        # marker 恢复原值（value_json 按读出的原文写回，编码读写对称）；
        # 用例中途写入的新 marker 被 setup 时的原值覆盖（无原值 = 删除）
        for key in _ALL_MARKER_KEYS:
            if key in saved_markers:
                await session.execute(
                    text(
                        "INSERT INTO app_settings (key, value_json) VALUES (:k, :v) "
                        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json"
                    ),
                    {"k": key, "v": saved_markers[key]},
                )
            else:
                await session.execute(
                    text("DELETE FROM app_settings WHERE key = :k"), {"k": key}
                )
        await session.commit()
    seed_assets.reset_cache()


# ── 1. 导出脚本 ───────────────────────────────────────────────────────


def test_export_seed_whitelist_and_dedup(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    con = sqlite3.connect(str(src))
    con.executescript(
        """
        CREATE TABLE fx_rates (currency_code TEXT PRIMARY KEY, rate_to_cny REAL, fetched_at TEXT);
        CREATE TABLE fx_rate_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            currency_code TEXT, rate_to_cny REAL, source TEXT, fetched_at TEXT);
        CREATE TABLE games (
            appid INTEGER PRIMARY KEY, name TEXT, xgp_tier TEXT, epic_date TEXT,
            is_epic INTEGER, is_hb INTEGER, hb_data TEXT, series_id TEXT);
        CREATE TABLE preset_games (
            appid INTEGER PRIMARY KEY, name TEXT, source TEXT, added_at TEXT);
        CREATE TABLE game_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appid INTEGER, region_code TEXT, currency TEXT, price INTEGER,
            original_price INTEGER, discount_percent INTEGER, sub_id INTEGER,
            is_gold INTEGER, version_suffix TEXT, is_bundle INTEGER,
            price_status TEXT, cny_fen INTEGER, snapshot_at TEXT);
        CREATE TABLE steam_accounts (steamid TEXT PRIMARY KEY, cookies TEXT);
        """
    )
    con.execute("INSERT INTO fx_rates VALUES ('XTS', 3.5, '2026-01-01 00:00:00')")
    con.executemany(
        "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("XTS", 3.0, "old", "2026-01-05 00:00:00"),
            ("XTS", 3.5, "new", "2026-01-05 00:00:00"),
        ],
    )
    # 一款有人工列 + 一款全空（全空不得进种子）+ 一行凭据（不得泄漏）
    con.execute(
        "INSERT INTO games VALUES (997201, '正版名A', 'GPU', NULL, 1, 0, 'HB24-1', 'SeriesX')"
    )
    con.execute(
        "INSERT INTO games VALUES (997202, NULL, NULL, NULL, 0, 0, NULL, NULL)"
    )
    con.execute("INSERT INTO steam_accounts VALUES ('765', 'secret')")
    # 预设池：已在库行（名字以库内为准）+ 未在库但登记时带名 + 两处都无名（不得进种子）
    con.executemany(
        "INSERT INTO preset_games (appid, name, source, added_at) VALUES (?, ?, ?, ?)",
        [
            (997201, "旧登记名", "上传优先.json", "2026-01-07 00:00:00"),
            (997203, "文件带来的名", "上传优先.json", "2026-01-07 00:00:00"),
            (997202, None, "视觉小说.json", "2026-01-07 00:00:00"),
        ],
    )
    # 价格历史整表带出（老源库缺新列按 NULL 带出）
    con.executemany(
        "INSERT INTO game_price_history (appid, region_code, currency, price, "
        "original_price, discount_percent, sub_id, is_gold, price_status, cny_fen, "
        "snapshot_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (997201, "CN", "CNY", 100, 2000, 5, 1, 0, "ok", 100, "2026-01-05 00:00:00"),
            (997201, "RU", "RUB", 300, 2000, 15, 1, 0, "ok", 21, "2026-01-05 00:00:00"),
        ],
    )
    con.commit()
    con.close()

    out = tmp_path / "seed" / "holdexar_seed.db"
    out.parent.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "export_seed.py"),
            "--db", str(src),
            "--out", str(out),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    s = sqlite3.connect(str(out))
    try:
        meta = dict(s.execute("SELECT key, value FROM seed_meta").fetchall())
        assert meta["version"] == meta["exported_at"]
        assert meta["schema_version"] == "9"
        # 人工列只收非空行；name 身份列随行带出（供合并侧落名单行）
        assert s.execute(
            "SELECT name, xgp_tier, is_epic, hb_data FROM games_curated"
        ).fetchall() == [("正版名A", "GPU", 1, "HB24-1")]
        # 预设池：库内名字优先（COALESCE）、登记名兜底；两处无名的行不进种子
        assert s.execute(
            "SELECT appid, name, source FROM preset_games ORDER BY appid"
        ).fetchall() == [
            (997201, "正版名A", "上传优先.json"),
            (997203, "文件带来的名", "上传优先.json"),
        ]
        assert meta["rows_preset"] == "2"
        # 价格历史整表带出（种子=完整库镜像）；老源库缺列按 NULL 带出
        assert meta["rows_price_history"] == "2"
        hist = s.execute(
            "SELECT appid, region_code, price, discount_end_ts, steam_event_key "
            "FROM game_price_history ORDER BY region_code"
        ).fetchall()
        assert hist == [
            (997201, "CN", 100, None, None),
            (997201, "RU", 300, None, None),
        ]
        # 白名单外数据零泄漏：凭据表、汇率（快照+历史）都不进种子
        tables = {
            r[0]
            for r in s.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "steam_accounts" not in tables
        assert "fx_rates" not in tables
        assert "fx_rate_history" not in tables
    finally:
        s.close()


# ── 2/3. 人工列补挂 + 爬虫联动 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_curated_seed_wins_and_crawl_hook(tmp_path: Path, monkeypatch) -> None:
    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        curated=[
            (APPID_A, "种子名A", "GPU Ultimate", None, 1, 0, "HB慈善包24年1月", "TestSeries")
        ],
    )
    writer = DbWriter()
    # 无种子状态先爬入游戏：爬虫本身不带人工列（联动钩子索引为空零开销）
    assert await writer.upsert_game_and_prices(_game_data(APPID_A, "原价监控测试游戏"), _prices(APPID_A))
    row = await _game_row(APPID_A)
    assert row["xgp_tier"] is None and row["is_epic"] == 0

    # 挂上合成种子并合并：已存在的 games 行覆写人工列
    seed_assets.reset_cache()
    monkeypatch.setattr(seed_assets, "seed_db_path", lambda: seed)
    summary = await seed_assets.merge_curated_seed(seed)
    assert summary is not None and summary["curated_updated"] == 1
    row = await _game_row(APPID_A)
    assert row["xgp_tier"] == "GPU Ultimate"
    assert row["is_epic"] == 1
    assert row["hb_data"] == "HB慈善包24年1月"
    assert row["series_id"] == "TestSeries"

    # 重爬（upsert 白名单不含人工列）：爬取字段更新、人工列不丢
    assert await writer.upsert_game_and_prices(
        _game_data(APPID_A, "改名后的测试游戏"), _prices(APPID_A)
    )
    row = await _game_row(APPID_A)
    assert row["xgp_tier"] == "GPU Ultimate" and row["is_epic"] == 1

    # 联动隔离：种子索引外的 appid 爬入不带标记
    assert await writer.upsert_game_and_prices(_game_data(APPID_B, "联动测试游戏"), _prices(APPID_B))
    row = await _game_row(APPID_B)
    assert row["xgp_tier"] is None

    # 种子加入 APPID_B（文件重写 mtime 变化 → 索引缓存自动失效）
    # → 再次爬入即自动贴标记（无显式 apply 调用，联动闭环）
    _make_seed(
        seed,
        curated=[
            (APPID_A, "种子名A", "GPU Ultimate", None, 1, 0, "HB慈善包24年1月", "TestSeries"),
            (APPID_B, "种子名B", "GPU", "2023-12-25 ~ 2024-01-01", 1, 0, None, None),
        ],
        version="2026-09-07 01:00:00",
    )
    assert await writer.upsert_game_and_prices(_game_data(APPID_B, "联动测试游戏"), _prices(APPID_B))
    row = await _game_row(APPID_B)
    assert row["xgp_tier"] == "GPU"
    assert row["epic_date"] == "2023-12-25 ~ 2024-01-01"


# ── 3.5 名单行随种子落地（不进监控）───────────────────────────────────


@pytest.mark.asyncio
async def test_curated_list_rows_ship_without_monitoring(tmp_path: Path) -> None:
    """名单行合并（老用户路径）：
    - 本地缺行 → 落完整 games 行（name 取种子，updated_at 非空 → 永不被
      孤儿补抓层捡去爬；监控范围只由愿望单驱动）；
    - 本地已有行 → 只覆写人工列，name 不动；
    - marker 幂等，二跑零开销。
    """
    writer = DbWriter()
    assert await writer.upsert_game_and_prices(
        _game_data(APPID_A, "本地爬来的名字"), _prices(APPID_A)
    )
    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        curated=[
            # APPID_C 本地不存在 → 落名单行
            (APPID_C, "名单新游戏", None, "2023-12-25 ~ 2024-01-01", 1, 0, None, None),
            # APPID_A 本地已存在 → 只覆写人工列
            (APPID_A, "种子名A", "GPU Ultimate", None, 0, 1, "HB慈善包24年1月", None),
        ],
    )
    stats = await seed_assets.merge_curated_seed(seed)
    assert stats == {"curated_updated": 1, "curated_inserted": 1}

    # 落行断言：name / 人工列 / updated_at 非空（防监控哨兵）
    async with get_session_factory()() as session:
        row_c = (
            await session.execute(
                text("SELECT name, is_epic, epic_date, updated_at FROM games WHERE appid = :a"),
                {"a": APPID_C},
            )
        ).first()
    assert row_c is not None
    assert row_c[0] == "名单新游戏" and row_c[1] == 1
    assert row_c[2] == "2023-12-25 ~ 2024-01-01"
    assert row_c[3] is not None  # updated_at 非空 = 孤儿补抓层看不见它

    # 已有行：人工列覆写、name 保留本地值
    row_a = await _game_row(APPID_A)
    assert row_a["xgp_tier"] == "GPU Ultimate" and row_a["is_hb"] == 1
    async with get_session_factory()() as session:
        name_a = (
            await session.execute(
                text("SELECT name FROM games WHERE appid = :a"), {"a": APPID_A}
            )
        ).scalar_one()
    assert name_a == "本地爬来的名字"

    # 幂等：marker 命中直接返回
    assert await seed_assets.merge_curated_seed(seed) is None


# ── 3.6 预设池随种子落地（缺行落行，已有行不动）───────────────────────


@pytest.mark.asyncio
async def test_preset_seed_inserts_missing_rows(tmp_path: Path) -> None:
    """预设池并入：缺行落完整 games 行（name 取种子）；已有行不动；同版幂等。"""
    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        preset=[
            (APPID_D, "预设名D", "上传优先.json", "2026-01-07 00:00:00"),
            (APPID_B, "种子不该覆盖的", "上传优先.json", "2026-01-07 00:00:00"),
        ],
    )
    async with get_session_factory()() as session:
        session.add(Game(appid=APPID_B, name="本地爬到的名", updated_at=datetime.now()))
        await session.commit()

    stats = await seed_assets.merge_preset_seed(seed)
    assert stats == {"preset_inserted": 1}

    async with get_session_factory()() as session:
        d = await session.get(Game, APPID_D)
        b = await session.get(Game, APPID_B)
    assert d is not None and d.name == "预设名D"
    assert d.updated_at is not None  # 带版本戳：孤儿补抓层不会重复捡（刷新走全池层）
    assert b is not None and b.name == "本地爬到的名"

    # 幂等：同版本二跑 marker 拦截
    assert await seed_assets.merge_preset_seed(seed) is None


@pytest.mark.asyncio
async def test_preset_merge_missing_table_or_empty_noop(tmp_path: Path) -> None:
    """旧 schema（无 preset_games 表）或空预设：静默 no-op。"""
    seed = _make_seed(tmp_path / "holdexar_seed.db")
    assert await seed_assets.merge_preset_seed(seed) is None  # 空表

    con = sqlite3.connect(str(seed))
    con.execute("DROP TABLE preset_games")
    con.commit()
    con.close()
    assert await seed_assets.merge_preset_seed(seed) is None  # 无表（旧 schema）


# ── 4. 现价快照合并 ───────────────────────────────────────────────────


def _gc_row(appid: int, name: str) -> tuple:
    """16 列 games_catalog 种子行（与 export_seed.GC_COLS 同序）。"""
    return (
        appid, name, None, "game", None, 0, 0, 0, 0, None,
        None, 0, 0, 0, None, None,
    )


def _gcp_row(appid: int, region: str = "CN", price: int = 1000) -> tuple:
    """12 列 game_current_prices 种子行（与 export_seed.GCP_COLS 同序）。"""
    return (
        appid, region, "CNY", price, price, 0, 1, "ok", 0, price, None,
        "2026-09-15 08:00:00",
    )


async def _tags_local_state(appid: int) -> list[tuple]:
    """本地 game_tags 现状 [(tagid, weight)]（票重降序，与读取侧同序）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tagid, weight FROM game_tags WHERE appid = :a "
                    "ORDER BY weight DESC, tagid"
                ),
                {"a": appid},
            )
        ).all()
    return [(int(t), int(w)) for t, w in rows]


async def _current_local_state(appid: int) -> tuple[dict | None, list]:
    """本地 (games 行 name, 现价行 [region, price]) 现状。"""
    async with get_session_factory()() as session:
        game = (
            await session.execute(
                text(f"SELECT name FROM games WHERE appid = {appid}")
            )
        ).first()
        prices = (
            await session.execute(
                text(
                    "SELECT region_code, price FROM game_current_prices "
                    f"WHERE appid = {appid} ORDER BY region_code"
                )
            )
        ).fetchall()
    return ({"name": game[0]} if game else None), [tuple(r) for r in prices]


@pytest.mark.asyncio
async def test_current_seed_fills_missing_and_yields_to_local(tmp_path: Path) -> None:
    """现价快照合并三口径：缺行落行 / 本地爬过的让位 / 本地有行无价的补价。

    - APPID_E：本地全无 → games 行 + 现价整包插入；
    - APPID_A：本地有 games 行且有现价 → 一个字段都不动（本地观测权威）；
    - APPID_B：本地有 games 行但无现价 → 现价补上（appid 粒度判定）。
    """
    async with get_session_factory()() as session:
        session.add(Game(appid=APPID_A, name="本地名A", type="game"))
        session.add(Game(appid=APPID_B, name="本地名B", type="game"))
        await session.commit()
    async with get_session_factory()() as session:
        session.add(GameCurrentPrice(
            appid=APPID_A, region_code="CN", price=9999, price_status="ok",
        ))
        await session.commit()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        games_catalog=[
            _gc_row(APPID_E, "种子目录行E"),
            _gc_row(APPID_A, "种子名A"),
            _gc_row(APPID_B, "种子名B"),
        ],
        current_prices=[
            _gcp_row(APPID_E),
            _gcp_row(APPID_E, "US", 2000),
            _gcp_row(APPID_A, "CN", 1111),
            _gcp_row(APPID_B, "CN", 2222),
        ],
    )
    stats = await seed_assets.merge_current_seed(seed)
    assert stats == {"games": 1, "prices": 3}

    game_e, prices_e = await _current_local_state(APPID_E)
    assert game_e == {"name": "种子目录行E"}
    assert prices_e == [("CN", 1000), ("US", 2000)]

    game_a, prices_a = await _current_local_state(APPID_A)
    assert game_a == {"name": "本地名A"}
    assert prices_a == [("CN", 9999)]  # 本地观测不被种子回拨

    game_b, prices_b = await _current_local_state(APPID_B)
    assert game_b == {"name": "本地名B"}
    assert prices_b == [("CN", 2222)]  # 本地有行无价 → 种子补价

    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.CURRENT_MARKER_KEY) == SEED_VERSION
    # 幂等：同版本种子二次合并零开销（marker 命中直接返回）
    assert await seed_assets.merge_current_seed(seed) is None


@pytest.mark.asyncio
async def test_game_tags_seed_fills_missing_and_yields_to_local(tmp_path: Path) -> None:
    """玩家标签合并两口径：本地没抓过的整款补齐 / 本地抓过的整款让位。

    让位必须是**整款**而非逐行：本地 {19,21}、种子 {19,21,122} 逐行 OR IGNORE
    会并出 122 —— 那是 Steam 早先摘掉的标签被种子复活。
    """
    async with get_session_factory()() as session:
        session.add(Game(appid=APPID_A, name="本地名A", type="game"))
        session.add(Game(appid=APPID_B, name="本地名B", type="game"))
        session.add(GameTag(appid=APPID_A, tagid=19, weight=500))
        session.add(GameTag(appid=APPID_A, tagid=21, weight=300))
        await session.commit()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        game_tags=[
            (APPID_A, 19, 900),
            (APPID_A, 21, 800),
            (APPID_A, 122, 700),
            (APPID_B, 87918, 600),
        ],
    )
    stats = await seed_assets.merge_game_tags_seed(seed)
    assert stats == {"tags": 1}

    assert await _tags_local_state(APPID_A) == [(19, 500), (21, 300)]
    assert await _tags_local_state(APPID_B) == [(87918, 600)]

    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.GAME_TAGS_MARKER_KEY) == SEED_VERSION
    # 幂等：同版本种子二次合并零开销（marker 命中直接返回）
    assert await seed_assets.merge_game_tags_seed(seed) is None


@pytest.mark.asyncio
async def test_game_tags_merge_no_table_noop(tmp_path: Path) -> None:
    """旧种子（无标签表）：合并静默 no-op，不留 marker。"""
    seed = tmp_path / "holdexar_seed.db"
    if seed.exists():
        seed.unlink()
    con = sqlite3.connect(str(seed))
    try:
        con.executescript(
            "CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);"
            f"INSERT INTO seed_meta VALUES ('version', '{SEED_VERSION}');"
        )
        con.commit()
    finally:
        con.close()

    assert await seed_assets.merge_game_tags_seed(seed) is None
    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.GAME_TAGS_MARKER_KEY) is None


@pytest.mark.asyncio
async def test_current_merge_no_catalog_table_noop(tmp_path: Path) -> None:
    """旧种子（无现价快照表）：合并静默 no-op，不留 marker。"""
    seed = tmp_path / "holdexar_seed.db"
    if seed.exists():
        seed.unlink()
    con = sqlite3.connect(str(seed))
    try:
        con.executescript(
            "CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);"
            f"INSERT INTO seed_meta VALUES ('version', '{SEED_VERSION}');"
        )
        con.commit()
    finally:
        con.close()
    assert await seed_assets.merge_current_seed(seed) is None
    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.CURRENT_MARKER_KEY) is None


def _gph_row(appid: int, region_code: str = "RU", snapshot_at: str = "2026-01-05 00:00:00.000000") -> tuple:
    return (
        appid, region_code, "USD", 300, 2000, 15, 1, 0, "GOTY", 0, "ok", 21,
        snapshot_at, None, None,
    )


def _bundle_row(bundle_id: int) -> tuple:
    return (
        bundle_id, f"包{bundle_id}", 0, 0, None, 0, None, 0, None, None,
        "[1,2]", 0, "2026-01-05 00:00:00",
    )


def _brp_row(bundle_id: int, region_code: str = "CN", price: int = 1000) -> tuple:
    return (
        bundle_id, region_code, "USD", price, 2000, 15, 10, "ok", 21, None,
        "[1,2]", "2026-01-05 00:00:00",
    )


@pytest.mark.asyncio
async def test_history_seed_fills_missing_and_backfills_suffix(tmp_path: Path) -> None:
    """历史合并三口径：缺键行补插 / 同键行版本名补空 / 幂等。

    六段逻辑键（appid, region, snapshot, sub, price, gold）与唯一索引同口径：
    - APPID_A RU：本地无 → 补插；
    - APPID_B CN：本地同键行 version_suffix 空 → 种子补空（价格不回拨——
      价格在逻辑键内，同键行价格本就相同）。
    """
    snap = datetime(2026, 1, 5)
    async with get_session_factory()() as session:
        session.add(GamePriceHistory(
            appid=APPID_B, region_code="CN", currency="CNY", price=300,
            original_price=2000, discount_percent=15, sub_id=1, is_gold=False,
            price_status="ok", cny_fen=999, snapshot_at=snap,
        ))
        await session.commit()
    # 读回本地存储格式（SQLAlchemy SQLite DateTime 带微秒位），种子行用同一
    # 文本作键——两侧键文本一致才命中同键行
    async with get_session_factory()() as session:
        snap_text = (
            await session.execute(
                text("SELECT snapshot_at FROM game_price_history WHERE appid = :a"),
                {"a": APPID_B},
            )
        ).scalar_one()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        price_history=[
            _gph_row(APPID_A, "RU"),
            _gph_row(APPID_B, "CN", snapshot_at=snap_text),
        ],
    )
    stats = await seed_assets.merge_history_seed(seed)
    assert stats == {"overwritten": 1, "inserted": 1}

    from app.core.database import sqlite_file_path

    con = sqlite3.connect(str(sqlite_file_path()))
    try:
        rows = con.execute(
            "SELECT appid, region_code, price, cny_fen, version_suffix "
            "FROM game_price_history WHERE appid IN (?, ?) ORDER BY appid",
            (APPID_A, APPID_B),
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        (APPID_A, "RU", 300, 21, "GOTY"),
        # 本地 cny_fen=999 被同键种子行覆写（派生列差异）；版本名补空
        (APPID_B, "CN", 300, 21, "GOTY"),
    ]
    # 幂等：同版本种子二次合并零开销
    assert await seed_assets.merge_history_seed(seed) is None


@pytest.mark.asyncio
async def test_history_merge_no_table_noop(tmp_path: Path) -> None:
    """旧种子（无历史表）：合并静默 no-op，不留 marker。"""
    seed = tmp_path / "holdexar_seed.db"
    if seed.exists():
        seed.unlink()
    con = sqlite3.connect(str(seed))
    try:
        con.executescript(
            "CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);"
            f"INSERT INTO seed_meta VALUES ('version', '{SEED_VERSION}');"
        )
        con.commit()
    finally:
        con.close()

    assert await seed_assets.merge_history_seed(seed) is None
    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.HISTORY_MARKER_KEY) is None


@pytest.mark.asyncio
async def test_bundles_seed_fills_missing_and_yields_to_local(tmp_path: Path) -> None:
    """捆绑包合并两口径：主档缺行落行 / 区价按包粒度让位本地观测。

    - BUNDLE_11：本地全无 → 主档 + 区价整包插入（未关注的包本地永远没有行，
      种子是唯一来源）；
    - BUNDLE_22：本地已有区价行（爬过）→ 种子区价不回拨；主档种子未带则缺。
    """
    async with get_session_factory()() as session:
        session.add(BundleRegionPrice(
            bundle_id=BUNDLE_22, region_code="CN", currency="USD", price=1000,
            price_status="ok", app_ids=[],
        ))
        await session.commit()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        bundles=[_bundle_row(BUNDLE_11)],
        bundle_prices=[
            _brp_row(BUNDLE_11, "CN"), _brp_row(BUNDLE_11, "US"),
            _brp_row(BUNDLE_22, "CN", 9999),
        ],
    )
    stats = await seed_assets.merge_bundles_seed(seed)
    assert stats == {"bundles": 1, "bundle_prices": 2}

    from app.core.database import sqlite_file_path

    con = sqlite3.connect(str(sqlite_file_path()))
    try:
        bundles = con.execute(
            "SELECT bundle_id, name FROM bundles WHERE bundle_id IN (?, ?) "
            "ORDER BY bundle_id", (BUNDLE_11, BUNDLE_22),
        ).fetchall()
        brp = con.execute(
            "SELECT bundle_id, region_code, price FROM bundle_region_prices "
            "WHERE bundle_id IN (?, ?) ORDER BY bundle_id, region_code",
            (BUNDLE_11, BUNDLE_22),
        ).fetchall()
    finally:
        con.close()
    assert bundles == [(BUNDLE_11, "包997011")]
    assert brp == [
        (BUNDLE_11, "CN", 1000), (BUNDLE_11, "US", 1000), (BUNDLE_22, "CN", 1000),
    ]
    # 幂等
    assert await seed_assets.merge_bundles_seed(seed) is None
