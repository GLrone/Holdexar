"""随包 Clash 内核资产验收。

覆盖五条行为：
1. 随包目录 → data/clash/ 就位，GeoIP 数据只补缺失、同名不覆盖；
2. 内核可执行文件版本感知换装：就位版本低于随包版本才替换，不低于
   （用户手换更高版）或版本不可辨时保留；
3. detect_kernel 只认 data/clash/——随包目录里有内核也不算「已就位」
   （不再检索本机其他 Clash 安装）；
4. install_kernel 随包优先：随包可用时**不触网**（下载函数被顶替为断言失败）；
5. 随包缺失时 install_kernel 才回退网络下载。

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


def _fake_kernel_version(monkeypatch, raw: str | None) -> None:
    """把 -v 解析链顶替为固定输出（raw=None 模拟版本不可辨）。"""
    monkeypatch.setattr(
        clash_manager, "_kernel_exe_version",
        lambda _exe: clash_manager.parse_kernel_version(raw),
    )


def test_ensure_kernel_copies_bundle_into_data_dir(tmp_path: Path, monkeypatch) -> None:
    """内核与 GeoIP 数据按需复制进 data/clash/，缺失清单随之清空。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    _fake_kernel_version(monkeypatch, None)  # 假内核不跑真 -v

    result = clash_manager.ensure_kernel(data_dir)

    assert result["kernelReady"] is True
    assert set(result["copied"]) == {kernel_filename(), *GEO_ASSETS}
    assert result["missing"] == []
    assert result["upgraded"] is None  # 无旧版本可比，纯新装
    for name in (kernel_filename(), *GEO_ASSETS):
        assert (data_dir / "clash" / name).is_file()


def test_ensure_kernel_keeps_existing_files(tmp_path: Path, monkeypatch) -> None:
    """已有 GeoIP 数据与内核一律不动（版本不可辨 = 保守保留用户文件）。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    clash_path = data_dir / "clash"
    clash_path.mkdir(parents=True)
    (clash_path / kernel_filename()).write_bytes(b"user-kernel")
    (clash_path / "geoip.dat").write_bytes(b"user-geoip")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    # 版本不可辨（-v 拿不到可解析输出）：既不证明旧、也不证明新，不替换
    _fake_kernel_version(monkeypatch, None)

    result = clash_manager.ensure_kernel(data_dir)

    assert (clash_path / kernel_filename()).read_bytes() == b"user-kernel"
    assert (clash_path / "geoip.dat").read_bytes() == b"user-geoip"
    assert kernel_filename() not in result["copied"]
    assert "geoip.dat" not in result["copied"]
    assert result["upgraded"] is None
    assert set(result["copied"]) == {"Country.mmdb", "geosite.dat", "ASN.mmdb"}


def _bump_target(monkeypatch, version: str) -> None:
    """把随包版本常量换成指定值（升级/保留判定的另一端）。"""
    monkeypatch.setattr(clash_manager, "MIHOMO_VERSION", version)


def test_ensure_kernel_upgrades_stale_kernel(tmp_path: Path, monkeypatch) -> None:
    """就位版本低于随包版本 → 替换（老安装升级后随包新内核落地）。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    clash_path = data_dir / "clash"
    clash_path.mkdir(parents=True)
    (clash_path / kernel_filename()).write_bytes(b"old-kernel")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    _fake_kernel_version(monkeypatch, "Mihomo Meta v1.19.20 windows amd64")

    result = clash_manager.ensure_kernel(data_dir)

    assert result["upgraded"] == f"1.19.20 → {MIHOMO_VERSION}"
    assert (clash_path / kernel_filename()).read_bytes() == b"MZ" + b"\x00" * 4096


def test_ensure_kernel_keeps_newer_user_kernel(tmp_path: Path, monkeypatch) -> None:
    """就位版本不低于随包版本 → 保留（用户手换更高版不夺回，同版也不折腾）。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    clash_path = data_dir / "clash"
    clash_path.mkdir(parents=True)
    (clash_path / kernel_filename()).write_bytes(b"user-newer-kernel")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    target = clash_manager.parse_kernel_version(MIHOMO_VERSION)
    assert target is not None
    _fake_kernel_version(
        monkeypatch,
        f"Mihomo Meta v{'.'.join(map(str, (target[0], target[1], target[2] + 1)))} windows amd64",
    )

    result = clash_manager.ensure_kernel(data_dir)

    assert result["upgraded"] is None
    assert (clash_path / kernel_filename()).read_bytes() == b"user-newer-kernel"


def test_ensure_kernel_skips_replacement_while_running(tmp_path: Path, monkeypatch) -> None:
    """内核进程在跑 → 不替换（Windows 下运行中的 exe 无法覆盖），留给下次。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    clash_path = data_dir / "clash"
    clash_path.mkdir(parents=True)
    (clash_path / kernel_filename()).write_bytes(b"old-kernel")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    _fake_kernel_version(monkeypatch, "Mihomo Meta v1.19.20 windows amd64")
    # 本 runtime 实例在跑：模块级 runtime.status() 命中即跳过
    monkeypatch.setattr(
        clash_manager.runtime, "status",
        lambda: {"running": True, "port": 7890, "configPath": None, "controllerUrl": None},
    )

    result = clash_manager.ensure_kernel(data_dir)

    assert result["upgraded"] is None
    assert (clash_path / kernel_filename()).read_bytes() == b"old-kernel"


def test_detect_kernel_only_checks_data_dir(tmp_path: Path, monkeypatch) -> None:
    """随包目录有内核也**不算**已就位：检测只认 data/clash/，不做任何外部检索。"""
    bundle = _make_bundle(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    _fake_kernel_version(monkeypatch, None)  # 假内核不跑真 -v

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
    # 全新安装无旧内核可比版本：升级判定不触发（_maybe_upgrade_kernel 短路）
    _fake_kernel_version(monkeypatch, None)

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

    def _fake_download(data_dir: Path) -> dict:
        calls.append("download")
        return {
            "ok": True, "path": str(data_dir / "clash" / kernel_filename()),
            "source": "github", "via": "直连",
        }

    calls: list[str] = []
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
