"""account 域路由：多账号 Steam 绑定（Cookie）与钱包余额。

路径 /api/v1/account/*（单数，与 wishlist 域的 /accounts 复数路由不冲突）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import login, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])


class SteamCookiesPayload(BaseModel):
    cookies: str


class ActiveAccountPayload(BaseModel):
    steam_id: str


class AccountStatus(BaseModel):
    has_cookie: bool
    cookie_steam_id: str
    bound_steam_id: str
    mismatch: bool
    profile: dict | None = None
    wallet: dict | None = None
    sync_error: str = ""
    message: str = ""
    accounts: list[dict] = []
    primary_steam_id: str = ""
    # 登录态：has_cookie 只表示"绑过"，会话是否仍可用看下面三项
    session_expires_at: str | None = None
    session_expired: bool = False
    session_has_refresh: bool = False
    # 当前账号 Steam 真实在线状态（顶栏头像 dot 数据源）
    is_online: bool = False
    in_game: str = ""


@router.get("", response_model=AccountStatus)
async def get_status() -> AccountStatus:
    # 活跃心跳：前端 60s 轮询此端点 = "有人在看"。钱包轮转按此降频
    #（无人看时快照阈值自动爬升到 30min，请求量砍 ~90%）
    service.mark_seen()
    return AccountStatus(**await service.get_status())


@router.get("/list")
async def list_accounts() -> list[dict]:
    """多账号列表（绑定顺序，第一个即主账号；不含 Cookie 明文）。"""
    return await service.list_accounts()


@router.put("/cookies", response_model=AccountStatus)
async def save_cookies(payload: SteamCookiesPayload) -> AccountStatus:
    """绑定 / 换绑 Cookie（upsert 账号 + 置当前账号）并拉起全量数据拉取。

    完整绑定链见 `service.bind_account`（落库 → 追踪注册 → 立即试抓钱包
    → 后台全量拉取），与账号密码登录共用。
    """
    raw = (payload.cookies or "").strip()
    if not raw:
        raise HTTPException(400, "Cookie 内容为空")
    if "steamLoginSecure" not in raw:
        raise HTTPException(400, "Cookie 中缺少 steamLoginSecure，请复制登录后的完整 Cookie")

    try:
        bound = await service.bind_account(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    status = await service.get_status()
    status["sync_error"] = (
        "" if bound["sync"].get("ok") else bound["sync"].get("error", "未知错误")
    )
    status["message"] = bound["result"].get("message", "")
    return AccountStatus(**status)


@router.put("/active", response_model=AccountStatus)
async def set_active(payload: ActiveAccountPayload) -> AccountStatus:
    """切换当前账号（钱包展示 / CDK 激活 / 免费领取跟随）。"""
    try:
        await service.set_active(payload.steam_id.strip())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return AccountStatus(**await service.get_status())


@router.delete("/cookies/{steam_id}", response_model=AccountStatus)
async def remove_account(steam_id: str) -> AccountStatus:
    """删除指定账号；删除当前账号时回退到剩余首个。全删后回未绑定态。"""
    try:
        await service.remove_account(steam_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return AccountStatus(**await service.get_status())


@router.delete("/cookies", response_model=AccountStatus)
async def clear_cookies() -> AccountStatus:
    """解绑全部账号（清账号表；手填 SteamID64 / API Key 不动）。"""
    await service.clear_cookies()
    return AccountStatus(**await service.get_status())


@router.post("/sync", response_model=AccountStatus)
async def sync_wallet() -> AccountStatus:
    """立即抓取当前账号钱包余额（顶栏手动刷新 / 设置页同步按钮）。"""
    sync = await service.sync_wallet(force=True)
    status = await service.get_status()
    status["sync_error"] = "" if sync.get("ok") else sync.get("error", "未知错误")
    return AccountStatus(**status)


# ── 应用内账号密码登录（Steam 认证 API 通道，见 login.py） ─────────────────


class LoginStartPayload(BaseModel):
    account_name: str
    password: str


class LoginCodePayload(BaseModel):
    code: str


@router.post("/login/start")
async def login_start(payload: LoginStartPayload) -> dict:
    """发起账号密码登录；成功后经二次验证与轮询自动完成绑定（登录即生效）。"""
    result = await login.start_login(payload.account_name, payload.password)
    if result.get("busy"):
        raise HTTPException(409, result.get("error", "已有登录进行中"))
    return result


@router.get("/login/status")
async def login_status() -> dict:
    """当前登录会话状态快照（前端轮询）。"""
    return login.login_status()


@router.post("/login/code")
async def login_code(payload: LoginCodePayload) -> dict:
    """提交 Steam Guard 验证码（手机令牌或邮箱验证码）。"""
    result = await login.submit_guard_code(payload.code)
    if not result.get("ok") and result.get("state", {}).get("state") != login.STATE_AWAITING_CODE:
        raise HTTPException(409, result.get("error", "当前没有等待验证码的登录"))
    return result


@router.post("/login/cancel")
async def login_cancel() -> dict:
    """取消当前登录会话。"""
    return login.cancel_login()
