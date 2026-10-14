from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

from app.domains.proxies import kernel_release


ROOT = Path(__file__).resolve().parents[2]
GEO_FILES = ("Country.mmdb", "ASN.mmdb", "geoip.dat", "geosite.dat")


def _load_script(name: str, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "server"))
    spec = importlib.util.spec_from_file_location(
        f"kernel_distribution_{name}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_assets(directory: Path, sizes: dict[str, int]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, size in sizes.items():
        with (directory / name).open("wb") as asset:
            asset.truncate(size)


@pytest.mark.parametrize(
    ("windows", "filename"), [(True, "mihomo.exe"), (False, "mihomo")]
)
def test_bundled_assets_include_only_kernel_license_and_source_note(
    monkeypatch, windows, filename
):
    monkeypatch.setattr(kernel_release, "is_windows", lambda: windows)
    asset_names = getattr(kernel_release, "bundled_asset_names", None)

    assert callable(asset_names)
    assert set(kernel_release.LICENSE_ASSETS) == {"LICENSE-mihomo.txt"}
    assert asset_names() == (filename, *kernel_release.LICENSE_ASSETS, "README.txt")


def test_offline_copy_excludes_geo_files(tmp_path, monkeypatch):
    fetch = _load_script("fetch_kernel", monkeypatch)
    source = tmp_path / "source"
    destination = tmp_path / "bundle"
    expected = {kernel_release.kernel_filename(), "LICENSE-mihomo.txt"}
    _write_assets(
        source,
        dict.fromkeys((*expected, *GEO_FILES, "LICENSE-meta-rules-dat.txt"), 16),
    )
    monkeypatch.setattr(fetch, "CLASH_DIR", destination)
    monkeypatch.setattr(fetch, "KERNEL_FILE", destination / kernel_release.kernel_filename())

    copied = fetch._copy_from_dir(source)

    assert set(copied) == expected
    assert {asset.name for asset in destination.iterdir()} == expected
    assert all((source / name).is_file() for name in GEO_FILES)


def test_fetch_accepts_core_assets_without_geo_or_network(tmp_path, monkeypatch):
    fetch = _load_script("fetch_kernel", monkeypatch)
    monkeypatch.setattr(fetch, "CLASH_DIR", tmp_path)
    monkeypatch.setattr(fetch, "KERNEL_FILE", tmp_path / kernel_release.kernel_filename())
    _write_assets(
        tmp_path,
        {
            kernel_release.kernel_filename(): fetch._MIN_KERNEL_BYTES,
            "LICENSE-mihomo.txt": fetch._MIN_LICENSE_BYTES,
            "README.txt": 16,
        },
    )

    def reject_download(*args, **kwargs):
        pytest.fail("Complete bundled assets must not request a download")

    monkeypatch.setattr(fetch, "_download", reject_download)

    assert fetch._missing() == []
    assert fetch.fetch() == 0
    assert not any((tmp_path / name).exists() for name in GEO_FILES)


def test_release_accepts_core_assets_without_geo(tmp_path, monkeypatch):
    build = _load_script("build_release", monkeypatch)
    monkeypatch.setattr(build, "CLASH_DIR", tmp_path)
    _write_assets(
        tmp_path,
        dict.fromkeys((kernel_release.kernel_filename(), "LICENSE-mihomo.txt", "README.txt"), 16),
    )

    build.ensure_clash_assets()

    assert not any((tmp_path / name).exists() for name in GEO_FILES)


def test_packaging_excludes_unlisted_files_from_asset_directory(tmp_path):
    bundle = tmp_path / "assets" / "clash"
    expected = {kernel_release.kernel_filename(), "LICENSE-mihomo.txt", "README.txt"}
    _write_assets(
        bundle,
        dict.fromkeys((*expected, *GEO_FILES, "LICENSE-meta-rules-dat.txt", "extra.bin"), 16),
    )
    tree = ast.parse((ROOT / "desktop" / "holdexar.spec").read_text(encoding="utf-8"))
    analysis = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    )
    datas = next(keyword.value for keyword in analysis.keywords if keyword.arg == "datas")
    namespace = {
        "ROOT": str(tmp_path),
        "SERVER": str(tmp_path / "server"),
        "os": os,
        "bundled_asset_names": getattr(kernel_release, "bundled_asset_names", None),
    }

    inputs = eval(compile(ast.Expression(datas), "holdexar.spec", "eval"), namespace)
    clash_inputs = [Path(source) for source, destination in inputs if destination == "clash"]

    assert {source.name for source in clash_inputs} == expected
    assert all(source.parent == bundle and source.is_file() for source in clash_inputs)
