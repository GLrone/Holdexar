"""MCP 开关的运行时回路：settings PUT → KV + 会话管理器真实启停。

设置面单一写入口：PUT /settings 的 mcp_enabled 字段（环境变量 HOLDEXAR_MCP
仍是更强的部署级覆盖——KV 关闭时它强制在役并经 mcp_forced_by_env 投影）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.database import init_db  # noqa: E402
from app.domains.agent import mcp  # noqa: E402
from app.domains.settings import service as settings_service  # noqa: E402
from app.domains.settings.router import (  # noqa: E402
    SettingsUpdate,
    get_settings as get_settings_payload,
    update_settings,
)


@pytest_asyncio.fixture(autouse=True)
async def _shutdown_mcp_after_each():
    yield
    await mcp.shutdown()


@pytest.mark.asyncio
async def test_toggle_starts_and_stops_runtime(tmp_data_dir):
    await init_db()
    assert (await get_settings_payload()).mcp_running is False

    payload = await update_settings(SettingsUpdate(mcp_enabled=True))
    assert payload.mcp_enabled is True
    assert payload.mcp_running is True
    assert payload.mcp_error == ""
    token = get_settings().data_dir / "mcp-token"
    assert token.is_file()

    payload = await update_settings(SettingsUpdate(mcp_enabled=False))
    assert payload.mcp_enabled is False
    assert payload.mcp_running is False
    assert not token.exists()
    assert await settings_service.get_value("mcp.enabled", None) is False


@pytest.mark.asyncio
async def test_env_force_beats_kv_toggle(tmp_data_dir, monkeypatch):
    await init_db()
    monkeypatch.setenv("HOLDEXAR_MCP", "1")
    payload = await update_settings(SettingsUpdate(mcp_enabled=False))
    assert payload.mcp_forced_by_env is True
    assert payload.mcp_running is True
    payload = await get_settings_payload()
    assert payload.mcp_enabled is False  # KV 如实记录用户意图
    assert payload.mcp_running is True  # 运行态按更强的一侧生效


@pytest.mark.asyncio
async def test_startup_failure_recorded_not_raised(tmp_data_dir, monkeypatch):
    await init_db()

    def _boom(data_dir: Path) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(mcp, "_ensure_token_file", _boom)
    payload = await update_settings(SettingsUpdate(mcp_enabled=True))
    assert payload.mcp_enabled is True
    assert payload.mcp_running is False
    assert "disk full" in payload.mcp_error

    monkeypatch.undo()
    payload = await update_settings(SettingsUpdate(mcp_enabled=True))
    assert payload.mcp_running is True
    assert payload.mcp_error == ""
