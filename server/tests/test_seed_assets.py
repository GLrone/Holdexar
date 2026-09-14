"""资产种子包行为验收（2026-09 M6 打包）。

覆盖五条链路（种子文件合成于 tmp_path，不碰 assets/seed；写库用合成
appid/币种 XTS，夹具清理——遵循 99xxxx 探针规约）：
1. export_seed.py 导出：白名单三表落种、同键汇率历史去重（末次胜出）、
   games_curated 只收人工列非空行
2. import_seed fx 并入幂等：差集追加 + 与现有行去重；同 version 二跑零开销
3. 人工列种子优先：已存在 games 行补挂；爬虫重爬（白名单不含人工列）不丢
4. 爬虫联动：upsert_game_and_prices 自动贴标记（无需显式调用）
5. 无种子文件零开销 no-op
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
from app.domains.games.models import Game

SYNTHETIC_CUR = "XTS"  # ISO 测试币种，不在任何白名单，误留也会被启动清洗掉
APPID_A = 997_102
APPID_B = 997_103
APPID_C = 997_104  # 价格历史合并用例专用（种子/本地行全合成）


@pytest.fixture
def fresh_library(monkeypatch) -> None:
    """把库伪装成「全新安装」（games=0）：一次性导入判定的前置。

    测试库是真实开发库（有存量 games 行），凡验证导入行为的用例必须
    挂本夹具；验证跳过行为的用例不挂（靠真实存量行）。
    """
    async def _zero() -> int:
        return 0

    monkeypatch.setattr(seed_assets, "_local_games_count", _zero)

SEED_VERSION = "2026-09-06 12:00:00"

_SCHEMA = """
CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE fx_rates (
    currency_code TEXT PRIMARY KEY, rate_to_cny REAL, fetched_at TEXT);
CREATE TABLE fx_rate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code TEXT, rate_to_cny REAL, source TEXT, fetched_at TEXT);
CREATE TABLE games_curated (
    appid INTEGER PRIMARY KEY, name TEXT, xgp_tier TEXT, epic_date TEXT,
    is_epic INTEGER, is_hb INTEGER, hb_data TEXT, series_id TEXT);
CREATE TABLE game_price_history (
    appid INTEGER, region_code TEXT, currency TEXT, price INTEGER,
    original_price INTEGER, discount_percent INTEGER, sub_id INTEGER,
    is_gold INTEGER, version_suffix TEXT, is_bundle INTEGER,
    price_status TEXT, cny_fen INTEGER, snapshot_at TEXT);
CREATE INDEX ix_seed_gph_key ON game_price_history (appid, region_code, snapshot_at);
"""


def _make_seed(
    path: Path,
    *,
    fx_rates: list[tuple] | None = None,
    fx_history: list[tuple] | None = None,
    curated: list[tuple] | None = None,
    history: list[tuple] | None = None,
    version: str = SEED_VERSION,
) -> Path:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    try:
        con.executescript(_SCHEMA)
        con.executemany("INSERT INTO fx_rates VALUES (?, ?, ?)", fx_rates or [])
        con.executemany(
            "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            fx_history or [],
        )
        con.executemany("INSERT INTO games_curated VALUES (?, ?, ?, ?, ?, ?, ?, ?)", curated or [])
        con.executemany(
            "INSERT INTO game_price_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            history or [],
        )
        con.executemany(
            "INSERT INTO seed_meta VALUES (?, ?)",
            [
                ("schema_version", "3"),
                ("version", version),
                ("exported_at", version),
                ("rows_fx_history", str(len(fx_history or []))),
                ("rows_history", str(len(history or []))),
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
    seed_assets.MARKER_KEY,
    seed_assets.CURATED_MARKER_KEY,
    seed_assets.HISTORY_MARKER_KEY,
)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    # setup：保存三个 marker 的真实值后清空——一次性导入语义下每个用例须从
    # 「无 marker」起步；但**绝不能只删不还**：本测试对真实开发库跑，marker
    # 是种子通道的幂等锚，被删后下一次应用启动会整链重跑（跳过判定、602 款
    # 名单合并、286 万行历史哨兵扫描全量过一遍），日志表现为「每次启动都
    # 在执行种子导入」——正是曾经发生过的真实污染（pytest 后启动必现）。
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
            delete(Game).where(Game.appid.in_([APPID_A, APPID_B, APPID_C]))
        )
        await session.execute(
            text(f"DELETE FROM fx_rate_history WHERE currency_code = '{SYNTHETIC_CUR}'")
        )
        await session.execute(
            text(f"DELETE FROM fx_rates WHERE currency_code = '{SYNTHETIC_CUR}'")
        )
        await session.execute(
            text(f"DELETE FROM game_price_history WHERE appid = {APPID_C}")
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
    # 同键两行（旧 source 值 → 新 source 值）：导出须保留末次
    con.executemany(
        "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("XTS", 3.0, "old", "2026-01-05 00:00:00"),
            ("XTS", 3.5, "new", "2026-01-05 00:00:00"),
            ("CNY", 1.0, None, "2026-01-06 00:00:00"),
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
    # 价格历史切片：同逻辑键（appid/区/时刻/sub/gold）不同价两行 → 保留最新 id；
    # 不同区 / 不同时刻 → 独立行
    con.executemany(
        "INSERT INTO game_price_history (appid, region_code, currency, price, "
        "original_price, discount_percent, sub_id, is_gold, price_status, cny_fen, "
        "snapshot_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (997201, "CN", "CNY", 100, 2000, 5, 1, 0, "ok", 100, "2026-01-05 00:00:00"),
            (997201, "CN", "CNY", 200, 2000, 10, 1, 0, "ok", 200, "2026-01-05 00:00:00"),
            (997201, "RU", "RUB", 300, 2000, 15, 1, 0, "ok", 21, "2026-01-05 00:00:00"),
            (997202, "CN", "CNY", 400, 4000, 10, 1, 0, "ok", 400, "2026-02-01 00:00:00"),
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
        # 同键去重：末次胜出
        rows = s.execute(
            "SELECT rate_to_cny, source FROM fx_rate_history "
            "WHERE currency_code='XTS' AND fetched_at='2026-01-05 00:00:00'"
        ).fetchall()
        assert rows == [(3.5, "new")]
        # NULL source 保留（CNY 行独立键）
        assert s.execute(
            "SELECT COUNT(*) FROM fx_rate_history WHERE currency_code='CNY'"
        ).fetchone()[0] == 1
        # 人工列只收非空行；name 身份列随行带出（供合并侧落名单行）
        assert s.execute(
            "SELECT name, xgp_tier, is_epic, hb_data FROM games_curated"
        ).fetchall() == [("正版名A", "GPU", 1, "HB24-1")]
        # 价格历史：同逻辑键保留最新 id（price 200 那行），不同区/时刻独立成行
        assert s.execute("SELECT COUNT(*) FROM game_price_history").fetchone()[0] == 3
        assert s.execute(
            "SELECT price, discount_percent FROM game_price_history "
            "WHERE appid=997201 AND region_code='CN' AND snapshot_at='2026-01-05 00:00:00'"
        ).fetchall() == [(200, 10)]
        # 白名单外数据零泄漏
        tables = {
            r[0]
            for r in s.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "steam_accounts" not in tables
        assert s.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 1
    finally:
        s.close()


# ── 2. 导入幂等 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_fx_idempotent_and_marker(tmp_path: Path, fresh_library) -> None:
    # 取一行现库原文，混进种子验证"与现有行去重"
    async with get_session_factory()() as session:
        existing = (
            await session.execute(
                text(
                    "SELECT currency_code, rate_to_cny, source, fetched_at "
                    "FROM fx_rate_history LIMIT 1"
                )
            )
        ).first()

    history = [(SYNTHETIC_CUR, 9.9, "seedtest", "2026-01-05 00:00:00")]
    if existing:
        history.append(tuple(existing))

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        fx_rates=[(SYNTHETIC_CUR, 9.9, "2026-01-05 00:00:00")],
        fx_history=history,
    )
    summary = await seed_assets.import_seed(seed)
    assert summary is not None
    assert summary["fx_history_new"] == 1  # 合成行新增；现库原文行被去重
    assert summary["fx_rates"] == 1

    async with get_session_factory()() as session:
        count = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM fx_rate_history WHERE currency_code='{SYNTHETIC_CUR}'")
            )
        ).scalar_one()
        rate = (
            await session.execute(
                text(f"SELECT rate_to_cny FROM fx_rates WHERE currency_code='{SYNTHETIC_CUR}'")
            )
        ).scalar_one()
    assert count == 1
    assert rate == 9.9

    # 同 version 二跑：marker 命中，零开销 no-op
    assert await seed_assets.import_seed(seed) is None
    # version 变化：重导仍幂等（差集为空）
    _make_seed(seed, fx_rates=[(SYNTHETIC_CUR, 9.9, "2026-01-05 00:00:00")],
               fx_history=history, version="2026-09-07 00:00:00")
    summary2 = await seed_assets.import_seed(seed)
    assert summary2 is not None and summary2["fx_history_new"] == 0


@pytest.mark.asyncio
async def test_import_missing_seed_noop(tmp_path: Path) -> None:
    assert await seed_assets.import_seed(tmp_path / "nope.db") is None


# ── 2.5 一次性导入：已有数据跳过 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_import_skipped_when_library_has_data(tmp_path: Path) -> None:
    """库中已有 games 行（不挂 fresh_library，靠开发库真实存量）→ 跳过导入。

    跳过后：种子数据零并入 + marker 记 skipped 形态 + 换新种子版本也不再导入。
    """
    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        fx_rates=[(SYNTHETIC_CUR, 9.9, "2026-01-05 00:00:00")],
        fx_history=[(SYNTHETIC_CUR, 9.9, "seedtest", "2026-01-05 00:00:00")],
    )
    summary = await seed_assets.import_seed(seed)
    assert summary is None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(seed_assets.MARKER_KEY)
    assert marker == f"skipped:{SEED_VERSION}"

    # 数据确实没进库
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM fx_rate_history WHERE currency_code='{SYNTHETIC_CUR}'")
            )
        ).scalar_one()
    assert count == 0

    # 换更新版本的种子：依然跳过（marker 前缀命中，永不合并）
    _make_seed(
        tmp_path / "holdexar_seed.db",
        fx_rates=[(SYNTHETIC_CUR, 8.8, "2026-02-05 00:00:00")],
        fx_history=[(SYNTHETIC_CUR, 8.8, "seedtest", "2026-02-05 00:00:00")],
        version="2026-10-01 00:00:00",
    )
    assert await seed_assets.import_seed(seed) is None
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM fx_rates WHERE currency_code='{SYNTHETIC_CUR}'")
            )
        ).scalar_one()
    assert count == 0


# ── 3/4. 人工列补挂 + 爬虫联动 ────────────────────────────────────────


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


# ── 4.5 名单行随种子落地（不进监控）───────────────────────────────────


@pytest.mark.asyncio
async def test_curated_list_rows_ship_without_monitoring(tmp_path: Path) -> None:
    """名单行合并（老用户路径，不挂 fresh_library）：
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


# ── 5. 价格历史合并（独立通道：老用户库也并入）─────────────────────────


def _gph_row(
    appid: int,
    snapshot_at: str,
    *,
    price: int = 1500,
    cny_fen: int | None = 1500,
    discount: int = 25,
    sub_id: int | None = 1,
    currency: str = "CNY",
    version_suffix: str | None = None,
) -> tuple:
    """13 列种子行（无 id）：appid, region, currency, price, original,
    discount, sub_id, is_gold, version_suffix, is_bundle, status, cny_fen, snapshot_at。"""
    return (
        appid, "CN", currency, price, 2000, discount, sub_id, 0, version_suffix, 0,
        "ok", cny_fen, snapshot_at,
    )


async def _gph_local_rows(appid: int) -> list:
    async with get_session_factory()() as session:
        rows = await session.execute(
            text(
                "SELECT region_code, currency, price, original_price, discount_percent, "
                "sub_id, is_gold, price_status, cny_fen, snapshot_at "
                f"FROM game_price_history WHERE appid = {appid} ORDER BY snapshot_at, sub_id"
            )
        )
        return rows.fetchall()


@pytest.mark.asyncio
async def test_history_merge_overwrite_and_insert(tmp_path: Path) -> None:
    """老用户库（不挂 fresh_library）合并：同键覆盖价格列 / 缺键插入 / 幂等。

    覆盖语义只动价格取值列；本地行自己的身份列（sub_id 等）保持不变。
    独立 marker 与一次性导入互不干扰（games 行数 > 0 不构成跳过条件）。
    """
    snap_a, snap_b = "2026-09-01 10:00:00", "2026-09-02 11:00:00"
    async with get_session_factory()() as session:
        # 本地已有两行：snap_a（同键，价格待覆盖）+ 无关区行（不应被动到）
        await session.execute(
            text(
                "INSERT INTO game_price_history (appid, region_code, currency, price, "
                "original_price, discount_percent, sub_id, is_gold, price_status, cny_fen, "
                "snapshot_at) VALUES (:a, 'CN', 'CNY', 1500, 2000, 25, 1, 0, 'ok', 1500, :s)"
            ),
            [{"a": APPID_C, "s": snap_a}, {"a": APPID_C, "s": snap_b}],
        )
        await session.commit()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        history=[
            # 同键（appid/region/snapshot/sub/gold 全同）→ 覆盖价格列
            _gph_row(APPID_C, snap_a, price=999, cny_fen=999, discount=50),
            # 缺键（新 snapshot）→ 插入
            _gph_row(APPID_C, "2026-09-03 12:00:00", price=888, cny_fen=888),
            # 缺键（sub_id NULL 与本地 1 归一后不同键）→ 插入
            _gph_row(APPID_C, snap_a, sub_id=None, price=777, cny_fen=777),
        ],
    )
    stats = await seed_assets.merge_history_seed(seed)
    assert stats == {"overwritten": 1, "inserted": 2}

    rows = await _gph_local_rows(APPID_C)
    by_key = {(r[9], r[5]) for r in rows}  # (snapshot_at, sub_id)
    assert ("2026-09-01 10:00:00", 1) in by_key
    # 覆盖后价格取值列 = 种子值
    assert any(r[9] == snap_a and r[5] == 1 and r[2] == 999 and r[4] == 50 for r in rows)
    # 本地独有行未被删（snap_b 仍在）
    assert any(r[9] == snap_b for r in rows)
    # 新增两行到位
    assert any(r[9] == "2026-09-03 12:00:00" for r in rows)
    assert any(r[5] is None and r[9] == snap_a for r in rows)

    from app.domains.settings import service as settings_service

    assert await settings_service.get_value(seed_assets.HISTORY_MARKER_KEY) == SEED_VERSION

    # 幂等：同版本种子二次合并零开销（marker 命中直接返回）
    assert await seed_assets.merge_history_seed(seed) is None
    rows_after = await _gph_local_rows(APPID_C)
    assert len(rows_after) == len(rows)


@pytest.mark.asyncio
async def test_history_merge_runs_despite_one_shot_skip(tmp_path: Path) -> None:
    """一次性导入写 skipped marker 后，价格历史通道不受影响（marker 相互独立）。"""
    from app.domains.settings import service as settings_service

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        fx_rates=[(SYNTHETIC_CUR, 9.9, "2026-01-05 00:00:00")],
        history=[_gph_row(APPID_C, "2026-09-01 10:00:00")],
    )
    # 开发库有存量 games 行 → 一次性导入走跳过
    assert await seed_assets.import_seed(seed) is None
    assert (
        await settings_service.get_value(seed_assets.MARKER_KEY)
        == f"skipped:{SEED_VERSION}"
    )
    # 价格历史照常并入
    stats = await seed_assets.merge_history_seed(seed)
    assert stats is not None and stats["inserted"] == 1
    rows = await _gph_local_rows(APPID_C)
    assert any(r[9] == "2026-09-01 10:00:00" for r in rows)


@pytest.mark.asyncio
async def test_history_merge_schema1_seed_noop(tmp_path: Path) -> None:
    """schema 1 旧种子（无价格历史表）：合并静默 no-op，不留 marker。"""
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
async def test_history_merge_backfills_blank_suffix(tmp_path: Path) -> None:
    """同键行版本名补空：本地空名被种子权威名回填（发行版用户库无爬虫
    通道，空名行只能靠本通道自愈——否则被「标准版」判据误收）；本地已有
    名不被旧种子快照拉回（本地爬虫持续更新，种子只补空不覆写）。"""
    snap = "2026-09-01 10:00:00"
    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO game_price_history (appid, region_code, currency, price, "
                "original_price, discount_percent, sub_id, is_gold, version_suffix, "
                "price_status, cny_fen, snapshot_at) VALUES "
                "(:a, 'CN', 'CNY', 1500, 2000, 0, 1, 0, NULL, 'ok', 1500, :s), "
                "(:a, 'CN', 'CNY', 1500, 2000, 0, 2, 0, :local_name, 'ok', 1500, :s)"
            ),
            {"a": APPID_C, "s": snap, "local_name": "本地已有版本名"},
        )
        await session.commit()

    seed = _make_seed(
        tmp_path / "holdexar_seed.db",
        history=[
            # 同键 sub=1：本地空名 → 种子名补空（价格列对齐，缺口纯由版本名构成）
            _gph_row(APPID_C, snap, sub_id=1, discount=0,
                     version_suffix="Digital Deluxe Edition"),
            # 同键 sub=2：本地有名 → 保留本地值（价格全同不触发更新）
            _gph_row(APPID_C, snap, sub_id=2, discount=0,
                     version_suffix="种子版本名"),
        ],
    )
    stats = await seed_assets.merge_history_seed(seed)
    # 仅空名行因版本名缺口触发 UPDATE；有名行值全同不重写
    assert stats == {"overwritten": 1, "inserted": 0}

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT sub_id, version_suffix FROM game_price_history "
                    f"WHERE appid = {APPID_C} ORDER BY sub_id"
                )
            )
        ).fetchall()
    by_sub = {int(r[0]): r[1] for r in rows}
    assert by_sub[1] == "Digital Deluxe Edition"  # 空名被种子补上
    assert by_sub[2] == "本地已有版本名"  # 本地名不被旧种子拉回
