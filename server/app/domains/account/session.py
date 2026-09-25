"""account 域登录会话续期：steamLoginSecure 有效期解析与刷新。

Steam 登录 Cookie 串里两枚令牌决定登录态还能用多久：

- `steamLoginSecure` = `<steamid64>||<access JWT>`：寿命约 24 小时的访问令牌，
  钱包 / 愿望单 / 已购 / 账单 / 家庭组 / CDK 激活等一切登录态请求都用它；
- `steamRefresh_steam` = `<steamid64>||<refresh JWT>`：长命续期凭据，登录时
  勾选「记住我」由 Steam 下发。浏览器能长期保持登录态，靠的就是它每次访问
  Steam 页面时换一张新的访问令牌。

访问令牌一旦过期，Steam 对所有登录态请求一律按未登录处理。本模块按 JWT 的
exp 判断剩余寿命，临期或过期时用续期凭据换一组新的 web Cookie（Steam Web
前端同款两跳）：

    POST https://login.steampowered.com/jwt/finalizelogin → {transfer_info: [...]}
    POST <transfer_info[].url>（表单带 steamID + params） → Set-Cookie: steamLoginSecure

exp 只用于本地判断「要不要提前续期」，不做签名校验——令牌真伪由 Steam 在
实际请求时判定。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

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
_TRANSFER_ATTEMPTS = 3
_TRANSFER_RETRY_SECONDS = 0.5


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
                await asyncio.sleep(_TRANSFER_RETRY_SECONDS)
    raise SessionRefreshError(f"Cookie 下发失败：{last}")


async def refresh_login_cookies(
    cookies_raw: str, *, proxy_url: str | None = None, timeout: float = REFRESH_TIMEOUT
) -> str:
    """用续期凭据换一组新的 web Cookie，返回整串（键序与入库口径一致）。

    失败一律抛 SessionRefreshError：调用方保持原 Cookie，并把「登录已过期」
    交给用户面处理，不把续期失败混进网络故障计数。Steam 可能同时轮换续期
    凭据，新值一并落库（旧值随后失效）。
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

    session_id = secrets.token_hex(12)
    async with httpx.AsyncClient(
        timeout=timeout, proxy=proxy_url, follow_redirects=True
    ) as client:
        try:
            resp = await client.post(
                FINALIZE_URL,
                headers=FINALIZE_HEADERS,
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
        for transfer in transfers:
            if not isinstance(transfer, dict) or not transfer.get("url"):
                continue
            params = transfer.get("params") or {}
            form = {"steamID": steam_id}
            if isinstance(params, dict):
                form.update({str(k): str(v) for k, v in params.items()})
            values.update(await _run_transfer(client, str(transfer["url"]), form))

    access_value = values.get(ACCESS_COOKIE, "")
    if not access_value:
        raise SessionRefreshError("续期响应未下发新的 steamLoginSecure")

    merged = {
        SESSION_COOKIE: session_id,
        COUNTRY_COOKIE: jar.get(COUNTRY_COOKIE, ""),
        ACCESS_COOKIE: access_value,
        REFRESH_COOKIE: values.get(REFRESH_COOKIE) or jar.get(REFRESH_COOKIE, ""),
        REMEMBER_COOKIE: jar.get(REMEMBER_COOKIE, ""),
    }
    return "; ".join(f"{k}={v}" for k, v in merged.items() if v)