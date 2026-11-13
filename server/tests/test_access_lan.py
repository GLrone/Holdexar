"""局域网访问模式：Host 闸扩展、解锁通道、限速与默认模式回归。

默认模式必须与历史行为逐点一致（回环-only、非回环 421、解锁 404）；
局域网模式开启后仅多放行本机地址，DNS 重绑定防线不放宽。
"""
from __future__ import annotations

import asyncio
import secrets

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access, lan_access
from app.domains.system.router import router as system_router
from app.domains.system.session_router import (
    ensure_lan_password,
    reset_bootstrap,
    router as session_router,
)

# 测试夹具密码：运行时生成，与真实凭据无关
_TEST_PASSWORD = "pw-" + secrets.token_hex(8)
_WRONG_PASSWORD = "wrong-" + secrets.token_hex(8)
_ENV_PASSWORD = "env-" + secrets.token_hex(8)
_LAN_HOST = "192.168.1.5:28765"


@pytest.fixture(autouse=True)
def _fresh_lan_state(monkeypatch):
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    access.set_extra_hosts(())
    yield
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    access.set_extra_hosts(())


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    return app


def _client(app: FastAPI, *, host: str = "127.0.0.1:28765") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url=f"http://{host}"
    )


def _enable_lan(monkeypatch, *, password: str | None = _TEST_PASSWORD):
    """开启局域网模式并注入固定密码（DB 无关路径）。"""
    lan_access.set_enabled(True)
    # 合成局域网地址与真实本机地址都入白名单，测试与真机两形态都覆盖
    access.set_extra_hosts([*lan_access.local_ips(), "192.168.1.5"])
    if password is not None:
        monkeypatch.setattr(
            "app.domains.system.session_router.ensure_lan_password",
            lambda: _async_of(password),
        )


async def _async_of(value: str) -> str:
    return value


def test_default_mode_lan_host_still_rejected():
    """默认模式回归：非回环 Host 一律 421（历史行为不变）。"""
    access.session_token()
    app = _app()
    async def run():
        async with _client(app, host=_LAN_HOST) as client:
            return await client.get("/api/v1/health")
    assert asyncio.run(run()).status_code == 421


def test_default_mode_unlock_disabled():
    """默认模式：解锁入口不存在（发行版无自助通道语义保持）。"""
    async def run():
        app = _app()
        async with _client(app) as client:
            return await client.post(
                "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
            )
    assert asyncio.run(run()).status_code == 404


def test_lan_mode_allows_local_ip_host(monkeypatch):
    """局域网模式：本机网卡地址过 Host 闸，落到会话闸（401 而非 421）。"""
    _enable_lan(monkeypatch)
    async def run():
        app = _app()
        async with _client(app, host=_LAN_HOST) as client:
            return await client.get("/api/v1/system/security")
    assert asyncio.run(run()).status_code == 401


def test_lan_mode_still_blocks_rebinding_domain(monkeypatch):
    """局域网模式不放宽重绑定防线：攻击者域名 Host 仍 421。"""
    _enable_lan(monkeypatch)
    async def run():
        app = _app()
        async with _client(app, host="evil.example:28765") as client:
            return await client.get("/api/v1/health")
    assert asyncio.run(run()).status_code == 421


def test_lan_mode_same_origin_lan_request_passes_gates(monkeypatch):
    """局域网直连形态：Host 与 Origin 同为本机地址 → 只差会话（401）。"""
    _enable_lan(monkeypatch)
    async def run():
        app = _app()
        async with _client(app, host=_LAN_HOST) as client:
            return await client.get(
                "/api/v1/system/security",
                headers={"Origin": f"http://{_LAN_HOST}"},
            )
    assert asyncio.run(run()).status_code == 401


def test_unlock_flow_wrong_password_then_limit(monkeypatch):
    """解锁：错密码 401 计数，连续失败达阈值后 429；正确密码放行并清零。"""
    _enable_lan(monkeypatch)
    async def run():
        app = _app()
        async with _client(app) as client:
            codes = []
            for _ in range(5):
                r = await client.post(
                    "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
                )
                codes.append(r.status_code)
            limited = await client.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            lan_access.reset_unlock_state()
            ok = await client.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            cookie = client.cookies.get(access.SESSION_COOKIE, "")
            protected = await client.get(
                "/api/v1/system/security",
                headers={access.SESSION_HEADER: cookie},
            )
            return codes, limited.status_code, ok.status_code, protected.status_code
    codes, limited, ok, protected = asyncio.run(run())
    assert codes == [401, 401, 401, 401, 401]
    assert limited == 429
    assert ok == 200
    assert protected == 200


def test_unlock_env_password_channel(monkeypatch):
    """无窗口通道：环境变量指定的密码直接生效。"""
    lan_access.set_enabled(True)
    access.set_extra_hosts(lan_access.local_ips())
    monkeypatch.setenv(lan_access.ENV_ACCESS_PASSWORD, _ENV_PASSWORD)
    async def run():
        app = _app()
        async with _client(app) as client:
            return await client.post(
                "/api/v1/session/unlock", json={"password": _ENV_PASSWORD}
            )
    assert asyncio.run(run()).status_code == 200


def test_refresh_extra_hosts_follows_toggle(monkeypatch):
    """开关与白名单联动：开＝本机地址集，关＝回环-only。"""
    monkeypatch.setattr(
        lan_access, "local_ips", lambda: ["127.0.0.1", "192.168.1.5"]
    )
    lan_access.set_enabled(True)
    lan_access.refresh_extra_hosts()
    assert access.host_allowed(_LAN_HOST)
    lan_access.set_enabled(False)
    lan_access.refresh_extra_hosts()
    assert not access.host_allowed(_LAN_HOST)
    assert access.host_allowed("127.0.0.1:28765")


def test_host_helpers_normalize_host_header():
    """Host 归一：端口剥离、IPv6 括号、大小写；额外地址不波及回环判定。"""
    access.set_extra_hosts(["192.168.1.5", "FE80::1"])
    assert access.host_allowed("192.168.1.5:28765")
    assert access.host_allowed("192.168.1.5")
    assert access.host_allowed("[fe80::1]:28765")
    assert access.host_allowed("LOCALHOST:9999")
    assert access.is_loopback_host("[::1]:28765")
    assert not access.is_loopback_host("192.168.1.5:28765")
    assert not access.host_allowed("")
    access.set_extra_hosts(())
    assert not access.host_allowed("192.168.1.5:28765")


def test_ensure_lan_password_generates_once(monkeypatch):
    """密码解析：无 KV 无环境变量时生成并回读同值（monkeypatch 存取通道）。"""
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)
    stored: dict[str, str] = {}
    import app.domains.system.session_router as session_router_mod

    async def fake_get_secret(key, default=None):
        return stored.get(key, default)

    async def fake_set_values(values, *, secret_values=None):
        stored.update(secret_values or {})

    monkeypatch.setattr(
        "app.domains.settings.service.get_secret_value", fake_get_secret
    )
    monkeypatch.setattr(
        "app.domains.settings.service.set_values", fake_set_values
    )
    async def run():
        first = await session_router_mod.ensure_lan_password()
        second = await session_router_mod.ensure_lan_password()
        return first, second
    first, second = asyncio.run(run())
    assert first == second
    assert len(first) >= 8
