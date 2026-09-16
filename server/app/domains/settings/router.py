"""settings 域路由："我"页面的后端（账户）。区服配置走 /api/v1/regions。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from . import service

router = APIRouter(prefix="/settings", tags=["settings"])


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return "****" + key[-4:] if len(key) > 4 else "****"


class AccountPayload(BaseModel):
    steam_id: str | None = None
    steam_api_key: str | None = None


class SettingsUpdate(BaseModel):
    account: AccountPayload | None = None
    # 新手教程完成标志（首次启动自动弹出；完成后落 KV 不再弹）
    onboarding_done: bool | None = None
    # 自动价格链总开关（False=关停定时爬价+失败修复，只留手动爬取）
    auto_price: bool | None = None
    # 已主动提示过的版本号（启动告知「一次一版本」的去重锚点）
    update_notified: str | None = None
    # 新版本提示总开关（False=有新版也不弹窗/不亮侧栏红点，只留手动检查）
    update_notify: bool | None = None
    # 静默自动更新开关（True=检测到新版本后台自动下载校验，不打扰；
    # 下次启动应用时桌面壳消费暂存目录自动换装）
    update_auto: bool | None = None
    # 主题镜像（dark/light）：网页主题存 localStorage，桌面壳读不到——
    # 前端 apply() 每次 apply/toggle 都镜像一份到这里，关闭弹窗（独立
    # WinForms 窗）按它跟随主题。
    theme: str | None = None


class SettingsPayload(BaseModel):
    account: dict
    onboarding_done: bool = False
    auto_price: bool = True
    update_notified: str = ""
    # 提示默认开（有新版该告诉用户）；静默自动更新默认关——后台悄悄
    # 下载 100MB+ 属于「用户没同意就不该做」的事，必须显式开启
    update_notify: bool = True
    update_auto: bool = False
    theme: str = "dark"


@router.get("")
async def get_settings() -> SettingsPayload:
    steam_id = await service.get_value("account.steam_id", "")
    api_key = await service.get_value("account.steam_api_key", "")
    onboarding_done = await service.get_value("ui.onboarding_done", False)
    auto_price = await service.get_value("crawl.auto_price", True)
    update_notified = await service.get_value("ui.update_notified", "")
    update_notify = await service.get_value("ui.update_notify", True)
    update_auto = await service.get_value("ui.update_auto", False)
    theme = await service.get_value("ui.theme", "dark")
    return SettingsPayload(
        account={
            "steam_id": steam_id or "",
            "steam_api_key": _mask_key(api_key or ""),
            "has_api_key": bool(api_key),
        },
        onboarding_done=bool(onboarding_done),
        auto_price=bool(auto_price),
        update_notified=str(update_notified or ""),
        update_notify=bool(update_notify),
        update_auto=bool(update_auto),
        theme=str(theme or "dark"),
    )


@router.put("")
async def update_settings(payload: SettingsUpdate) -> SettingsPayload:
    if payload.account:
        if payload.account.steam_id is not None:
            await service.set_value(
                "account.steam_id", payload.account.steam_id.strip()
            )
        if payload.account.steam_api_key is not None:
            key = payload.account.steam_api_key.strip()
            # 前端掩码回显（****xxxx）不回写，避免把掩码存成真值
            if key and not key.startswith("****"):
                await service.set_value("account.steam_api_key", key)

    if payload.onboarding_done is not None:
        await service.set_value("ui.onboarding_done", bool(payload.onboarding_done))

    if payload.auto_price is not None:
        await service.set_value("crawl.auto_price", bool(payload.auto_price))

    if payload.update_notified is not None:
        await service.set_value("ui.update_notified", payload.update_notified.strip())

    if payload.update_notify is not None:
        await service.set_value("ui.update_notify", bool(payload.update_notify))

    if payload.update_auto is not None:
        await service.set_value("ui.update_auto", bool(payload.update_auto))

    if payload.theme is not None:
        # 主题镜像只认两值：异常值忽略（防脏数据把弹窗配色带歪）
        if payload.theme in ("dark", "light"):
            await service.set_value("ui.theme", payload.theme)

    return await get_settings()
