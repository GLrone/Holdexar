"""应用内账号密码登录测试：protobuf 请求构造 / RSA 密码加密 / 认证会话状态机 /
验证码提交 / 轮询命中后的绑定收尾 / 失败与超时路径 / 单例互斥 / 密码不落日志。
网络层全部打桩，绑定链替换为记录器，库不参与。"""
import asyncio
import base64
import struct
import sys
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
CLIENT_ID = 123456789012345
RSA_KEY_URL = login_module._RSA_KEY_URL
BEGIN_URL = login_module._BEGIN_URL
GUARD_CODE_URL = login_module._GUARD_CODE_URL
POLL_URL = login_module._POLL_URL
CHECK_DEVICE_URL = f"{login_module._CHECK_DEVICE_URL}/{SID}"


# ── 打桩设施 ─────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, *, status_code=200, content=b"", json_body=None, eresult="1"):
        self.status_code = status_code
        self.content = content
        self._json = json_body
        self.headers = {
            "content-type": "application/json" if json_body is not None else "application/octet-stream",
            "x-eresult": str(eresult),
        }

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """按 URL 回放预置响应（可给列表实现逐次弹出的重放），并记录每次请求。"""

    def __init__(self, responses: dict, calls: list):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method: str, url: str, **kwargs):
        self._calls.append((method, url, kwargs))
        resp = self._responses.get(url)
        if resp is None:
            raise AssertionError(f"未预置的 URL：{url}")
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_httpx(monkeypatch, responses: dict, calls: list) -> None:
    fake = types.SimpleNamespace(
        AsyncClient=lambda **_kw: _FakeClient(responses, calls),
        HTTPError=login_module.httpx.HTTPError,
        ConnectError=login_module.httpx.ConnectError,
        TransportError=login_module.httpx.TransportError,
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


def _req_pb(kwargs: dict) -> list:
    """打桩请求里的 input_protobuf_encoded 字段解码为 protobuf 字段列表。"""
    raw = base64.b64decode(kwargs["files"]["input_protobuf_encoded"][1])
    return login_module._pb_fields(raw)


def _req_form(kwargs: dict) -> dict:
    return {k: v[1] for k, v in (kwargs.get("files") or {}).items()}


def _begin_ok(guards: list[int], *, interval: float = 0.01) -> _FakeResponse:
    content = (
        login_module._pb_varint_field(1, CLIENT_ID)
        + login_module._pb_bytes_field(2, b"\x11" * 16)
        + login_module._pb_fixed32_field(3, struct.pack("<f", interval))
        + b"".join(
            login_module._pb_bytes_field(4, login_module._pb_varint_field(1, g)) for g in guards
        )
        + login_module._pb_varint_field(5, int(SID))
    )
    return _FakeResponse(content=content)


_KEY = None


def _rsa_key_response() -> _FakeResponse:
    """真实小密钥对的公钥响应（mod/exp 数值必须满足 RSA 参数约束）。"""
    global _KEY
    from cryptography.hazmat.primitives.asymmetric import rsa

    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pub = _KEY.public_key().public_numbers()
    content = (
        login_module._pb_bytes_field(1, format(pub.n, "x").encode("ascii"))
        + login_module._pb_bytes_field(2, format(pub.e, "x").encode("ascii"))
        + login_module._pb_varint_field(3, 1700000000000)
    )
    return _FakeResponse(content=content)


def _rsa_private_key():
    _rsa_key_response()
    return _KEY


def _begin_rejected(code: int = 5) -> _FakeResponse:
    return _FakeResponse(content=b"", eresult=code)


def _check_device_response(device_ok: bool) -> _FakeResponse:
    return _FakeResponse(json_body={"success": device_ok, "result": 1 if device_ok else 8})


async def _wait_task_done(timeout: float = 5.0) -> None:
    task = login_module._task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


# ── RSA 加密 ─────────────────────────────────────────────

def test_encrypt_password_roundtrip_with_private_key():
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    key = _rsa_private_key()
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
        CHECK_DEVICE_URL: _check_device_response(False),
    }
    _patch_httpx(monkeypatch, responses, calls)

    out = await login_module.start_login("user1", "pw123")

    assert out["ok"] is True
    assert out["state"]["state"] == STATE_AWAITING_CODE
    assert out["state"]["code_hint"] == "email"

    get_calls = [c for c in calls if c[0] == "GET" and c[1] == RSA_KEY_URL]
    assert get_calls and get_calls[0][2]["params"]["origin"] == login_module._STORE_ORIGIN
    key_fields = login_module._pb_fields(
        base64.b64decode(get_calls[0][2]["params"]["input_protobuf_encoded"])
    )
    assert login_module._pb_pick(key_fields, 1, 2) == b"user1"

    begin_calls = [c for c in calls if c[0] == "POST" and c[1] == BEGIN_URL]
    form = _req_pb(begin_calls[0][2])
    assert login_module._pb_pick(form, 2, 2) == b"user1"
    encrypted = base64.b64decode(login_module._pb_pick(form, 3, 2))
    assert b"pw123" not in encrypted
    pub = _rsa_private_key().public_key().public_numbers()
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    assert _rsa_private_key().decrypt(
        encrypted, asym_padding.PKCS1v15()
    ).decode("utf-8") == "pw123"
    assert login_module._pb_pick(form, 4, 0) == 1700000000000
    assert login_module._pb_pick(form, 7, 0) == login_module._PERSISTENCE_PERSISTENT
    assert login_module._pb_pick(form, 8, 2) == b"Store"
    device = login_module._pb_fields(login_module._pb_pick(form, 9, 2))
    assert login_module._pb_pick(device, 2, 0) == login_module._PLATFORM_WEB_BROWSER
    assert login_module._pb_pick(form, 11, 0) == login_module._LANGUAGE_SCHINESE

    device_calls = [c for c in calls if c[0] == "POST" and c[1] == CHECK_DEVICE_URL]
    assert device_calls
    assert _req_form(device_calls[0][2])["steamid"] == SID


@pytest.mark.asyncio
async def test_begin_without_code_goes_to_confirmation_state(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_DEVICE_CONFIRM]),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    assert out["state"]["code_hint"] == ""


@pytest.mark.asyncio
async def test_begin_device_ok_skips_code_entry(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE]),
            CHECK_DEVICE_URL: _check_device_response(True),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    assert out["state"]["code_hint"] == ""


@pytest.mark.asyncio
async def test_begin_totp_account_defaults_to_confirmation(monkeypatch):
    """手机验证器账号（令牌+App确认双形态）默认等用户在手机上确认，输码为备选。"""
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok(
                [login_module._GUARD_DEVICE_CODE, login_module._GUARD_DEVICE_CONFIRM]
            ),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["ok"] is True
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    assert out["state"]["code_hint"] == ""
    assert out["state"]["code_available"] is True


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
async def test_connect_error_is_retried_then_succeeds(monkeypatch):
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: [
                login_module.httpx.ConnectError("reset"),
                login_module.httpx.ConnectError("reset"),
                _rsa_key_response(),
            ],
            BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE]),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["ok"] is True
    get_calls = [c for c in calls if c[0] == "GET" and c[1] == RSA_KEY_URL]
    assert len(get_calls) == 3


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
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_EMAIL_CODE]),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
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
            CHECK_DEVICE_URL: _check_device_response(False),
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
    responses = {GUARD_CODE_URL: _FakeResponse(content=b"", eresult=88)}
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
    _patch_httpx(
        monkeypatch, {GUARD_CODE_URL: _FakeResponse(content=b"")}, calls
    )

    out = await login_module.submit_guard_code(" 12345 ")

    assert out["ok"] is True
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    post_calls = [c for c in calls if c[0] == "POST"]
    form = _req_pb(post_calls[0][2])
    assert login_module._pb_pick(form, 1, 0) == CLIENT_ID
    assert login_module._pb_pick(form, 2, 1) == struct.pack("<Q", int(SID))
    assert login_module._pb_pick(form, 3, 2) == b"12345"  # 输入去空白
    assert login_module._pb_pick(form, 4, 0) == login_module._GUARD_EMAIL_CODE


@pytest.mark.asyncio
async def test_guard_code_response_rotation_is_followed(monkeypatch):
    """提交后 Steam 轮换会话标识（响应携带新 client_id/request_id）→ 轮询跟随新值拿到令牌。"""
    calls: list = []
    bind = login_module.bind_account
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()

    new_client_id = 999888777666555
    new_request_id = b"\x22" * 16
    submit_resp = _FakeResponse(
        content=(
            login_module._pb_varint_field(5, new_client_id)
            + login_module._pb_bytes_field(6, new_request_id)
        )
    )
    _patch_httpx(monkeypatch, {GUARD_CODE_URL: submit_resp}, calls)
    out = await login_module.submit_guard_code("12345")
    assert out["ok"] is True
    assert login_module._session["client_id"] == new_client_id
    assert login_module._session["request_id"] == new_request_id

    poll_resp = _FakeResponse(content=login_module._pb_bytes_field(3, b"rt-jwt"))
    _patch_httpx(monkeypatch, {POLL_URL: poll_resp}, calls)

    async def _fake_cookies(refresh, steam_id, *, proxy_url=None, timeout=None, inherited=None):
        assert refresh == "rt-jwt"
        return "sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"

    monkeypatch.setattr(login_module, "web_cookies_from_refresh_token", _fake_cookies)
    await _wait_task_done(timeout=10)

    poll_calls = [c for c in calls if c[0] == "POST" and c[1] == POLL_URL]
    assert poll_calls, "轮询未发生"
    form = _req_pb(poll_calls[-1][2])
    assert login_module._pb_pick(form, 1, 0) == new_client_id  # 轮询携带轮换后的 client_id
    assert login_module._pb_pick(form, 2, 2) == new_request_id
    assert login_module.login_status()["state"] == STATE_DONE
    assert bind.bound == ["sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"]


@pytest.mark.asyncio
async def test_guard_code_without_session_rejected(monkeypatch):
    out = await login_module.submit_guard_code("12345")
    assert out["ok"] is False
    assert out.get("no_session") is True


@pytest.mark.asyncio
async def test_guard_code_accepted_from_confirmation_state(monkeypatch):
    """确认形态账号切换到输码路径：等待确认态下提交令牌码同样被接受。"""
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok(
                [login_module._GUARD_DEVICE_CODE, login_module._GUARD_DEVICE_CONFIRM]
            ),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    calls.clear()
    _patch_httpx(
        monkeypatch, {GUARD_CODE_URL: _FakeResponse(content=b"")}, calls
    )

    result = await login_module.submit_guard_code("12345")

    assert result["ok"] is True
    assert result["state"]["state"] == STATE_AWAITING_CONFIRM
    form = _req_pb([c for c in calls if c[0] == "POST"][0][2])
    assert login_module._pb_pick(form, 4, 0) == login_module._GUARD_DEVICE_CODE


@pytest.mark.asyncio
async def test_guard_code_rejected_without_code_form(monkeypatch):
    """仅确认形态（无令牌/邮箱码）的会话不接受验证码提交。"""
    calls: list = []
    _patch_httpx(
        monkeypatch,
        {
            RSA_KEY_URL: _rsa_key_response(),
            BEGIN_URL: _begin_ok([login_module._GUARD_DEVICE_CONFIRM]),
            CHECK_DEVICE_URL: _check_device_response(False),
        },
        calls,
    )
    out = await login_module.start_login("user1", "pw")
    assert out["state"]["state"] == STATE_AWAITING_CONFIRM
    assert out["state"]["code_available"] is False

    result = await login_module.submit_guard_code("12345")

    assert result["ok"] is False
    assert result.get("no_session") is True


# ── 轮询命中 → 绑定收尾 ───────────────────────────────────


@pytest.mark.asyncio
async def test_poll_hit_finalizes_and_binds(monkeypatch):
    calls: list = []
    bind = login_module.bind_account
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    poll_resp = _FakeResponse(content=login_module._pb_bytes_field(3, b"rt-jwt"))
    _patch_httpx(
        monkeypatch,
        {GUARD_CODE_URL: _FakeResponse(content=b""), POLL_URL: poll_resp},
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
    poll_calls = [c for c in calls if c[0] == "POST" and c[1] == POLL_URL]
    form = _req_pb(poll_calls[0][2])
    assert login_module._pb_pick(form, 1, 0) == CLIENT_ID
    assert login_module._pb_pick(form, 2, 2) == b"\x11" * 16


@pytest.mark.asyncio
async def test_poll_rejection_fails_with_user_message(monkeypatch):
    """手机上拒绝 → 轮询侧非 OK 结果码稳态 → 用户语言失败，不等满超时。"""
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    _patch_httpx(
        monkeypatch,
        {POLL_URL: _FakeResponse(content=b"", eresult=9)},
        calls,
    )

    await _wait_task_done(timeout=10)

    state = login_module.login_status()
    assert state["state"] == STATE_FAILED
    assert "拒绝" in state["error"]
    assert "错误码" not in state["error"]  # 内部枚举不得直出


@pytest.mark.asyncio
async def test_poll_timeout_fails(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    monkeypatch.setattr(login_module, "_LOGIN_TIMEOUT_S", 0.05)
    calls.clear()
    _patch_httpx(
        monkeypatch,
        {POLL_URL: _FakeResponse(content=b"")},
        calls,
    )

    await _wait_task_done(timeout=10)

    state = login_module.login_status()
    assert state["state"] == STATE_FAILED
    assert "超时" in state["error"]


@pytest.mark.asyncio
async def test_finalize_transient_failure_is_retried(monkeypatch):
    """令牌已到手：换 Cookie 遇网络瞬断不作废登录，重试后完成绑定。"""
    from app.domains.account.session import SessionRefreshError

    calls: list = []
    bind = login_module.bind_account
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    poll_resp = _FakeResponse(content=login_module._pb_bytes_field(3, b"rt-jwt"))
    _patch_httpx(
        monkeypatch,
        {GUARD_CODE_URL: _FakeResponse(content=b""), POLL_URL: poll_resp},
        calls,
    )
    await login_module.submit_guard_code("12345")

    attempts: list[int] = []

    async def _flaky(refresh, steam_id, **_kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise SessionRefreshError("Cookie 下发失败（ReadTimeout: ）")
        return "sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"

    monkeypatch.setattr(login_module, "web_cookies_from_refresh_token", _flaky)
    await _wait_task_done(timeout=20)

    assert len(attempts) == 3
    assert login_module.login_status()["state"] == STATE_DONE
    assert bind.bound == ["sessionid=x; steamLoginSecure=a; steamRefresh_steam=b"]


@pytest.mark.asyncio
async def test_finalize_failure_marks_failed(monkeypatch):
    calls: list = []
    await _enter_awaiting_code(monkeypatch, calls)
    calls.clear()
    poll_resp = _FakeResponse(content=login_module._pb_bytes_field(3, b"rt-jwt"))
    _patch_httpx(
        monkeypatch,
        {GUARD_CODE_URL: _FakeResponse(content=b""), POLL_URL: poll_resp},
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
