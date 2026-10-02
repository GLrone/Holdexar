"""settings 域路由："我"页面的后端（账户）。区服配置走 /api/v1/regions。"""
from __future__ import annotations

import asyncio

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
    # 每日自动备份开关（False=只留手动备份；启动补备同闸）
    backup_auto: bool | None = None
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
    backup_auto: bool = True
    update_notified: str = ""
    # 提示默认开（有新版该告诉用户）；静默自动更新默认关——后台悄悄
    # 下载 100MB+ 属于「用户没同意就不该做」的事，必须显式开启
    update_notify: bool = True
    update_auto: bool = False
    theme: str = "dark"


# 「自动抓取」页的六个内容源开关：payload 字段 → app_settings KV。
# fx_auto 沿用历史 key `crawl.auto_refresh_rates`（该闸原无任何写入点，
# 本端点是它的第一个写入口）。
_FETCH_SETTING_KEYS: dict[str, str] = {
    "epic_free": "fetch.epic_free",
    "hb_choice": "fetch.hb_choice",
    "boards": "fetch.boards",
    "bundle_counts": "fetch.bundle_counts",
    "fx_auto": "crawl.auto_refresh_rates",
    "fx_history": "fetch.fx_history",
}


class FetchSettingsPayload(BaseModel):
    epic_free: bool = True
    hb_choice: bool = True
    boards: bool = True
    bundle_counts: bool = True
    fx_auto: bool = True
    fx_history: bool = True
    # 价格刷新网格步长（小时）；夹取 1..72，默认 6
    price_interval_hours: int = 6
    # 目录层随价格更新：True = 每轮价格更新带上未关注的目录游戏
    # （特惠榜尾段恒随轮）；改动从下一轮生效，不打断在跑的轮
    catalog_refresh: bool = False


class FetchSettingsUpdate(BaseModel):
    epic_free: bool | None = None
    hb_choice: bool | None = None
    boards: bool | None = None
    bundle_counts: bool | None = None
    fx_auto: bool | None = None
    fx_history: bool | None = None
    price_interval_hours: int | None = None
    catalog_refresh: bool | None = None


@router.get("")
async def get_settings() -> SettingsPayload:
    steam_id = await service.get_value("account.steam_id", "")
    api_key = await service.get_secret_value("account.steam_api_key", "")
    onboarding_done = await service.get_value("ui.onboarding_done", False)
    auto_price = await service.get_value("crawl.auto_price", True)
    backup_auto = await service.get_value("backup.auto", True)
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
        backup_auto=bool(backup_auto),
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
                await service.set_secret_value("account.steam_api_key", key)

    if payload.onboarding_done is not None:
        await service.set_value("ui.onboarding_done", bool(payload.onboarding_done))

    if payload.auto_price is not None:
        await service.set_value("crawl.auto_price", bool(payload.auto_price))

    if payload.backup_auto is not None:
        await service.set_value("backup.auto", bool(payload.backup_auto))

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


async def _read_fetch_settings() -> FetchSettingsPayload:
    fields: dict[str, bool] = {}
    for field, key in _FETCH_SETTING_KEYS.items():
        fields[field] = bool(await service.get_value(key, True))
    payload = FetchSettingsPayload(**fields)
    try:
        payload.price_interval_hours = int(
            await service.get_value("crawl.price_interval_hours", 6)
        )
    except (TypeError, ValueError):
        pass
    payload.catalog_refresh = bool(
        await service.get_value("crawl.catalog_refresh", True)
    )
    return payload


@router.get("/fetch")
async def get_fetch_settings() -> FetchSettingsPayload:
    return await _read_fetch_settings()


@router.put("/fetch")
async def update_fetch_settings(payload: FetchSettingsUpdate) -> FetchSettingsPayload:
    for field, key in _FETCH_SETTING_KEYS.items():
        value = getattr(payload, field)
        if value is not None:
            await service.set_value(key, bool(value))
    if payload.price_interval_hours is not None:
        hours = max(1, min(72, int(payload.price_interval_hours)))
        await service.set_value("crawl.price_interval_hours", hours)
        # 立即生效：后台重锚下一格（网络探测不阻塞响应；调度器未跑时静默，
        # 首轮 job 的轮转重锚会按新步长收敛）
        try:
            from app.core import scheduler as core_scheduler

            if core_scheduler.scheduler.running:
                asyncio.create_task(
                    core_scheduler._reanchor_price_refresh("间隔调整")
                )
        except Exception:  # noqa: BLE001 —— 重锚触发失败不阻塞保存
            pass
    if payload.catalog_refresh is not None:
        # 只落偏好：价格轮每轮开工时现读，改动从下一轮生效
        await service.set_value("crawl.catalog_refresh", bool(payload.catalog_refresh))
    return await _read_fetch_settings()
