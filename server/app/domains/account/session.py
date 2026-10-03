"""account 域登录会话续期：steamLoginSecure 有效期解析与刷新。
两枚令牌：steamLoginSecure=<steamid>||<access JWT>（约 24h，一切登录态请求用）；
steamRefresh_steam=<steamid>||<refresh JWT>（长命，勾选记住我时下发）。
按 JWT exp 判剩余寿命，临期/过期用续期凭据换新 web Cookie：POST
jwt/finalizelogin → POST transfer_info[].url 取 Set-Cookie。令牌按域签发
（store/community/help/checkout/steam.tv 各一枚互不通用）；本应用取 **store
域令牌**（全部消费点在 store 与 api.steampowered.com）；未来若出现 community
消费点须经其 settoken 单独取令牌，不得复用 store 令牌。exp 只做本地提前续期
判断，不验签——真伪由 Steam 实际请求判定。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import httpx

from app.crawler.utils import BEIJING_TZ
from .steam_wallet import parse_cookie_str, steam_id_from_cookies

logger = logging.getLogger(__name__)

# 登录 Cookie 三件套 + 续期凭据（入库口径：只有这几个键）
SESSION_COOKIE = "sessionid"
COUNTRY_COOKIE = "steamCountry"
ACCESS_COOKIE = "steamLoginSecure"
REFRESH_COOKIE = "steamRefresh_steam"
REMEMBER_COOKIE = "steamRememberLogin"

# 续期提前量（分钟）：访问令牌剩余寿命低于此值即换新，为网络失败留余量
REFRESH_AHEAD_MINUTES = 120.0

FINALIZE_URL = "https://login.steampowered.com/jwt/finalizelogin"
FINALIZE_REDIR = "https://steamcommunity.com/login/home/?goto="
FINALIZE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Origin": "https://steamcommunity.com",
    "Referer": "https://steamcommunity.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
REFRESH_TIMEOUT = 20.0
_TRANSFER_ATTEMPTS = 4
_TRANSFER_RETRY_SECONDS = 1.0
# finalize 第一跳的瞬断重发（代理节点抖动下整段续期链的成败所在）
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_BACKOFF_S = 0.8
# 各域 settoken 下发的 steamLoginSecure 逐域不同（JWT aud：web:store /
# web:community / web:steamtv…），合并为单串时以商店域令牌为准——与手贴
# 浏览器商店 Cookie 的既有口径一致，其余域不覆盖已取到的值
_LOGIN_SECURE_HOST_PRIORITY = ("store.steampowered.com", "steamcommunity.com")


class SessionRefreshError(RuntimeError):
    """登录续期失败（续期凭据缺失/失效、Steam 拒绝、响应结构异常）。"""


def decode_jwt_claims(token: str) -> dict:
    """JWT 载荷解码（不校验签名）：只读 exp / sub / aud 这类本地判定用声明。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _owner_and_token(value: str) -> tuple[str, str]:
    """`<steamid64>||<token>` → (steamid64, token)。

    Cookie 值有 Steam 原生的百分号编码（`%7C%7C`）与明文 `||` 两种形态；
    单个数字值按 SteamID64 处理（无令牌段）。
    """
    body = unquote((value or "").strip())
    if "||" in body:
        owner, _, token = body.partition("||")
        return owner.strip(), token.strip()
    return ("", body.strip()) if not body.isdigit() else (body.strip(), "")


def refresh_token_of(cookies_raw: str) -> str:
    """续期凭据（steamRefresh_steam 的 JWT 段）；缺失返回空串。"""
    jar = parse_cookie_str(cookies_raw)
    return _owner_and_token(jar.get(REFRESH_COOKIE, ""))[1]


def access_token_expires_at(cookies_raw: str) -> datetime | None:
    """steamLoginSecure 里访问令牌的到期时刻（UTC）；无法解析返回 None。"""
    jar = parse_cookie_str(cookies_raw)
    _sid, token = _owner_and_token(jar.get(ACCESS_COOKIE, ""))
    exp = decode_jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(exp), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def session_freshness(cookies_raw: str) -> dict:
    """登录态新鲜度（纯本地判定，不发网络请求）。

    expires_at 为北京时间无时区 ISO 串，与账号行其余时间戳同口径；
    exp 缺失（令牌非 JWT 形态）时按「新鲜度未知」处理——expired/expiring
    均为 False，不把判定不出的 Cookie 当成过期。
    """
    jar = parse_cookie_str(cookies_raw)
    has_refresh = bool(_owner_and_token(jar.get(REFRESH_COOKIE, ""))[1])
    expires = access_token_expires_at(cookies_raw)
    if expires is None:
        return {
            "expires_at": None,
            "expired": False,
            "expiring": False,
            "seconds_left": None,
            "has_refresh_token": has_refresh,
        }
    left = (expires - datetime.now(timezone.utc)).total_seconds()
    return {
        "expires_at": expires.astimezone(BEIJING_TZ).replace(tzinfo=None).isoformat(),
        "expired": left <= 0,
        "expiring": left <= REFRESH_AHEAD_MINUTES * 60,
        "seconds_left": left,
        "has_refresh_token": has_refresh,
    }


def _set_cookie_values(headers) -> dict[str, str]:
    """响应头 Set-Cookie → {名字: 值}（同名后者覆盖）。"""
    out: dict[str, str] = {}
    for raw in headers.get_list("set-cookie"):
        name, _, value = raw.split(";", 1)[0].partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


async def _post_with_transient_retry(
    client: httpx.AsyncClient, *, files: dict
) -> httpx.Response:
    """finalize 单发 + 瞬断重发：仅传输层失败（连接/读写中断）时重试。

    请求未达对端即可安全重发；响应已返回后的错误不重试。
    """
    last_exc: Exception | None = None
    for attempt in range(_TRANSIENT_RETRY_ATTEMPTS):
        try:
            return await client.post(FINALIZE_URL, headers=FINALIZE_HEADERS, files=files)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt + 1 < _TRANSIENT_RETRY_ATTEMPTS:
                await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_S * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def _run_transfer(
    client: httpx.AsyncClient, url: str, form: dict[str, str]
) -> dict[str, str]:
    """执行一条 transfer（Steam 下发 Cookie 的第二跳），返回其 Set-Cookie。"""
    last: Exception | None = None
    for attempt in range(_TRANSFER_ATTEMPTS):
        try:
            resp = await client.post(url, files={k: (None, v) for k, v in form.items()})
            if resp.status_code >= 400:
                raise SessionRefreshError(f"Cookie 下发失败（HTTP {resp.status_code}）")
            values = _set_cookie_values(resp.headers)
            if ACCESS_COOKIE not in values:
                raise SessionRefreshError("Cookie 下发响应未包含 steamLoginSecure")
            return values
        except (httpx.HTTPError, SessionRefreshError) as exc:
            last = exc
            if attempt + 1 < _TRANSFER_ATTEMPTS:
                await asyncio.sleep(_TRANSFER_RETRY_SECONDS * (attempt + 1))
    detail = f"{type(last).__name__}: {last}" if last is not None else "unknown"
    raise SessionRefreshError(f"Cookie 下发失败（{detail}）")


async def web_cookies_from_refresh_token(
    refresh_token: str,
    steam_id: str,
    *,
    proxy_url: str | None = None,
    timeout: float = REFRESH_TIMEOUT,
    inherited: dict[str, str] | None = None,
) -> str:
    """用续期凭据走 finalize 两跳换一组 web Cookie，返回整串（键序与入库口径一致）。

    入参是裸 JWT 凭据与 SteamID64；inherited 携带沿用值（steamCountry /
    steamRememberLogin）。Steam 可能同时轮换续期凭据，新值一并写入结果；
    未轮换时凭据本身（`<steamid>||<JWT>` 形态）充当 steamRefresh_steam。
    失败一律抛 SessionRefreshError。
    """
    inherited = inherited or {}
    session_id = secrets.token_hex(12)
    async with httpx.AsyncClient(
        timeout=timeout, proxy=proxy_url, follow_redirects=True
    ) as client:
        try:
            resp = await _post_with_transient_retry(
                client,
                files={
                    "nonce": (None, refresh_token),
                    "sessionid": (None, session_id),
                    "redir": (None, FINALIZE_REDIR),
                },
            )
        except httpx.HTTPError as exc:
            raise SessionRefreshError(f"续期请求失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise SessionRefreshError(f"续期被 Steam 拒绝（HTTP {resp.status_code}）")
        try:
            body = resp.json()
        except ValueError as exc:
            raise SessionRefreshError("续期响应不是 JSON") from exc
        if not isinstance(body, dict):
            raise SessionRefreshError("续期响应结构异常")
        if body.get("error"):
            raise SessionRefreshError(f"Steam 返回错误码 {body.get('error')}")
        transfers = body.get("transfer_info")
        if not isinstance(transfers, list) or not transfers:
            raise SessionRefreshError("续期响应缺少 transfer_info")

        # finalize 自身也可能下发新的续期凭据（steamRefresh_steam）
        values = _set_cookie_values(resp.headers)
        login_secure_by_host: dict[str, str] = {}
        for transfer in transfers:
            if not isinstance(transfer, dict) or not transfer.get("url"):
                continue
            params = transfer.get("params") or {}
            form = {"steamID": steam_id}
            if isinstance(params, dict):
                form.update({str(k): str(v) for k, v in params.items()})
            got = await _run_transfer(client, str(transfer["url"]), form)
            host = urlsplit(str(transfer["url"])).netloc
            if ACCESS_COOKIE in got:
                login_secure_by_host.setdefault(host, got[ACCESS_COOKIE])
            values.update(got)
        for host in (*_LOGIN_SECURE_HOST_PRIORITY, *login_secure_by_host):
            if host in login_secure_by_host:
                values[ACCESS_COOKIE] = login_secure_by_host[host]
                break

    access_value = values.get(ACCESS_COOKIE, "")
    if not access_value:
        raise SessionRefreshError("续期响应未下发新的 steamLoginSecure")

    refresh_value = values.get(REFRESH_COOKIE) or inherited.get(REFRESH_COOKIE, "")
    if not refresh_value:
        # finalize 未轮换凭据时，传入的 nonce 即长命续期凭据
        refresh_value = f"{steam_id}||{refresh_token}"
    merged = {
        SESSION_COOKIE: session_id,
        COUNTRY_COOKIE: inherited.get(COUNTRY_COOKIE, ""),
        ACCESS_COOKIE: access_value,
        REFRESH_COOKIE: refresh_value,
        REMEMBER_COOKIE: inherited.get(REMEMBER_COOKIE, ""),
    }
    return "; ".join(f"{k}={v}" for k, v in merged.items() if v)


async def refresh_login_cookies(
    cookies_raw: str, *, proxy_url: str | None = None, timeout: float = REFRESH_TIMEOUT
) -> str:
    """用本地存储 Cookie 里的续期凭据换一组新的 web Cookie。

    失败一律抛 SessionRefreshError：调用方保持原 Cookie，并把「登录已过期」
    交给用户面处理，不把续期失败混进网络故障计数。
    """
    jar = parse_cookie_str(cookies_raw)
    refresh_token = refresh_token_of(cookies_raw)
    if not refresh_token:
        raise SessionRefreshError("缺少续期凭据 steamRefresh_steam（登录时未勾选「记住我」）")
    steam_id = steam_id_from_cookies(cookies_raw) or _owner_and_token(
        jar.get(REFRESH_COOKIE, "")
    )[0]
    if not steam_id:
        raise SessionRefreshError("无法从 Cookie 解析 SteamID64")
    return await web_cookies_from_refresh_token(
        refresh_token,
        steam_id,
        proxy_url=proxy_url,
        timeout=timeout,
        inherited={
            COUNTRY_COOKIE: jar.get(COUNTRY_COOKIE, ""),
            REFRESH_COOKIE: jar.get(REFRESH_COOKIE, ""),
            REMEMBER_COOKIE: jar.get(REMEMBER_COOKIE, ""),
        },
    )