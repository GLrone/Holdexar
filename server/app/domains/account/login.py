"""account 域应用内登录：账号密码直调 Steam 认证 API。

流程（出网全部经代理策略引擎，密码只在内存驻留、不落库不写日志）：

    GET  IAuthenticationService/GetPasswordRSAPublicKey/v1
         → RSA 公钥（mod / exp / 下发时刻）
    POST IAuthenticationService/BeginAuthSessionViaCredentials/v1
         → 密码以 RSA PKCS#1 v1.5 加密提交，返回会话标识与二次验证形态
    [需要验证码] POST IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1
    轮询 POST IAuthenticationService/PollAuthSessionStatus/v1
         → 续期凭据（refresh token）
    续期凭据 → session.web_cookies_from_refresh_token 产出登录 Cookie
    → service.bind_account 落库生效（试抓钱包 + 后台全量拉取）

二次验证形态（allowed_confirmations，同一账号可能同时具备多种）：
  - 手机验证器令牌（TOTP）/ 邮箱验证码：用户在本应用输入 5 位码提交；
  - 手机 App 确认 / 邮件确认：无需输入，轮询等待用户在手机/邮箱确认。
  轮询全程在跑：用户不走输码路径、直接在手机上确认登录同样会命中。

会话为模块级单例：同一时刻只允许一个登录流程，重开前先取消。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from .service import _strategy_proxy, bind_account
from .session import web_cookies_from_refresh_token

logger = logging.getLogger(__name__)

_AUTH_BASE = "https://api.steampowered.com/IAuthenticationService"
_RSA_KEY_URL = f"{_AUTH_BASE}/GetPasswordRSAPublicKey/v1"
_BEGIN_URL = f"{_AUTH_BASE}/BeginAuthSessionViaCredentials/v1"
_GUARD_CODE_URL = f"{_AUTH_BASE}/UpdateAuthSessionWithSteamGuardCode/v1"
_POLL_URL = f"{_AUTH_BASE}/PollAuthSessionStatus/v1"

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Origin": "https://store.steampowered.com",
    "Referer": "https://store.steampowered.com/",
}
_API_TIMEOUT = 15.0
# 等验证码 / 手机确认的总时限：超时判失败，用户重开即可
_LOGIN_TIMEOUT_S = 300.0
# EAuthTokenPlatformType：WebBrowser——令牌受众为 web，finalize 后可直接用于站点请求
_PLATFORM_WEB_BROWSER = 2
# 令牌持久化：Persistent（勾选「记住我」语义，下发长命续期凭据）
_PERSISTENCE_PERSISTENT = 1
# 认证请求的站点标识（Community = 桌面/网页客户端常规登录）
_WEBSITE_ID_COMMUNITY = "Community"

# 二次验证形态（k_EAuthSessionGuardType）
_GUARD_EMAIL_CODE = 2
_GUARD_DEVICE_CODE = 3
_GUARD_DEVICE_CONFIRM = 4
_GUARD_EMAIL_CONFIRM = 5
# 码形态提交优先级：手机令牌在 App 内随手可查，邮箱码要翻邮件，靠后
_CODE_GUARD_ORDER = (_GUARD_DEVICE_CODE, _GUARD_EMAIL_CODE)

STATE_IDLE = "idle"
STATE_SIGNING = "signing"
STATE_AWAITING_CODE = "awaiting_code"
STATE_AWAITING_CONFIRM = "awaiting_confirmation"
STATE_FINALIZING = "finalizing"
STATE_DONE = "done"
STATE_FAILED = "failed"

_NETWORK_MESSAGE = "连不上 Steam 登录服务，请检查网络或代理设置后重试"


class LoginError(RuntimeError):
    """登录协议层失败（Steam 拒绝、响应结构异常、网络不通）。"""


def encrypt_password(password: str, *, mod_hex: str, exp_hex: str) -> str:
    """密码按 Steam 下发的 RSA 公钥加密（PKCS#1 v1.5），返回 hex 密文。"""
    public_key = RSAPublicNumbers(int(exp_hex, 16), int(mod_hex, 16)).public_key()
    return public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15()).hex()


# ── 登录会话单例 ──────────────────────────────────────────────────────────

_lock = asyncio.Lock()
_session: dict | None = None
_task: asyncio.Task | None = None
_cancelled = asyncio.Event()


def _new_session(state: str, message: str = "", *, error: str = "") -> dict:
    return {
        "state": state,
        "message": message,
        "error": error,
        "code_hint": "",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _update_state(state: str, message: str = "", *, error: str = "", code_hint: str | None = None) -> None:
    if _session is None:
        return
    _session["state"] = state
    _session["message"] = message
    _session["error"] = error
    if code_hint is not None:
        _session["code_hint"] = code_hint
    _session["updated_at"] = datetime.now().isoformat(timespec="seconds")


def login_status() -> dict:
    """当前登录会话状态快照（不含任何凭据性字段）。"""
    if _session is None:
        return _new_session(STATE_IDLE)
    return {k: _session[k] for k in ("state", "message", "error", "code_hint", "started_at", "updated_at")}


# ── Steam 认证调用 ────────────────────────────────────────────────────────

def _api_error_message(code: int, desc: str) -> str:
    if code == 5:  # 账号或密码不正确
        return "账号或密码不正确"
    if code in (65, 88):  # 邮箱验证码错 / 手机令牌码错
        return "验证码不正确，请重试"
    return f"Steam 拒绝了登录请求：{desc or f'错误码 {code}'}"


def _parse_webapi_body(resp: httpx.Response) -> dict:
    """WebAPI 响应 → response 对象；结果码以 x-eresult 头承载（HTTP 常为 200）。

    认证接口是 protobuf 形态的 WebAPI：请求用 input_json 包 JSON，响应头
    x-eresult 携带 EResult（1 = OK），凭据被拒时响应体为空对象、全靠头判定。
    """
    if resp.status_code >= 400:
        raise LoginError(f"Steam 登录服务不可用（HTTP {resp.status_code}）")
    eresult = resp.headers.get("x-eresult")
    if eresult is not None and eresult.isdigit() and int(eresult) != 1:
        desc = resp.headers.get("x-error_message") or ""
        raise LoginError(_api_error_message(int(eresult), desc))
    try:
        body = resp.json()
    except ValueError as exc:
        raise LoginError("Steam 登录服务响应异常，请稍后重试") from exc
    if not isinstance(body, dict):
        raise LoginError("Steam 登录服务响应异常，请稍后重试")
    response = body.get("response") or {}
    if not isinstance(response, dict):
        response = {}
    err = response.get("error") or {}
    if isinstance(err, dict) and err.get("error_code"):
        raise LoginError(
            _api_error_message(int(err.get("error_code") or 0), str(err.get("error_desc") or ""))
        )
    return response


async def _begin_session(account: str, password: str, proxy_url: str | None) -> dict:
    """提交账号密码，返回会话信息（凭据性字段只留在进程内，不外泄）。"""
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(_RSA_KEY_URL, params={"account_name": account})
            key = _parse_webapi_body(resp)
            mod, exp, ts = key.get("publickey_mod"), key.get("publickey_exp"), key.get("timestamp")
            if not (mod and exp and ts):
                raise LoginError("Steam 登录服务响应异常，请稍后重试")
            resp = await client.post(
                _BEGIN_URL,
                data={
                    "input_json": json.dumps(
                        {
                            "account_name": account,
                            "encrypted_password": encrypt_password(
                                password, mod_hex=str(mod), exp_hex=str(exp)
                            ),
                            "encryption_timestamp": int(ts),
                            "remember_login": True,
                            "persistence": _PERSISTENCE_PERSISTENT,
                            "website_id": _WEBSITE_ID_COMMUNITY,
                            "device_details": {
                                "device_friendly_name": "Holdexar",
                                "platform_type": _PLATFORM_WEB_BROWSER,
                            },
                        }
                    )
                },
            )
    except httpx.HTTPError as exc:
        raise LoginError(_NETWORK_MESSAGE) from exc
    body = _parse_webapi_body(resp)

    steam_id = str(body.get("steamid") or "")
    client_id = str(body.get("client_id") or "")
    request_id = str(body.get("request_id") or "")
    if not (steam_id and client_id and request_id):
        raise LoginError("Steam 登录服务响应异常，请稍后重试")
    guards = [
        int(g.get("confirmation_type") or 0)
        for g in (body.get("allowed_confirmations") or [])
        if isinstance(g, dict)
    ]
    code_guard = next((g for g in _CODE_GUARD_ORDER if g in guards), 0)
    return {
        "steam_id": steam_id,
        "client_id": client_id,
        "request_id": request_id,
        "interval": min(max(float(body.get("interval") or 5.0), 1.0), 15.0),
        "guards": guards,
        "code_guard": code_guard,
    }


async def _submit_guard_code(session: dict, code: str, guard: int, proxy_url: str | None) -> None:
    """提交 Steam Guard 验证码；错误码 65/88（邮箱码错 / 令牌码错）映射为可重试提示。"""
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.post(
                _GUARD_CODE_URL,
                data={
                    "input_json": json.dumps(
                        {
                            "client_id": session["client_id"],
                            "steamid": session["steam_id"],
                            "code": code.strip(),
                            "code_type": guard,
                        }
                    )
                },
            )
    except httpx.HTTPError as exc:
        raise LoginError(_NETWORK_MESSAGE) from exc
    body = _parse_webapi_body(resp)
    if not body.get("correct_code", True):
        raise LoginError("验证码不正确，请重试")


async def _poll_once(session: dict, proxy_url: str | None) -> str:
    """轮询一次认证状态，返回 refresh token（未确认时返回空串）。"""
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.post(
                _POLL_URL,
                data={
                    "input_json": json.dumps(
                        {"client_id": session["client_id"], "request_id": session["request_id"]}
                    )
                },
            )
    except httpx.HTTPError:
        return ""  # 单次网络抖动不判失败，交给超时兜底
    if resp.status_code >= 500:
        return ""
    body = _parse_webapi_body(resp)
    return str(body.get("refresh_token") or "")


# ── 会话动作 ─────────────────────────────────────────────────────────────

async def start_login(account_name: str, password: str) -> dict:
    """发起账号密码登录。返回 {"ok", "state"|"busy"?, "error"?}。"""
    global _session, _task, _cancelled
    async with _lock:
        if _task is not None and not _task.done():
            return {"ok": False, "busy": True, "state": login_status(), "error": "已有登录进行中"}
        account = (account_name or "").strip()
        if not account or not password:
            return {"ok": False, "state": login_status(), "error": "请填写账号与密码"}
        _cancelled = asyncio.Event()
        _session = _new_session(STATE_SIGNING, "正在登录…")
        try:
            proxy_url = await _strategy_proxy()
            info = await _begin_session(account, password, proxy_url)
        except LoginError as exc:
            logger.info("[account] 应用内登录失败（账号=%s…）：%s", account[:3], exc)
            _update_state(STATE_FAILED, error=str(exc))
            return {"ok": False, "state": login_status()}
        # 会话凭据性字段并入会话容器（login_status 快照白名单不外泄它们）
        _session.update(
            {k: info[k] for k in ("steam_id", "client_id", "request_id", "interval", "code_guard")}
        )
        _update_state(
            STATE_AWAITING_CODE if info["code_guard"] else STATE_AWAITING_CONFIRM,
            "请在手机 Steam App 上确认本次登录" if not info["code_guard"] else "",
            code_hint=("totp" if info["code_guard"] == _GUARD_DEVICE_CODE else "email")
            if info["code_guard"]
            else "",
        )
        _task = asyncio.get_running_loop().create_task(_poll_until_authed(info, proxy_url))
        return {"ok": True, "state": login_status()}


async def _poll_until_authed(info: dict, proxy_url: str | None) -> None:
    """轮询至 Steam 确认；命中续期凭据即换 Cookie 并走完整绑定链。"""
    global _session
    deadline = time.monotonic() + _LOGIN_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if _cancelled.is_set():
                return
            await asyncio.sleep(info["interval"])
            if _cancelled.is_set():
                return
            refresh = await _poll_once(info, proxy_url)
            if not refresh:
                continue
            _update_state(STATE_FINALIZING, "正在建立登录态…")
            cookies = await web_cookies_from_refresh_token(
                refresh, info["steam_id"], proxy_url=proxy_url
            )
            await bind_account(cookies)
            logger.info("[account] 应用内登录成功并已绑定 %s", info["steam_id"])
            _update_state(STATE_DONE, "登录成功")
            return
        if not _cancelled.is_set():
            _update_state(STATE_FAILED, error="登录等待超时，请重试")
    except LoginError as exc:
        if not _cancelled.is_set():
            _update_state(STATE_FAILED, error=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 落库链异常归为登录失败，凭据链路细节留日志
        logger.exception("[account] 应用内登录收尾异常")
        if not _cancelled.is_set():
            _update_state(STATE_FAILED, error="登录收尾失败，请重试")


async def submit_guard_code(code: str) -> dict:
    """提交 Steam Guard 验证码（手机令牌或邮箱码，按会话首选形态）。"""
    session = _session
    if session is None or session["state"] != STATE_AWAITING_CODE:
        return {"ok": False, "state": login_status(), "error": "当前没有等待验证码的登录"}
    if not (code or "").strip():
        return {"ok": False, "state": login_status(), "error": "请输入验证码"}
    try:
        proxy_url = await _strategy_proxy()
        await _submit_guard_code(session, code, int(session["code_guard"]), proxy_url)
    except LoginError as exc:
        return {"ok": False, "state": login_status(), "error": str(exc)}
    _update_state(
        STATE_AWAITING_CONFIRM, "验证码已接受，正在完成登录…"
    )
    return {"ok": True, "state": login_status()}


def cancel_login() -> dict:
    """取消当前登录会话（等待中的轮询任务随后退出）。"""
    _cancelled.set()
    if _session is not None and _session["state"] not in (STATE_DONE, STATE_FAILED):
        _update_state(STATE_IDLE)
    return {"ok": True, "state": login_status()}
