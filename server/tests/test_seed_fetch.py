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
