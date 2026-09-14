"""随包 Clash 内核资产验收。

覆盖四条行为：
1. 随包目录 → data/clash/ 就位，只补缺失、同名不覆盖（用户手动换过的内核不被夺回）；
2. detect_kernel 只认 data/clash/——随包目录里有内核也不算「已就位」
   （不再检索本机其他 Clash 安装）；
3. install_kernel 随包优先：随包可用时**不触网**（下载函数被顶替为断言失败）；
4. 随包缺失时 install_kernel 才回退网络下载。

真实资产（`assets/clash/`，gitignored）不进测试：这里在 tmp_path 合成同构目录，
用 monkeypatch 顶替 bundled_clash_dir()。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.proxies import clash_manager
from app.domains.proxies.kernel_release import (
    GEO_ASSETS,
    MIHOMO_VERSION,
    is_windows,
    kernel_filename,
    mihomo_asset,
    mihomo_url,
)


def _make_bundle(tmp_path: Path, *, kernel: bool = True) -> Path:
    """合成「随包内核目录」：内核可执行文件 + GeoIP 数据（内容不参与判定）。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    if kernel:
        (bundle / kernel_filename()).write_bytes(b"MZ" + b"\x00" * 4096)
    for name in GEO_ASSETS:
        (bundle / name).write_bytes(b"geo-data")
    return bundle


def test_ensure_kernel_copies_bundle_into_data_dir(tmp_path: Path, monkeypatch) -> None:
    """内核与 GeoIP 数据按需复制进 data/clash/，缺失清单随之清空。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)

    result = clash_manager.ensure_kernel(data_dir)

    assert result["kernelReady"] is True
    assert set(result["copied"]) == {kernel_filename(), *GEO_ASSETS}
    assert result["missing"] == []
    for name in (kernel_filename(), *GEO_ASSETS):
        assert (data_dir / "clash" / name).is_file()


def test_ensure_kernel_keeps_existing_files(tmp_path: Path, monkeypatch) -> None:
    """已有文件一律不动（用户可能手动换了更高版本的内核/更新的 GeoIP 数据）。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    clash_path = data_dir / "clash"
    clash_path.mkdir(parents=True)
    (clash_path / kernel_filename()).write_bytes(b"user-kernel")
    (clash_path / "geoip.dat").write_bytes(b"user-geoip")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)

    result = clash_manager.ensure_kernel(data_dir)

    assert (clash_path / kernel_filename()).read_bytes() == b"user-kernel"
    assert (clash_path / "geoip.dat").read_bytes() == b"user-geoip"
    assert kernel_filename() not in result["copied"]
    assert "geoip.dat" not in result["copied"]
    assert set(result["copied"]) == {"Country.mmdb", "geosite.dat", "ASN.mmdb"}


def test_detect_kernel_only_checks_data_dir(tmp_path: Path, monkeypatch) -> None:
    """随包目录有内核也**不算**已就位：检测只认 data/clash/，不做任何外部检索。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)

    assert clash_manager.detect_kernel(data_dir) == {
        "found": False, "path": None, "builtin": False,
    }

    clash_manager.ensure_kernel(data_dir)
    detect = clash_manager.detect_kernel(data_dir)

    assert detect["found"] is True
    assert detect["builtin"] is True
    assert Path(detect["path"]).parent == data_dir / "clash"


def test_install_kernel_prefers_bundle_without_network(tmp_path: Path, monkeypatch) -> None:
    """随包可用时安装不触网——两个下载函数都被顶替成「调了就失败」。"""
    bundle = _make_bundle(tmp_path)
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)

    def _boom(*_args, **_kwargs):
        raise AssertionError("随包资产可用时不应走网络下载")

    monkeypatch.setattr(clash_manager, "download_kernel", _boom)
    monkeypatch.setattr(clash_manager, "download_kernel_linux", _boom)

    result = clash_manager.install_kernel(tmp_path / "data")

    assert result["ok"] is True
    assert result["source"] == "bundled"
    assert Path(result["path"]).is_file()


def test_install_kernel_falls_back_to_download(tmp_path: Path, monkeypatch) -> None:
    """随包缺失（源码 clone 未补资产）才回退网络下载，且走当前平台那支。"""
    bundle = _make_bundle(tmp_path, kernel=False)
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    calls: list[str] = []

    def _fake_download(data_dir: Path) -> dict:
        calls.append("download")
        return {
            "ok": True, "path": str(data_dir / "clash" / kernel_filename()),
            "source": "github", "via": "直连",
        }

    monkeypatch.setattr(clash_manager, "download_kernel", _fake_download)
    monkeypatch.setattr(clash_manager, "download_kernel_linux", _fake_download)

    result = clash_manager.install_kernel(tmp_path / "data")

    assert calls == ["download"]
    assert result["source"] == "github"  # 来自下载分支，而非随包复制


def test_kernel_release_constants_are_single_source() -> None:
    """版本号只有一处：资产名与下载地址都必须带同一个 MIHOMO_VERSION。"""
    asset_name, shape = mihomo_asset()
    assert MIHOMO_VERSION in asset_name
    assert MIHOMO_VERSION in mihomo_url()
    assert shape in ("zip", "gz")
    assert asset_name.endswith(".zip") is is_windows()
