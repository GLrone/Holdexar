from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from desktop.version_resource import read_pe_properties, stamp_launcher_resources


ROOT = Path(__file__).resolve().parents[2]


def load_build():
    spec = importlib.util.spec_from_file_location("go_release_build", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_go_build_locks_dependencies_and_windows_target(tmp_path, monkeypatch):
    build = load_build()
    monkeypatch.setattr(build, "DIST_DIR", tmp_path)
    monkeypatch.setattr(build.shutil, "which", lambda name: "go.exe")
    calls = []
    monkeypatch.setattr(build.subprocess, "check_call", lambda args, **kwargs: calls.append((args, kwargs)))

    result = build.build_go_launcher("0.3.8", Path("python.exe"))

    command, options = calls[0]
    assert command[:3] == ["go.exe", "build", "-mod=readonly"]
    assert command[-2:] == [str(result), "."]
    assert options["cwd"] == str(build.DESKTOP / "go_launcher")
    assert {key: options["env"][key] for key in ("GOOS", "GOARCH", "CGO_ENABLED", "GOTOOLCHAIN", "GOWORK")} == {
        "GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOWORK": "off",
    }
    assert calls[1][0][-4:] == ["--stamp-launcher", str(result), "--version", "0.3.8"]


def test_go_toolchain_missing_stops_build(tmp_path, monkeypatch):
    build = load_build()
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Go 工具链"):
        build.build_go_launcher("0.3.8", Path("python.exe"))


def test_release_layout_has_one_go_launcher_and_retains_update_host(tmp_path, monkeypatch):
    build = load_build()
    app = tmp_path / "build" / "Holdexar"
    app.mkdir(parents=True)
    (app / "Holdexar.exe").write_bytes(b"desktop")
    monkeypatch.setattr(build, "APP_DIR", app)
    monkeypatch.setattr(build, "DIST_DIR", app.parent)
    monkeypatch.setattr(build, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(build, "MINI_SEED_SRC", tmp_path / "absent.db")
    native = app.parent / "_launcher_build" / "Holdexar.exe"

    def build_go(version, python):
        native.parent.mkdir()
        native.write_bytes(b"go-launcher")
        return native

    calls = []

    def compile_update_host(args, **kwargs):
        calls.append(args)
        output = Path(args[args.index("--distpath") + 1])
        output.mkdir(parents=True)
        (output / "UpdateHost.exe").write_bytes(b"update-host")

    def verify(directory, version):
        assert (directory / "Holdexar.exe").read_bytes() == b"go-launcher"
        assert (directory / "updater" / "UpdateHost.exe").read_bytes() == b"update-host"
        assert (directory / "versions" / version / "Holdexar.exe").read_bytes() == b"desktop"
        return {}

    monkeypatch.setattr(build, "build_go_launcher", build_go)
    monkeypatch.setattr(build.subprocess, "check_call", compile_update_host)
    monkeypatch.setattr(build, "verify_exe_version_resources", verify)
    build.assemble_versioned_layout("0.3.8", Path("python.exe"))

    assert len(calls) == 1
    assert calls[0][-1].endswith("update_host_main.py")
    assert calls[0][calls[0].index("--name") + 1] == "UpdateHost"
    assert not (app / ".launcher").exists()
    assert json.loads((app / "state" / "current.json").read_text())["version"] == "0.3.8"


@pytest.mark.skipif(os.name != "nt" or not shutil.which("go"), reason="Windows Go PE required")
def test_stamped_go_pe_keeps_protocol_and_windows_resources(tmp_path):
    source = tmp_path / "main.go"
    source.write_text('package main\nimport "fmt"\nfunc main(){fmt.Println(`{"updateDataProtocol":1}`)}\n', encoding="utf-8")
    executable = tmp_path / "Holdexar.exe"
    subprocess.run(["go", "build", "-ldflags=-H=windowsgui", "-o", str(executable), str(source)],
                   check=True, capture_output=True, env={**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"})
    stamp_launcher_resources(executable, "0.3.8-beta")

    props = read_pe_properties(executable)
    assert props["ProductVersion"] == "0.3.8-beta"
    assert props["FileVersion"] == "0.3.8.0"
    assert props["FileDescription"] == "Holdexar Launcher"
    result = subprocess.run([str(executable), "--update-protocol"], capture_output=True, check=True)
    assert json.loads(result.stdout) == {"updateDataProtocol": 1}

    import pefile

    with pefile.PE(str(executable)) as pe:
        assert pe.FILE_HEADER.Machine == 0x8664
        resources = {entry.id: entry for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries}
        assert {14, 16, 24} <= resources.keys()
        manifest = resources[24].directory.entries[0].directory.entries[0].data.struct
        data = pe.get_data(manifest.OffsetToData, manifest.Size)
        assert b'asInvoker' in data
