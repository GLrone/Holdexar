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

import hmac
import secrets

from starlette.responses import JSONResponse
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
    }
)

_API_PREFIX = "/api/"

_token: str | None = None


def session_token() -> str:
    """当前进程的会话令牌（首次调用时生成，进程退出即失效）。

    不落盘、不进日志、不进任何响应体——它只存在于进程内存与 `HttpOnly`
    Cookie 里。桌面壳与后端同进程，直接调用本函数取用。
    """
    global _token
    if _token is None:
        _token = secrets.token_urlsafe(32)
    return _token


def reset_token() -> None:
    """丢弃当前令牌（测试隔离 / 换数据目录用）。"""
    global _token
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
    expected = session_token()
    candidates = (_cookie_value(scope), _header_value(scope, SESSION_HEADER.encode()))
    return any(
        candidate and hmac.compare_digest(candidate, expected) for candidate in candidates
    )


def host_allowed(host: str) -> bool:
    """只接受回环 Host，挡住 DNS 重绑定。

    重绑定的本质是让攻击者域名解析到 127.0.0.1——浏览器随后发出的请求
    带的是**攻击者域名**作为 Host。因此 Host 必须是本机回环名；端口不限
    （端口协商会换）。
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    # IPv6 字面量形如 [::1]:port
    if host.startswith("["):
        return host.startswith("[::1]")
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname in {"127.0.0.1", "localhost"}


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
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    origin_host = (parts.hostname or "").lower()
    if not host_allowed(origin_host):
        return False
    # 端口必须一致：这正是跨端口场景要挡住的东西。
    request_host = _header_value(scope, b"host").strip().lower()
    request_hostname = request_host.rsplit(":", 1)[0] if ":" in request_host else request_host
    request_port = request_host.rsplit(":", 1)[1] if ":" in request_host else ""
    return origin_host == request_hostname and str(parts.port or "") == request_port


class LocalAccessMiddleware:
    """统一准入：所有 `/api/` 路径默认要求会话，公开路径显式白名单。

    非 API 路径（SPA 静态资源与页面回退）不拦——它们不含用户数据，
    拦截只会让用户看到白屏；页面拿到会话后自会带着 Cookie 调 API。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # 预检不带 Cookie 也不产生副作用，放行给 CORS 中间件应答。
        if scope["method"] == "OPTIONS" or not _is_api(path) or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if not host_allowed(_header_value(scope, b"host")):
            response = JSONResponse({"detail": "请求来源不被信任"}, status_code=421)
            await response(scope, receive, send)
            return

        if not origin_allowed(scope):
            response = JSONResponse({"detail": "请求来源与应用不同源"}, status_code=403)
            await response(scope, receive, send)
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
        session_token(),
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
    "origin_allowed",
    "issue_session",
    "reset_token",
    "session_token",
]
