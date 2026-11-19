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

import hmac
import logging
import secrets
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app.core import access, lan_access
from app.core.logging import log_event
from app.domains.account.login import LoginError, cancel_login, login_status, recovery_steam_id, set_recovery_guard, start_login, submit_guard_code

logger = logging.getLogger(__name__)

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
    # 只允许站内相对路径，避免变成开放跳转。反斜杠会被浏览器归一为正斜杠
    # （/\evil.example 绕过 // 前缀检查），控制字符会被 URL 解析器剥除
    # （/\t/evil.example 绕过前缀检查），一并拒绝（P3 纵深加固）。
    safe_next = (
        next.startswith("/")
        and not next.startswith(("//", "/\\"))
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in next)
    )
    target = next if safe_next else "/"
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
    # 来源必须回环（对端地址判定）：dev 通道是全权会话发放，信任判定不能
    # 交给 Host 头（客户端可伪造；P0——局域网设备伪造 Host: 127.0.0.1 仍可
    # 直换全权会话）。Vite 调试经本机回环代理，对端恒为回环，不受影响。
    if not access.peer_is_loopback(request.scope):
        raise HTTPException(status_code=404, detail="Not Found")
    log_event(
        __import__("logging").getLogger(__name__),
        "开发态会话已发放（发行版无此入口）",
        level=__import__("logging").WARNING,
    )
    response = JSONResponse({"ok": True})
    access.issue_session(response)
    return response


class _UnlockBody(BaseModel):
    password: str


async def resolve_lan_password_verifier() -> str:
    """解锁验证器解析（P0-1 终态：访问密码以 PHC 哈希存储，不可逆）。

    优先级：用户设置口令（哈希 KV）→ 环境口令（无窗口运维兜底）→ 旧
    可逆密文一次性迁移（解密→重哈希→清密文）。用户口令恒优先于环境
    口令——界面改密即吊销运维通道，不出现「设置成功却不生效」的静默
    遮蔽。读不到任何验证器返回空串——解锁拒绝并提示先在本机设置密码，
    服务端自始至终不掌握明文。
    """
    from app.domains.settings import service as settings_service

    phc = await settings_service.get_value(settings_service.LAN_PASSWORD_HASH_KEY, "")
    if phc:
        return phc
    env_pw = lan_access.env_password()
    if env_pw:
        return lan_access.hash_password(env_pw)
    legacy = await settings_service.get_secret_value(settings_service.LAN_PASSWORD_KEY, "")
    if not legacy:
        return ""  # 解密失败（keyring 锁定）已在 get_secret_value 内降级为无凭据
    phc = lan_access.hash_password(str(legacy))
    await settings_service.set_value(settings_service.LAN_PASSWORD_HASH_KEY, phc)
    await settings_service.delete_value(settings_service.LAN_PASSWORD_KEY)
    return phc


@router.post("/session/unlock")
async def session_unlock(body: _UnlockBody, request: Request):
    """局域网自助解锁：访问密码换会话 Cookie（仅局域网模式存在此入口）。

    密码常数时间比较；限速按来源对端分桶（单全局计数器会让任一恶意
    设备把合法设备的解锁一并锁死），连续失败达阈值后锁定该对端。
    """
    if not lan_access.is_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    peer = lan_access.peer_key(request.scope)
    locked = lan_access.unlock_locked_seconds(peer)
    if locked > 0:
        raise HTTPException(status_code=429, detail=f"尝试过于频繁，请 {int(locked) + 1} 秒后再试")
    verifier = await resolve_lan_password_verifier()
    if not verifier:
        lan_access.unlock_record_failure(peer)
        raise HTTPException(status_code=401, detail="访问密码未设置，请先在本机桌面端设置访问密码")
    try:
        matched = lan_access.verify_password(body.password.strip(), verifier)
    except ValueError:
        matched = False  # 验证器被篡改（参数/长度非法）：按失败计并计入限速
    if not matched:
        lan_access.unlock_record_failure(peer)
        raise HTTPException(status_code=401, detail="访问密码不正确")
    lan_access.unlock_record_success(peer)
    response = JSONResponse({"ok": True})
    access.issue_session(response)
    return response


@router.post("/session/logout", status_code=204)
async def session_logout(request: Request):
    access.revoke_session(request.scope)
    response = Response(status_code=204)
    response.delete_cookie(access.SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


# 信任锚 = Steam 官方认证产生的登录身份 ∈ 数据库绑定账号集合（登录流程在
# 绑定落库前经一次性守卫校验，未绑定账号不落库不建登录态）。通过后签发
# 本机会话，用户随后在设置页修改局域网访问密码。
# 攻击面控制：五个端点全部公开可达（找回发生在锁定态、无会话），共用
# 局域网解锁限速器——同一条口令防线，任一通道穷举都触发全局锁定。

class _RecoveryStartBody(BaseModel):
    account: str
    password: str


class _RecoveryCodeBody(BaseModel):
    code: str


_recovery_last_cancel = time.monotonic() - 1.0


async def _bound_account_check(steam_id: str) -> None:
    """找回守卫：登录身份必须是已绑定账号，否则拒绝（不落库）。"""
    from app.domains.account import service as account_service

    bound = {sid for sid, _ in await account_service.list_bound_cookies()}
    if steam_id not in bound:
        raise LoginError("该 Steam 账号未绑定到本应用，无法用于找回访问密码")


@router.post("/session/recovery/login/start")
async def recovery_login_start(body: _RecoveryStartBody, request: Request):
    locked = lan_access.unlock_locked_seconds(lan_access.peer_key(request.scope))
    if locked > 0:
        return {"ok": False, "error": f"尝试次数过多，{int(locked) + 1} 秒后可重试"}
    set_recovery_guard(_bound_account_check)
    return await start_login(body.account.strip(), body.password)


@router.post("/session/recovery/login/code")
async def recovery_login_code(body: _RecoveryCodeBody):
    return await submit_guard_code(body.code)


@router.get("/session/recovery/login/status")
async def recovery_login_status():
    return login_status()


@router.post("/session/recovery/cancel")
async def recovery_cancel():
    # 冷却 1 秒：免认证端点不可被无成本高频重置（打断进行中的找回）
    global _recovery_last_cancel
    if time.monotonic() - _recovery_last_cancel < 1.0:
        return {"ok": True}
    _recovery_last_cancel = time.monotonic()
    return cancel_login()


@router.post("/session/recovery/complete")
async def recovery_complete(request: Request):
    locked = lan_access.unlock_locked_seconds(lan_access.peer_key(request.scope))
    if locked > 0:
        return {"ok": False, "error": f"尝试次数过多，{int(locked) + 1} 秒后可重试"}
    _steam_id, done = recovery_steam_id()
    if not done:
        return {"ok": False, "error": "Steam 登录流程未完成"}
    response = JSONResponse({"ok": True})
    access.issue_session(response)
    lan_access.unlock_record_success(lan_access.peer_key(request.scope))
    log_event(logger, "找回完成：Steam 登录身份通过绑定核验，已签发会话", tag="成功")
    return response
