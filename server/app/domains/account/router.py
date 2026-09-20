"""account 域路由：多账号 Steam 绑定（Cookie）与钱包余额。

路径 /api/v1/account/*（单数，与 wishlist 域的 /accounts 复数路由不冲突）。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

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

    新 SteamID = 新增账号；已有 SteamID = 该账号换绑 Cookie。均置为当前账号。
    绑定响应内同步试抓钱包（顶栏余额要立刻可见），随后**后台依次**拉全
    该账号数据（愿望单/已购 → 账单），见 `_post_bind_fetch`。
    """
    raw = (payload.cookies or "").strip()
    if not raw:
        raise HTTPException(400, "Cookie 内容为空")
    if "steamLoginSecure" not in raw:
        raise HTTPException(400, "Cookie 中缺少 steamLoginSecure，请复制登录后的完整 Cookie")

    try:
        result = await service.save_cookies(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # 注册愿望单追踪（游戏数/愿望单数计数来源；失败不阻断）
    await service.after_bind(result["steam_id"])

    # 立即试抓（失败不阻断保存：可能只是当前网络不通）
    sync = await service.sync_wallet(force=True)
    if not sync.get("ok"):
        logger.info("[account] Cookie 保存成功但首次抓取失败：%s", sync.get("error"))

    # 绑定后置全量拉取：后台任务**依次**抓全该账号数据，不阻塞绑定响应。
    # 只主账号触发账单同步会使用户看到「绑了但游戏库/账单不来」。依次口径：
    #   ① 愿望单 + 已购库（sync_account：差异入库 + 新增条目按自动价格链
    #      开关即时首爬，与 15min 定时同步同口径）；
    #   ② 账单（跟随**主账号**：bills 表无 steamid 维度，混流即数据污染，
    #      绑二号/切号不触发；主账号换绑时重拉一次）。
    steam_id = result["steam_id"]
    is_primary = steam_id == await service.get_primary_steam_id()

    async def _post_bind_fetch() -> None:
        try:
            from app.core.scheduler import price_auto_enabled
            from app.domains.wishlist import service as wishlist_service

            auto_crawl = await price_auto_enabled()
            r = await wishlist_service.sync_account(steam_id, auto_crawl=auto_crawl)
            logger.info(
                "[account] 绑定后同步 %s：愿望单新增 %s 款 / 已购新增 %s 款（活跃 %s）",
                steam_id, r.get("added"), r.get("addedOwned"), r.get("active"),
            )
        except Exception:  # noqa: BLE001 —— 失败由 15min 定时同步兜底
            logger.exception("[account] 绑定后愿望单/已购同步异常（定时任务将兜底）")

        if not is_primary:
            return
        try:
            from app.domains.bills import service as bills_service

            r = await bills_service.sync_bills(force=True)
            if r.get("ok"):
                logger.info(
                    "[account] 绑定后账单同步：history %s 行 / licenses %s 行",
                    r.get("historyRows"), r.get("licenseRows"),
                )
            elif r.get("status") != "no_cookie":
                logger.info("[account] 绑定后账单同步失败：%s", r.get("error"))
        except Exception:  # noqa: BLE001 —— 失败由 30min 定时同步兜底
            logger.exception("[account] 绑定后账单同步异常（定时任务将兜底）")

    asyncio.get_running_loop().create_task(_post_bind_fetch())

    status = await service.get_status()
    status["sync_error"] = "" if sync.get("ok") else sync.get("error", "未知错误")
    status["message"] = result.get("message", "")
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
