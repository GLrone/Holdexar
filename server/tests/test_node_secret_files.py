"""节点认证明文的落盘约束：权限收紧与生命周期清理。

内核运行时必须读到节点口令，因此这些文件不能加密；本模块把「明文的
暴露面」压到最小：POSIX 下 0600/0700、可重建副本在用完或启动时清理。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.domains.proxies import node_secret_files


@pytest.fixture
def data_dir(tmp_path):
    for name in ("clash", "proxypool"):
        (tmp_path / name).mkdir()
    return tmp_path


def _make(data_dir: Path, relative: str, text: str = "proxies: []") -> Path:
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_sweep_removes_runtime_and_tmp_only(data_dir):
    """清理只删可重建副本，原始订阅缓存（来源证据）保留。"""
    raw = _make(data_dir, "clash/clash-sub-1.yaml")
    runtime = _make(data_dir, "clash/clash-sub-1.runtime.yaml")
    tmp = _make(data_dir, "proxypool/crawl-runtime.yaml.tmp")
    keep = _make(data_dir, "proxypool/crawl-runtime.yaml")

    removed = node_secret_files.sweep_transient(data_dir)
    assert removed == 2
    assert raw.exists()          # 原始缓存保留
    assert not runtime.exists()
    assert not tmp.exists()
    # 主运行配置不是 *.runtime.yaml 也不是 .tmp，不在清理面
    assert keep.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位")
def test_tighten_sets_owner_only(data_dir):
    f = _make(data_dir, "clash/sub.yaml")
    node_secret_files.tighten(f)
    assert oct(f.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位")
def test_tighten_all_covers_both_dirs(data_dir):
    a = _make(data_dir, "clash/a.yaml")
    b = _make(data_dir, "proxypool/b.yaml")
    count = node_secret_files.tighten_all(data_dir)
    assert count == 2
    assert oct(a.stat().st_mode & 0o777) == "0o600"
    assert oct(b.stat().st_mode & 0o777) == "0o600"
    assert oct((data_dir / "clash").stat().st_mode & 0o777) == "0o700"


def test_tighten_missing_path_is_silent(tmp_path):
    node_secret_files.tighten(tmp_path / "nope.yaml")
    node_secret_files.tighten_dir(tmp_path / "nope")


def test_sweep_on_empty_dir_is_noop(tmp_path):
    assert node_secret_files.sweep_transient(tmp_path) == 0


def test_secret_bearing_covers_expected_dirs():
    assert set(node_secret_files.SECRET_BEARING) == {"clash", "proxypool"}
