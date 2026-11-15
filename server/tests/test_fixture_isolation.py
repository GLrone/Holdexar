"""隔离夹具自身的机械防线：本文件未豁免，数据目录必须落在临时目录。"""
import os
import tempfile
import time

import conftest


def test_data_dir_is_isolated_temp_dir():
    data_dir = os.environ.get("HOLDEXAR_DATA_DIR", "")
    assert data_dir, "隔离夹具未生效：HOLDEXAR_DATA_DIR 未设置"
    assert data_dir.startswith(tempfile.gettempdir()), (
        f"数据目录未隔离进临时目录：{data_dir}"
    )


def test_tempfile_root_pins_to_dedicated_base():
    import pytest

    if conftest._TEMP_BASE is None:
        pytest.skip("专用临时基地不可用，已回落系统临时目录")
    assert tempfile.gettempdir() == str(conftest._TEMP_BASE)


def test_stale_temp_sweep_only_takes_expired_family_entries(tmp_path, monkeypatch):
    stale_dir = tmp_path / "holdexar-stale-dir"
    stale_dir.mkdir()
    stale_file = tmp_path / "a2a_stale.db"
    stale_file.write_bytes(b"x")
    fresh_dir = tmp_path / "holdexar-fresh"
    fresh_dir.mkdir()
    foreign = tmp_path / "not-our-prefix"
    foreign.mkdir()
    expired = time.time() - conftest._TEMP_STALE_SECONDS - 60
    for p in (stale_dir, stale_file, foreign):
        os.utime(p, (expired, expired))
    monkeypatch.setattr(conftest, "_TEMP_BASE", tmp_path)

    conftest._sweep_stale_temp_leftovers()

    assert not stale_dir.exists()
    assert not stale_file.exists()
    assert fresh_dir.exists(), "6 小时内有动静的在途目录不许清"
    assert foreign.exists(), "非本项目家族条目不许清"
