"""应用内账号密码登录测试：RSA 密码加密 / 认证会话状态机 / 验证码提交 /
轮询命中后的绑定收尾 / 失败与超时路径 / 单例互斥 / 密码不落日志。
网络层全部打桩，绑定链替换为记录器，库不参与。"""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.account import login as login_module
from app.domains.account.login import (
    STATE_AWAITING_CODE,
    STATE_AWAITING_CONFIRM,
    STATE_DONE,
    STATE_FAILED,
    STATE_IDLE,
    LoginError,
    encrypt_password,
)

SID = "76561198000000001"
RSA_KEY_URL = login_module._RSA_KEY_URL
BEGIN_URL = login_module._BEGIN_URL
GUARD_CODE_URL = login_module._GUARD_CODE_URL
POLL_URL = login_module._POLL_URL


# ── 打桩设施 ─────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, eresult="1"):
        self.status_code = status_code
        self._body = json_body if json_body is not None else {}
        self.headers = {"content-type": "application/json", "x-eresult": str(eresult)}

    def json(self):
        return self._body


class _FakeClient:
    """按 URL 回放预置响应（GET/POST），并记录每次请求。"""

    def __init__(self, responses: dict, calls: list):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        self._calls.append(("GET", url, kwargs))
        resp = self._responses.get(url)
        if resp is None:
            raise AssertionError(f"未预置的 URL：{url}")
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def post(self, url: str, **kwargs):
        self._calls.append(("POST", url, kwargs))
        resp = self._responses.get(url)
        if resp is None:
            raise AssertionError(f"未预置的 URL：{url}")
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_httpx(monkeypatch, responses: dict, calls: list) -> None:
    fake = types.SimpleNamespace(
        AsyncClient=lambda **_kw: _FakeClient(responses, calls),
        HTTPError=login_module.httpx.HTTPError,
        Response=login_module.httpx.Response,
    )
    monkeypatch.setattr(login_module, "httpx", fake)


@pytest.fixture(autouse=True)
def _reset_session():
    login_module._session = None
    login_module._task = None
    login_module._cancelled = asyncio.Event()
    yield
    if login_module._task is not None and not login_module._task.done():
        login_module._cancelled.set()
    login_module._session = None
    login_module._task = None


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(login_module, "_strategy_proxy", _none)


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    """绑定链替换为记录器：登录状态机测试不落库。"""
    bound: list[str] = []

    async def _record(cookies_raw: str) -> dict:
        bound.append(cookies_raw)
        return {"result": {"ok": True, "steam_id": SID, "message": ""}, "sync": {"ok": True}}

    fake_bind = _record
    fake_bind.bound = bound  # type: ignore[attr-defined]
    monkeypatch.setattr(login_module, "bind_account", fake_bind)
    return fake_bind


def _begin_ok(guards: list[int], *, interval: float = 0.01) -> _FakeResponse:
    return _FakeResponse(
        json_body={
            "response": {
                "steamid": SID,
                "client_id": "cid-1",
                "request_id": "rid-1",
                "interval": interval,
                "allowed_confirmations": [{"confirmation_type": g} for g in guards],
            }
        }
    )


_KEY = None


def _rsa_key_response() -> _FakeResponse:
    """真实小密钥对的公钥响应（mod/exp 数值必须满足 RSA 参数约束）。"""
    global _KEY
    from cryptography.hazmat.primitives.asymmetric import rsa

    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pub = _KEY.public_key().public_numbers()
    return _FakeResponse(
        json_body={
            "response": {
                "publickey_mod": format(pub.n, "x"),
                "publickey_exp": format(pub.e, "x"),
                "timestamp": "1700000000000",
            }
        }
    )


def _begin_rejected(code: int = 5) -> _FakeResponse:
    return _FakeResponse(json_body={"response": {}}, eresult=code)


async def _wait_task_done(timeout: float = 5.0) -> None:
    task = login_module._task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


# ── RSA 加密 ─────────────────────────────────────────────

def test_encrypt_password_roundtrip_with_private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pub = key.public_key().public_numbers()
    ciphertext = bytes.fromhex(
        encrypt_password("s3cret-密码", mod_hex=format(pub.n, "x"), exp_hex=format(pub.e, "x"))
    )
    plain = key.decrypt(ciphertext, asym_padding.PKCS1v15()).decode("utf-8")
    assert plain == "s3cret-密码"


# ── 发起登录（begin 阶段） ────────────────────────────────


@pytest.mark.asyncio
async def test_begin_builds_encrypted_form_and_enters_awaiting_code(monkeypatch):
    calls: list = []
    responses = {
        RSA_KEY_URL: _rsa_key_response(),
        BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE]),
    }
    _patch_httpx(monkeypatch, responses, calls)

    out = await login_module.start_login("user1", "pw123")

    assert out["ok"] is True
    assert out["state"]["state"] == STATE_AWAITING_CODE
    assert out["state"]["code_hint"] == "email"
    get_calls = [c for c in calls if c[0] == "GET" and c[1] == RSA_KEY_URL]
    assert get_calls and get_calls[0][2]["params"] == {"account_name": "user1"}
    begin_calls = [c for c in calls if c[0] == "POST" and c[1] == BEGIN_URL]
    form = json.loads(begin_calls[0][2]["data"]["input_json"])
    assert form["account_name"] == "user1"
    assert "pw123" not in form["encrypted_password"]
    assert form["encryption_timestamp"] == 1700000000000
    assert form["persistence"] == login_module._PERSISTENCE_PERSISTENT
    assert form["device_details"]["platform_type"] == login_module._PLATFORM_WEB_BROWSER


@pytest.mark.asyncio
async def test_begin_without_code_goes_to_confirmation_state(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_DEVICE_CONFIRM]),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    assert out["state"]["code_hint"] == ""


@pytest.mark.asyncio
async def test_wrong_password_fails_immediately(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_rejected(5),
        },
        calls,
    )
    out = await login_module.start_login("user1", "badpw")
    assert out["ok"] is False
    assert out["state"]["state"] == STATE_FAILED
    assert "账号或密码" in out["state"]["error"]


@pytest.mark.asyncio
async def test_network_failure_reports_retryable_message(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {RSA_KEY_URL: login_module.httpx.HTTPError("boom")},
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_FAILED
    assert "连不上" in out["state"]["error"]


@pytest.mark.asyncio
async def test_empty_fields_rejected_without_network(monkeypatch):
    calls: list = []
    _patch_httpx(monkeypatch, {}, calls)
    out = await login_module.start_login("  ", "")
    assert out["ok"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_second_start_is_rejected_while_running(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {RSA_KEY_URL: _rsa_key_response(), BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE])},
        calls,
    )

    async def _hanging_poll(info, proxy):
        await asyncio.sleep(30)

    monkeypatch.setattr(login_module, "_poll_until_authed", _hanging_poll)
    first = await login_module.start_login("user1", "pw")
    assert first["ok"] is True
    second = await login_module.start_login("user2", "pw2")
    assert second.get("busy") is True
    login_module.cancel_login()


# ── 验证码提交 ────────────────────────────────────────────


async def _enter_awaiting_code(monkeypatch, calls: list) -> None:
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE]),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CODE


@pytest.mark.asyncio
async def test_guard_code_wrong_keeps_awaiting_state(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    responses = {GUARD_CODE_URL: _FakeResponse(json_body={"response": {}}, eresult=88)}
    _patch_httpx(monkeypatch, responses, calls)

    out = await login_module.submit_guard_code("00000")

    assert out["ok"] is False
    assert "验证码不正确" in out["error"]
    assert out["state"]["state"] == STATE_AWAITING_CODE


@pytest.mark.asyncio
async def test_guard_code_accepted_moves_to_confirmation(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    _patch_httpx(monkeypatch, {GUARD_CODE_URL: _FakeResponse(json_body={"response": {}})}, calls)

    out = await login_module.submit_guard_code(" 12345 ")

    assert out["ok"] is True
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    form = json.loads([c for c in calls if c[0] == "POST"][0][2]["data"]["input_json"])
    assert form["code"] == "12345"  # 输入去空白
    assert form["code_type"] == login_module._GUARD_EMAIL_CODE


@pytest.mark.asyncio
async def test_guard_code_without_session_rejected(monkeypatch):
    out = await login_module.submit_guard_code("12345")
    assert out["ok"] is False


# ── 轮询命中 → 绑定收尾 ───────────────────────────────────


@pytest.mark.asyncio
async def test_poll_hit_finalizes_and_binds(monkeypatch):
    calls: list = []
    bind = login_module.bind_account
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    _patch_httpx(
        monkeypatch,
        {GUARD_CODE_URL: _FakeResponse(json_body={"response": {}}), POLL_URL: _FakeResponse(json_body={"response": {"refresh_token": "rt-jwt"}})},
        calls,
    )
    await login_module.submit_guard_code("12345")

    async def _fake_cookies(refresh, steam_id, *, proxy_url=None, timeout=None, inherited=None):
        assert refresh == "rt-jwt"
        assert steam_id == SID
        return "sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"

    monkeypatch.setattr(login_module, "web_cookies_from_refresh_token", _fake_cookies)
    await _wait_task_done()

    assert login_module.login_status()["state"] == STATE_DONE
    assert bind.bound == ["sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"]


@pytest.mark.asyncio
async def test_poll_timeout_fails(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    monkeypatch.setattr(login_module, "_LOGIN_TIMEOUT_S", 0.05)
    calls.clear()
    _patch_httpx(monkeypatch, {POLL_URL: _FakeResponse(json_body={"response": {}})}, calls)

    await _wait_task_done(timeout=10)

    state = login_module.login_status()
    assert state["state"] == STATE_FAILED
    assert "超时" in state["error"]


@pytest.mark.asyncio
async def test_finalize_failure_marks_failed(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    _patch_httpx(
        monkeypatch,
        {GUARD_CODE_URL: _FakeResponse(json_body={"response": {}}), POLL_URL: _FakeResponse(json_body={"response": {"refresh_token": "rt-jwt"}})},
        calls,
    )
    await login_module.submit_guard_code("12345")

    async def _boom(refresh, steam_id, **_kw):
        raise LoginError("Cookie 下发失败")

    monkeypatch.setattr(login_module, "web_cookies_from_refresh_token", _boom)
    await _wait_task_done()

    state = login_module.login_status()
    assert state["state"] == STATE_FAILED
    assert "Cookie 下发失败" in state["error"]


# ── 取消与状态查询 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_returns_to_idle(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    out = login_module.cancel_login()
    assert out["state"]["state"] == STATE_IDLE
    assert login_module.login_status()["state"] == STATE_IDLE


@pytest.mark.asyncio
async def test_status_without_session_is_idle():
    assert login_module.login_status()["state"] == STATE_IDLE


# ── 密码不落日志 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_never_logged(monkeypatch, caplog):
    import logging

    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_rejected(5),
        },
        calls,
    )
    with caplog.at_level(logging.DEBUG):
        await login_module.start_login("user1", "topsecret-pw")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "topsecret-pw" not in joined
