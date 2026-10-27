"""小种子（holdexar_mini_seed.db）导入验收：空库恢复 / 未抓价真实 /
union-only 不覆盖 / 幂等 / 捆绑身份保真 / 不进爬取队列 / 无价格数据。

隔离：conftest 临时数据目录 + 合成小种子夹具（导出器同 schema），全程无网络。
"""
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import mini_seed
from app.core.database import get_session_factory
from app.domains.games.models import Bundle, Game, GamePriceHistory, GameCurrentPrice
from app.domains.monitoring.models import MonitorTarget
from app.domains.settings import service as settings_service

APPID_NEW = 998_001   # 仅在种子里（空库导入场景）
APPID_OLD = 998_002   # 本地已有行（union-only 场景）
BID_NEW = 998_101
BID_OLD = 998_102
STAMP = "2026-10-25T04:51:19.313341+00:00"


def _make_seed(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE catalog (
            appid INTEGER PRIMARY KEY, name TEXT, name_en TEXT, type TEXT
        );
        CREATE TABLE curated (
            appid INTEGER PRIMARY KEY, xgp_tier TEXT, epic_date TEXT,
            is_epic INTEGER, is_hb INTEGER, hb_data TEXT, series_id TEXT
        );
        CREATE TABLE bundle_index (
            bundle_id INTEGER NOT NULL, item_kind INTEGER,
            name TEXT, app_ids TEXT, must_purchase_as_set INTEGER,
            PRIMARY KEY (bundle_id)
        );
        """
    )
    con.executemany(
        "INSERT INTO catalog VALUES (?,?,?,?)",
        [
            (APPID_NEW, "种子新游戏", "Seed New", "GAME"),
            (APPID_OLD, "本地已有游戏", "Local Existing", "GAME"),
        ],
    )
    con.executemany(
        "INSERT INTO curated VALUES (?,?,?,?,?,?,?)",
        [
            # 新行：xgp 权威覆写 + epic 正向并集
            (APPID_NEW, "core", None, 0, 0, None, "seed-series"),
            # 老行：种子想降级（本地已有更新事实，必须保持本地）
            (APPID_OLD, None, "2020-01-01", 0, 1, None, "seed-tries-downgrade"),
        ],
    )
    con.executemany(
        "INSERT INTO bundle_index VALUES (?,?,?,?,?)",
        [
            # 新包：item_kind=-1 未知保真；app_ids JSON 文本
            (BID_NEW, -1, "种子新捆绑包", "[10, 80]", 0),
            # 本地已有 bundle_id：不覆盖
            (BID_OLD, 1, "种子版本名（不得覆盖）", "[1]", 1),
        ],
    )
    con.executemany(
        "INSERT INTO seed_meta VALUES (?,?)",
        [
            ("schema_version", "1"),
            ("exported_at_utc", STAMP),
            ("source_data_as_of", "2026-10-16"),
            ("price_history_included", "false"),
            ("current_prices_included", "false"),
            ("rows_catalog", "2"),
            ("rows_curated", "2"),
            ("rows_bundle_index", "2"),
        ],
    )
    con.commit()
    con.close()


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "holdexar_mini_seed.db"
    _make_seed(path)
    return path


async def _seed_local_facts():
    """本地既有事实：已有游戏行（更新的事实不被种子降级）+ 已有捆绑包。"""
    async with get_session_factory()() as session:
        session.add_all([
            Game(
                appid=APPID_OLD, name="本地已有游戏",
                is_epic=True, epic_date="2026-10-01 ~ 2026-10-08",
                is_hb=False, hb_data="", series_id="local-series",
            ),
            Bundle(bundle_id=BID_OLD, name="本地捆绑包", item_kind=0,
                   must_purchase_as_set=0, app_ids=[7, 8]),
        ])
        await session.commit()


async def _counts():
    async with get_session_factory()() as session:
        games = (await session.execute(select(func.count()).select_from(Game))).scalar_one()
        bundles = (await session.execute(select(func.count()).select_from(Bundle))).scalar_one()
        prices = (await session.execute(select(func.count()).select_from(GameCurrentPrice))).scalar_one()
        history = (await session.execute(select(func.count()).select_from(GamePriceHistory))).scalar_one()
        monitored = (await session.execute(select(func.count()).select_from(MonitorTarget))).scalar_one()
        return games, bundles, prices, history, monitored


@pytest.mark.asyncio
async def test_empty_db_import_recovers_catalog_curated_bundles(seed_file):
    """空库导入：目录/人工列/捆绑主档齐备；未抓价真实（现价与历史 0 行、
    updated_at=种子时刻而非当下）；不写任何监控目标（目录不进价格队列）。"""
    stats = await mini_seed.import_mini_seed(seed_file)
    # 目录段先落两行，人工列段随后两行都可命中；捆绑两包本地皆无 → 全新落
    assert stats == {"games": 2, "curated": 2, "bundles": 2}

    games, bundles, prices, history, monitored = await _counts()
    assert games == 2 and bundles == 2
    assert prices == 0 and history == 0      # 未抓价真实；无任何价格数据
    assert monitored == 0                    # 目录全集不进爬取队列

    async with get_session_factory()() as session:
        row = await session.get(Game, APPID_NEW)
        assert row.name == "种子新游戏" and row.type == "GAME"
        assert row.xgp_tier == "core"        # xgp 无本地写者：权威覆写
        assert row.smart_score is not None
        assert row.series_id == "seed-series"
        assert row.updated_at == datetime.fromisoformat(STAMP.replace("+00:00", ""))

    marker = await settings_service.get_value(mini_seed.MARKER_KEY)
    assert marker == STAMP


@pytest.mark.asyncio
async def test_catalog_import_groups_new_series_and_rolls_back_derived_failure(seed_file, monkeypatch):
    from app.domains.games import derived

    with sqlite3.connect(seed_file) as source:
        source.executemany("UPDATE catalog SET name=?,name_en=? WHERE appid=?", [
            ("Alpha Story 1", "Alpha Story 1", APPID_NEW),
            ("Alpha Story 2", "Alpha Story 2", APPID_OLD),
        ])
        source.execute("UPDATE curated SET series_id=NULL")

    actual = derived.update_price_derived

    async def interrupted(*_args, **_kwargs):
        raise RuntimeError("interrupted before derived commit")

    monkeypatch.setattr(derived, "update_price_derived", interrupted)
    assert await mini_seed.import_mini_seed(seed_file) is None
    assert (await _counts())[0] == 0
    assert await settings_service.get_value(mini_seed.MARKER_KEY) is None

    monkeypatch.setattr(derived, "update_price_derived", actual)
    assert (await mini_seed.import_mini_seed(seed_file))["games"] == 2
    async with get_session_factory()() as session:
        first = await session.get(Game, APPID_NEW)
        second = await session.get(Game, APPID_OLD)
        assert first.series_id and first.series_id == second.series_id
        assert first.smart_score is not None and second.smart_score is not None


@pytest.mark.asyncio
async def test_repeat_import_idempotent_and_new_version_merges(seed_file, monkeypatch):
    """同版本幂等空转；换版本重并仍幂等（已有行不重复、不回拨）。"""
    await mini_seed.import_mini_seed(seed_file)
    assert await mini_seed.import_mini_seed(seed_file) is None  # marker 命中跳过

    # 换版本（模拟种子更新）：重并，目录零新增（已在位）
    con = sqlite3.connect(seed_file)
    con.execute("UPDATE seed_meta SET value=? WHERE key='exported_at_utc'",
                ("2026-11-01T00:00:00+00:00",))
    con.commit(); con.close()
    stats = await mini_seed.import_mini_seed(seed_file)
    # 已在位行零新增；人工列重并幂等（union-only 对已饱和事实是恒等操作）
    assert stats == {"games": 0, "curated": 2, "bundles": 0}
    games, _, _, _, _ = await _counts()
    assert games == 2


@pytest.mark.asyncio
async def test_union_only_never_downgrades_local_facts(seed_file):
    """union-only：本地更新事实（Epic 日期/系列归属/捆绑包身份）不被旧快照
    降级或覆盖；正向并集（种子有而本地无的仍补齐）。"""
    await _seed_local_facts()
    stats = await mini_seed.import_mini_seed(seed_file)
    # 本地已有 APPID_OLD/BID_OLD：目录与捆绑只补缺（各 +1）；
    # 人工列两行都在场（老行本地在场、新行刚被目录段落位）
    assert stats == {"games": 1, "curated": 2, "bundles": 1}

    async with get_session_factory()() as session:
        row = await session.get(Game, APPID_OLD)
        assert row.epic_date == "2026-10-01 ~ 2026-10-08"  # 本地更新日期保住
        assert row.series_id == "local-series"              # 本地系列保住
        assert row.is_hb is True                            # 种子正向并集（1 生效）
        bundle = (await session.execute(
            select(Bundle).where(Bundle.bundle_id == BID_OLD)
        )).scalar_one()
        assert bundle.name == "本地捆绑包"                   # 主档不覆盖
        assert bundle.app_ids == [7, 8]


@pytest.mark.asyncio
async def test_bundle_identity_fidelity(seed_file):
    """捆绑身份保真：item_kind=-1（未知）原样保留不猜；app_ids 从 JSON 文本
    解析为列表（不双重编码）。"""
    await mini_seed.import_mini_seed(seed_file)
    async with get_session_factory()() as session:
        bundle = (await session.execute(
            select(Bundle).where(Bundle.bundle_id == BID_NEW)
        )).scalar_one()
        assert bundle.item_kind == -1
        assert bundle.must_purchase_as_set == 0
        assert bundle.app_ids == [10, 80]     # 列表而非 '["10","80"]' 字符串


@pytest.mark.asyncio
async def test_bad_seed_rejected(tmp_path):
    """坏档拒绝：结构不符 / 声明含价格 → 按无种子处理，零写入零 marker。"""
    # 表集多一张价格表（结构不符 + 声明含价双违规）
    path = tmp_path / "holdexar_mini_seed.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE catalog (appid INTEGER PRIMARY KEY, name TEXT, name_en TEXT, type TEXT);
        CREATE TABLE curated (appid INTEGER PRIMARY KEY);
        CREATE TABLE bundle_index (bundle_id INTEGER PRIMARY KEY);
        CREATE TABLE sneaky_prices (appid INTEGER);
        INSERT INTO seed_meta VALUES ('schema_version','1');
        INSERT INTO seed_meta VALUES ('exported_at_utc','2026-10-25T00:00:00+00:00');
        INSERT INTO seed_meta VALUES ('price_history_included','true');
        INSERT INTO seed_meta VALUES ('current_prices_included','false');
        """
    )
    con.commit(); con.close()
    assert await mini_seed.import_mini_seed(path) is None
    games, _, _, _, _ = await _counts()
    assert games == 0
    assert await settings_service.get_value(mini_seed.MARKER_KEY) is None
