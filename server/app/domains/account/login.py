"""account 域应用内登录：账号密码直调 Steam 认证 API，协议与商店登录页一致。
密码只在内存驻留、不落库不写日志。流程：GetPasswordRSAPublicKey →
BeginAuthSessionViaCredentials（RSA PKCS#1 v1.5 加密进 protobuf，
website_id=Store）→ jwt/checkdevice（记忆名单免验证码）→ [需要时]
UpdateAuthSessionWithSteamGuardCode → PollAuthSessionStatus 轮询拿 refresh
token → 换登录 Cookie → bind_account 落库。二次验证多形态并存：App/邮件
确认默认轮询等待；TOTP/邮箱码由用户在状态卡切换后输 5 位码；轮询全程在跑。
请求与响应均为 protobuf（结果码在 x-eresult 头，HTTP 常为 200），编解码用
本文件极简 varint 读写器。会话为模块级单例，重开前先取消。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from datetime import datetime

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from app.core.logging import log_event
from .service import _strategy_proxy, bind_account
from .session import SessionRefreshError, web_cookies_from_refresh_token

logger = logging.getLogger(__name__)

_STORE_ORIGIN = "https://store.steampowered.com"
_AUTH_BASE = "https://api.steampowered.com/IAuthenticationService"
_RSA_KEY_URL = f"{_AUTH_BASE}/GetPasswordRSAPublicKey/v1"
_BEGIN_URL = f"{_AUTH_BASE}/BeginAuthSessionViaCredentials/v1"
_GUARD_CODE_URL = f"{_AUTH_BASE}/UpdateAuthSessionWithSteamGuardCode/v1"
_POLL_URL = f"{_AUTH_BASE}/PollAuthSessionStatus/v1"
_CHECK_DEVICE_URL = "https://login.steampowered.com/jwt/checkdevice"

# 登录页浏览器同款请求头与设备名（device_friendly_name 即 UA 串）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
_API_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Origin": _STORE_ORIGIN,
    "Referer": f"{_STORE_ORIGIN}/login/",
}
_API_TIMEOUT = 15.0
# 连接建立失败（代理节点瞬断、TLS 握手被重置）时安全重发：请求未达对端
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.8
# 等验证码 / 手机确认的总时限：超时判失败，用户重开即可
_LOGIN_TIMEOUT_S = 300.0
# 拿到令牌后的收尾重试：网络瞬断不应作废用户已完成的手机确认
_FINALIZE_ATTEMPTS = 4
_FINALIZE_RETRY_BACKOFF_S = 2.0
# EAuthTokenPlatformType：WebBrowser——令牌受众为 web，finalize 后可直接用于站点请求
_PLATFORM_WEB_BROWSER = 2
# 令牌持久化：Persistent（勾选「记住我」语义，下发长命续期凭据）
_PERSISTENCE_PERSISTENT = 1
# 认证请求的站点标识：商店登录页会话归 Store
_WEBSITE_ID_STORE = "Store"
# 提交语言：简体中文
_LANGUAGE_SCHINESE = 6

# 二次验证形态（k_EAuthSessionGuardType）
_GUARD_EMAIL_CODE = 2
_GUARD_DEVICE_CODE = 3
_GUARD_DEVICE_CONFIRM = 4
_GUARD_EMAIL_CONFIRM = 5
# 码形态提交优先级：手机令牌在 App 内随手可查，邮箱码要翻邮件，靠后
_CODE_GUARD_ORDER = (_GUARD_DEVICE_CODE, _GUARD_EMAIL_CODE)
# 码形态（可输码）与确认形态（无需输码）的分野：确认形态优先呈现，
# 输码作为用户主动切换的备选（与商店登录页「改为输入代码」同语义）
_CODE_GUARDS = frozenset({_GUARD_EMAIL_CODE, _GUARD_DEVICE_CODE})
_CONFIRM_GUARDS = frozenset({_GUARD_DEVICE_CONFIRM, _GUARD_EMAIL_CONFIRM})

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


# ── protobuf 编解码（认证接口传输形态） ──────────────────────────────────

def _pb_write_varint(value: int) -> bytes:
    out = bytearray()
    v = value & 0xFFFFFFFFFFFFFFFF
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _pb_read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("protobuf varint 越界")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _pb_bytes_field(field: int, payload: bytes) -> bytes:
    return _pb_write_varint((field << 3) | 2) + _pb_write_varint(len(payload)) + payload


def _pb_varint_field(field: int, value: int) -> bytes:
    return _pb_write_varint((field << 3) | 0) + _pb_write_varint(value)


def _pb_fixed64_field(field: int, raw: bytes) -> bytes:
    return _pb_write_varint((field << 3) | 1) + raw


def _pb_fixed32_field(field: int, raw: bytes) -> bytes:
    return _pb_write_varint((field << 3) | 5) + raw


def _pb_fields(data: bytes) -> list[tuple[int, int, "bytes | int"]]:
    """protobuf 消息平铺为 (字段号, 线型, 原始值)；长度界定段不解包。"""
    out: list[tuple[int, int, "bytes | int"]] = []
    pos = 0
    while pos < len(data):
        key, pos = _pb_read_varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _pb_read_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        elif wire == 2:
            length, pos = _pb_read_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        else:
            break
        out.append((field, wire, value))
    return out


def _pb_pick(fields: list, field: int, wire: int):
    """取指定字段首个匹配值；不存在返回 None。"""
    for f, w, v in fields:
        if f == field and w == wire:
            return v
    return None


# ── 请求体构造（与商店登录页同字段序） ───────────────────────────────────

def _rsakey_request_body(account: str) -> str:
    msg = _pb_bytes_field(1, account.encode("utf-8"))
    return base64.b64encode(msg).decode("ascii")


def _begin_request_body(account: str, encrypted_b64: str, timestamp: int) -> str:
    device_details = _pb_bytes_field(1, _USER_AGENT.encode("utf-8")) + _pb_varint_field(
        2, _PLATFORM_WEB_BROWSER
    )
    msg = (
        _pb_bytes_field(2, account.encode("utf-8"))
        + _pb_bytes_field(3, encrypted_b64.encode("ascii"))
        + _pb_varint_field(4, timestamp)
        + _pb_varint_field(5, 1)
        + _pb_varint_field(7, _PERSISTENCE_PERSISTENT)
        + _pb_bytes_field(8, _WEBSITE_ID_STORE.encode("ascii"))
        + _pb_bytes_field(9, device_details)
        + _pb_varint_field(11, _LANGUAGE_SCHINESE)
    )
    return base64.b64encode(msg).decode("ascii")


def _guard_code_request_body(client_id: int, steam_id: int, code: str, code_type: int) -> str:
    msg = (
        _pb_varint_field(1, client_id)
        + _pb_fixed64_field(2, struct.pack("<Q", steam_id))
        + _pb_bytes_field(3, code.encode("utf-8"))
        + _pb_varint_field(4, code_type)
    )
    return base64.b64encode(msg).decode("ascii")


def _poll_request_body(client_id: int, request_id: bytes) -> str:
    msg = _pb_varint_field(1, client_id) + _pb_bytes_field(2, request_id)
    return base64.b64encode(msg).decode("ascii")


# ── Steam 认证调用 ────────────────────────────────────────────────────────

def _api_error_message(code: int, desc: str) -> str:
    if code == 5:  # 账号或密码不正确
        return "账号或密码不正确"
    if code in (65, 88):  # 邮箱验证码错 / 手机令牌码错
        return "验证码不正确，请重试"
    if code == 84:  # 尝试过于频繁
        return "尝试次数过多，请稍等几分钟再试"
    return f"Steam 拒绝了登录请求：{desc or f'错误码 {code}'}"


def _check_eresult(resp: httpx.Response) -> None:
    """结果码以 x-eresult 头承载；凭据被拒时响应体为空、全靠头判定。"""
    if resp.status_code >= 400:
        raise LoginError(f"Steam 登录服务不可用（HTTP {resp.status_code}）")
    eresult = resp.headers.get("x-eresult")
    if eresult is not None and eresult.isdigit() and int(eresult) != 1:
        desc = resp.headers.get("x-error_message") or ""
        raise LoginError(_api_error_message(int(eresult), desc))


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """单次请求；仅传输层失败（连接建立/读写中断）时重发，其余网络错误直接抛出。"""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _pb_form(body_b64: str) -> dict:
    """认证接口 POST 形态：input_protobuf_encoded 多部分表单字段。"""
    return {"files": {"input_protobuf_encoded": (None, body_b64)}}


async def _check_device(
    client: httpx.AsyncClient, steam_id: str, client_id: int
) -> bool:
    """登录页同款设备确认：本机在 Steam 记忆名单时免验证码直通轮询。

    网络失败按「不在名单」处理，回落到验证码路径。
    """
    try:
        resp = await _request(
            client,
            "POST",
            f"{_CHECK_DEVICE_URL}/{steam_id}",
            files={"clientid": (None, str(client_id)), "steamid": (None, str(steam_id))},
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return bool(isinstance(body, dict) and body.get("success"))


async def _begin_session(account: str, password: str, proxy_url: str | None) -> dict:
    """提交账号密码，返回会话信息（凭据性字段只留在进程内，不外泄）。"""
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await _request(
                client,
                "GET",
                _RSA_KEY_URL,
                params={
                    "origin": _STORE_ORIGIN,
                    "input_protobuf_encoded": _rsakey_request_body(account),
                },
            )
            _check_eresult(resp)
            key = _pb_fields(resp.content)
            mod, exp, ts = _pb_pick(key, 1, 2), _pb_pick(key, 2, 2), _pb_pick(key, 3, 0)
            if not (mod and exp and ts is not None):
                raise LoginError("Steam 登录服务响应异常，请稍后重试")
            encrypted_b64 = base64.b64encode(
                bytes.fromhex(encrypt_password(password, mod_hex=mod.decode("ascii"), exp_hex=exp.decode("ascii")))
            ).decode("ascii")
            resp = await _request(
                client, "POST", _BEGIN_URL, **_pb_form(_begin_request_body(account, encrypted_b64, int(ts)))
            )
            _check_eresult(resp)
            fields = _pb_fields(resp.content)
            client_id = _pb_pick(fields, 1, 0)
            request_id = _pb_pick(fields, 2, 2)
            steam_id = _pb_pick(fields, 5, 0)
            if not (client_id is not None and request_id and steam_id):
                raise LoginError("Steam 登录服务响应异常，请稍后重试")
            interval_raw = _pb_pick(fields, 3, 5)
            interval = struct.unpack("<f", interval_raw)[0] if interval_raw else 5.0
            guards: list[int] = []
            for f, w, v in fields:
                if f != 4 or w != 2:
                    continue
                ctype = _pb_pick(_pb_fields(v), 1, 0)
                if ctype is not None:
                    guards.append(int(ctype))
            # 登录页同序的设备确认：免验证设备在这里被识别，输码步骤跳过
            device_ok = await _check_device(client, str(steam_id), int(client_id))
    except httpx.HTTPError as exc:
        raise LoginError(_NETWORK_MESSAGE) from exc

    code_guard = next((g for g in _CODE_GUARD_ORDER if g in guards), 0)
    return {
        "steam_id": str(steam_id),
        "client_id": int(client_id),
        "request_id": request_id,
        "interval": min(max(float(interval), 1.0), 15.0),
        "guards": guards,
        "code_guard": code_guard,
        "code_available": bool(_CODE_GUARDS & set(guards)),
        "device_ok": device_ok,
    }


async def _submit_guard_code(session: dict, code: str, guard: int, proxy_url: str | None) -> None:
    """提交 Steam Guard 验证码；错误码 65/88（邮箱码错 / 令牌码错）映射为可重试提示。"""
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await _request(
                client,
                "POST",
                _GUARD_CODE_URL,
                **_pb_form(
                    _guard_code_request_body(
                        int(session["client_id"]), int(session["steam_id"]), code.strip(), guard
                    )
                ),
            )
    except httpx.HTTPError as exc:
        raise LoginError(_NETWORK_MESSAGE) from exc
    _check_eresult(resp)
    # 提交后 Steam 可轮换会话标识（响应携带新 client_id / request_id），
    # 轮询必须跟随新值，否则 Steam 侧已确认、程序侧永远等不到令牌
    fields = _pb_fields(resp.content)
    new_client = _pb_pick(fields, 5, 0)
    new_request = _pb_pick(fields, 6, 2)
    if new_client is not None:
        session["client_id"] = int(new_client)
    if new_request:
        session["request_id"] = new_request


async def _poll_once(session: dict, proxy_url: str | None) -> tuple[str, str]:
    """轮询一次认证状态，返回 (状态, refresh token)。

    状态："" = 未确认继续等；"token" = 已确认；"rejected" = 会话已终结
    （手机上拒绝 / 会话过期，服务端以非 OK 结果码 + 空响应稳态表达）。
    网络抖动按未确认处理，交给总时限兜底。
    """
    try:
        async with httpx.AsyncClient(
            timeout=_API_TIMEOUT, proxy=proxy_url, headers=_API_HEADERS, follow_redirects=True
        ) as client:
            resp = await _request(
                client,
                "POST",
                _POLL_URL,
                **_pb_form(_poll_request_body(int(session["client_id"]), session["request_id"])),
            )
    except httpx.HTTPError:
        return "", ""  # 单次网络抖动不判失败，交给超时兜底
    if resp.status_code >= 500:
        return "", ""
    eresult = resp.headers.get("x-eresult", "")
    if eresult.isdigit() and int(eresult) != 1:
        # 等待期出现非 OK 结果码 = 会话终结（拒绝 / 过期），不是可重试错误
        return "rejected", ""
    refresh = _pb_pick(_pb_fields(resp.content), 3, 2)
    if isinstance(refresh, bytes) and refresh:
        return "token", refresh.decode("ascii")
    return "", ""


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
        "code_available": False,
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
    return {
        k: _session[k]
        for k in ("state", "message", "error", "code_hint", "code_available", "started_at", "updated_at")
    }


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
            log_event(
                logger,
                "应用内账号密码登录失败",
                tag="未完成",
                detail={"账号前缀": account[:3], "原因": str(exc)},
            )
            _update_state(STATE_FAILED, error=str(exc))
            return {"ok": False, "state": login_status()}
        # 会话凭据性字段并入会话容器（login_status 快照白名单不外泄它们）
        _session.update(
            {
                k: info[k]
                for k in ("steam_id", "client_id", "request_id", "interval", "code_guard", "code_available")
            }
        )
        if info["device_ok"]:
            _update_state(STATE_AWAITING_CONFIRM, "本机已获 Steam 信任，正在完成登录…")
        elif _CONFIRM_GUARDS & set(info["guards"]):
            # 手机验证器账号与登录页同序：默认等用户在手机上确认（轮询全程在跑），
            # 输码是用户主动切换的备选路径
            _update_state(STATE_AWAITING_CONFIRM, "")
        elif info["code_guard"]:
            _update_state(
                STATE_AWAITING_CODE,
                "",
                code_hint="totp" if info["code_guard"] == _GUARD_DEVICE_CODE else "email",
            )
        else:
            _update_state(STATE_AWAITING_CONFIRM, "")
        # 轮询任务直接持有会话容器：提交验证码后的会话轮换（_submit_guard_code
        # 原地更新 client_id / request_id）对轮询即时可见
        _task = asyncio.get_running_loop().create_task(_poll_until_authed(_session, proxy_url))
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
            status, refresh = await _poll_once(info, proxy_url)
            if status == "rejected":
                _update_state(
                    STATE_FAILED,
                    error="本次登录未获确认（手机上已拒绝或已超时），请重新登录",
                )
                return
            if not refresh:
                continue
            _update_state(STATE_FINALIZING, "正在建立登录态…")
            # 令牌在手，收尾链（换 Cookie + 绑定）不因一次网络瞬断作废——
            # 作废意味着用户要在手机上重新确认一遍
            cookies = ""
            for attempt in range(_FINALIZE_ATTEMPTS):
                try:
                    cookies = await web_cookies_from_refresh_token(
                        refresh, info["steam_id"], proxy_url=proxy_url
                    )
                    break
                except SessionRefreshError:
                    if attempt + 1 >= _FINALIZE_ATTEMPTS or time.monotonic() > deadline:
                        raise
                    # 过程反馈：重试期间让用户看见「在自动恢复」而不是「卡住」
                    _update_state(
                        STATE_FINALIZING,
                        f"网络不稳，正在自动恢复登录（第 {attempt + 1}/{_FINALIZE_ATTEMPTS - 1} 次重试）…",
                    )
                    log_event(
                        logger,
                        "登录收尾遇网络瞬断，稍后重试",
                        level=logging.WARNING,
                        detail={"第几次尝试": attempt + 1},
                    )
                    await asyncio.sleep(_FINALIZE_RETRY_BACKOFF_S * (attempt + 1))
            await bind_account(cookies)
            log_event(
                logger,
                "应用内登录成功并已绑定账号",
                tag="成功",
                detail={"SteamID": info["steam_id"]},
            )
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
        log_event(logger, "应用内登录收尾失败", level=logging.ERROR, exc_info=True)
        if not _cancelled.is_set():
            _update_state(STATE_FAILED, error="登录收尾失败，请重试")


async def submit_guard_code(code: str) -> dict:
    """提交 Steam Guard 验证码（手机令牌或邮箱码，按会话首选形态）。

    等待确认态同样可提交：确认形态账号默认呈现「手机确认」，用户切换到
    输码路径时经此入口提交（无输码形态的会话拒绝并带 no_session 标记，
    供路由层区分 409 与可重试错误）。
    """
    session = _session
    if (
        session is None
        or session["state"] not in (STATE_AWAITING_CODE, STATE_AWAITING_CONFIRM)
        or not session.get("code_guard")
    ):
        return {
            "ok": False,
            "no_session": True,
            "state": login_status(),
            "error": "当前没有等待验证码的登录",
        }
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
