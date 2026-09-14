"""FastAPI 应用工厂：API 路由 + 前端静态托管（SPA fallback）。"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.app_info import APP_NAME
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import setup_logging
from app.core.scheduler import start_scheduler, stop_scheduler
from app.domains.account.router import router as account_router
from app.domains.alerts.router import router as alerts_router
from app.domains.bills.router import router as bills_router
from app.domains.bundles.router import router as bundles_router
from app.domains.crawl.router import router as crawl_router
from app.domains.family.router import router as family_router
from app.domains.games.router import router as games_router
from app.domains.metadata.router import router as metadata_router
from app.domains.proxies.router import router as proxies_router
from app.domains.rates.router import router as rates_router
from app.domains.redeem.router import router as redeem_router
from app.domains.regions.router import router as regions_router
from app.domains.settings.router import router as settings_router
from app.domains.system.router import router as system_router
from app.domains.wishlist.router import router as wishlist_router

logger = logging.getLogger(__name__)


async def _autostart_clash() -> None:
    """Clash 内核随服务自启（后台任务，不阻塞 lifespan）。

    有内置内核 + 已存 clash 订阅才拉起；下载失败自动回退本地缓存
    config.yaml——开机即有代理可用，proxy_first 不降级直连。
    """
    try:
        from app.domains.proxies import clash_manager, service as proxies_service

        settings = get_settings()
        detect = clash_manager.detect_kernel(settings.data_dir)
        if not detect["found"]:
            logger.info("未找到 Clash 内核，跳过自启")
            return
        subs = await proxies_service.list_subscriptions("clash")
        usable = [s for s in subs if not s.get("deprecated")]
        if not usable:
            if subs:
                logger.warning(
                    "Clash 订阅全部处于废弃状态（不可用节点超过 95%），跳过内核自启"
                )
            else:
                logger.info("无 Clash 订阅，跳过内核自启")
            return
        sub_url = usable[-1]["url"]
        try:
            meta = await clash_manager.runtime.download_subscription(sub_url, settings.data_dir)
            config_path = meta["path"]
        except Exception as e:  # noqa: BLE001 —— 下载全败且无缓存时才放弃
            logger.warning("自启订阅下载失败：%s", e)
            return
        status = clash_manager.runtime.start(detect["path"], config_path)
        # 账本收敛：自启下载的是最新订阅内容，已下线/改名节点的旧行随启动清理
        # （订阅名不再自动回填——手动改名，见 proxies 域）
        try:
            from pathlib import Path

            names = clash_manager.parse_node_names(
                Path(config_path).read_text(encoding="utf-8", errors="ignore")
            )
            if names:
                pruned = await proxies_service.prune_clash_node_ledger(
                    usable[-1]["id"], set(names)
                )
                if pruned:
                    logger.info("[Clash自启] 账本收敛：删除 %d 个已下线节点行", pruned)
        except Exception:  # noqa: BLE001 —— 收敛失败不影响启动
            logger.warning("[Clash自启] 账本收敛跳过")
        logger.info("Clash 内核已随服务自启：port=%s", status.get("port"))
    except Exception:  # noqa: BLE001 —— 自启失败不阻塞服务
        logger.exception("Clash 内核自启失败（不阻塞服务）")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.data_dir)
    await init_db()

    # 资产种子并入（随包公共数据：汇率档案 + games 人工策划列；无种子文件时零开销）
    from app.core.seed_assets import import_seed, merge_seed_incremental

    try:
        await import_seed()
    except Exception:  # noqa: BLE001
        logger.exception("资产种子导入失败（不阻塞启动）")

    # 增量种子合并（按种子版本对全体用户生效，子通道各自独立 marker 幂等）：
    # 人工列名单（XGP/Epic/HB/系列：缺行落 games 行，行带非空 updated_at
    # 永不进孤儿补抓——监控只由愿望单驱动，名单 ≠ 监控）+ 价格历史切片
    await merge_seed_incremental()

    # 家庭库后台预热：实时聚合要逐成员调 Steam HTTPS（代理、秒级起步），
    # 不预热的话每次启动后的首次打开都要干等。cached_family_library 自带
    # 「快照优先 + 后台拉新」语义，这里只是把它提前到启动时触发；失败
    # （无快照且实时不可用）只记日志，不影响启动。
    from app.domains.family import service as family_service

    async def _warm_family_library() -> None:
        try:
            await family_service.cached_family_library()
        except Exception:  # noqa: BLE001
            logger.info("[family] 启动预热失败（无快照且实时聚合不可用），跳过")

    asyncio.create_task(_warm_family_library())

    from app.domains.crawl.service import cleanup_orphan_jobs
    await cleanup_orphan_jobs()

    # 汇率白名单清洗：41 区货币集（+ TRY/ARS 预留）之外的历史币种数据清除
    from app.domains.rates import service as rates_service

    try:
        await rates_service.cleanup_disallowed()
    except Exception:  # noqa: BLE001
        logger.exception("汇率白名单清洗失败（不阻塞启动）")

    # 旧 Clash 订阅设置 → 订阅表（一次性）
    from app.domains.proxies import service as proxies_service

    try:
        await proxies_service.migrate_legacy_subscription()
    except Exception:  # noqa: BLE001
        logger.exception("旧订阅迁移失败（不阻塞启动）")

    # 旧单账号 Cookie KV → steam_accounts 多账号表（一次性）
    from app.domains.account import service as account_service

    try:
        migrated = await account_service.migrate_legacy_kv()
        if migrated:
            logger.info("旧单账号 Cookie 已迁移至多账号表（%d 个）", migrated)
    except Exception:  # noqa: BLE001
        logger.exception("旧单账号 Cookie 迁移失败（不阻塞启动）")

    # 史低标记 + 永降标记 + 排序缓存预计算列全库初始化（秒级；爬取后另有增量刷新）
    from app.domains.games import service as games_service

    try:
        refreshed = await games_service.refresh_hl_flags()
        logger.info("史低标记初始化完成：%d 款", refreshed)
    except Exception:  # noqa: BLE001
        logger.exception("史低标记初始化失败（不阻塞启动）")
    try:
        refreshed = await games_service.refresh_pp_flags()
        logger.info("永降标记初始化完成：%d 款", refreshed)
    except Exception:  # noqa: BLE001
        logger.exception("永降标记初始化失败（不阻塞启动）")
    try:
        refreshed = await games_service.refresh_sort_cache()
        logger.info("排序缓存初始化完成：%d 款", refreshed)
    except Exception:  # noqa: BLE001
        logger.exception("排序缓存初始化失败（不阻塞启动）")

    # 随包内核就位：mihomo 与 GeoIP 数据随发行包分发，复制进 data/clash/
    # （只补缺失文件）。必须早于内核自启——自启与代理策略都以内核就位为前提。
    # 失败不阻断启动：前端「内核缺失」态与自动安装入口仍可兜底。
    from app.domains.proxies import clash_manager

    try:
        installed = clash_manager.ensure_kernel(get_settings().data_dir)
        if installed["copied"]:
            logger.info("随包 Clash 内核就位：%s", ", ".join(installed["copied"]))
    except Exception:  # noqa: BLE001
        logger.exception("随包 Clash 内核就位失败（不阻塞启动）")

    # Clash 内核随服务自启（常驻后台语义）：有内核 + 有 clash 订阅即拉起。
    # 服务重启后 proxy_first 策略才不会降级直连（Steam 域直连基本不可用）。
    async def _autostart_and_health_check() -> None:
        await _autostart_clash()
        # 启动体检（6h 门槛内跳过；本地软件不常驻，重启即检查点是设计语义）
        from app.domains.proxies import service as proxies_service

        try:
            state = await proxies_service.maybe_run_clash_health_check()
            if state == "checked":
                logger.info("[启动体检] Clash 节点检测完成")
        except Exception:  # noqa: BLE001
            logger.exception("[启动体检] Clash 节点检测失败（不阻塞启动）")

    asyncio.create_task(_autostart_and_health_check())

    # 汇率启动兜底：错过每日 03:00 定点（关机/服务重启）时按快照龄补刷新，
    # 保证"每日自动抓取"承诺不因服务频繁重启落空（内含 >12h 阈值，幂等安全）
    async def _rates_startup_check() -> None:
        try:
            await rates_service.refresh_if_stale()
        except Exception:  # noqa: BLE001
            logger.exception("[启动] 汇率过期检查失败（不阻塞启动）")
        # 历史缺口补齐：建档以来错过的交易日按上一交易日值延续补行
        # （幂等，零缺口零写入；关机漏刷的日子在这里补上）
        try:
            stats = await rates_service.backfill_history()
            if stats["inserted"]:
                logger.info("[启动] 汇率历史缺口补齐：插入 %d 行", stats["inserted"])
        except Exception:  # noqa: BLE001
            logger.exception("[启动] 汇率历史缺口补齐失败（不阻塞启动）")

    asyncio.create_task(_rates_startup_check())

    start_scheduler()
    # 数据目录布局入日志：本机支持多布局（系统数据目录 / 便携 / 存量便携），
    # 「数据跑哪去了」是最高频的排查问题，启动即亮明。
    from app.core.paths import LAYOUT_LABEL, describe_layout

    layout = describe_layout(settings.data_dir)
    logger.info(
        "%s %s 启动：data=%s（%s）port=%d",
        APP_NAME,
        settings.version,
        settings.data_dir,
        LAYOUT_LABEL.get(layout, layout),
        settings.port,
    )
    yield
    stop_scheduler()
    logger.info("%s 已停止", APP_NAME)


# vite 构建产物带 8 位内容指纹；public/ 拷贝物（logo、奖杯、flags）与构建产物
# 同在 dist 下但**无指纹**，两者缓存策略必须分开。
_FINGERPRINTED_NAME = re.compile(r"-[A-Za-z0-9_-]{8}\.\w+$")
_STATIC_SUFFIXES = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".otf", ".map",
}


def _cache_control_for(path: str) -> str | None:
    """按请求路径给静态资源定 Cache-Control；API/文档路径返回 None 不干预。

    - 带指纹产物：内容变更即换名 → 一年 immutable，浏览器零协商
    - 无指纹公共件（logo/奖杯/国旗/字体）：文件名不变，只能靠时长 + ETag
      协商兜底 → 一天
    - 其余（index.html、SPA 路由回退、splash 等动态 HTML）：no-cache，
      每次回源校验——前端发新版后旧壳必须立刻看到新 index
    """
    if path.startswith(("api/", "docs", "redoc", "openapi.json")):
        return None
    name = path.rsplit("/", 1)[-1]
    if name and "." in name and f".{name.rsplit('.', 1)[-1].lower()}" in _STATIC_SUFFIXES:
        if _FINGERPRINTED_NAME.search(name):
            return "public, max-age=31536000, immutable"
        return "public, max-age=86400"
    return "no-cache"


def _mount_spa(app: FastAPI, dist) -> None:
    """托管 Vue 构建产物：/assets 走静态，其余非 API 路径回退 index.html。"""
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="spa-assets")

    # 启动等待页（桌面壳窗口首载，JS 轮询 health 就绪即跳转真实应用）。
    # 同源路由而非 data-URL：data: 源向 127.0.0.1 发 fetch 会被 WebView2
    # 跨源策略拦截（双击冒烟实证等待页卡死）。
    from fastapi.responses import HTMLResponse

    @app.get("/__splash", include_in_schema=False)
    async def splash() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{APP_NAME}</title>"
            "<style>html,body{height:100%;margin:0}"
            "body{display:flex;align-items:center;justify-content:center;"
            "background:#1b2838;color:#c7d5e0;"
            "font:15px/1.8 'Segoe UI',system-ui,sans-serif}"
            ".box{text-align:center}"
            ".spin{width:34px;height:34px;margin:0 auto 16px;border-radius:50%;"
            "border:3px solid rgba(199,213,224,.2);border-top-color:#66c0f4;"
            "animation:r .9s linear infinite}"
            "@keyframes r{to{transform:rotate(360deg)}}"
            "</style></head><body><div class='box'><div class='spin'></div>"
            f"{APP_NAME} 启动中，请稍候…</div>"
            "<script>(function poll(){"
            "fetch('api/v1/health').then(function(r){"
            "if(r.ok){location.replace('/')}else{throw 0}"
            "}).catch(function(){setTimeout(poll,100)})"
            "})()</script></body></html>"
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            raise HTTPException(status_code=404)
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.data_dir)

    # 数据目录落在仓库工作树内 → 每次启动都告警。
    # 位置在 setup_logging 之后、lifespan（含 init_db 迁移）之前：告警要能在
    # 「数据正在被写进 git 仓库」这件事继续发生之前就被人看到。create_app 是所有
    # 启动方式（普通 / --server / 桌面壳）的唯一入口，故只需在这里发一次。
    from app.core.paths import layout_warning

    warning = layout_warning(settings.data_dir)
    if warning:
        logger.warning(warning)

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _add_static_cache_headers(request, call_next):
        response = await call_next(request)
        cache_control = _cache_control_for(request.url.path.lstrip("/"))
        if cache_control:
            response.headers.setdefault("Cache-Control", cache_control)
        return response

    app.include_router(system_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(games_router, prefix="/api/v1")
    app.include_router(metadata_router, prefix="/api/v1")
    app.include_router(wishlist_router, prefix="/api/v1")
    app.include_router(crawl_router, prefix="/api/v1")
    app.include_router(proxies_router, prefix="/api/v1")
    app.include_router(alerts_router, prefix="/api/v1")
    app.include_router(rates_router, prefix="/api/v1")
    app.include_router(regions_router, prefix="/api/v1")
    app.include_router(account_router, prefix="/api/v1")
    app.include_router(bundles_router, prefix="/api/v1")
    app.include_router(bills_router, prefix="/api/v1")
    app.include_router(family_router, prefix="/api/v1")
    app.include_router(redeem_router, prefix="/api/v1")

    dist = settings.web_dist_dir
    if (dist / "index.html").is_file():
        _mount_spa(app, dist)
    else:
        logger.warning("前端构建产物缺失（web/dist），当前仅提供 API。构建：cd web && npm run build")
    return app


app = create_app()
