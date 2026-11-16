"""访问闸全覆盖回归：非 API 路径闸、锁定页、OPTIONS 规则、health 分级、
per-IP 解锁限速、主机文件触点回环收容、更新标识校验。

闸覆盖全部路径是 AUTH-1 的根因修复：静态资源与页面不再对局域网无会话
对端开放，锁定页是唯一可见面。
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
    create_bootstrap,
    reset_bootstrap,
    router as session_router,
)

_TEST_PASSWORD = "pw-" + secrets.token_hex(8)
_LAN_PEER = ("192.168.1.5", 43210)
_LAN_HOST = "192.168.1.5:28765"


def _setup():
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    access.set_extra_hosts(())


@pytest.fixture(autouse=True)
def _fresh_state():
    _setup()
    yield
    _setup()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    return app


def _lan_client(app: FastAPI, *, host: str = _LAN_HOST) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=_LAN_PEER),
        base_url=f"http://{host}",
    )


def _local_client(app: FastAPI, *, host: str = "127.0.0.1:28765") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=f"http://{host}")


def _enable_lan():
    lan_access.set_enabled(True)
    access.set_extra_hosts([*lan_access.local_ips(), "192.168.1.5"])


async def _run(client: AsyncClient, method: str, path: str, **kw):
    async with client:
        return await client.request(method, path, **kw)


def test_lan_no_session_root_serves_lock_page():
    """局域网无会话对端访问 / ：内联锁定页（非完整 SPA），带 nonce CSP。"""
    _enable_lan()
    access.session_token()
    r = asyncio.run(_run(_lan_client(_app()), "GET", "/"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "访问密码" in r.text and "session/unlock" in r.text
    assert "nonce-" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # 完整应用资产不再随锁定页外泄：静态资源 401
    assets = asyncio.run(_run(_lan_client(_app()), "GET", "/assets/app.js"))
    assert assets.status_code == 401


def test_lan_with_session_serves_static_paths():
    """持会话局域网对端过闸（测试应用无静态路由 → 404 表示已过闸）。"""
    _enable_lan()
    token = access.session_token()
    r = asyncio.run(_run(_lan_client(_app()), "GET", "/assets/app.js",
                         headers={access.SESSION_HEADER: token}))
    assert r.status_code == 404  # 闸放行，路由不存在
    root = asyncio.run(_run(_lan_client(_app()), "GET", "/",
                            headers={access.SESSION_HEADER: token}))
    assert root.status_code == 404  # 无 SPA 回退路由，非锁定页


def test_default_mode_lan_peer_gets_nothing_unauthenticated():
    """默认模式（未开局域网）绑 0.0.0.0：局域网对端连页面也拿不到（421）。"""
    access.session_token()
    for path in ("/", "/assets/app.js", "/api/v1/health"):
        r = asyncio.run(_run(_lan_client(_app()), "GET", path))
        assert r.status_code == 421, path


def test_options_requires_loopback_or_session():
    """OPTIONS 不再先于会话闸短路：无凭据局域网对端 403，方法表不可探测。"""
    _enable_lan()
    access.session_token()
    r = asyncio.run(_run(_lan_client(_app()), "OPTIONS", "/api/v1/health"))
    assert r.status_code == 403
    local = asyncio.run(_run(_local_client(_app()), "OPTIONS", "/api/v1/health"))
    assert local.status_code == 405  # 回环预检放行到路由（开发工作流）
    token = access.session_token()
    with_session = asyncio.run(_run(_lan_client(_app()), "OPTIONS", "/api/v1/health",
                                    headers={access.SESSION_HEADER: token}))
    assert with_session.status_code == 405


def test_hostname_rejects_garbage_port():
    """Host 端口段非数字不剥：整串按主机名对待，不归一成白名单条目。"""
    access.set_extra_hosts({"192.168.1.5"})
    assert not access.host_allowed("192.168.1.5:evil")
    assert not access.host_allowed("127.0.0.1:javascript")
    assert access.host_allowed("192.168.1.5:28765")
    access.set_extra_hosts(())


def test_bootstrap_next_rejects_control_chars():
    """bootstrap next 控制字符（tab 归一化绕过形态）拒为站内跳转。"""
    reset_bootstrap()
    access.session_token()
    nonce = create_bootstrap()
    r = asyncio.run(_run(_local_client(_app()), "GET",
                         f"/api/v1/session/bootstrap",
                         params={"nonce": nonce, "next": "/\t/evil.example"}))
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    reset_bootstrap()
    nonce = create_bootstrap()
    r = asyncio.run(_run(_local_client(_app()), "GET",
                         f"/api/v1/session/bootstrap",
                         params={"nonce": nonce, "next": "/dashboard"}))
    assert r.headers["location"] == "/dashboard"
    reset_bootstrap()


def test_health_face_split():
    """health：无会话局域网对端只见 status；回环与持会话对端见完整快照。"""
    _enable_lan()
    token = access.session_token()
    anon = asyncio.run(_run(_lan_client(_app()), "GET", "/api/v1/health")).json()
    assert anon == {"status": "ok"}
    held = asyncio.run(_run(_lan_client(_app()), "GET", "/api/v1/health",
                            headers={access.SESSION_HEADER: token})).json()
    assert held["version"] and held["app"]
    local = asyncio.run(_run(_local_client(_app()), "GET", "/api/v1/health")).json()
    assert local["version"] and "dataDir" in local


def test_update_ready_empty_body_400():
    """update-ready 空体 400（公开端点健壮性，不再 500）。"""
    access.session_token()
    _enable_lan()
    r = asyncio.run(_run(_lan_client(_app()), "POST", "/api/v1/system/update-ready",
                         content=b"", headers={"content-type": "application/json"}))
    assert r.status_code == 400


def test_import_legacy_loopback_only():
    """旧库导入是主机文件触点：局域网对端（即使持会话）403。"""
    _enable_lan()
    token = access.session_token()
    r = asyncio.run(_run(_lan_client(_app()), "POST", "/api/v1/import-legacy",
                         json={"path": "C:/nope/legacy.db"},
                         headers={access.SESSION_HEADER: token}))
    assert r.status_code == 403


def test_update_download_tag_shape():
    """更新标识白名单：路径注入形态（../、?、#）400。"""
    access.session_token()
    token = access.session_token()
    headers = {access.SESSION_HEADER: token}
    for bad in ("v0.0.0/../../evil", "v1?x=1", "v1#frag", ""):
        r = asyncio.run(_run(_local_client(_app()), "POST",
                             "/api/v1/system/update-download",
                             json={"tag": bad, "sha256": "a" * 64}, headers=headers))
        assert r.status_code == 400, bad


def test_unlock_rate_limit_per_peer():
    """解锁限速按对端分桶：恶意对端锁不住合法对端。"""
    lan_access.reset_unlock_state()
    attacker, victim = "10.0.0.9", "10.0.0.8"
    for _ in range(5):
        lan_access.unlock_record_failure(attacker)
    assert lan_access.unlock_locked_seconds(attacker) > 0
    assert lan_access.unlock_locked_seconds(victim) == 0
    lan_access.unlock_record_success(attacker)
    assert lan_access.unlock_locked_seconds(attacker) == 0


def test_unlock_rate_limit_bucket_cap():
    """限速桶有帽：超限后新对端仍可用（过期清+最旧丢）。"""
    lan_access.reset_unlock_state()
    for i in range(600):
        lan_access.unlock_record_failure(f"10.0.{i // 256}.{i % 256}")
    assert lan_access.unlock_locked_seconds("10.0.99.99") == 0  # 新对端不受限
    lan_access.reset_unlock_state()


def test_password_source_surface(monkeypatch):
    """设置面浮出口令来源：user 恒优先、env 兜底、none 未设置。"""
    from app.domains.settings import router as settings_router
    from app.domains.settings import service as settings_service

    stored: dict[str, str] = {}

    async def fake_get_value(key, default=None):
        return stored.get(key, default)

    async def fake_get_secret(key, default=None):
        return stored.get(key, default)

    monkeypatch.setattr(settings_service, "get_value", fake_get_value)
    monkeypatch.setattr(settings_service, "get_secret_value", fake_get_secret)
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)

    assert asyncio.run(settings_router._lan_state()).password_source == "none"

    monkeypatch.setenv(lan_access.ENV_ACCESS_PASSWORD, "env-pw-12345678")
    assert asyncio.run(settings_router._lan_state()).password_source == "env"

    stored[settings_service.LAN_PASSWORD_HASH_KEY] = lan_access.hash_password("user-pw-12345")
    assert asyncio.run(settings_router._lan_state()).password_source == "user"


def test_security_lock_rotates_session():
    """security/lock 是安全动作：响应重签会话 Cookie，旧令牌全量吊销。"""
    token = access.session_token()
    r = asyncio.run(_run(_local_client(_app()), "POST", "/api/v1/system/security/lock",
                         headers={access.SESSION_HEADER: token}))
    assert r.status_code == 200
    new_token = r.cookies.get(access.SESSION_COOKIE)
    assert new_token and new_token != token
