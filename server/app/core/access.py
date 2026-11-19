"""本地接口准入：进程内会话令牌 + Host 校验。

本机 API 的信任边界有三类对手，必须分别对待：

- **本机其他进程**：能自由发 HTTP，不受任何浏览器策略约束。挡它的只能是
  「它拿不到的东西」——令牌在进程内随机生成、不落盘、不进前端源码；
- **浏览器里的恶意页面**：跨源读取被 CORS 挡住，但简单请求仍可产生副作用；
  **DNS 重绑定**把跨源变成同源，CORS 完全不介入，唯一的结构性防线是
  服务端校验 `Host`；
- **桌面壳自身**：与后端同进程，直接读进程内令牌即可，不走网络。

令牌经 `HttpOnly` Cookie 承载（`EventSource` 无法带自定义头，Cookie 是
唯一能让 SSE 也带凭据的载体），前端 JS 读不到它；桌面壳页面通过
`pywebview.api` 取令牌后换取 Cookie。
"""
from __future__ import annotations

from ipaddress import ip_address
import secrets
import time
from typing import Callable

from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "holdexar_session"
SESSION_HEADER = "x-holdexar-session"

# 公开 API：无会话即可访问。健康检查是桌面壳判活与外部探针的唯一入口；
# 更新验收确认要在页面拿到会话之前调用。清单是唯一事实源，禁止在各路由
# 里另行标注公开属性。
PUBLIC_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/system/update-ready",
        "/api/v1/session/bootstrap",
        "/api/v1/session/dev",
        "/api/v1/session/unlock",
        # 忘记密码找回（Steam 登录验证身份）：锁定态公开通道，共用解锁限速
        "/api/v1/session/recovery/login/start",
        "/api/v1/session/recovery/login/code",
        "/api/v1/session/recovery/login/status",
        "/api/v1/session/recovery/cancel",
        "/api/v1/session/recovery/complete",
    }
)

_API_PREFIX = "/api/"

_SESSION_TTL_S = 12 * 60 * 60
_sessions: dict[str, float] = {}
_token: str | None = None

# 局域网模式放行的本机地址集合（字面量、小写、无端口）；空＝默认回环-only。
_extra_hosts: frozenset[str] = frozenset()

# MCP 路由闸（由 agent 域 mcp 模块注册）：Host/Origin 闸之后、会话闸之前生效。
# 外部本机进程拿不到进程内会话令牌，凭数据目录令牌文件准入；
# 未注册或未启用时该前缀一律 404。
_mcp_path_prefix: str | None = None
_mcp_gate: Callable[[Scope], bool | None] | None = None


def register_mcp_gate(path_prefix: str, gate: Callable[[Scope], bool | None]) -> None:
    global _mcp_path_prefix, _mcp_gate
    _mcp_path_prefix = path_prefix
    _mcp_gate = gate


def session_token() -> str:
    """Return the active compatibility session used by local callers."""
    global _token
    _prune_sessions()
    if _token is None:
        _token = _new_session()
    return _token


def _new_session() -> str:
    _prune_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.monotonic() + _SESSION_TTL_S
    return token


def _prune_sessions() -> None:
    global _token
    now = time.monotonic()
    for token, expiry in tuple(_sessions.items()):
        if expiry <= now:
            _sessions.pop(token, None)
    if _token not in _sessions:
        _token = None


def reset_token() -> None:
    """Revoke every active session (security reset and test isolation)."""
    global _token
    _sessions.clear()
    _token = None


def _header_value(scope: Scope, wanted: bytes) -> str:
    for name, value in scope.get("headers", []):
        if name == wanted:
            return value.decode("latin-1")
    return ""


def _cookie_value(scope: Scope) -> str:
    for part in _header_value(scope, b"cookie").split(";"):
        key, _, raw = part.strip().partition("=")
        if key == SESSION_COOKIE:
            return raw
    return ""


def _authorized(scope: Scope) -> bool:
    _prune_sessions()
    candidates = (_cookie_value(scope), _header_value(scope, SESSION_HEADER.encode()))
    return any(candidate in _sessions for candidate in candidates if candidate)


def revoke_session(scope: Scope) -> None:
    global _token
    candidates = (_cookie_value(scope), _header_value(scope, SESSION_HEADER.encode()))
    for candidate in candidates:
        if candidate:
            _sessions.pop(candidate, None)
            if candidate == _token:
                _token = None


def host_allowed(host: str) -> bool:
    """接受回环 Host 与本机网卡地址，挡住 DNS 重绑定。

    重绑定的本质是让攻击者域名解析到本机——浏览器随后发出的请求带的是
    **攻击者域名**作为 Host，永远不在白名单里；白名单只收字面地址。默认
    模式白名单为空＝历史行为（仅回环）；局域网模式启动/开关时写入本机
    网卡地址（`set_extra_hosts`）。端口不限（端口协商会换）。
    """
    return _hostname(host) in _extra_hosts or is_loopback_host(host)


def is_loopback_host(host: str) -> bool:
    """Host 是否指向本机回环（Host 头形态；仅用于 dev 通道诊断性判定）。

    信任判定禁止使用 Host——客户端可伪造（P0）。
    """
    return _hostname(host) in {"127.0.0.1", "localhost", "::1"}


def peer_is_loopback(scope: Scope) -> bool:
    """对端地址是否回环：来源可信判定的唯一事实源（对端地址不可伪造）。

    合成 scope（无 client 键，测试直调）按本机处理；IPv4-mapped 回环段
    一并覆盖（::ffff:127.0.0.1 形态）。
    """
    client = scope.get("client")
    if not client:
        return True
    addr_text = client[0] if isinstance(client, (list, tuple)) else str(client)
    try:
        addr = ip_address(addr_text)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return addr.is_loopback


def set_extra_hosts(hosts) -> None:
    """更新局域网模式放行的本机地址集合；空集合＝回环-only 默认模式。"""
    global _extra_hosts
    normalized = set()
    for host in hosts or ():
        hostname = _hostname(host)
        if hostname:
            normalized.add(hostname)
    _extra_hosts = frozenset(normalized)


def _hostname(host: str) -> str:
    """取 Host 头的主机名部分（剥离端口；IPv6 字面量剥方括号）。

    端口段必须全数字才剥——垃圾端口（`127.0.0.1:evil`）整串按主机名
    对待，不会归一成白名单条目。
    """
    host = (host or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else ""
    if host.count(":") > 1:
        return host  # 裸 IPv6 字面量（该形态本就不带端口）
    if ":" in host:
        name, _, port = host.rpartition(":")
        return name if port.isdigit() else host
    return host


def _is_api(path: str) -> bool:
    return path.startswith(_API_PREFIX)


def origin_allowed(scope: Scope) -> bool:
    """请求来源是否属于本应用自身。

    Cookie 的作用域**不含端口**：`http://localhost:8080` 与后端所在端口在
    浏览器眼里是同一个 site，`SameSite=Strict` 拦不住跨端口携带。开发机
    上常驻着别的本地 web 服务（另一个 vite、调试页、任意本地应用），它们
    的页面发起的请求会自动带上本应用的会话 Cookie——所以必须由服务端校验
    `Origin`，不能只靠 Cookie 属性。

    规则：带 Origin 时必须与请求的 Host 同源（scheme 不校验——回环 HTTP
    与 HTTPS 都可能出现）；不带 Origin 的请求放行（同源导航、非浏览器
    客户端）。`Origin: null` 属外部来源，一律拒绝。
    """
    origin = _header_value(scope, b"origin").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(origin)
        request = urlsplit("//" + _header_value(scope, b"host"))
        if parts.scheme not in {"http", "https"} or not host_allowed(parts.netloc):
            return False
        origin_port = parts.port or (443 if parts.scheme == "https" else 80)
        request_port = request.port or (443 if scope.get("scheme") == "https" else 80)
        return parts.hostname == request.hostname and origin_port == request_port
    except ValueError:
        return False


def request_authorized(scope: Scope) -> bool:
    """请求是否携带有效本地会话（路由层公开面分级用）。"""
    return _authorized(scope)


# 局域网无会话对端的唯一可见页面：自包含（零外部资源），脚本走一次性
# nonce——全局 CSP 收紧后内联脚本只有这里放行。
_LOCK_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Holdexar 访问锁定</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #10141a; color: #dbe2ea;
         font: 15px/1.6 system-ui, "Microsoft YaHei", sans-serif; }
  main { width: min(340px, 88vw); padding: 32px 28px; border-radius: 14px;
         background: #171d26; box-shadow: 0 8px 30px rgba(0,0,0,.35); text-align: center; }
  h1 { margin: 0 0 6px; font-size: 20px; }
  p { margin: 0 0 18px; color: #8b97a5; }
  input { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
          border: 1px solid #2a3441; background: #10141a; color: #dbe2ea; margin-bottom: 12px; }
  button { width: 100%; padding: 10px 0; border: 0; border-radius: 8px;
           background: #3b82f6; color: #fff; font-size: 15px; cursor: pointer; }
  #msg { min-height: 1.2em; margin: 12px 0 0; color: #f87171; white-space: pre-line; }
</style>
</head>
<body>
<main>
  <h1>Holdexar</h1>
  <p>此设备需要访问密码才能继续</p>
  <form id="f">
    <input id="pw" type="password" autocomplete="current-password" placeholder="访问密码">
    <button type="submit">解锁</button>
  </form>
  <p id="msg"></p>
</main>
<script nonce="__NONCE__">
var msg = document.getElementById('msg');
var pw = document.getElementById('pw');
document.getElementById('f').addEventListener('submit', function (e) {
  e.preventDefault();
  fetch('/api/v1/session/unlock', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pw.value })
  }).then(function (r) {
    if (r.ok) { location.replace('/'); return null; }
    return r.json().catch(function () { return {}; });
  }).then(function (d) {
    if (d && d.detail) { msg.textContent = d.detail; }
  }).catch(function () { msg.textContent = '服务暂不可用'; });
});
pw.focus();
</script>
</body>
</html>
"""


async def _lock_page(scope: Scope, receive: Receive, send: Send) -> None:
    nonce = secrets.token_urlsafe(16)
    response = HTMLResponse(_LOCK_PAGE_TEMPLATE.replace("__NONCE__", nonce))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    await response(scope, receive, send)


class LocalAccessMiddleware:
    """统一准入：闸覆盖全部路径，`/api/` 默认要求会话，公开路径显式白名单。

    非 API 路径（SPA 静态资源与页面回退）不做「无闸放行」：对端回环
    （本机同用户基线）或持会话才放行；局域网无会话对端只拿到内联
    锁定页——静态资源与端点结构不再对外暴露（与 Go 侧 lockedpage 同型）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if not host_allowed(_header_value(scope, b"host")):
            response = JSONResponse({"detail": "请求来源不被信任"}, status_code=421)
            await response(scope, receive, send)
            return

        # 默认模式（未开局域网）下对端必须回环：Host 头客户端可伪造，
        # 伪造 Host: 127.0.0.1 的局域网请求在这里被对端地址拦截（P0）。
        from app.core import lan_access

        if not lan_access.is_enabled() and not peer_is_loopback(scope):
            response = JSONResponse({"detail": "请求来源不被信任"}, status_code=421)
            await response(scope, receive, send)
            return

        if not _is_api(path):
            if peer_is_loopback(scope) or _authorized(scope):
                await self.app(scope, receive, send)
                return
            if path in {"/", "/index.html"}:
                await _lock_page(scope, receive, send)
                return
            response = JSONResponse({"detail": "需要本地会话"}, status_code=401)
            await response(scope, receive, send)
            return

        # 预检请求只放行本机对端（开发态跨端口预检全部来自回环浏览器）；
        # 无凭据局域网对端的 OPTIONS 不再先于会话闸短路，端点方法表
        # 不构成无凭据探测通道。
        if scope["method"] == "OPTIONS":
            if peer_is_loopback(scope) or _authorized(scope):
                await self.app(scope, receive, send)
                return
            response = JSONResponse({"detail": "请求来源不被信任"}, status_code=403)
            await response(scope, receive, send)
            return

        if not origin_allowed(scope):
            response = JSONResponse({"detail": "请求来源与应用不同源"}, status_code=403)
            await response(scope, receive, send)
            return

        if _mcp_path_prefix and path.startswith(_mcp_path_prefix):
            verdict = _mcp_gate(scope) if _mcp_gate is not None else None
            if verdict is not True:
                status = 401 if verdict is False else 404
                response = JSONResponse({"detail": "not found" if status == 404
                                         else "缺少有效的 MCP 令牌"}, status_code=status)
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if not _authorized(scope):
            response = JSONResponse({"detail": "缺少有效的本地会话"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def issue_session(response, *, secure: bool = False) -> None:
    """把会话令牌写进响应 Cookie（HttpOnly + SameSite=Strict）。

    `SameSite=Strict` 让浏览器在跨站请求里根本不带这个 Cookie——与 Host
    校验互为冗余：即便 Host 被伪造，跨站上下文也拿不到会话。
    `Secure` 在回环 HTTP 下不能置位（浏览器会拒收），故默认关闭。
    """
    response.set_cookie(
        SESSION_COOKIE,
        _new_session(),
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


__all__ = [
    "LocalAccessMiddleware",
    "PUBLIC_PATHS",
    "SESSION_COOKIE",
    "SESSION_HEADER",
    "host_allowed",
    "is_loopback_host",
    "origin_allowed",
    "issue_session",
    "request_authorized",
    "revoke_session",
    "reset_token",
    "session_token",
    "set_extra_hosts",
]
