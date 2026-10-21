"""本地接口准入：Host 校验、会话要求、公开白名单。

覆盖三类对手的边界：本机进程（无会话）、浏览器跨源（简单请求）、
DNS 重绑定（Host 不是回环名）。三者必须分别有断言，不能互相替代。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access
from app.domains.system.router import router as system_router
from app.domains.system.session_router import (
    create_bootstrap,
    reset_bootstrap,
    router as session_router,
)


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


def _client(app: FastAPI, *, host: str = "127.0.0.1:28765") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url=f"http://{host}"
    )


@pytest.mark.asyncio
async def test_sensitive_route_requires_session():
    """无会话直接调用（本机任意进程的形态）必须被拒。"""
    async with _client(_app()) as client:
        r = await client.get("/api/v1/system/security")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_is_public():
    """健康检查必须公开：桌面壳判活与外部探针都靠它。"""
    async with _client(_app()) as client:
        r = await client.get("/api/v1/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_rebinding_host_rejected_even_with_session():
    """DNS 重绑定形态：Host 是攻击者域名 → 即便会话正确也拒。

    这是重绑定唯一的结构性防线——重绑定后浏览器认为同源，CORS 不介入。
    """
    app = _app()
    token = access.session_token()
    async with _client(app, host="evil.example:28765") as client:
        r = await client.get(
            "/api/v1/system/security", headers={access.SESSION_HEADER: token}
        )
        assert r.status_code == 421


@pytest.mark.asyncio
async def test_session_header_accepted():
    """持有令牌即放行（桌面壳 / 开发态直连形态）。"""
    app = _app()
    token = access.session_token()
    async with _client(app) as client:
        r = await client.get(
            "/api/v1/system/security", headers={access.SESSION_HEADER: token}
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_wrong_token_rejected():
    """令牌错误不得放行——防止「有 Cookie 就过」的伪实现。"""
    app = _app()
    access.session_token()
    async with _client(app) as client:
        r = await client.get(
            "/api/v1/system/security", headers={access.SESSION_HEADER: "guess"}
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_nonce_is_single_use():
    """引导票据只能用一次：重放不得再次发放会话。"""
    app = _app()
    nonce = create_bootstrap()
    async with _client(app) as client:
        first = await client.get(
            f"/api/v1/session/bootstrap?nonce={nonce}", follow_redirects=False
        )
        assert first.status_code == 303
        assert access.SESSION_COOKIE in first.cookies

        replay = await client.get(
            f"/api/v1/session/bootstrap?nonce={nonce}", follow_redirects=False
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_bootstrap_rejects_open_redirect():
    """`next` 只接受站内相对路径。"""
    app = _app()
    nonce = create_bootstrap()
    async with _client(app) as client:
        r = await client.get(
            f"/api/v1/session/bootstrap?nonce={nonce}&next=https://evil.example",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"


@pytest.mark.asyncio
async def test_expired_bootstrap_rejected(monkeypatch):
    """票据过期即失效（不依赖进程内时间累积）。"""
    from app.domains.system import session_router as module

    app = _app()
    nonce = create_bootstrap()
    monkeypatch.setattr(module, "_BOOTSTRAP_TTL_S", -1.0)
    # 重新签发一张立即过期的票据
    expired = create_bootstrap()
    async with _client(app) as client:
        r = await client.get(
            f"/api/v1/session/bootstrap?nonce={expired}", follow_redirects=False
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_preflight_passes_through():
    """预检请求不带 Cookie 也不产生副作用，必须放行到路由层。

    本夹具没挂 CORS 中间件，因此 405 正是「已越过准入层」的证据——
    若准入层拦了预检会得到 401/421。
    """
    async with _client(_app()) as client:
        r = await client.options(
            "/api/v1/system/security",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 405


@pytest.mark.asyncio
async def test_simple_request_without_session_has_no_side_effect():
    """跨源简单请求（text/plain，无预检）不得产生副作用。

    这是浏览器跨源攻击面的核心断言：CORS 挡不住发送，只能靠准入层。
    """
    app = _app()
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/system/security/lock",
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
            content=b"{}",
        )
        # 外部 Origin 在会话校验之前就被拦下（403）；无 Origin 的同类请求才是 401
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_non_api_paths_not_gated():
    """SPA 静态资源与页面回退不拦——否则用户看到白屏而非引导页。"""
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)

    @app.get("/")
    async def index():
        return {"ok": True}

    async with _client(app) as client:
        r = await client.get("/")
        assert r.status_code == 200


def test_host_allowed_matrix():
    assert access.host_allowed("127.0.0.1:28765")
    assert access.host_allowed("localhost:28765")
    assert access.host_allowed("[::1]:28765")
    assert not access.host_allowed("evil.example:28765")
    assert not access.host_allowed("127.0.0.1.evil.example:28765")
    assert not access.host_allowed("")
