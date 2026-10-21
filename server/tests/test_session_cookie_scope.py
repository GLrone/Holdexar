"""会话 Cookie 的跨端口边界：同 host 不同端口按浏览器语义属于 same-site。

Cookie 作用域不含端口，因此 `http://localhost:8080` 上的任意页面（开发机上
跑着别的 web 服务的常见情形）向 `http://localhost:28765` 发请求时，浏览器
**会自动带上**本应用的会话 Cookie，而 `SameSite=Strict` 拦不住（它按 registrable
domain 判定，localhost 的不同端口仍是 same-site）。

结论：Cookie 本身不构成端口隔离，必须由服务端校验 `Origin` 兜住——
带 Cookie 且 Origin 不属于本应用自身的请求一律拒绝。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access
from app.domains.system.router import router as system_router
from app.domains.system.session_router import reset_bootstrap, router as session_router


@pytest.fixture(autouse=True)
def _fresh_tokens():
    access.reset_token()
    reset_bootstrap()
    yield
    access.reset_token()
    reset_bootstrap()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    return app


def _client(app: FastAPI, port: int) -> AsyncClient:
    """固定 host=localhost、可变端口——复现 Cookie 的端口无关作用域。"""
    return AsyncClient(
        transport=ASGITransport(app=app), base_url=f"http://localhost:{port}"
    )


@pytest.mark.asyncio
async def test_cookie_is_not_port_scoped():
    """服务端不设 Domain：Cookie 只绑 host，跨端口可复用（浏览器语义基线）。"""
    token = access.session_token()
    app = _app()
    async with _client(app, 28765) as client:
        r = await client.get(
            "/api/v1/system/security", headers={access.SESSION_HEADER: token}
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_cross_port_origin_rejected_even_with_valid_cookie():
    """同 host 不同端口的页面带 Cookie 发请求 → 必须被 Origin 校验拒绝。

    这是本测试的核心断言：Cookie 的 same-site 语义不区分端口，
    `SameSite=Strict` 不足以挡住开发机上其他本地 web 服务。
    """
    app = _app()
    token = access.session_token()
    async with _client(app, 28765) as client:
        r = await client.post(
            "/api/v1/system/security/lock",
            headers={
                "Origin": "http://localhost:8080",
                "Cookie": f"{access.SESSION_COOKIE}={token}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_same_origin_request_accepted():
    """同源（Origin 与 Host 一致）带 Cookie 必须放行，否则正常 UI 会失效。"""
    app = _app()
    token = access.session_token()
    async with _client(app, 28765) as client:
        r = await client.get(
            "/api/v1/system/security",
            headers={
                "Origin": "http://localhost:28765",
                "Cookie": f"{access.SESSION_COOKIE}={token}",
            },
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_no_origin_request_accepted_with_cookie():
    """无 Origin（同源导航 / 非浏览器客户端）不得被误杀。"""
    app = _app()
    token = access.session_token()
    async with _client(app, 28765) as client:
        r = await client.get(
            "/api/v1/system/security",
            headers={"Cookie": f"{access.SESSION_COOKIE}={token}"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_null_origin_rejected():
    """`Origin: null`（sandbox iframe / file://）属外部来源，必须拒绝。"""
    app = _app()
    token = access.session_token()
    async with _client(app, 28765) as client:
        r = await client.get(
            "/api/v1/system/security",
            headers={
                "Origin": "null",
                "Cookie": f"{access.SESSION_COOKIE}={token}",
            },
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_foreign_origin_rejected():
    """外部域名 Origin 带有效 Cookie 同样拒绝（纵深防御）。"""
    app = _app()
    token = access.session_token()
    async with _client(app, 28765) as client:
        r = await client.post(
            "/api/v1/system/security/lock",
            headers={
                "Origin": "https://evil.example",
                "Cookie": f"{access.SESSION_COOKIE}={token}",
            },
            content=b"{}",
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_preflight_not_blocked_by_origin_check():
    """预检不带 Cookie、不产生副作用，仍须放行给 CORS 应答。"""
    async with _client(_app(), 28765) as client:
        r = await client.options(
            "/api/v1/system/security",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 405  # 越过准入层（本夹具无 CORS 中间件）
