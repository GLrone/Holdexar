"""备份功能测试：VACUUM INTO 快照语义 / 轮转 / 路径穿越防护 / 三段式恢复。

用户核心担忧逐条验证：
- 「备份不最新」→ 快照必须包含 WAL 已提交未 checkpoint 的数据
- 「备份不完整」→ 快照独立打开 integrity_check 必须通过、表行数与源库一致
- 「误恢复损坏生产库」→ 校验不过的备份拒绝恢复；恢复成功后原库内容被替换；
  路径穿越文件名拒绝
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import backup as core_backup
from app.core.config import get_settings


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    """临时 data 目录 + 造一个带 WAL 未 checkpoint 写入的库。

    关键：写入后保持连接不关也不 checkpoint——模拟「SQLite 一直占用、
    最近写入还在 -wal 文件里」的生产形态。
    """
    db = tmp_path / "holdexar.db"
    conn = sqlite3.connect(db.as_posix(), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE games (appid INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO games VALUES (620, 'Portal 2')")
    conn.commit()
    # 模拟持续占用：连接保持打开，写入留在 WAL
    holder = {"conn": conn}

    class _FakeSettings:
        data_dir = tmp_path
        db_filename = "holdexar.db"

    monkeypatch.setattr(core_backup, "get_settings", lambda: _FakeSettings())
    # 常数缩到 3 便于轮转测试；总量上限放大，让份数用例不被体积上限干扰
    monkeypatch.setattr(core_backup, "BACKUP_KEEP", 3)
    monkeypatch.setattr(core_backup, "BACKUP_TOTAL_CAP_RATIO", 1e18)
    yield tmp_path, holder
    conn.close()


def _wal_has_pending(db: Path) -> bool:
    """-wal 文件存在且非空 = 有未 checkpoint 的已提交事务。"""
    wal = db.with_name(db.name + "-wal")
    return wal.exists() and wal.stat().st_size > 0


@pytest.mark.asyncio
async def test_snapshot_includes_wal_committed_data(data_env):
    """担忧①最新性：WAL 未 checkpoint 的写入必须进快照（copy 主文件会丢）。"""
    tmp, holder = data_env
    db = tmp / "holdexar.db"
    # 占用连接再写入（数据只落 -wal，主文件没有）
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].execute("INSERT INTO games VALUES (400, 'Portal')")
    holder["conn"].commit()
    assert _wal_has_pending(db), "前置失败：写入未滞留 WAL（测试环境不成立）"

    result = await core_backup.create_backup()
    assert result["integrityOk"] is True
    assert result["games"] == 3  # 620 + 440 + 400（WAL 里的 2 条必须在）


@pytest.mark.asyncio
async def test_snapshot_is_standalone_and_consistent(data_env):
    """担忧②完整性：备份独立打开 integrity 通过、行数与源一致。"""
    tmp, holder = data_env
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].commit()

    result = await core_backup.create_backup()
    # 备份文件自洽：没有 -wal 伴随文件（VACUUM INTO 产出独立单文件）
    backup_path = Path(result["path"])
    assert backup_path.is_file()
    assert not backup_path.with_name(backup_path.name + "-wal").exists()

    conn = sqlite3.connect(backup_path.as_posix())
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_rotation_keeps_recent(data_env):
    """轮转：只保留最近 BACKUP_KEEP(=3) 份。"""
    tmp, _ = data_env
    for i in range(5):
        # 名称时间戳同秒会撞名——直接造不同 label
        await core_backup.create_backup(label=f"t{i}")
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 3
    assert any(f"t4" in n for n in names)  # 最新的保留
    assert not any(f"t0" in n for n in names)  # 最老的清掉


@pytest.mark.asyncio
async def test_manual_backup_keeps_only_latest(data_env, monkeypatch):
    """手动备份独立轮转：手动备份多次仅保留最新一份（-manual 标记）。"""
    tmp, _ = data_env
    seq = iter(range(10))
    monkeypatch.setattr(core_backup, "_ts", lambda: f"20260101-0000{next(seq)}")
    for _ in range(3):
        await core_backup.create_backup(manual=True)
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 1
    assert names[0].endswith("-manual.db")
    assert "-00002-manual.db" in names[0]  # 留的是最新那份


@pytest.mark.asyncio
async def test_manual_backup_independent_of_auto_rotation(data_env, monkeypatch):
    """两类互不占位：自动轮转不清手动备份，手动替换只换手动。"""
    tmp, _ = data_env
    seq = iter(range(10))
    monkeypatch.setattr(core_backup, "_ts", lambda: f"20260101-0000{next(seq)}")
    for i in range(4):  # BACKUP_KEEP=3：第 4 份自动备份轮掉 a0
        await core_backup.create_backup(label=f"a{i}")
    await core_backup.create_backup(manual=True)
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 4  # 3 自动 + 1 手动
    assert not any("a0" in n for n in names)
    assert sum(1 for n in names if n.endswith("-manual.db")) == 1
    # 再手动一次：旧手动被顶替，自动 3 份原样
    await core_backup.create_backup(manual=True)
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 4
    assert sum(1 for n in names if n.endswith("-manual.db")) == 1
    assert any("a3" in n for n in names)


@pytest.mark.asyncio
async def test_rotation_caps_total_size(data_env, monkeypatch):
    """总量上限：主库体积×倍数封顶时只留最新的 BACKUP_MIN_KEEP 份。"""
    tmp, _ = data_env
    monkeypatch.setattr(core_backup, "BACKUP_TOTAL_CAP_RATIO", 2.0)
    # 上限钉到 2KB：任何一份备份都会超——正好验证「至少保留 2 份」的下限
    monkeypatch.setattr(core_backup, "_main_db_bytes", lambda: 1024)
    seq = iter(range(10))
    monkeypatch.setattr(core_backup, "_ts", lambda: f"20260101-0000{next(seq)}")
    for i in range(4):
        await core_backup.create_backup(label=f"c{i}")
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 2
    assert any("c3" in n for n in names)  # 最新一份必在
    assert not any("c0" in n for n in names)  # 最老的被清


@pytest.mark.asyncio
async def test_path_traversal_rejected(data_env):
    """担忧③安全性：路径穿越/非法文件名一律拒绝。"""
    from app.core.backup import safe_backup_path

    with pytest.raises(ValueError):
        safe_backup_path(core_backup.backup_dir(), "../holdexar.db")
    with pytest.raises(ValueError):
        safe_backup_path(core_backup.backup_dir(), "..\\holdexar.db")
    with pytest.raises(ValueError):
        safe_backup_path(core_backup.backup_dir(), "sub/holdexar.db")
    with pytest.raises(ValueError):
        safe_backup_path(core_backup.backup_dir(), "evil.exe")


@pytest.mark.asyncio
async def test_corrupt_backup_rejected_for_restore(data_env):
    """担忧③误恢复：integrity 不过的备份文件拒绝恢复。"""
    tmp, _ = data_env
    # 造一个损坏的「备份」文件
    bad = core_backup.backup_dir() / "bad.db"
    bad.write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(RuntimeError, match="校验失败"):
        await core_backup.restore_backup("bad.db")


@pytest.mark.asyncio
async def test_restore_replaces_production_db(data_env, monkeypatch):
    """恢复闭环：停写入面 → 替换 → 引擎重建 → 内容等于备份时刻。

    调度器/引擎交互 mock 掉（进程内 APScheduler/aiosqlite 全链路在
    E2E 验证，单元层只验文件轮换与数据正确性）。
    """
    tmp, holder = data_env
    db = tmp / "holdexar.db"

    # 1) 先备份（此刻只有 620）
    result = await core_backup.create_backup(label="base")
    backup_name = result["name"]

    # 2) 生产库继续演进（新增 440——备份里没有）
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].commit()

    # 3) 恢复：mock 掉引擎/调度器层（单元测试不碰全局状态）
    import app.core.database as database_module

    async def _noop_dispose():
        pass

    async def _noop_rebuild():
        return True

    class _FakeSched:
        def __init__(self):
            self.calls = []

        def __call__(self):
            self.calls.append(1)

    fake_sched = _FakeSched()
    monkeypatch.setattr(core_backup, "_dispose_engine", _noop_dispose)
    monkeypatch.setattr(core_backup, "_rebuild_engine", _noop_rebuild)
    import app.core.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "stop_scheduler", fake_sched)
    monkeypatch.setattr(sched_mod, "start_scheduler", fake_sched)
    import app.domains.crawl.service as crawl_service

    monkeypatch.setattr(crawl_service, "_active", None)

    # 占用连接必须先关——恢复前模拟「停服」释放句柄
    holder["conn"].close()

    res = await core_backup.restore_backup(backup_name)
    assert res["restored"] is True

    # 4) 生产库内容回到备份时刻：只有 620，没有 440
    conn = sqlite3.connect(db.as_posix())
    try:
        rows = conn.execute("SELECT appid FROM games ORDER BY appid").fetchall()
        assert rows == [(620,)]
    finally:
        conn.close()

    # 陈旧 WAL 已清理（新库打开不会重放旧帧）
    assert not db.with_name(db.name + "-wal").exists()
    # 成功后回滚档清理
    assert not db.with_suffix(".rollback.db").exists()
    assert not db.with_suffix(".restoring.db").exists()
    assert len(fake_sched.calls) == 2  # stop + start
