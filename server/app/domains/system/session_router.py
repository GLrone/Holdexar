"""本地会话发放：一次性引导票据 + 会话 Cookie。

页面（桌面 WebView / 开发态浏览器）本身无法凭空拿到会话令牌——令牌只
在进程内存里。这里提供两条受控通道：

- **桌面壳**：与后端同进程，直接调用 `create_bootstrap()` 拿一次性票据，
  再把窗口导航到 `GET /api/v1/session/bootstrap?nonce=…`。服务端校验后
  写 HttpOnly Cookie 并跳回首页。票据单次有效、短 TTL，进程外无法预测。
- **开发态**（仅非打包态）：`GET /api/v1/session/dev` 直接发放 Cookie。
  开发态同机同用户本来就共享文件与进程，令牌放内存挡不住本机进程；把
  这条通道限定在非打包态并在日志里明示，避免开发体验与生产安全互相拖累。

打包态下 dev 通道返回 404——发行版没有任何「本机进程可自助取会话」的入口。
"""
from __future__ import annotations

import secrets
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import access
from app.core.logging import log_event

router = APIRouter(tags=["session"])

# 引导票据：nonce → 过期时刻（单调时钟）。单次使用，取出即删。
_BOOTSTRAP_TTL_S = 60.0
_bootstrap: dict[str, float] = {}


def create_bootstrap() -> str:
    """生成一次性引导票据（桌面壳调用；进程内传递，不经网络）。"""
    now = time.monotonic()
    for key, expiry in list(_bootstrap.items()):
        if expiry <= now:
            _bootstrap.pop(key, None)
    nonce = secrets.token_urlsafe(32)
    _bootstrap[nonce] = now + _BOOTSTRAP_TTL_S
    return nonce


def _consume_bootstrap(nonce: str) -> bool:
    expiry = _bootstrap.pop(nonce, None)
    return expiry is not None and expiry > time.monotonic()


def reset_bootstrap() -> None:
    """清空未消费票据（测试隔离用）。"""
    _bootstrap.clear()


def is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


@router.get("/session/bootstrap")
async def session_bootstrap(nonce: str, next: str = "/"):
    """一次性票据换会话 Cookie，随后跳回应用首页。"""
    if not _consume_bootstrap(nonce):
        raise HTTPException(status_code=401, detail="引导票据无效或已过期")
    # 只允许站内相对路径，避免变成开放跳转。
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    access.issue_session(response)
    return response


@router.get("/session/dev")
async def session_dev(request: Request):
    """开发态会话发放（打包态不存在）。

    开发态下本机同用户进程本就共享一切，把这条通道关掉只会让 Vite 调试
    无法进行，而不会提高实际安全性；打包态则完全没有这条入口。
    """
    if is_packaged():
        raise HTTPException(status_code=404, detail="Not Found")
    from fastapi.responses import JSONResponse

    log_event(
        __import__("logging").getLogger(__name__),
        "开发态会话已发放（发行版无此入口）",
        level=__import__("logging").WARNING,
    )
    response = JSONResponse({"ok": True})
    access.issue_session(response)
    return response
