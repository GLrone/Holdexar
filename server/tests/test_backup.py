"""备份功能测试：VACUUM INTO 快照 + gzip 归档与哈希校验 / 轮转 / 路径穿越防护 / 三段式恢复。

用户核心担忧逐条验证：
- 「备份不最新」→ 快照必须包含 WAL 已提交未 checkpoint 的数据
- 「备份不完整」→ 归档解档后 integrity 通过、表行数与源库一致；随档哈希能识破篡改
- 「误恢复损坏生产库」→ 校验不过/哈希不符的备份拒绝恢复；恢复成功后原库内容被替换；
  路径穿越文件名拒绝；升级前的未压缩旧档仍可用
- 「恢复中途失败」→ 主库句柄被占用时先重关引擎再重试；替换未成功则不留残留档
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import backup as core_backup, db_safety
from app.core.config import get_settings


@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime")
def test_exited_process_with_open_handle_is_not_a_live_lock_owner():
    process = subprocess.Popen([sys.executable, "-c", "pass"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    process.wait(timeout=5)
    assert process._handle is not None
    assert db_safety._pid_is_running(process.pid) is False


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
async def test_snapshot_is_compressed_and_consistent(data_env):
    """担忧②完整性：快照压成 gzip 归档，解档后 integrity 通过、行数与源一致。"""
    tmp, holder = data_env
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].commit()

    result = await core_backup.create_backup()
    archive = Path(result["path"])
    assert archive.name.endswith(".db.gz")
    assert archive.is_file()
    # 未压缩中间档不留（否则备份目录里同时躺着两份同内容库）
    assert not archive.with_name(archive.name[: -len(".gz")]).exists()
    # 哈希随档走
    meta = core_backup._read_archive_meta(archive)
    assert meta["sha256"] == result["sha256"]
    assert meta["games"] == 2
    assert meta["rawBytes"] > archive.stat().st_size  # 压缩确实省了体积

    probe = tmp / "opened.db"
    core_backup._decompress_archive(archive, probe)
    conn = sqlite3.connect(probe.as_posix())
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
    finally:
        conn.close()
    # 归档自洽：单文件即可解，无 -wal 伴随
    assert not archive.with_name(archive.name + "-wal").exists()


@pytest.mark.asyncio
async def test_rotation_keeps_recent(data_env):
    """轮转：只保留最近 BACKUP_KEEP(=3) 份。"""
    tmp, _ = data_env
    for i in range(5):
        # 名称时间戳同秒会撞名——直接造不同 label
        await core_backup.create_backup(label=f"t{i}")
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 3
    assert any("t4" in n for n in names)  # 最新的保留
    assert not any("t0" in n for n in names)  # 最老的清掉


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
    assert names[0].endswith("-manual.db.gz")
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
    assert sum(1 for n in names if n.endswith("-manual.db.gz")) == 1
    # 再手动一次：旧手动被顶替，自动 3 份原样
    await core_backup.create_backup(manual=True)
    names = [b["name"] for b in core_backup.list_backups()]
    assert len(names) == 4
    assert sum(1 for n in names if n.endswith("-manual.db.gz")) == 1
    assert any("a3" in n for n in names)


@pytest.mark.asyncio
async def test_rotation_caps_total_size(data_env, monkeypatch):
    """总量上限：主库体积×倍数封顶时只留最新的 BACKUP_MIN_KEEP 份。"""
    tmp, _ = data_env
    monkeypatch.setattr(core_backup, "BACKUP_TOTAL_CAP_RATIO", 2.0)
    # 上限钉到比任何一档都小（压缩档体积随数据形态变化，不按固定 KB 钉）——
    # 正好验证「总量受限时仍至少保留 BACKUP_MIN_KEEP 份」的下限
    monkeypatch.setattr(core_backup, "_main_db_bytes", lambda: 1)
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
    # 归档后缀合法（.db.gz），元数据后缀不放开给恢复入口
    assert safe_backup_path(core_backup.backup_dir(), "a.db.gz").name == "a.db.gz"
    with pytest.raises(ValueError):
        safe_backup_path(core_backup.backup_dir(), "a.db.gz.json")


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
async def test_restore_rejects_backup_from_newer_schema_before_replacing(data_env):
    tmp, holder = data_env
    current = tmp / "holdexar.db"
    backup = core_backup.backup_dir() / "newer-schema.db"
    from app.core import database as database_module

    supported = max(version for version, _, _ in database_module._MIGRATIONS)
    conn = sqlite3.connect(backup.as_posix())
    try:
        conn.execute("CREATE TABLE games (appid INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO games VALUES (620, 'Portal 2')")
        conn.execute(f"PRAGMA user_version = {supported + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        await core_backup.restore_backup(backup.name)

    assert holder["conn"].execute("SELECT appid FROM games").fetchall() == [(620,)]
    assert current.exists()


@pytest.fixture
def maintenance_mock(monkeypatch):
    """恢复链单元层替身：调度器/引擎/爬取位全部 mock，只验文件轮换与数据正确性。"""
    calls: list[int] = []

    async def _noop_dispose():
        return None

    async def _noop_rebuild(*, initialize=False, maintenance_token=None):
        return True

    class _Sched:
        def __call__(self):
            calls.append(1)

    monkeypatch.setattr(core_backup, "_dispose_engine", _noop_dispose)
    monkeypatch.setattr(core_backup, "_rebuild_engine", _noop_rebuild)
    import app.core.scheduler as sched_mod
    from types import SimpleNamespace

    monkeypatch.setattr(sched_mod, "scheduler", SimpleNamespace(running=True))
    monkeypatch.setattr(sched_mod, "stop_scheduler", _Sched())
    monkeypatch.setattr(sched_mod, "start_scheduler", _Sched())
    import app.domains.crawl.service as crawl_service

    monkeypatch.setattr(crawl_service, "_active", None)
    return calls


@pytest.mark.asyncio
async def test_dispose_engine_skips_when_none_cached(monkeypatch):
    """引擎缓存为空时直接返回：此时 get_engine() 会凭空建出一个持有主库句柄的引擎。"""
    from types import SimpleNamespace

    from app.core import database as db_mod

    def _boom():
        raise AssertionError("无缓存引擎时不得新建引擎")

    _boom.cache_info = lambda: SimpleNamespace(currsize=0)
    monkeypatch.setattr(db_mod, "get_engine", _boom)
    await core_backup._dispose_engine()


@pytest.mark.asyncio
async def test_restore_replaces_production_db(data_env, maintenance_mock):
    """恢复闭环（归档档）：解档 → 校验 → 替换 → 内容等于备份时刻。"""
    tmp, holder = data_env
    db = tmp / "holdexar.db"

    # 1) 先备份（此刻只有 620）
    result = await core_backup.create_backup(label="base")

    # 2) 生产库继续演进（新增 440——备份里没有）
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].commit()

    # 占用连接必须先关——恢复前模拟「停服」释放句柄
    holder["conn"].close()

    res = await core_backup.restore_backup(result["name"])
    assert res["restored"] is True

    # 3) 生产库内容回到备份时刻：只有 620，没有 440
    conn = sqlite3.connect(db.as_posix())
    try:
        rows = conn.execute("SELECT appid FROM games ORDER BY appid").fetchall()
        assert rows == [(620,)]
    finally:
        conn.close()
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_suffix(".rollback.db").exists()
    assert not db.with_suffix(".restoring.db").exists()
    assert len(maintenance_mock) == 2
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["compact", "restore"])
async def test_cancelled_maintenance_waits_for_open_sqlite_thread(
    data_env, maintenance_mock, monkeypatch, operation
):
    import threading
    from app.core import database

    tmp, holder = data_env
    db = tmp / "holdexar.db"
    result = await core_backup.create_backup(label="cancel")
    holder["conn"].close()
    monkeypatch.setattr(database, "sqlite_file_path", lambda: db)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    acquired = asyncio.Event()
    real_snapshot = core_backup.snapshot_to

    def block_thread(src, dest=None):
        if dest is not None:
            real_snapshot(src, dest)
        conn = sqlite3.connect(dest or src)
        try:
            conn.execute("BEGIN IMMEDIATE")
            entered.set()
            assert release.wait(4)
            conn.rollback()
        finally:
            conn.close()
            closed.set()

    monkeypatch.setattr(core_backup, "_vacuum_file", block_thread)
    monkeypatch.setattr(core_backup, "snapshot_to", block_thread)
    action = core_backup.compact_main_db() if operation == "compact" else core_backup.restore_backup(result["name"])
    task = asyncio.create_task(action)
    waiter = None

    async def maintenance():
        with pytest.raises(RuntimeError, match="维护正在进行"):
            async with database.write_maintenance_barrier():
                acquired.set()

    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        waiter = asyncio.create_task(maintenance())
        await asyncio.sleep(0.02)
        assert not task.done()
        assert (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
        assert not acquired.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if waiter is not None:
            await waiter
    assert task.cancelled()
    assert closed.is_set()
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
    assert not db.with_suffix(".rollback.db").exists()
    async with database.write_maintenance_barrier():
        assert closed.is_set()


@pytest.mark.asyncio
async def test_maintenance_does_not_start_a_previously_stopped_scheduler(
    data_env, maintenance_mock, monkeypatch
):
    import app.core.scheduler as sched_mod

    sched_mod.scheduler.running = False
    data_env[1]["conn"].close()
    monkeypatch.setattr(core_backup, "_vacuum_file", lambda _: None)
    await core_backup.compact_main_db()
    assert maintenance_mock == []

@pytest.mark.asyncio
async def test_restore_from_legacy_plain_db_still_works(data_env, maintenance_mock):
    """旧格式兼容：升级前留下的未压缩 .db 备份照样能列、能校验、能恢复。"""
    tmp, holder = data_env
    db = tmp / "holdexar.db"
    legacy = core_backup.backup_dir() / "holdexar-20251231-000000.db"
    core_backup.snapshot_to(db, legacy)
    holder["conn"].close()

    names = [b["name"] for b in core_backup.list_backups()]
    assert legacy.name in names
    assert (await core_backup.verify_backup(legacy.name))["integrityOk"] is True
    assert (await core_backup.restore_backup(legacy.name))["restored"] is True


@pytest.mark.asyncio
async def test_replace_main_db_retries_after_file_is_held(data_env, monkeypatch):
    """主库句柄被并发请求重新持有 → 先重关引擎再重试，替换最终完成。"""
    tmp, holder = data_env
    src = tmp / "holdexar.db"
    staging = tmp / "holdexar.restoring.db"
    staging.write_bytes(b"staged")
    holder["conn"].close()  # 占用连接先释放，替换才有成功的可能
    disposes: list[int] = []
    attempts = {"n": 0}
    real_replace = os.replace

    async def _fake_dispose():
        disposes.append(1)

    def _flaky(a, b):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PermissionError(13, "拒绝访问")
        return real_replace(a, b)

    monkeypatch.setattr(core_backup, "_dispose_engine", _fake_dispose)
    monkeypatch.setattr(core_backup, "_REPLACE_RETRY_SECONDS", 0)
    monkeypatch.setattr(core_backup.os, "replace", _flaky)
    await core_backup._replace_main_db(staging, src)
    assert attempts["n"] == 2
    assert disposes == [1]
    assert src.read_bytes() == b"staged"


@pytest.mark.asyncio
async def test_restore_failure_leaves_no_leftovers(data_env, maintenance_mock, monkeypatch):
    """替换未成功：主库内容一字不动，就位档/回滚档/解档探针/锁全部清掉。"""
    tmp, holder = data_env
    db = tmp / "holdexar.db"
    result = await core_backup.create_backup(label="f")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].commit()
    holder["conn"].close()

    async def _blocked(staging, src):
        raise RuntimeError("主库文件被占用")

    monkeypatch.setattr(core_backup, "_replace_main_db", _blocked)
    with pytest.raises(RuntimeError, match="被占用"):
        await core_backup.restore_backup(result["name"])

    conn = sqlite3.connect(db.as_posix())
    try:
        rows = conn.execute("SELECT appid FROM games ORDER BY appid").fetchall()
        assert rows == [(440,), (620,)]
    finally:
        conn.close()
    assert not db.with_suffix(".rollback.db").exists()
    assert not db.with_suffix(".restoring.db").exists()
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()


@pytest.mark.asyncio
async def test_restore_initialization_failure_is_not_reported_as_success(
    data_env, maintenance_mock, monkeypatch
):
    tmp, holder = data_env
    result = await core_backup.create_backup(label="rollback")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].close()
    calls = []

    async def fail_then_recover(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("injected initialization failure")
        return True

    monkeypatch.setattr(core_backup, "_rebuild_engine", fail_then_recover)
    with pytest.raises(RuntimeError, match="恢复失败") as caught:
        await core_backup.restore_backup(result["name"])
    assert caught.value.rolled_back is True
    assert caught.value.database_available is True
    assert "injected initialization failure" in str(caught.value)
    conn = sqlite3.connect(tmp / "holdexar.db")
    try:
        assert conn.execute("SELECT appid FROM games ORDER BY appid").fetchall() == [(440,), (620,)]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_failed_restore_and_failed_recovery_preserve_snapshot_and_block_writes(
    data_env, maintenance_mock, monkeypatch
):
    from app.core import database

    tmp, holder = data_env
    result = await core_backup.create_backup(label="fail-all")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].close()
    monkeypatch.setattr(database, "sqlite_file_path", lambda: tmp / "holdexar.db")

    async def failed(**kwargs):
        raise RuntimeError("injected unavailable engine")

    monkeypatch.setattr(core_backup, "_rebuild_engine", failed)
    with pytest.raises(core_backup.RestoreFailure) as caught:
        await core_backup.restore_backup(result["name"])
    assert caught.value.database_available is False
    assert caught.value.rolled_back is False
    assert maintenance_mock == [1]
    rollback = tmp / "holdexar.rollback.db"
    assert rollback.exists()
    conn = sqlite3.connect(rollback)
    try:
        assert conn.execute("SELECT appid FROM games ORDER BY appid").fetchall() == [(440,), (620,)]
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="恢复未完成"):
        async with database.write_gate():
            pytest.fail("unavailable engine must not reopen for ordinary writes")
    assert not database.write_scheduler_diagnostics()["busy"]


@pytest.mark.asyncio
async def test_failed_original_database_replacement_cannot_be_unlocked_by_owner_exit(
    data_env, maintenance_mock, monkeypatch
):
    import json

    tmp, holder = data_env
    result = await core_backup.create_backup(label="unrecovered")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'original-user-data')")
    holder["conn"].close()
    replace = core_backup._replace_main_db
    replacements = []

    async def failed_init(**kwargs):
        raise RuntimeError("target initialization failed")

    async def replace_once(staging, src):
        replacements.append(1)
        if len(replacements) > 1:
            raise RuntimeError("original file replacement failed")
        await replace(staging, src)

    monkeypatch.setattr(core_backup, "_rebuild_engine", failed_init)
    monkeypatch.setattr(core_backup, "_replace_main_db", replace_once)
    with pytest.raises(core_backup.RestoreFailure) as caught:
        await core_backup.restore_backup(result["name"])
    assert caught.value.database_available is False
    lock = core_backup.backup_dir() / core_backup._LOCK_FILE
    metadata = json.loads(lock.read_text(encoding="utf-8"))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda _: False)
    with pytest.raises(RuntimeError, match="原库恢复未完成"):
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert lock.exists()
    assert metadata["rollbackPath"] == str(tmp / "holdexar.rollback.db")
    for path, expected in [(tmp / "holdexar.db", [(620,)]),
                           (tmp / "holdexar.rollback.db", [(440,), (620,)])]:
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT appid FROM games ORDER BY appid").fetchall() == expected
        finally:
            conn.close()


@pytest.mark.asyncio
async def test_verify_uses_hash_and_rejects_tampered_archive(data_env):
    """校验口径：哈希不符直接判否；哈希相符时默认档再证「能解出正确字节流」。"""
    result = await core_backup.create_backup(label="v")
    name = result["name"]
    ok = await core_backup.verify_backup(name)
    assert ok["integrityOk"] is True
    assert ok["games"] == 1
    assert ok["sha256"] == result["sha256"]
    # 默认档只做「哈希 + 流式解压」两件事，不开 SQLite
    assert ok["checks"] == {"hashOk": True, "restorableOk": True, "deepOk": None}

    archive = core_backup.backup_dir() / name
    body = bytearray(archive.read_bytes())
    body[len(body) // 2] ^= 0xFF
    archive.write_bytes(bytes(body))
    assert (await core_backup.verify_backup(name))["integrityOk"] is False


@pytest.mark.asyncio
async def test_verify_deep_opens_the_database(data_env):
    """深校验档：解档落盘后开库自检，并给出这一档的独立结论。"""
    result = await core_backup.create_backup(label="d")
    deep = await core_backup.verify_backup(result["name"], deep=True)
    assert deep["integrityOk"] is True
    assert deep["games"] == 1
    assert deep["checks"] == {"hashOk": True, "restorableOk": None, "deepOk": True}
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))


@pytest.mark.asyncio
async def test_verify_without_meta_still_proves_restorable(data_env):
    """断电窗口：有归档、无元数据。默认档仍能证明「可解出完整字节流」。"""
    result = await core_backup.create_backup(label="n")
    archive = core_backup.backup_dir() / result["name"]
    core_backup._archive_meta_path(archive).unlink()

    ok = await core_backup.verify_backup(result["name"])
    assert ok["integrityOk"] is True
    assert ok["checks"]["hashOk"] is None      # 没有随档哈希可比
    assert ok["checks"]["restorableOk"] is True


@pytest.mark.asyncio
async def test_verify_detects_truncated_archive_without_meta(data_env):
    """无元数据 + 被截断：默认档靠 gzip 的 CRC/长度自证失败，且不落探针。"""
    result = await core_backup.create_backup(label="tr")
    archive = core_backup.backup_dir() / result["name"]
    archive.write_bytes(archive.read_bytes()[:-64])
    core_backup._archive_meta_path(archive).unlink()

    ok = await core_backup.verify_backup(result["name"])
    assert ok["integrityOk"] is False
    assert ok["checks"]["restorableOk"] is False
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))


@pytest.mark.asyncio
async def test_restore_rejects_tampered_archive(data_env, maintenance_mock):
    """哈希不符的归档拒绝恢复：不碰生产库，也不留解档探针。"""
    tmp, holder = data_env
    result = await core_backup.create_backup(label="t")
    archive = core_backup.backup_dir() / result["name"]
    archive.write_bytes(archive.read_bytes()[:-8])  # 截断即哈希不符
    with pytest.raises(RuntimeError, match="哈希不符"):
        await core_backup.restore_backup(result["name"])
    assert (tmp / "holdexar.db").is_file()
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))


@pytest.mark.asyncio
async def test_restore_leaves_no_probe_when_archive_cannot_be_opened(
    data_env, maintenance_mock
):
    """无元数据 + 截断：解档当场抛错，探针必须被清掉。

    段 1 若把清理留给后面的 finally，异常会直接穿出并把探针留在备份目录里；
    那个名字既不被列表认领（只收 .db/.db.gz）也不被删除入口放行——用户永远
    看不见、也删不掉。
    """
    tmp, holder = data_env
    result = await core_backup.create_backup(label="bad")
    archive = core_backup.backup_dir() / result["name"]
    archive.write_bytes(archive.read_bytes()[:-64])
    core_backup._archive_meta_path(archive).unlink()

    with pytest.raises(Exception):
        await core_backup.restore_backup(result["name"])
    assert not list(core_backup.backup_dir().glob("*.verify.tmp"))
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
    assert (tmp / "holdexar.db").is_file()


@pytest.mark.asyncio
async def test_create_backup_refuses_while_maintenance_lock_held(data_env):
    """维护锁在位（恢复/收缩进行中）时拒绝再开一份快照。"""
    core_backup.backup_dir().joinpath(core_backup._LOCK_FILE).write_text("x", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="身份无法确认"):
            await core_backup.create_backup(label="lk")
    finally:
        core_backup.backup_dir().joinpath(core_backup._LOCK_FILE).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_rotation_removes_meta_with_archive(data_env):
    """轮转删档连带删元数据档：不留无人认领的哈希。"""
    for i in range(5):  # BACKUP_KEEP=3 → 最老两份被清
        await core_backup.create_backup(label=f"r{i}")
    base = core_backup.backup_dir()
    archives = {p.name for p in base.glob(f"*{core_backup.ARCHIVE_SUFFIX}")}
    metas = {
        p.name[: -len(core_backup.META_SUFFIX)]
        for p in base.glob(f"*{core_backup.META_SUFFIX}")
    }
    assert archives and metas == archives


def test_vacuum_reclaims_free_pages_and_absorbs_wal(data_env) -> None:
    """空间回收：删行留下的空闲页被收回、WAL 里的写入并入主库。

    只看主文件会误判 VACUUM 的效果（合并 WAL 会让主文件变大），故口径是
    「主库 + WAL + shm」的总占用下降，同时主库自身相对收缩前变小。
    """
    tmp, holder = data_env
    db = tmp / "holdexar.db"
    conn = holder["conn"]
    conn.execute("CREATE TABLE bloat (payload TEXT)")
    conn.executemany(
        "INSERT INTO bloat VALUES (?)", [("y" * 400,) for _ in range(20000)]
    )
    conn.commit()
    conn.execute("DELETE FROM bloat")
    conn.commit()
    before_main = db.stat().st_size
    before_total = core_backup._footprint(db)
    assert before_total > before_main, "前置失败：写入未滞留 WAL（测试环境不成立）"

    holder["conn"].close()  # 真实回收会先停写入面并断开所有连接
    core_backup._vacuum_file(db)

    after_main = db.stat().st_size
    after_total = core_backup._footprint(db)
    # 主文件可能因页分配而不变小；真正的回收口径是「总占用下降」（WAL 收进主库）
    assert after_main <= before_main, f"主文件反而变大：{before_main} → {after_main}"
    assert after_total < before_total, f"磁盘总占用未下降：{before_total} → {after_total}"
    check = sqlite3.connect(db.as_posix())
    try:
        assert check.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    finally:
        check.close()


def test_vacuum_translates_disk_full(tmp_path: Path, monkeypatch) -> None:
    """磁盘不够时 SQLite 只报 "disk is full"：转成能照做的提示，不透传原文。"""

    class _Conn:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, *args, **kwargs):  # noqa: ANN401
            self.calls += 1
            if self.calls > 2:
                raise sqlite3.OperationalError("database or disk is full")

        def close(self) -> None:
            return None

    monkeypatch.setattr(core_backup.sqlite3, "connect", lambda *a, **k: _Conn())
    with pytest.raises(RuntimeError, match="磁盘空间不足"):
        core_backup._vacuum_file(tmp_path / "x.db")


def test_startup_keeps_old_restore_lock_without_proven_owner(data_env):
    tmp, _ = data_env
    lock = core_backup.backup_dir() / ".restore.lock"
    lock.write_text("2020-01-01T00:00:00", encoding="utf-8")
    os.utime(lock, (1, 1))

    with pytest.raises(RuntimeError, match="身份无法确认"):
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert lock.exists()


@pytest.mark.parametrize("payload", ["{broken", "{}", '{"pid":0}', '{"pid":-1}', '{"pid":true}',
                                     '{"pid":123,"purpose":"unknown"}',
                                     '{"pid":123,"purpose":null}', '{"pid":123,"purpose":[]}',
                                     '{"pid":123,"purpose":{}}'])
def test_invalid_restore_owner_never_expires_by_age(data_env, monkeypatch, payload):
    tmp, _ = data_env
    lock = core_backup.backup_dir() / core_backup._LOCK_FILE
    lock.write_text(payload, encoding="utf-8")
    os.utime(lock, (1, 1))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda _: False)
    with pytest.raises(RuntimeError, match="身份无法确认"):
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert lock.exists()


def test_positive_dead_legacy_owner_can_be_cleared_but_live_owner_never_expires(data_env, monkeypatch):
    import json

    tmp, _ = data_env
    lock = core_backup.backup_dir() / core_backup._LOCK_FILE
    lock.write_text(json.dumps({"pid": 123, "token": "legacy"}), encoding="utf-8")
    os.utime(lock, (1, 1))
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda _: True)
    with pytest.raises(RuntimeError, match="维护正在进行"):
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert lock.exists()
    monkeypatch.setattr(db_safety, "_pid_is_running", lambda _: False)
    core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert not lock.exists()


def test_restore_recovery_message_does_not_claim_missing_original_snapshot(data_env):
    import json

    tmp, _ = data_env
    lock = core_backup.backup_dir() / core_backup._LOCK_FILE
    missing = tmp / "missing.rollback.db"
    lock.write_text(json.dumps({"pid": 123, "purpose": "restore", "rollbackPath": str(missing)}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="未找到原库副本") as caught:
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")
    assert "副本保留在" not in str(caught.value)
    assert lock.exists()


def test_live_restore_lock_blocks_startup_and_is_not_removed(data_env):
    import json

    tmp, _ = data_env
    lock = core_backup.backup_dir() / ".restore.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "token": "live"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="维护正在进行中"):
        core_backup.ensure_restore_lock_available(tmp / "holdexar.db")

    assert lock.exists()


def test_restore_lock_is_exclusive_and_release_is_token_owned(data_env):
    lock, token = core_backup._acquire_restore_lock()
    try:
        with pytest.raises(RuntimeError, match="维护正在进行中"):
            core_backup._acquire_restore_lock()
        core_backup._release_restore_lock(lock, "another-operation")
        assert lock.exists()
    finally:
        core_backup._release_restore_lock(lock, token)
    assert not lock.exists()


@pytest.mark.asyncio
async def test_compaction_recovers_engine_after_one_rebuild_failure(
    data_env, maintenance_mock, monkeypatch
):
    data_env[1]["conn"].close()
    monkeypatch.setattr(core_backup, "_vacuum_file", lambda _: None)
    calls = []

    async def rebuild(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("injected rebuild failure")
        return True

    monkeypatch.setattr(core_backup, "_rebuild_engine", rebuild)
    with pytest.raises(RuntimeError, match="injected rebuild failure"):
        await core_backup.compact_main_db()
    assert calls == [1, 1]
    assert maintenance_mock == [1, 1]
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()


@pytest.mark.asyncio
async def test_cancellation_during_restored_database_initialization_rolls_back(
    data_env, maintenance_mock, monkeypatch
):
    tmp, holder = data_env
    result = await core_backup.create_backup(label="cancel-init")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'TF2')")
    holder["conn"].close()
    entered = asyncio.Event()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    calls = []

    async def rebuild(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()
        cleanup_entered.set()
        await cleanup_release.wait()
        return True

    monkeypatch.setattr(core_backup, "_rebuild_engine", rebuild)
    task = asyncio.create_task(core_backup.restore_backup(result["name"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
    finally:
        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    conn = sqlite3.connect(tmp / "holdexar.db")
    try:
        assert conn.execute("SELECT appid FROM games ORDER BY appid").fetchall() == [(440,), (620,)]
    finally:
        conn.close()
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
    assert maintenance_mock == [1, 1]


@pytest.mark.asyncio
async def test_restore_http_failure_exposes_original_reason_and_preserved_database(monkeypatch):
    import json
    from app.domains.system import router as system_router

    async def failed(name):
        raise core_backup.RestoreFailure(name, RuntimeError("original failure"),
                                         rolled_back=True, database_available=True)

    monkeypatch.setattr(system_router, "restore_backup_impl", failed)
    response = await system_router.backup_restore("backup.db")
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["restored"] is False
    assert body["rolledBack"] is True
    assert body["databaseAvailable"] is True
    assert "original failure" in body["detail"]


@pytest.mark.asyncio
async def test_restore_cleanup_file_occupancy_does_not_hide_initial_failure(
    data_env, maintenance_mock, monkeypatch
):
    _, holder = data_env
    result = await core_backup.create_backup(label="held-cleanup")
    holder["conn"].close()
    calls = []
    unlink = Path.unlink

    async def rebuild(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("original failure before cleanup")
        return True

    def held_probe(path, **kwargs):
        if path.name.endswith(".verify.tmp"):
            raise PermissionError("held cleanup probe")
        return unlink(path, **kwargs)

    monkeypatch.setattr(core_backup, "_rebuild_engine", rebuild)
    monkeypatch.setattr(Path, "unlink", held_probe)
    with pytest.raises(core_backup.RestoreFailure, match="original failure before cleanup"):
        await core_backup.restore_backup(result["name"])
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()


@pytest.mark.asyncio
async def test_restored_database_remains_success_when_old_snapshot_cleanup_is_held(
    data_env, maintenance_mock, monkeypatch, caplog
):
    tmp, holder = data_env
    result = await core_backup.create_backup(label="chosen")
    holder["conn"].execute("INSERT INTO games VALUES (440, 'original-user-data')")
    holder["conn"].close()
    unlink = Path.unlink

    def held_rollback(path, **kwargs):
        if path.name.endswith(".rollback.db") and path.exists():
            raise PermissionError("held old snapshot")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", held_rollback)
    restored = await core_backup.restore_backup(result["name"])
    assert restored["restored"] is True
    assert restored["rolledBack"] is False
    assert restored["cleanupPending"] is True
    for path, expected in [(tmp / "holdexar.db", [(620,)]),
                           (tmp / "holdexar.rollback.db", [(440,), (620,)])]:
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT appid FROM games ORDER BY appid").fetchall() == expected
        finally:
            conn.close()
    assert "临时备份文件暂未清理" in caplog.text
    assert not (core_backup.backup_dir() / core_backup._LOCK_FILE).exists()
    assert maintenance_mock == [1, 1]


def test_process_exit_after_database_replacement_keeps_recovery_protection(tmp_path):
    src = tmp_path / "holdexar.db"
    chosen = tmp_path / "chosen.db"
    for path, value in [(src, "original-user-data"), (chosen, "chosen-backup-data")]:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE result (value TEXT)")
        conn.execute("INSERT INTO result VALUES (?)", (value,))
        conn.commit()
        conn.close()
    code = """
import os, sys
from pathlib import Path
from app.core import backup
from app.core.database import sync_write_gate
src, chosen = map(Path, sys.argv[1:])
with sync_write_gate(src):
    lock, token = backup._acquire_restore_lock(db_path=src)
    rollback = src.with_suffix('.rollback.db')
    backup.snapshot_to(src, rollback)
    backup._protect_restore_recovery(lock, token, src, rollback, backup.sha256_of(rollback))
    os.replace(chosen, src)
    os._exit(0)
"""
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    child = subprocess.run([sys.executable, "-c", code, str(src), str(chosen)],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           timeout=10, **kwargs)
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    with pytest.raises(RuntimeError, match="原库恢复未完成"):
        core_backup.ensure_restore_lock_available(src)
    assert (tmp_path / "backups" / core_backup._LOCK_FILE).exists()
    for path, expected in [(src, "chosen-backup-data"),
                           (src.with_suffix(".rollback.db"), "original-user-data")]:
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT value FROM result").fetchone() == (expected,)
        finally:
            conn.close()
