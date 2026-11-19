"""忘记密码找回：Steam 登录验证号主身份（绑定集合守卫 + 会话签发）。

信任锚 = Steam 官方登录身份 ∈ steam_accounts 绑定集合；未绑定账号登录
不落库不签发会话。五个端点公开可达，与解锁限速共用一条防线。
"""
from __future__ import annotations

import asyncio
import secrets

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access, lan_access
from app.domains import account
from app.domains.account import login
from app.domains.system.router import router as system_router
from app.domains.system.session_router import (
    reset_bootstrap,
    router as session_router,
)

_OWNER_SID = "76561198000000001"
_STRANGER_SID = "76561198000000099"


@pytest.fixture(autouse=True)
def _fresh_state():
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(True)
    yield
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    login.set_recovery_guard(None)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    return app


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://127.0.0.1:28765"
    )


def _mock_steam_login(monkeypatch, *, login_sid: str, bound_sids: set[str]) -> list[str]:
    """把 Steam 登录协议链替换为本地假流；返回绑定落库调用记录。"""
    async def fake_proxy():
        return None

    async def fake_begin(acct, pwd, proxy_url):
        return {
            "steam_id": login_sid,
            "client_id": 1,
            "request_id": b"req",
            "interval": 0.01,
            "code_guard": 0,
            "code_available": False,
            "device_ok": True,
            "guards": (),
        }

    async def fake_poll(session, proxy_url):
        return "token", "fake-refresh"

    async def fake_web_cookies(refresh, steam_id, proxy_url=None):
        return f"steamLoginSecure={steam_id}%7C%7Cfake"

    bound_calls: list[str] = []

    async def fake_bind(cookies):
        bound_calls.append(cookies)

    async def fake_bound_list():
        return [(sid, "cookie") for sid in sorted(bound_sids)]

    monkeypatch.setattr(login, "strategy_proxy_or_direct", fake_proxy)
    monkeypatch.setattr(login, "_begin_session", fake_begin)
    monkeypatch.setattr(login, "_poll_once", fake_poll)
    monkeypatch.setattr(login, "web_cookies_from_refresh_token", fake_web_cookies)
    monkeypatch.setattr(login, "bind_account", fake_bind)
    monkeypatch.setattr(account.service, "list_bound_cookies", fake_bound_list)
    return bound_calls


async def _wait_login_done(client: AsyncClient) -> dict:
    for _ in range(50):
        status = (await client.get("/api/v1/session/recovery/login/status")).json()
        if status["state"] in ("done", "failed"):
            return status
        await asyncio.sleep(0.01)
    return status


def test_owner_recovery_signs_session(monkeypatch):
    """绑定号主登录：守卫放行 → 绑定落库 → complete 签发会话 Cookie。"""
    bound_calls = _mock_steam_login(monkeypatch, login_sid=_OWNER_SID, bound_sids={_OWNER_SID})

    async def run():
        async with _client() as client:
            start = await client.post(
                "/api/v1/session/recovery/login/start",
                json={"account": "owner", "password": "steam-pw"},
            )
            assert start.status_code == 200
            assert start.json()["ok"] is True
            status = await _wait_login_done(client)
            complete = await client.post("/api/v1/session/recovery/complete")
            return status, complete

    status, complete = asyncio.run(run())
    assert status["state"] == "done", status
    assert complete.status_code == 200
    assert complete.json()["ok"] is True
    assert "holdexar_session" in complete.headers.get("set-cookie", "")
    assert bound_calls  # 已绑定账号正常落库


def test_stranger_login_blocked_from_binding_and_session(monkeypatch):
    """未绑定账号：守卫拦截 → 不落库、登录失败态、complete 拒签发。"""
    bound_calls = _mock_steam_login(monkeypatch, login_sid=_STRANGER_SID, bound_sids={_OWNER_SID})

    async def run():
        async with _client() as client:
            start = await client.post(
                "/api/v1/session/recovery/login/start",
                json={"account": "stranger", "password": "steam-pw"},
            )
            assert start.status_code == 200
            status = await _wait_login_done(client)
            complete = await client.post("/api/v1/session/recovery/complete")
            return status, complete

    status, complete = asyncio.run(run())
    assert status["state"] == "failed", status
    assert "未绑定" in status["error"]
    assert bound_calls == []  # 未绑定账号不落库
    assert complete.json()["ok"] is False
    assert "holdexar_session" not in complete.headers.get("set-cookie", "")


def test_recovery_complete_requires_done_state():
    """未走完登录流程直接 complete：拒绝签发。"""

    async def run():
        async with _client() as client:
            return await client.post("/api/v1/session/recovery/complete")

    complete = asyncio.run(run())
    assert complete.json()["ok"] is False


def test_recovery_shares_unlock_rate_limit(monkeypatch):
    """找回与解锁共用限速：解锁失败计数打满后找回登录同样被拒。"""
    _mock_steam_login(monkeypatch, login_sid=_OWNER_SID, bound_sids={_OWNER_SID})
    for _ in range(5):
        # 与 ASGITransport 对端同桶（client=127.0.0.1 → 分桶键即此地址）
        lan_access.unlock_record_failure("127.0.0.1")

    async def run():
        async with _client() as client:
            return await client.post(
                "/api/v1/session/recovery/login/start",
                json={"account": "owner", "password": "steam-pw"},
            )

    response = asyncio.run(run())
    body = response.json()
    assert body["ok"] is False
    assert "尝试次数过多" in body["error"]


def test_recovery_cancel_cooldown(monkeypatch):
    """取消端点 1 秒冷却：连打第二次为无操作回执，不触碰登录链。"""
    bound_calls = _mock_steam_login(monkeypatch, login_sid=_OWNER_SID, bound_sids={_OWNER_SID})

    async def run():
        async with _client() as client:
            first = await client.post("/api/v1/session/recovery/cancel")
            second = await client.post("/api/v1/session/recovery/cancel")
            return first, second

    first, second = asyncio.run(run())
    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert bound_calls == []
