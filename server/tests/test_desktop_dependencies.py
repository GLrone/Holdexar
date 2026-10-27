"""Desktop imports and core operations work without excluded build dependencies."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_desktop_core_dependencies_work_without_runtime_downloads(tmp_path):
    root = Path(__file__).resolve().parents[2]
    probe = r'''
import asyncio
import importlib.machinery
import sys

excluded = {
    "PIL", "pygments", "openpyxl", "et_xmlfile", "asyncpg",
    "watchfiles", "setuptools", "pkg_resources", "packaging",
}
find_spec = importlib.machinery.PathFinder.find_spec

def without_excluded(fullname, path=None, target=None):
    if fullname.split(".")[0] in excluded:
        return None
    return find_spec(fullname, path, target)

importlib.machinery.PathFinder.find_spec = without_excluded
network_attempts = []

def deny_network(event, args):
    if event == "socket.connect" and args[1][0] in {"127.0.0.1", "::1"}:
        return
    if event in {"socket.connect", "socket.getaddrinfo"}:
        network_attempts.append(event)
        raise RuntimeError("dependency import must work offline")

sys.addaudithook(deny_network)
sys.path.insert(0, "server")
from desktop import main
import httpx
import uvicorn
from uvicorn.supervisors import ChangeReload
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core import secretbox

assert ChangeReload.__name__ == "StatReload"
assert FastMCP("offline-import-probe").name == "offline-import-probe"
with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
    assert client.get("https://example.invalid/").status_code == 200
master = bytes(range(32))
token = secretbox.encrypt_with("credential", "probe", master)
assert secretbox.decrypt_with(token, "probe", master) == "credential"

async def check_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()

asyncio.run(check_sqlite())
assert not network_attempts, network_attempts
assert not (excluded & sys.modules.keys()), excluded & sys.modules.keys()
print("desktop/httpx/uvicorn/FastMCP/SQLite/secretbox: offline OK")
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env={**os.environ, "HOLDEXAR_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offline OK" in result.stdout
