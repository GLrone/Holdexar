"""settings 域路由："我"页面的后端（账户）。区服配置走 /api/v1/regions。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.core import access, lan_access
from . import service
from . import steam_local

router = APIRouter(prefix="/settings", tags=["settings"])


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return "****" + key[-4:] if len(key) > 4 else "****"


class AccountPayload(BaseModel):
    steam_id: str | None = None
    steam_api_key: str | None = None


class LocalSteamAccountImport(BaseModel):
    steam_id: str = Field(min_length=17, max_length=17)


class LanAccessUpdate(BaseModel):
    enabled: bool | None = None
    # 空串不改动（自动生成机制已随 PHC 化移除）；**** 前缀的掩码回显不回写
    password: str | None = None


class LanAccessState(BaseModel):
    enabled: bool
    # 环境变量强制开启时设置页开关只是显示值，关闭不生效
    env_forced: bool = False
    has_password: bool = False
    # 生效口令的来源：user=设置页口令（恒优先）、env=环境变量兜底、none=未设置
    password_source: str = "none"
    # 密码原文永不回显；设置/修改走独立输入端点，读取面只有 has_password 布尔


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
    # 用户明确跳过的版本号（严格等值匹配：跳过 A 不拦 A 之后的 B；
    # 手动检查不受它限制——用户主动查就该看到结果）
    update_skipped: str | None = None
    # 新版本提示总开关（False=有新版也不弹窗/不亮侧栏红点，只留手动检查）
    update_notify: bool | None = None
    # 静默自动更新开关（True=检测到新版本后台自动下载校验，不打扰；
    # 下次启动应用时桌面壳消费暂存目录自动换装）
    update_auto: bool | None = None
    # 直连模式加速器建议弹窗已看过（首次使用直连自动弹一次）
    direct_notice_seen: bool | None = None
    # 主题镜像（dark/light）：网页主题存 localStorage，桌面壳读不到——
    # 前端 apply() 每次 apply/toggle 都镜像一份到这里，关闭弹窗（独立
    # WinForms 窗）按它跟随主题。
    theme: str | None = None
    # MCP 服务开关（外部 AI 接入面；写入即运行时启停，非重启生效）
    mcp_enabled: bool | None = None
    # 局域网访问模式（开关 + 访问密码；写入即生效，运行状态见 LanAccessState）
    lan_access: LanAccessUpdate | None = None


class SettingsPayload(BaseModel):
    account: dict
    onboarding_done: bool = False
    auto_price: bool = True
    backup_auto: bool = True
    update_notified: str = ""
    update_skipped: str = ""
    # 提示默认开（有新版该告诉用户）；静默自动更新默认关——后台悄悄
    # 下载 100MB+ 属于「用户没同意就不该做」的事，必须显式开启
    update_notify: bool = True
    update_auto: bool = False
    direct_notice_seen: bool = False
    theme: str = "dark"
    # MCP 面：KV 开关值 + 运行态（running=会话管理器在役；error 为空串=无故障）
    mcp_enabled: bool = False
    mcp_running: bool = False
    mcp_error: str = ""
    mcp_forced_by_env: bool = False
    lan_access: LanAccessState


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

# 开启内容源即补一拍首跑：键 → 对应的主拍任务。热门新品/即将推出是每日档
# 大抓取，不由本开关逐次补拍，跟随各自日节奏；榜单即时需求由每小时热销榜覆盖。
_SOURCE_KICK_JOBS: dict[str, tuple[str, ...]] = {
    "fetch.epic_free": ("epic_free",),
    "fetch.hb_choice": ("hb_choice",),
    "fetch.boards": ("board_topsellers",),
    "fetch.bundle_counts": ("bartervg_bundles",),
    "crawl.auto_refresh_rates": ("fx_refresh",),
    "fetch.fx_history": ("fx_history_repair",),
}


def _kick_job_ids_for_enabled_source(
    old_effective: dict[str, bool], new_values: dict[str, bool]
) -> list[str]:
    """收集 off→on 翻转的内容源键对应的补拍任务（旧值按「缺省=开」口径）。"""
    kicks: list[str] = []
    for key, jobs in _SOURCE_KICK_JOBS.items():
        if new_values.get(key) is True and not old_effective.get(key, True):
            kicks.extend(jobs)
    return kicks


class ContentSchedule(BaseModel):
    """内容源自动刷新节奏：时刻（HH:MM）+ 间隔天数（1=每天，上限 30）。"""

    time: str
    days: int = 1

    @field_validator("time")
    @classmethod
    def _normalize_time(cls, value: str) -> str:
        from app.core.scheduler import parse_schedule_time

        hour, minute = parse_schedule_time(value)
        return f"{hour:02d}:{minute:02d}"

    @field_validator("days")
    @classmethod
    def _clamp_days(cls, value: int) -> int:
        return max(1, min(30, int(value)))


def _default_schedule(job_id: str) -> ContentSchedule:
    from app.core.scheduler import CONTENT_SCHEDULE_DEFAULTS

    default = CONTENT_SCHEDULE_DEFAULTS[job_id]
    return ContentSchedule(time=default["time"], days=int(default["days"]))


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
    # 任务队列逐段开关（任务中心队列组成卡）：False = 该段不随轮组队，
    # 改动从下一轮组队生效；catalog 段沿用 catalog_refresh（特惠榜尾段
    # 跟随它，不设独立开关）
    queue_missing: bool = True
    queue_pool: bool = True
    queue_discounts: bool = True
    # 内容源自定义刷新节奏（自动抓取页；改动即时重排对应定时拍）
    hb_schedule: ContentSchedule = Field(default_factory=lambda: _default_schedule("hb_choice"))
    epic_schedule: ContentSchedule = Field(default_factory=lambda: _default_schedule("epic_free"))


class FetchSettingsUpdate(BaseModel):
    epic_free: bool | None = None
    hb_choice: bool | None = None
    boards: bool | None = None
    bundle_counts: bool | None = None
    fx_auto: bool | None = None
    fx_history: bool | None = None
    price_interval_hours: int | None = None
    catalog_refresh: bool | None = None
    queue_missing: bool | None = None
    queue_pool: bool | None = None
    queue_discounts: bool | None = None
    hb_schedule: ContentSchedule | None = None
    epic_schedule: ContentSchedule | None = None


async def _lan_state() -> LanAccessState:
    """局域网访问模式状态。密码原文永不回显；读取面只有 has_password 布尔，
    设置/修改走独立的输入端点。
    """
    stored_hash = await service.get_value(service.LAN_PASSWORD_HASH_KEY, "")
    stored = await service.get_secret_value(service.LAN_PASSWORD_KEY, "")
    if stored_hash or stored:
        source = "user"
    elif lan_access.env_password():
        source = "env"
    else:
        source = "none"
    return LanAccessState(
        enabled=lan_access.is_enabled(),
        env_forced=lan_access.env_forced(),
        # 哈希 KV（PHC 终态）/ 旧可逆密文（迁移前存量）/ 环境口令，任一存在即可解锁
        has_password=bool(stored_hash or stored or lan_access.env_password()),
        password_source=source,
    )


@router.get("")
async def get_settings(request: Request = None) -> SettingsPayload:
    steam_id = await service.get_value("account.steam_id", "")
    api_key = await service.get_secret_value("account.steam_api_key", "")
    onboarding_done = await service.get_value("ui.onboarding_done", False)
    auto_price = await service.get_value("crawl.auto_price", True)
    backup_auto = await service.get_value("backup.auto", True)
    update_notified = await service.get_value("ui.update_notified", "")
    update_skipped = await service.get_value("ui.update_skipped_version", "")
    update_notify = await service.get_value("ui.update_notify", True)
    update_auto = await service.get_value("ui.update_auto", False)
    direct_notice_seen = await service.get_value("ui.direct_notice_seen", False)
    theme = await service.get_value("ui.theme", "dark")
    from app.domains.agent import mcp as agent_mcp

    mcp_state = agent_mcp.status()
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
        update_skipped=str(update_skipped or ""),
        update_notify=bool(update_notify),
        update_auto=bool(update_auto),
        direct_notice_seen=bool(direct_notice_seen),
        theme=str(theme or "dark"),
        mcp_enabled=bool(await service.get_value("mcp.enabled", False)),
        mcp_running=bool(mcp_state["running"]),
        mcp_error=str(mcp_state["error"]),
        mcp_forced_by_env=bool(mcp_state["forcedByEnv"]),
        lan_access=await _lan_state(),
    )


@router.put("")
async def update_settings(
    payload: SettingsUpdate, request: Request = None, response: Response = None
) -> SettingsPayload:
    rotate_session = False
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

    if payload.update_skipped is not None:
        await service.set_value("ui.update_skipped_version", payload.update_skipped.strip())

    if payload.update_notify is not None:
        await service.set_value("ui.update_notify", bool(payload.update_notify))

    if payload.update_auto is not None:
        await service.set_value("ui.update_auto", bool(payload.update_auto))

    if payload.direct_notice_seen is not None:
        await service.set_value("ui.direct_notice_seen", bool(payload.direct_notice_seen))

    if payload.theme is not None:
        # 主题镜像只认两值：异常值忽略（防脏数据把弹窗配色带歪）
        if payload.theme in ("dark", "light"):
            await service.set_value("ui.theme", payload.theme)

    if payload.mcp_enabled is not None:
        from app.core.config import get_settings as get_app_settings
        from app.domains.agent import mcp as agent_mcp

        await service.set_value("mcp.enabled", bool(payload.mcp_enabled))
        await agent_mcp.reapply(get_app_settings().data_dir)

    if payload.lan_access is not None:
        if payload.lan_access.enabled is not None:
            await service.set_value(
                service.LAN_ENABLED_KEY, bool(payload.lan_access.enabled)
            )
            lan_access.set_enabled(bool(payload.lan_access.enabled))
        if payload.lan_access.password is not None:
            password = payload.lan_access.password.strip()
            if password == "":
                # 显式清空：可设可清对称；清密码即吊销全部解锁会话
                await service.delete_value(service.LAN_PASSWORD_HASH_KEY)
                await service.delete_value(service.LAN_PASSWORD_KEY)
                access.reset_token()
                rotate_session = True
            elif not password.startswith("****"):
                # 前端掩码回显不回写，避免把掩码存成真值；最短 8 位（防一字符
                # 口令让解锁防穷举形同虚设——P0 审计修复）
                if len(password) < 8:
                    raise HTTPException(status_code=422, detail="访问密码至少 8 位")
                # 用户口令恒优先于环境口令（界面改密即吊销运维通道），所以
                # 环境口令在场也算「改密前有验证器」——旧解锁会话一并吊销
                previous = bool(
                    await service.get_value(service.LAN_PASSWORD_HASH_KEY, "")
                ) or bool(await service.get_secret_value(service.LAN_PASSWORD_KEY, "")) or bool(
                    lan_access.env_password()
                )
                await service.set_value(
                    service.LAN_PASSWORD_HASH_KEY,
                    lan_access.hash_password(password),
                )
                await service.delete_value(service.LAN_PASSWORD_KEY)
                if previous:
                    # 改密即吊销旧解锁会话：本次响应重新签发（当前会话不中断），
                    # 其余持旧口令解锁的会话全部失效
                    access.reset_token()
                    rotate_session = True
        lan_access.refresh_extra_hosts()

    result = await get_settings(request)
    if rotate_session and response is not None:
        access.issue_session(response)
    return result


@router.get("/steam-local-accounts")
async def get_local_steam_accounts() -> dict[str, list[dict[str, str]]]:
    return {"accounts": steam_local.discover_local_steam_accounts()}


@router.post("/steam-local-accounts/import")
async def import_local_steam_account(payload: LocalSteamAccountImport) -> dict[str, str]:
    account = next(
        (
            candidate
            for candidate in steam_local.discover_local_steam_accounts()
            if candidate["steam_id"] == payload.steam_id
        ),
        None,
    )
    if account is None:
        raise HTTPException(status_code=404, detail="本机 Steam 账号记录已不可用")
    await service.set_value("account.steam_id", account["steam_id"])
    return account


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
    # 与 crawl.default_queue_specs 同一有效默认：直连形态下目录段默认不随
    # 价格轮出网，未显式设置时开关如实显示「关」（默认 direct_only，
    # 初次打开即直连）
    catalog_default = (
        False
        if (await service.get_value("proxy.strategy", "direct_only")) == "direct_only"
        else True
    )
    payload.catalog_refresh = bool(
        await service.get_value("crawl.catalog_refresh", catalog_default)
    )
    # 任务队列逐段开关：与 crawl.default_queue_specs 同源（KV 缺失默认开）
    for field in ("queue_missing", "queue_pool", "queue_discounts"):
        setattr(payload, field, bool(await service.get_value(f"crawl.{field}", True)))
    # 内容源自定义节奏：KV 缺失/非法逐项回落注册默认（同一套默认）
    from app.core.scheduler import CONTENT_SCHEDULE_KEYS, normalize_schedule

    for field, job_id in (("hb_schedule", "hb_choice"), ("epic_schedule", "epic_free")):
        raw = await service.get_value(CONTENT_SCHEDULE_KEYS[job_id], None)
        time_text, days = normalize_schedule(raw, job_id)
        setattr(payload, field, ContentSchedule(time=time_text, days=days))
    return payload


@router.get("/fetch")
async def get_fetch_settings() -> FetchSettingsPayload:
    return await _read_fetch_settings()


@router.put("/fetch")
async def update_fetch_settings(payload: FetchSettingsUpdate) -> FetchSettingsPayload:
    # off→on 补拍判定要用旧值：先按「缺省=开」口径读一轮有效旧值再写
    old_effective = {
        key: bool(await service.get_value(key, True))
        for key in set(_FETCH_SETTING_KEYS.values())
    }
    for field, key in _FETCH_SETTING_KEYS.items():
        value = getattr(payload, field)
        if value is not None:
            await service.set_value(key, bool(value))
    kicks = _kick_job_ids_for_enabled_source(
        old_effective,
        {
            key: bool(getattr(payload, field))
            for field, key in _FETCH_SETTING_KEYS.items()
            if getattr(payload, field) is not None
        },
    )
    if kicks:
        # 开源即补一拍首跑：不等下一个定时拍（改排失败不阻塞保存）
        try:
            from app.core import scheduler as core_scheduler

            if core_scheduler.scheduler.running:
                asyncio.create_task(core_scheduler.kick_source_jobs(kicks))
        except Exception:  # noqa: BLE001
            pass
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
    for field in ("queue_missing", "queue_pool", "queue_discounts"):
        value = getattr(payload, field)
        if value is not None:
            # 只落偏好：组队时现读，改动从下一轮生效
            await service.set_value(f"crawl.{field}", bool(value))
    # 内容源自定义节奏：落 KV + 调度在跑时立即重排（下次启动按 KV 再应用）
    from app.core.scheduler import CONTENT_SCHEDULE_KEYS

    for field, job_id in (("hb_schedule", "hb_choice"), ("epic_schedule", "epic_free")):
        cfg = getattr(payload, field)
        if cfg is None:
            continue
        await service.set_value(
            CONTENT_SCHEDULE_KEYS[job_id], {"time": cfg.time, "days": cfg.days}
        )
        try:
            from app.core import scheduler as core_scheduler

            if core_scheduler.scheduler.running:
                core_scheduler.apply_content_schedule(job_id, cfg.time, cfg.days)
        except Exception:  # noqa: BLE001 —— 调度器未起时由启动链按 KV 应用
            pass
    return await _read_fetch_settings()
