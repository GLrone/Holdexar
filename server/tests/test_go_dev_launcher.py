from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_default_dev_uses_go_backend(tmp_path):
    launcher = tmp_path / "dev.bat"
    launcher.write_bytes((ROOT / "dev.bat").read_bytes())
    (tmp_path / "dev-go.bat").write_text(
        "@echo off\necho GO_DEFAULT=%*\nexit /b 7\n", encoding="ascii"
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "--server"],
        cwd=tmp_path,
        input="\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "GO_DEFAULT=--server" in result.stdout
    assert result.returncode == 7


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
@pytest.mark.parametrize("desktop_code", [0, 9])
def test_go_dev_launcher_opens_desktop_from_any_directory(tmp_path, desktop_code):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    (root / "server-go").mkdir()
    launcher = root / "dev-go.bat"
    launcher.write_bytes((ROOT / "dev-go.bat").read_bytes())
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python.cmd").write_text(
        "@echo off\n"
        "echo ROOT=%HOLDEXAR_INSTALL_ROOT%\n"
        "echo CWD=%CD%\n"
        "echo DESKTOP=%*\n"
        "echo SERVER=%HOLDEXAR_SERVER_EXE%\n"
        "echo LOCK=%HOLDEXAR_LOCK_PORT%\n"
        "echo WATCH=%HOLDEXAR_DEV_WATCH%\n"
        f"exit /b {desktop_code}\n",
        encoding="ascii",
    )
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "-h"],
        cwd=tmp_path,
        env=env,
        input="\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == desktop_code, result.stdout + result.stderr
    assert f"ROOT={root}" in result.stdout
    assert f"CWD={root}" in result.stdout
    assert "DESKTOP=run.py --port 28767 -h" in result.stdout
    assert "SERVER=\n" in result.stdout
    assert "LOCK=28966" in result.stdout
    assert f"WATCH={root / 'server-go'}" in result.stdout
