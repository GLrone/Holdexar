from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native_probe_exe(tmp_path_factory) -> Path:
    if os.name != "nt" or not shutil.which("go"):
        pytest.skip("Windows Go toolchain required")
    work = tmp_path_factory.mktemp("dev_launcher_probe")
    src = work / "main.go"
    src.write_text(
        'package main\n'
        'import (\n'
        '\t"encoding/json"\n'
        '\t"fmt"\n'
        '\t"os"\n'
        '\t"strconv"\n'
        ')\n'
        'func main() {\n'
        '\tcwd, _ := os.Getwd()\n'
        '\tsrv := os.Getenv("HOLDEXAR_SERVER_EXE")\n'
        '\t_, err := os.Stat(srv)\n'
        '\tb, _ := json.Marshal(map[string]any{\n'
        '\t\t"root": os.Getenv("HOLDEXAR_INSTALL_ROOT"),\n'
        '\t\t"cwd": cwd,\n'
        '\t\t"port": os.Getenv("HOLDEXAR_PORT"),\n'
        '\t\t"lock": os.Getenv("HOLDEXAR_LOCK_PORT"),\n'
        '\t\t"watch": os.Getenv("HOLDEXAR_DEV_WATCH"),\n'
        '\t\t"server": srv,\n'
        '\t\t"serverExists": err == nil,\n'
        '\t\t"args": os.Args[1:],\n'
        '\t})\n'
        '\tfmt.Println("PROBE_JSON=" + string(b))\n'
        '\tcode, _ := strconv.Atoi(os.Getenv("HOLDEXAR_TEST_EXIT_CODE"))\n'
        '\tos.Exit(code)\n'
        '}\n',
        encoding="utf-8",
    )
    exe = work / "probe.exe"
    subprocess.run(
        ["go", "build", "-o", str(exe), str(src)],
        check=True,
        capture_output=True,
        env={**os.environ, "CGO_ENABLED": "0", "GOWORK": "off"},
    )
    return exe


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
def test_dev_go_native_delegates_to_dev_go(tmp_path):
    launcher = tmp_path / "dev-go-native.bat"
    launcher.write_bytes((ROOT / "dev-go-native.bat").read_bytes())
    (tmp_path / "dev-go.bat").write_text(
        "@echo off\necho NATIVE_DELEGATE=%*\nexit /b 5\n", encoding="ascii"
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "--dev", "--port", "29000"],
        cwd=tmp_path,
        input="\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "NATIVE_DELEGATE=--dev --port 29000" in result.stdout
    assert result.returncode == 5


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
@pytest.mark.parametrize(("desktop_code", "preset_port", "expected_port"), [
    (0, "", "28767"),
    (9, "31234", "31234"),
])
def test_go_dev_launcher_opens_desktop_without_python_and_cleans_temp(
    tmp_path, native_probe_exe, desktop_code, preset_port, expected_port
):
    root = tmp_path / "checkout with spaces"
    (root / "server-go").mkdir(parents=True)
    (root / "desktop" / "windowhost").mkdir(parents=True)
    launcher = root / "dev-go.bat"
    launcher.write_bytes((ROOT / "dev-go.bat").read_bytes())

    tools = tmp_path / "tools"
    tools.mkdir()
    build_log = tmp_path / "builds.log"
    (tools / "python.cmd").write_text("@echo off\necho PYTHON_INVOKED\nexit /b 99\n", encoding="ascii")
    (tools / "go.cmd").write_text(
        "@echo off\n"
        f'echo %CD%^|%*>>"{build_log}"\n'
        'if not "%~1"=="build" exit /b 2\n'
        'if not "%~2"=="-o" exit /b 2\n'
        f'copy /y "{native_probe_exe}" "%~3" >nul\n'
        "exit /b 0\n",
        encoding="ascii",
    )

    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "HOLDEXAR_TEST_EXIT_CODE": str(desktop_code),
    }
    if preset_port:
        env["HOLDEXAR_PORT"] = preset_port
    else:
        env.pop("HOLDEXAR_PORT", None)

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher), "-h", "--server"],
        cwd=tmp_path,
        env=env,
        input="\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == desktop_code, result.stdout + result.stderr
    assert "PYTHON_INVOKED" not in result.stdout

    builds = [line.strip() for line in build_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(builds) == 2
    assert builds[0].startswith(f"{root / 'server-go'}|build -o ")
    assert builds[0].endswith("./cmd/serverd")
    assert builds[1].startswith(f"{root / 'desktop' / 'windowhost'}|build -o ")
    assert builds[1].endswith(" .")

    probe_lines = [line for line in result.stdout.splitlines() if line.startswith("PROBE_JSON=")]
    assert len(probe_lines) == 1
    payload = json.loads(probe_lines[0].removeprefix("PROBE_JSON="))
    assert Path(payload["root"]) == root
    assert Path(payload["cwd"]) == root
    assert payload["port"] == expected_port
    assert payload["lock"] == "28966"
    assert Path(payload["watch"]) == root / "server-go"
    assert payload["serverExists"] is True
    assert payload["args"] == ["-h", "--server"]

    server_exe = Path(payload["server"])
    assert server_exe.name == "serverd.exe"
    assert server_exe.parent.name.startswith("holdexar-dev-bin-")
    assert not server_exe.parent.exists(), "per-launch temp bin directory was not cleaned up"
