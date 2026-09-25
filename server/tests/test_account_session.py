"""登录会话续期测试：访问令牌有效期解析 / 续期两跳（finalizelogin + transfer）/
续期失败与凭据缺席时的诚实降级 / 登录过期不进网络退避计数。
网络层全部打桩，库为独立临时库。"""
import base64
import json
import sys
import time
import types
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.account import session as session_module
from app.domains.account.models import SteamAccount
from app.domains.account.session import (
    SessionRefreshError,
    access_token_expires_at,
    refresh_login_cookies,
    refresh_token_of,
    session_freshness,
)
from app.domains.account.steam_wallet import parse_cookie_str

A = "76561198000000001"
TRANSFER_URL = "https://store.steampowered.com/login/settoken"
_DAY = 86400.0


# ── Cookie / JWT 构造 ────────────────────────────────────

def _jwt(claims: dict) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'EdDSA', 'typ': 'JWT'})}.{seg(claims)}.sig"


def access_value(sid: str = A, *, exp: float) -> str:
    return f"{sid}%7C%7C{_jwt({'sub': sid, 'aud': ['web:store'], 'exp': exp})}"


def refresh_value(sid: str = A, *, exp: float | None = None) -> str:
    exp = time.time() + 200 * _DAY if exp is None else exp
    return f"{sid}%7C%7C{_jwt({'sub': sid, 'aud': ['web', 'renew', 'derive'], 'exp': exp})}"


def cookie_str(*, access_exp: float, with_refresh: bool = True) -> str:
    parts = [
        "sessionid=abc123",
        "steamCountry=CN%7C",
        f"steamLoginSecure={access_value(exp=access_exp)}",
    ]
    if with_refresh:
        parts.append(f"steamRefresh_steam={refresh_value()}")
    return "; ".join(parts)


# ── httpx 替身 ───────────────────────────────────────────

class _FakeHeaders:
    def __init__(self, mapping: dict):
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get_list(self, name: str) -> list[str]:
        val = self._mapping.get(name.lower(), [])
        return list(val) if isinstance(val, list) else [val]


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_body=None, set_cookie=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = _FakeHeaders({"set-cookie": set_cookie or []})

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    """按 URL 回放预置响应，并记录每次 POST 的表单。"""

    def __init__(self, responses: dict, calls: list):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        self._calls.append((url, kwargs))
        resp = self._responses.get(url)
        if resp is None:
            raise AssertionError(f"未预置的 URL：{url}")
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_httpx(monkeypatch, responses: dict, calls: list) -> None:
    fake = types.SimpleNamespace(
        AsyncClient=lambda **_kw: _FakeClient(responses, calls),
        HTTPError=session_module.httpx.HTTPError,
    )
    monkeypatch.setattr(session_module, "httpx", fake)


def _finalize_ok(*, set_cookie=None) -> _FakeResponse:
    return _FakeResponse(
        json_body={"transfer_info": [{"url": TRANSFER_URL, "params": {"auth": "1"}}]},
        set_cookie=set_cookie or [],
    )


# ── 有效期解析 ───────────────────────────────────────────

def test_access_token_expires_at_reads_jwt_exp():
    exp = time.time() + _DAY
    parsed = access_token_expires_at(cookie_str(access_exp=exp))
    assert parsed is not None
    assert abs(parsed.timestamp() - exp) < 1.0


def test_access_token_expires_at_tolerates_non_jwt_values():
    assert access_token_expires_at(f"steamLoginSecure={A}%7C%7Cnot-a-jwt") is None
    assert access_token_expires_at("sessionid=abc") is None


def test_session_freshness_reports_expiry_and_refresh_presence():
    live = session_freshness(cookie_str(access_exp=time.time() + 3 * _DAY))
    assert live["expired"] is False and live["expiring"] is False
    assert live["seconds_left"] > 2 * _DAY
    assert live["has_refresh_token"] is True

    # 剩余寿命进入续期提前量：不判过期但需要提前换新
    ahead = session_freshness(cookie_str(access_exp=time.time() + 600))
    assert ahead["expired"] is False and ahead["expiring"] is True

    dead = session_freshness(cookie_str(access_exp=time.time() - 60))
    assert dead["expired"] is True
    # expires_at 与账号行其余时间戳同口径：北京时间无时区 ISO 串
    assert dead["expires_at"] is not None and "+" not in dead["expires_at"]

    without = session_freshness(cookie_str(access_exp=time.time() + _DAY, with_refresh=False))
    assert without["has_refresh_token"] is False


def test_refresh_token_of_strips_owner_prefix():
    raw = cookie_str(access_exp=time.time() - 10)
    token = refresh_token_of(raw)
    assert token and "||" not in token and "%7C%7C" not in token
    assert refresh_token_of("sessionid=abc") == ""


# ── 续期两跳 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_login_cookies_runs_finalize_then_transfer(monkeypatch):
    calls: list = []
    new_access = access_value(exp=time.time() + _DAY)
    rotated_refresh = refresh_value(exp=time.time() + 200 * _DAY)
    responses = {
        session_module.FINALIZE_URL: _finalize_ok(
            set_cookie=[f"steamRefresh_steam={rotated_refresh}; Path=/"]
        ),
        TRANSFER_URL: _FakeResponse(
            set_cookie=[
                f"steamLoginSecure={new_access}; Path=/; HttpOnly",
                "sessionid=stale; Path=/",
            ]
        ),
    }
    _patch_httpx(monkeypatch, responses, calls)

    raw = cookie_str(access_exp=time.time() - 10)
    out = await refresh_login_cookies(raw)

    # 第一跳：nonce = 续期令牌（不带 steamid|| 前缀）
    finalize_url, finalize_kwargs = calls[0]
    assert finalize_url == session_module.FINALIZE_URL
    assert finalize_kwargs["files"]["nonce"][1] == refresh_token_of(raw)
    assert finalize_kwargs["files"]["redir"][1] == session_module.FINALIZE_REDIR
    # 第二跳：steamID + transfer 自带 params
    assert calls[1][0] == TRANSFER_URL
    assert calls[1][1]["files"]["steamID"][1] == A
    assert calls[1][1]["files"]["auth"][1] == "1"

    jar = parse_cookie_str(out)
    assert jar["steamLoginSecure"] == new_access      # 换发的新访问令牌
    assert jar["steamRefresh_steam"] == rotated_refresh  # 轮换后的续期凭据落库
    assert jar["steamCountry"] == "CN%7C"             # 未变字段原样保留
    assert jar["sessionid"] and jar["sessionid"] != "stale"


@pytest.mark.asyncio
async def test_refresh_login_cookies_requires_refresh_credential():
    raw = cookie_str(access_exp=time.time() - 10, with_refresh=False)
    with pytest.raises(SessionRefreshError, match="记住我"):
        await refresh_login_cookies(raw)


@pytest.mark.asyncio
async def test_refresh_login_cookies_surfaces_steam_error(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {session_module.FINALIZE_URL: _FakeResponse(json_body={"error": 84})},
        calls,
    )
    with pytest.raises(SessionRefreshError, match="84"):
        await refresh_login_cookies(cookie_str(access_exp=time.time() - 10))


@pytest.mark.asyncio
async def test_refresh_login_cookies_rejects_malformed_and_failed_transfer(monkeypatch):
    calls: list = []
    # 缺 transfer_info
    _patch_httpx(
        monkeypatch,
        {session_module.FINALIZE_URL: _FakeResponse(json_body={"foo": 1})},
        calls,
    )
    with pytest.raises(SessionRefreshError, match="transfer_info"):
        await refresh_login_cookies(cookie_str(access_exp=time.time() - 10))

    # transfer 未下发 steamLoginSecure（重试后仍失败）
    calls.clear()
    _patch_httpx(
        monkeypatch,
        {
            session_module.FINALIZE_URL: _finalize_ok(),
            TRANSFER_URL: _FakeResponse(status_code=500),
        },
        calls,
    )
    with pytest.raises(SessionRefreshError):
        await refresh_login_cookies(cookie_str(access_exp=time.time() - 10))


# ── ensure_live_session / 钱包路径 ────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def _isolate(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(account_service, "_strategy_proxy", _none)
    account_service._session_refresh_at.clear()
    account_service._session_refresh_locks.clear()


async def _seed(steam_id: str, cookies: str) -> None:
    async with account_service.get_session_factory()() as session:
        session.add(SteamAccount(steam_id=steam_id, cookies=cookies, is_active=True))
        await session.commit()


async def _row(steam_id: str) -> SteamAccount:
    async with account_service.get_session_factory()() as session:
        return await session.get(SteamAccount, steam_id)


@pytest.mark.asyncio
async def test_ensure_live_session_renews_expiring_cookie(db, monkeypatch):
    old = cookie_str(access_exp=time.time() - 10)
    await _seed(A, old)
    new_access = access_value(exp=time.time() + _DAY)
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            session_module.FINALIZE_URL: _finalize_ok(),
            TRANSFER_URL: _FakeResponse(set_cookie=[f"steamLoginSecure={new_access}; Path=/"]),
        },
        calls,
    )

    out = await account_service.ensure_live_session(A, old)

    assert parse_cookie_str(out)["steamLoginSecure"] == new_access
    assert (await _row(A)).cookies == out          # 续期结果落库，消费方共享
    assert session_freshness(out)["expired"] is False


@pytest.mark.asyncio
async def test_ensure_live_session_skips_network_when_token_healthy(db, monkeypatch):
    def _boom(**_kw):
        raise AssertionError("健康令牌不得发起续期请求")

    monkeypatch.setattr(session_module, "httpx", types.SimpleNamespace(AsyncClient=_boom))
    live = cookie_str(access_exp=time.time() + 3 * _DAY)
    assert await account_service.ensure_live_session(A, live) == live


@pytest.mark.asyncio
async def test_ensure_live_session_without_credential_returns_original(db, monkeypatch):
    def _boom(**_kw):
        raise AssertionError("无续期凭据时不得发起请求")

    monkeypatch.setattr(session_module, "httpx", types.SimpleNamespace(AsyncClient=_boom))
    raw = cookie_str(access_exp=time.time() - 10, with_refresh=False)
    assert await account_service.ensure_live_session(A, raw) == raw


@pytest.mark.asyncio
async def test_expired_session_freezes_rotation_without_network_backoff(db, monkeypatch):
    """登录过期是显式终态：不进网络退避/熔断计数，也不硬打 Steam。"""
    raw = cookie_str(access_exp=time.time() - 10, with_refresh=False)
    await _seed(A, raw)

    async def _boom(*_a, **_kw):
        raise AssertionError("登录已过期时不得请求 Steam")

    monkeypatch.setattr(account_service, "fetch_wallet", _boom)

    result = await account_service._sync_wallet_of(A)

    assert result["status"] == "session_expired"
    row = await _row(A)
    assert row.wallet_frozen is True
    assert int(row.wallet_fail_streak or 0) == 0
    assert int(row.wallet_backoff_level or 0) == 0
    assert row.wallet_error == account_service.SESSION_EXPIRED_MESSAGE


@pytest.mark.asyncio
async def test_renew_failure_with_credential_retries_instead_of_freezing(db, monkeypatch):
    """有续期凭据但本轮续期失败 = 可自愈：按普通退避重试，不要求用户重新登录。"""
    raw = cookie_str(access_exp=time.time() - 10)
    await _seed(A, raw)

    async def _fail(*_a, **_kw):
        raise SessionRefreshError("续期请求失败：ConnectError")

    monkeypatch.setattr(account_service, "refresh_login_cookies", _fail)

    async def _boom(*_a, **_kw):
        raise AssertionError("登录已过期时不得请求 Steam 钱包")

    monkeypatch.setattr(account_service, "fetch_wallet", _boom)

    result = await account_service._sync_wallet_of(A)

    assert result["status"] == "session_renew_failed"
    row = await _row(A)
    assert row.wallet_frozen is False                  # 不冻结：下一轮继续尝试
    assert int(row.wallet_fail_streak or 0) == 1       # 计入普通失败退避
    assert int(row.wallet_backoff_level or 0) == 1
    assert row.wallet_error == account_service.SESSION_RENEW_FAILED_MESSAGE


@pytest.mark.asyncio
async def test_status_exposes_session_state(db):
    dead = cookie_str(access_exp=time.time() - 10, with_refresh=False)
    await _seed(A, dead)

    status = await account_service.get_status()

    assert status["has_cookie"] is True       # 绑过仍为真
    assert status["session_expired"] is True  # 能不能用另说
    assert status["session_has_refresh"] is False
    assert status["session_expires_at"] is not None
    assert status["accounts"][0]["session_expired"] is True


@pytest.mark.asyncio
async def test_bind_without_refresh_credential_reports_hint(db):
    result = await account_service.save_cookies(
        cookie_str(access_exp=time.time() + _DAY, with_refresh=False)
    )
    assert result["has_refresh"] is False
    assert result["message"] == account_service.SESSION_NO_REFRESH_MESSAGE

    with_refresh = await account_service.save_cookies(cookie_str(access_exp=time.time() + _DAY))
    assert with_refresh["has_refresh"] is True
    assert with_refresh["message"] == ""