"""按需获取种子：候选通道构成、清单哈希闸、半截档处理、落位与并入。

覆盖的是「用户机器上首次启动拿到种子」这条链路里最容易静默出错的三处：
通道拼错（地址必然 404）、校验缺失（半截档被当成种子并入）、临时档处理
（可续传的半截被误删，或坏档被无限续传）。
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import seed_fetch  # noqa: E402
from app.core.app_info import DATA_CDN_BASE, GITHUB_REPO  # noqa: E402


def _make_seed_db(path: Path, version: str = "2026-01-01 00:00:00") -> None:
    """造一份最小可用种子（seed_meta.version 非空即算有效）。"""
    conn = sqlite3.connect(path.as_posix())
    try:
        conn.execute("CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO seed_meta VALUES ('version', ?)", (version,))
        conn.execute("CREATE TABLE games_catalog (appid INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
    finally:
        conn.close()


def _gz(path: Path, payload: bytes) -> None:
    with gzip.open(path, "wb") as f:
        f.write(payload)


# ── 候选通道构成 ────────────────────────────────────────────────


def test_cdn_url_carries_cache_key() -> None:
    """自建分发地址带缓存区分串：固定文件名 + CDN 缓存会让换版后一直发旧档。"""
    assert seed_fetch._cdn_seed_url("") == f"{DATA_CDN_BASE}/seed/{seed_fetch.SEED_GZ_NAME}"
    keyed = seed_fetch._cdn_seed_url("abcdef123456")
    assert keyed.endswith("?v=abcdef123456")
    assert keyed.startswith(f"{DATA_CDN_BASE}/seed/")


def test_github_tail_is_untouched_by_cdn() -> None:
    """GitHub 尾部地址与自建分发是两个独立端点，不互相拼接。"""
    tail = f"https://github.com/{GITHUB_REPO}/releases/latest/download/{seed_fetch.SEED_GZ_NAME}"
    assert DATA_CDN_BASE not in tail
    assert "releases/latest/download" in tail


@pytest.mark.asyncio
async def test_manifest_entry_prefers_archive_role(monkeypatch) -> None:
    """清单里两条种子条目：只认声明**压缩档**的那条，拿库哈希比 gz 必然不符。"""
    async def _fake_manifest():
        return {
            "assets": [
                {"role": "app", "name": "x.zip", "sha256": "a" * 64},
                {"role": "seed", "name": "holdexar_seed.db", "sha256": "b" * 64, "size": 999},
                {"role": "seed-archive", "name": "holdexar_seed.db.gz",
                 "sha256": "C" * 64, "size": 123},
            ]
        }

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_fetch_manifest", _fake_manifest)
    entry = await seed_fetch._seed_manifest_entry()
    assert entry == {"sha256": "c" * 64, "size": 123}


@pytest.mark.asyncio
async def test_manifest_entry_absent_role_degrades(monkeypatch) -> None:
    """清单可达但没有压缩档条目（旧版清单）→ 空字典，调用方退化为结构校验。"""
    async def _fake_manifest():
        return {"assets": [{"role": "seed", "name": "holdexar_seed.db", "sha256": "b" * 64}]}

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_fetch_manifest", _fake_manifest)
    assert await seed_fetch._seed_manifest_entry() == {}


@pytest.mark.asyncio
async def test_manifest_entry_unreachable_degrades(monkeypatch) -> None:
    """清单不可达：不抛，返回空字典让下载继续（只是校验档位降低）。"""
    async def _boom():
        raise RuntimeError("network down")

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_fetch_manifest", _boom)
    assert await seed_fetch._seed_manifest_entry() == {}


# ── 校验闸 ──────────────────────────────────────────────────────


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    blob = tmp_path / "b.bin"
    blob.write_bytes(b"holdexar" * 1000)
    assert seed_fetch._sha256_of(blob) == hashlib.sha256(blob.read_bytes()).hexdigest()


def test_validate_seed_requires_version(tmp_path: Path) -> None:
    good = tmp_path / "good.db"
    _make_seed_db(good, "2026-05-05 01:02:03")
    assert seed_fetch._validate_seed(good) == "2026-05-05 01:02:03"

    # 没有 seed_meta 表 → 坏档
    bad = tmp_path / "bad.db"
    sqlite3.connect(bad.as_posix()).close()
    with pytest.raises(RuntimeError):
        seed_fetch._validate_seed(bad)


def test_status_snapshot_reports_presence(tmp_path: Path, monkeypatch) -> None:
    """状态面：库里没有种子时 present=False，不会因为 .part 存在就误报。"""
    class _S:
        data_dir = tmp_path

    monkeypatch.setattr(seed_fetch, "get_settings", lambda: _S())
    monkeypatch.setattr(seed_fetch, "seed_db_path", lambda: tmp_path / "holdexar_seed.db")
    (tmp_path / "holdexar_seed.db.gz.part").write_bytes(b"1f8b0808" * 100)

    import asyncio

    snap = asyncio.run(seed_fetch.status_snapshot())
    assert snap["present"] is False
    assert snap["path"] is None


@pytest.mark.asyncio
async def test_seed_fetch_sha256_mismatch_falls_back_to_next_mirror(
    tmp_path: Path, monkeypatch
) -> None:
    """镜像内容 SHA256 不符时立即清理坏档并换下一通道，下一通道正确完成。"""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    good_db = tmp_path / "origin.db"
    _make_seed_db(good_db, "2026-10-15 12:00:00")
    good_gz_bytes = gzip.compress(good_db.read_bytes())
    good_sha = hashlib.sha256(good_gz_bytes).hexdigest()

    bad_gz_bytes = b"corrupt payload that differs in hash" * 100

    class _S:
        data_dir = tmp_path

    monkeypatch.setattr(seed_fetch, "get_settings", lambda: _S())
    monkeypatch.setattr(seed_fetch, "_data_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(seed_fetch, "seed_db_path", lambda: seed_dir / "holdexar_seed.db")
    monkeypatch.setattr(seed_fetch, "_MIN_SEED_BYTES", 0)

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)

    async def _mock_manifest_entry():
        return {"sha256": good_sha, "size": len(good_gz_bytes)}

    monkeypatch.setattr(seed_fetch, "_seed_manifest_entry", _mock_manifest_entry)

    channels_probed = []

    async def _fake_stream(url, proxy, target_file: Path, *args, **kwargs):
        channels_probed.append(url)
        if url == "https://mirror1/seed.db.gz":
            target_file.write_bytes(bad_gz_bytes)
        else:
            target_file.write_bytes(good_gz_bytes)

    async def _fake_probe(channels, fallback_total):
        return [
            {"label": "镜像1", "url": "https://mirror1/seed.db.gz", "proxy": None, "missing": False, "bogus": False, "total": len(bad_gz_bytes)},
            {"label": "镜像2", "url": "https://mirror2/seed.db.gz", "proxy": None, "missing": False, "bogus": False, "total": len(good_gz_bytes)},
        ]

    async def _fake_merge():
        pass

    monkeypatch.setattr(updater, "_stream_to_file", _fake_stream)
    monkeypatch.setattr(updater, "_probe_channels", _fake_probe)
    monkeypatch.setattr(seed_fetch, "merge_seed_incremental", _fake_merge)

    result = await seed_fetch.ensure_seed_online()
    assert result["present"] is True
    assert (seed_dir / "holdexar_seed.db").is_file()
    assert channels_probed == ["https://mirror1/seed.db.gz", "https://mirror2/seed.db.gz"]
    # 坏档未残留为 gz 或 part
    assert not (seed_dir / seed_fetch.SEED_GZ_NAME).exists()
    assert not (seed_dir / seed_fetch._PART_NAME).exists()


@pytest.mark.asyncio
async def test_seed_fetch_channel_truncation_does_not_corrupt_next_channel(
    tmp_path: Path, monkeypatch
) -> None:
    """通道异常断开导致部分写入时，回滚临时文件到进入该通道前的位置，不污染后续通道。"""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    good_db = tmp_path / "origin2.db"
    _make_seed_db(good_db, "2026-10-15 13:00:00")
    good_gz_bytes = gzip.compress(good_db.read_bytes())
    good_sha = hashlib.sha256(good_gz_bytes).hexdigest()

    class _S:
        data_dir = tmp_path

    monkeypatch.setattr(seed_fetch, "get_settings", lambda: _S())
    monkeypatch.setattr(seed_fetch, "_data_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(seed_fetch, "seed_db_path", lambda: seed_dir / "holdexar_seed.db")
    monkeypatch.setattr(seed_fetch, "_MIN_SEED_BYTES", 0)

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)

    async def _mock_manifest_entry():
        return {"sha256": good_sha, "size": len(good_gz_bytes)}

    monkeypatch.setattr(seed_fetch, "_seed_manifest_entry", _mock_manifest_entry)

    channels_probed = []

    async def _fake_stream(url, proxy, target_file: Path, *args, **kwargs):
        channels_probed.append(url)
        if url == "https://mirror1/seed.db.gz":
            # 写入半截并抛出网络中断异常
            target_file.write_bytes(b"garbage-cut-off")
            raise ConnectionResetError("Connection lost mid-stream")
        target_file.write_bytes(good_gz_bytes)

    async def _fake_probe(channels, fallback_total):
        return [
            {"label": "镜像1", "url": "https://mirror1/seed.db.gz", "proxy": None, "missing": False, "bogus": False, "total": 1000},
            {"label": "镜像2", "url": "https://mirror2/seed.db.gz", "proxy": None, "missing": False, "bogus": False, "total": len(good_gz_bytes)},
        ]

    async def _fake_merge():
        pass

    monkeypatch.setattr(updater, "_stream_to_file", _fake_stream)
    monkeypatch.setattr(updater, "_probe_channels", _fake_probe)
    monkeypatch.setattr(seed_fetch, "merge_seed_incremental", _fake_merge)

    result = await seed_fetch.ensure_seed_online()
    assert result["present"] is True
    assert (seed_dir / "holdexar_seed.db").is_file()
    assert channels_probed == ["https://mirror1/seed.db.gz", "https://mirror2/seed.db.gz"]


@pytest.mark.asyncio
async def test_seed_fetch_all_channels_fail_does_not_install_bad_db(
    tmp_path: Path, monkeypatch
) -> None:
    """所有通道均下载失败或哈希不符时，不生成正式 holdexar_seed.db。"""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    class _S:
        data_dir = tmp_path

    monkeypatch.setattr(seed_fetch, "get_settings", lambda: _S())
    monkeypatch.setattr(seed_fetch, "_data_seed_dir", lambda: seed_dir)
    monkeypatch.setattr(seed_fetch, "seed_db_path", lambda: seed_dir / "holdexar_seed.db")
    monkeypatch.setattr(seed_fetch, "_MIN_SEED_BYTES", 0)

    import app.core.updater as updater

    monkeypatch.setattr(updater, "_MIN_PACKAGE_BYTES", 0)

    async def _mock_manifest_entry():
        return {"sha256": "expected_valid_sha", "size": 100}

    monkeypatch.setattr(seed_fetch, "_seed_manifest_entry", _mock_manifest_entry)

    async def _fake_stream(url, proxy, target_file: Path, *args, **kwargs):
        target_file.write_bytes(b"bad_bytes")

    async def _fake_probe(channels, fallback_total):
        return [
            {"label": "镜像1", "url": "https://mirror1/seed.db.gz", "proxy": None, "missing": False, "bogus": False, "total": 100},
        ]

    monkeypatch.setattr(updater, "_stream_to_file", _fake_stream)
    monkeypatch.setattr(updater, "_probe_channels", _fake_probe)

    result = await seed_fetch.ensure_seed_online()
    assert result["state"] == "failed"
    snapshot = await seed_fetch.status_snapshot()
    assert snapshot["present"] is False
    assert not (seed_dir / "holdexar_seed.db").exists()

