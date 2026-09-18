"""schema 迁移链行为验收（user_version 版本账本）。

不碰开发库：tmp 目录独立 SQLite + monkeypatch get_settings.db_url /
get_session_factory / get_engine（对齐 test_backup.py / test_account_wallet
的独立库夹具模式）。验证三件事：
1. 存量库（user_version=0）首启登 v1，二次启动零迁移
2. 登记新迁移步骤后（v2），停在 v1 的库自动补跑差额、user_version 前进
3. 每步成功即落版本：步骤二失败时步骤一已生效（断点续跑语义）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module  # noqa: E402


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path, monkeypatch):
    """tmp 独立 SQLite 库：换 engine/session 工厂 + 重置迁移登记。"""
    db = tmp_path / "t.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db.as_posix()}", echo=False
    )

    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        database_module,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )

    saved_migrations = list(database_module._MIGRATIONS)
    saved_version = database_module.SCHEMA_VERSION
    database_module._MIGRATIONS.clear()
    yield db
    # 精确还原（先 clear 再 extend 会把本用例追加的测试步骤留在真实链里，
    # 随后任何 init_db（测试夹具直接打在生产库上）都会把它们真跑进去并把
    # user_version 推高——真实迁移步骤会被目标版本高于当前而永久跳过）
    database_module._MIGRATIONS[:] = saved_migrations
    database_module.SCHEMA_VERSION = saved_version
    await engine.dispose()


def _user_version(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


@pytest.mark.asyncio
async def test_ledger_bootstrap_and_idempotent(isolated_db: Path) -> None:
    """存量库（0）首启登 v1；空链二跑零迁移零前进。"""
    # 建最小 games 表（init_db 域模型导入链需要可建表库；此处直接全量 init）
    await database_module.init_db()
    assert _user_version(isolated_db) == 1

    # 二跑：版本未动（空链无增量）
    await database_module.init_db()
    assert _user_version(isolated_db) == 1


@pytest.mark.asyncio
async def test_migration_chain_applies_and_resumes(isolated_db: Path) -> None:
    """登记 v2/v3 步骤后：v1 库补跑全部差额；v3 半途失败 v2 已落账。"""
    await database_module.init_db()  # → v1

    database_module._MIGRATIONS.extend([
        (2, "v2 测试：建回填表", [
            "CREATE TABLE IF NOT EXISTS _mig_probe (id INTEGER PRIMARY KEY, v TEXT)",
        ]),
        (3, "v3 测试：回填行", [
            "INSERT INTO _mig_probe (v) VALUES ('v3-done')",
        ]),
    ])

    await database_module._run_schema_migrations()
    assert _user_version(isolated_db) == 3
    con = sqlite3.connect(str(isolated_db))
    try:
        assert con.execute("SELECT v FROM _mig_probe").fetchall() == [("v3-done",)]
    finally:
        con.close()

    # 断点续跑语义：v4 抛错 → user_version 停在 3，v4 变更未落；
    # 修好后重放只执行 v4（v2/v3 幂等吸收不重复执行）
    async def _boom(_conn):
        raise RuntimeError("v4 炸了")

    database_module._MIGRATIONS.append((4, "v4 测试：必失败", _boom))
    with pytest.raises(RuntimeError, match="v4 炸了"):
        await database_module._run_schema_migrations()
    assert _user_version(isolated_db) == 3

    database_module._MIGRATIONS[-1] = (
        4, "v4 测试：修复后", ["INSERT INTO _mig_probe (v) VALUES ('v4-done')"]
    )
    await database_module._run_schema_migrations()
    assert _user_version(isolated_db) == 4
    con = sqlite3.connect(str(isolated_db))
    try:
        rows = sorted(v for (v,) in con.execute("SELECT v FROM _mig_probe").fetchall())
        # v3 行不因重放翻倍（v3 已在账本内跳过，非靠 SQL 幂等兜底）
        assert rows == ["v3-done", "v4-done"]
    finally:
        con.close()


@pytest.mark.asyncio
async def test_snapshot_taken_only_when_migration_pending(isolated_db: Path) -> None:
    """无差额不落快照；有差额先落快照，且快照里是**迁移前**的版本号。

    「快照里的 user_version 小于迁移后」是这条用例的关键断言——只断言
    「文件存在」会漏掉「快照落在迁移之后」这种等于没备份的实现。
    """
    await database_module.init_db()  # 空链：无差额
    snap = isolated_db.with_name(isolated_db.name + ".pre-migration.bak")
    assert not snap.exists(), "无差额迁移不该落快照（每次启动都会覆盖一份）"

    database_module._MIGRATIONS.append(
        (2, "v2 测试：建回填表", ["CREATE TABLE IF NOT EXISTS _mig_probe (id INTEGER)"])
    )
    await database_module._run_schema_migrations()
    assert _user_version(isolated_db) == 2
    assert snap.is_file(), "有差额迁移却没落迁移前快照"

    # 快照必须自洽可独立打开，且停在迁移前的版本
    con = sqlite3.connect(str(snap))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        con.close()


# ── v4：game_price_history 幂等唯一索引 ──────────────────────────────
# 写入侧用 INSERT OR REPLACE 去重，靠的就是这个索引；此前它只手工建在开发库上，
# 代码里没有定义 → 新装的库没有约束，同日同 sub 反复堆积。

_GPH_DDL = (
    "CREATE TABLE game_price_history ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, appid BIGINT, region_code VARCHAR(10),"
    " currency VARCHAR(10), price BIGINT, original_price BIGINT,"
    " discount_percent INTEGER DEFAULT 0, sub_id INTEGER, is_gold BOOLEAN,"
    " version_suffix VARCHAR(100), is_bundle BOOLEAN, price_status VARCHAR(20),"
    " cny_fen BIGINT, snapshot_at DATETIME)"
)


def _index_names(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {n for (n,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='game_price_history'"
        )}
    finally:
        con.close()


@pytest.mark.asyncio
async def test_v4_creates_gph_unique_index(isolated_db: Path) -> None:
    """v4 在 create_all 出来的表上补建 ux_gph_snapshot（新库路径）。

    夹具清空了 _MIGRATIONS（防止测试步骤泄漏进真实链），故这里显式登记
    真实的 v4 步骤体——顺带确保登记用的就是生产里那个 callable。
    """
    database_module._MIGRATIONS.append(
        (4, "v4：game_price_history 幂等唯一索引", database_module._migrate_gph_snapshot_unique)
    )
    await database_module.init_db()
    assert _user_version(isolated_db) == 4
    assert "ux_gph_snapshot" in _index_names(isolated_db)


@pytest.mark.asyncio
async def test_v4_index_is_actually_enforcing(isolated_db: Path) -> None:
    """索引真的在约束写入：同 (appid, 区, 时刻, sub, 价, gold) 第二次落库被 REPLACE。

    这条是 ux_gph_snapshot 存在的唯一理由——只断言「索引存在」会漏掉
    表达式写错（例：漏 COALESCE 让 NULL 各自互不相等，索引形同虚设）。
    """
    database_module._MIGRATIONS.append(
        (4, "v4：game_price_history 幂等唯一索引", database_module._migrate_gph_snapshot_unique)
    )
    await database_module.init_db()
    con = sqlite3.connect(str(isolated_db))
    try:
        # 复刻写入侧的语句形态（裸 INSERT OR REPLACE，非 ORM：ORM 走普通 INSERT
        # 撞唯一索引会直接抛错，而真实路径要的正是 REPLACE 语义）
        row = (
            "INSERT OR REPLACE INTO game_price_history"
            " (appid, region_code, snapshot_at, sub_id, price, is_gold, is_bundle,"
            "  discount_percent, price_status)"
            " VALUES (?,?,?,?,?,0,0,0,'ok')"
        )
        con.execute(row, (1, "CN", "2026-01-01 00:00:00", None, 100))
        con.execute(row, (1, "CN", "2026-01-01 00:00:00", None, 100))
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM game_price_history").fetchone()[0]
        assert n == 1, f"NULL 列未被索引约束（COALESCE 漏了？），落库 {n} 行"
        # 同刻不同价仍是两行（促销价与原价各记一条是合法数据）
        con.execute(row, (1, "CN", "2026-01-01 00:00:00", None, 200))
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM game_price_history").fetchone()[0] == 2
    finally:
        con.close()


@pytest.mark.asyncio
async def test_v4_skips_index_when_duplicates_exist(isolated_db: Path) -> None:
    """库内已有重复行时**不删数据也不建索引**（只告警），避免静默删用户历史。"""
    # 先造一张无索引的表并塞入重复行——模拟「此前无约束写入」的存量库
    con = sqlite3.connect(str(isolated_db))
    try:
        con.execute(_GPH_DDL)
        for _ in range(2):
            con.execute(
                "INSERT INTO game_price_history (appid, region_code, snapshot_at, sub_id, price)"
                " VALUES (1, 'CN', '2026-01-01 00:00:00', NULL, 100)"
            )
        con.commit()
    finally:
        con.close()

    # 只跑 v4 这一步（init_db 会 create_all，把那两行留在原地即可）
    conn_engine = database_module.get_engine()
    async with conn_engine.begin() as conn:
        await database_module._migrate_gph_snapshot_unique(conn)

    assert "ux_gph_snapshot" not in _index_names(isolated_db)
    con = sqlite3.connect(str(isolated_db))
    try:
        assert con.execute("SELECT COUNT(*) FROM game_price_history").fetchone()[0] == 2
    finally:
        con.close()


# ── v5：wishlist_items 愿望单成员标记回填 ──────────────────────────
#
# 监控池三模块语义下，愿望单（wishlisted）与星标关注（manual）是爬取队列
# 第一优先级。存量库的新列由 _ensure_schema 加，但「哪些行是愿望单来源」
# 只有迁移链知道：活跃且非已购、非关注的行全部来自愿望单同步（手动入池
# 功能此前不存在），必须回填——否则升级后老愿望单条目掉出第一优先级。


@pytest.mark.asyncio
async def test_v5_backfills_wishlist_member_flag(tmp_path: Path, monkeypatch) -> None:
    """存量库（v0）首启跑到 v5：活跃愿望单行回填 wishlisted=1，
    关注/已购/脱池行不回填。**走真实迁移链**（v5 步骤就是生产那一条）。"""
    db = tmp_path / "legacy.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db.as_posix()}", echo=False
    )
    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        database_module,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )
    # 迁移前形态：旧列结构 + 存量行
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE wishlist_items ("
            " steamid VARCHAR(20), appid BIGINT, added_at DATETIME,"
            " active BOOLEAN, owned BOOLEAN, manual BOOLEAN,"
            " PRIMARY KEY (steamid, appid))"
        )
        con.execute("INSERT INTO wishlist_items VALUES ('1', 100, NULL, 1, 0, 0)")  # 愿望单
        con.execute("INSERT INTO wishlist_items VALUES ('1', 200, NULL, 1, 0, 1)")  # 关注
        con.execute("INSERT INTO wishlist_items VALUES ('1', 300, NULL, 1, 1, 0)")  # 已购
        con.execute("INSERT INTO wishlist_items VALUES ('1', 400, NULL, 0, 0, 0)")  # 已脱池
        con.commit()
    finally:
        con.close()

    await database_module.init_db()
    try:
        assert _user_version(db) == database_module.SCHEMA_VERSION
        con = sqlite3.connect(str(db))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(wishlist_items)")}
            assert {"wishlisted", "manual_pool", "excluded"} <= cols
            rows = dict(
                con.execute("SELECT appid, wishlisted FROM wishlist_items").fetchall()
            )
        finally:
            con.close()
        assert rows == {100: 1, 200: 0, 300: 0, 400: 0}, (
            "活跃且非已购非关注的行应回填为愿望单成员；关注/已购/脱池行不动。"
            f"实际 {rows}"
        )
    finally:
        await engine.dispose()


# ── v7：proxy_nodes 身份/来源分表 ────────────────────────────────────
#
# 早期 proxy_nodes 同时挂了单列 subscription_id（NOT NULL）与全局唯一
# fingerprint：同一份配置被两个订阅提供时第二条插不进去（撞唯一约束），
# 「订阅 A 撤掉、订阅 B 仍在提供」又会被读成节点消失（把来源当身份）。
# 身份模型定死为「节点表只留身份 + 来源归属进 proxy_node_sources」，
# 本步清理遗留列，并补齐指纹唯一性（老库是「有 index 无 unique」建的，
# 唯一性其实没被约束）。两件事都只在表内无行时动，绝不删用户数据。

_LEGACY_PROXY_NODES_DDL = (
    "CREATE TABLE proxy_nodes ("
    " id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
    " node_id VARCHAR(64) NOT NULL,"
    " subscription_id INTEGER NOT NULL,"
    " fingerprint VARCHAR(64) NOT NULL,"
    " runtime_name VARCHAR(400) NOT NULL,"
    " original_name VARCHAR(255) NOT NULL,"
    " proxy_type VARCHAR(32) NOT NULL,"
    " server VARCHAR(255),"
    " normalized_config JSON,"
    " state VARCHAR(16),"
    " first_seen DATETIME, last_seen DATETIME, last_source_seen DATETIME,"
    " last_l0_at DATETIME, last_l1_at DATETIME, last_l2_at DATETIME,"
    " exit_ip VARCHAR(64), exit_group_id INTEGER,"
    " consecutive_failures INTEGER, capacity_score FLOAT,"
    " UNIQUE (node_id), UNIQUE (runtime_name))"
)
# 遗留索引：普通（非唯一）指纹索引 + 两个依赖归属列的索引
_LEGACY_PROXY_NODES_INDEXES = (
    "CREATE INDEX ix_proxy_nodes_fingerprint ON proxy_nodes(fingerprint)",
    "CREATE INDEX ix_proxy_nodes_subscription_id ON proxy_nodes(subscription_id)",
    "CREATE INDEX ix_pn_sub_state ON proxy_nodes(subscription_id, state)",
)


def _columns_of(db: Path, table: str) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _indexes_of(db: Path, table: str) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {r[1] for r in con.execute(f"PRAGMA index_list({table})")}
    finally:
        con.close()


def _legacy_proxy_nodes_db(db: Path, monkeypatch, rows: list[tuple]):
    """建一个「迁移前形态」的 proxy_nodes（带遗留列与遗留索引），可预置行。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}", echo=False)
    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        database_module,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )
    con = sqlite3.connect(str(db))
    try:
        con.execute(_LEGACY_PROXY_NODES_DDL)
        for ddl in _LEGACY_PROXY_NODES_INDEXES:
            con.execute(ddl)
        for row in rows:
            con.execute(
                "INSERT INTO proxy_nodes (node_id, subscription_id, fingerprint,"
                " runtime_name, original_name, proxy_type, state)"
                " VALUES (?,?,?,?,?,?,?)",
                row,
            )
        con.commit()
    finally:
        con.close()
    return engine


@pytest.mark.asyncio
async def test_v7_repairs_legacy_proxy_nodes(tmp_path: Path, monkeypatch) -> None:
    """遗留库：清掉 NOT NULL 归属列（否则新模型插不进去）+ 补指纹唯一约束。"""
    db = tmp_path / "legacy_nodes.db"
    engine = _legacy_proxy_nodes_db(db, monkeypatch, [])

    await database_module.init_db()
    assert _user_version(db) == database_module.SCHEMA_VERSION

    cols = _columns_of(db, "proxy_nodes")
    assert not {"subscription_id", "original_name"} & cols, (
        "遗留 NOT NULL 归属列没清掉——新模型的插入会直接撞 NOT NULL"
    )
    assert {"node_id", "fingerprint", "runtime_name", "state"} <= cols

    indexes = _indexes_of(db, "proxy_nodes")
    assert "ux_proxy_nodes_fingerprint" in indexes, "指纹唯一性没补上"
    assert not {"ix_proxy_nodes_fingerprint", "ix_pn_sub_state",
                "ix_proxy_nodes_subscription_id"} & indexes, "遗留索引没清掉"

    # 行为验证：新模型的插入不再撞 NOT NULL，且同一指纹插不进第二行。
    # （唯一性必须真被约束住，不能只是「有个索引」）
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO proxy_nodes (node_id, fingerprint, runtime_name, proxy_type)"
            " VALUES ('a', 'fp-1', '1|A', 'vmess')"
        ))
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO proxy_nodes (node_id, fingerprint, runtime_name, proxy_type)"
                " VALUES ('b', 'fp-1', '1|B', 'vmess')"
            ))
    await engine.dispose()


@pytest.mark.asyncio
async def test_v7_skips_structural_change_when_legacy_table_has_rows(
    tmp_path: Path, monkeypatch
) -> None:
    """有行则整步跳过：遗留列不是本链预期形态，宁可不动也不删用户数据。"""
    db = tmp_path / "legacy_nodes_with_rows.db"
    engine = _legacy_proxy_nodes_db(
        db, monkeypatch,
        [("a", 1, "fp-1", "1|A", "A", "vmess", "ACTIVE")],
    )

    await database_module.init_db()

    cols = _columns_of(db, "proxy_nodes")
    assert {"subscription_id", "original_name"} <= cols, "有行时不得删列"
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM proxy_nodes").fetchone()[0] == 1
    finally:
        con.close()
    await engine.dispose()
