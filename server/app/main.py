"""FastAPI 应用工厂：API 路由 + 前端静态托管（SPA fallback）。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.app_info import APP_NAME, ENV_PREFIX
from app.core.access import LocalAccessMiddleware
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import log_event, setup_logging
from app.core.scheduler import start_scheduler, stop_scheduler
from app.domains.account.router import router as account_router
from app.domains.agent import mcp as agent_mcp
from app.domains.agent.router import router as agent_router
from app.domains.achievements.challenges_router import router as achievements_challenges_router
from app.domains.achievements.level_router import router as achievements_level_router
from app.domains.achievements.router import router as achievements_router
from app.domains.achievements.social_router import router as achievements_social_router
from app.domains.alerts.router import router as alerts_router
from app.domains.bills.router import router as bills_router
from app.domains.bundles.router import router as bundles_router
from app.domains.crawl.router import router as crawl_router
from app.domains.family.router import router as family_router
from app.domains.games.router import router as games_router
from app.domains.metadata.router import router as metadata_router
from app.domains.monitoring.router import router as monitoring_router
from app.domains.notifications.router import router as notifications_router
from app.domains.pilot.router import router as pilot_router
from app.domains.proxies.router import router as proxies_router
from app.domains.proxypool.router import router as proxypool_router
from app.domains.rates.router import router as rates_router
from app.domains.redeem.router import router as redeem_router
from app.domains.regions.router import router as regions_router
from app.domains.settings.router import router as settings_router
from app.domains.steam_events.router import router as steam_events_router
from app.domains.system.router import router as system_router
from app.domains.system.session_router import router as session_router
from app.domains.wishlist.router import router as wishlist_router

logger = logging.getLogger(__name__)


async def _initialize_runtime() -> None:
    from app.domains.account import service as accounts
    from app.domains.agent import service as agents
    from app.domains.crawl import service as crawl
    from app.domains.proxies import node_secret_files, service as proxies
    from app.domains.proxypool import jobruns

    try:
        await accounts.migrate_legacy_kv()
    except Exception:
        log_event(logger, "旧账号兼容迁移失败，保留现有数据", level=logging.ERROR, exc_info=True)
    try:
        await proxies.migrate_legacy_subscription()
    except Exception:
        log_event(logger, "旧订阅兼容迁移失败，保留现有数据", level=logging.ERROR, exc_info=True)
    try:
        await accounts.seal_credentials_at_rest()
    except Exception:
        log_event(logger, "账号凭据封存未完成，保留兼容读取", level=logging.ERROR, exc_info=True)
    try:
        await proxies.seal_subscription_urls()
    except Exception:
        log_event(logger, "订阅链接封存未完成，保留兼容读取", level=logging.ERROR, exc_info=True)
    try:
        await proxies.seal_proxy_credentials()
    except Exception:
        log_event(logger, "代理凭据封存未完成，保留兼容读取", level=logging.ERROR, exc_info=True)
    try:
        await proxies.maybe_revert_to_direct()
    except Exception:
        log_event(logger, "代理出口策略恢复未完成", level=logging.ERROR, exc_info=True)
    try:
        await asyncio.to_thread(node_secret_files.tighten_all, get_settings().data_dir)
        await asyncio.to_thread(node_secret_files.sweep_transient, get_settings().data_dir)
    except Exception:
        log_event(logger, "节点文件权限维护未完成", level=logging.WARNING, exc_info=True)
    for cleanup_fn in (crawl.cleanup_orphan_jobs, jobruns.record_interrupted, agents.reconcile_orphan_runs):
        try:
            await cleanup_fn()
        except Exception as error:
            log_event(logger, "遗留运行状态恢复未完成", level=logging.ERROR,
                      exc_info=(type(error), error, error.__traceback__))
    start_scheduler()
    try:
        from app.core.scheduler import apply_content_schedules_from_settings

        await apply_content_schedules_from_settings()
    except Exception:
        log_event(logger, "自定义刷新时间应用失败，继续使用默认时间", level=logging.ERROR, exc_info=True)


@asynccontextmanager
async def _application_lifespan(application: FastAPI):
    """结构与换装安全检查完成后开放HTTP，兼容恢复后启用正常调度。"""

    settings = get_settings()
    setup_logging(settings.data_dir)
    update_attempt = os.environ.get(f"{ENV_PREFIX}UPDATE_ATTEMPT_ID")
    from app.core import layout as update_layout, update_data

    with update_data.startup_authorization(settings.data_dir) as update_token:
        await init_db(maintenance_token=update_token)
    await agent_mcp.startup(settings.data_dir)
    application.state.update_verification = {
        "protected": bool(update_token), "pageReady": False,
    }

    from app.core import rekey

    async def resume_keys():
        try:
            await rekey.resume_pending_migration(settings.data_dir)
        except Exception:
            log_event(
                logger, "密钥换装收尾失败，保留过渡记录待下次续跑", level=logging.ERROR,
                exc_info=True,
            )

    if not update_token:
        await resume_keys()

    # 数据目录布局入日志：本机支持多布局（系统数据目录 / 便携 / 存量便携），
    # 「数据跑哪去了」是最高频的排查问题，启动即亮明。
    from app.core.paths import LAYOUT_LABEL, describe_layout

    layout = describe_layout(settings.data_dir)
    log_event(
        logger,
        f"{APP_NAME} {settings.version} 数据库已准备，HTTP服务正在启动",
        detail={
            "数据目录": settings.data_dir,
            "目录布局": LAYOUT_LABEL.get(layout, layout),
            "端口": settings.port,
        },
    )
    async def start_after_acceptance():
        if update_token:
            root = Path(os.environ[f"{ENV_PREFIX}INSTALL_ROOT"])
            while True:
                transaction = update_layout.read_transaction(root) or {}
                if (transaction.get("attemptId") == update_attempt
                        and transaction.get("dataToken") == update_token
                        and transaction.get("dataAccepted") is True):
                    from app.core.backup import ensure_restore_lock_available

                    try:
                        ensure_restore_lock_available(settings.data_dir / settings.db_filename)
                    except RuntimeError:
                        pass
                    else:
                        application.state.update_verification["protected"] = False
                        os.environ.pop(f"{ENV_PREFIX}UPDATE_ATTEMPT_ID", None)
                        os.environ.pop(f"{ENV_PREFIX}UPDATE_DATA_TOKEN", None)
                        break
                await asyncio.sleep(0.1)
            await resume_keys()
        await _initialize_runtime()

    startup_task = asyncio.create_task(start_after_acceptance())
    try:
        yield
    finally:
        startup_task.cancel()
        await asyncio.gather(startup_task, return_exceptions=True)
        await agent_mcp.shutdown()
        stop_scheduler()
        log_event(logger, f"{APP_NAME} 已停止")


@asynccontextmanager
async def lifespan(application: FastAPI):
    from app.core.instance import production_instance

    async with production_instance(get_settings().data_dir):
        async with _application_lifespan(application):
            yield


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

    # 启动等待页已移交桌面壳内联 HTML（desktop/main.py _SPLASH_HTML）：
    # uvicorn 在 lifespan 完成前不监听端口，后端同源等待页在窗口期必然
    # 连接被拒，等待逻辑只有放在不依赖网络的进程内页面才能成立。

    # 兜底必须接受全部 HTTP 方法：只挂 GET 时，打到未知 API 路径的 POST/PUT
    # 会以「路径匹配、方法不符」回 405，把「接口不存在」伪装成「方法不对」。
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
        include_in_schema=False,
    )
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
        log_event(logger, warning, level=logging.WARNING)

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 传输压缩。text/event-stream 在 Starlette 默认排除表内，crawl / pilot /
    # logs 三条 SSE 不被缓冲；图片、字体、压缩包同样排除。
    # compresslevel 取 6 而非默认 9：捆绑包全量列表是 50MB 级 JSON，9 下单次
    # 压缩为秒级 CPU，而压缩比只高约 4%——6 是压缩率与单请求 CPU 的平衡点。
    app.add_middleware(GZipMiddleware, compresslevel=6)
    # 本地准入：加在 CORS 之后 → 位于其外层，预检由本层直接放行、其余 API
    # 先过 Host 与会话校验。中间件顺序即洋葱顺序，最后一个 add 在最外层。
    app.add_middleware(LocalAccessMiddleware)

    @app.middleware("http")
    async def _add_static_cache_headers(request, call_next):
        verification = getattr(request.app.state, "update_verification", {})
        if (verification.get("protected")
                and request.url.path.startswith("/api/")
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.url.path != "/api/v1/system/update-ready"):
            response = JSONResponse({"detail": "正在完成更新，请稍后再试"}, status_code=503)
        else:
            response = await call_next(request)
        cache_control = _cache_control_for(request.url.path.lstrip("/"))
        if cache_control:
            response.headers.setdefault("Cache-Control", cache_control)
        # 基础安全响应头：本应用只服务自己的页面，不需要被任何外部页面嵌入，
        # 也不该让浏览器去猜内容类型。
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # CSP：页面只加载同源资源；内联样式与脚本是当前构建产物的现实
        # （Vue 注入的 style 标签、index.html 的内联引导），这里放开
        # unsafe-inline 但禁止一切外部来源与 object/embed。
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https://cdn.cloudflare.steamstatic.com "
            "https://shared.akamai.steamstatic.com https://store.fastly.steamstatic.com; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "object-src 'none'; "
            "base-uri 'self'",
        )
        return response

    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    app.include_router(agent_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(games_router, prefix="/api/v1")
    app.include_router(achievements_router, prefix="/api/v1")
    app.include_router(achievements_level_router, prefix="/api/v1")
    app.include_router(achievements_challenges_router, prefix="/api/v1")
    app.include_router(achievements_social_router, prefix="/api/v1")
    app.include_router(metadata_router, prefix="/api/v1")
    app.include_router(monitoring_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(pilot_router, prefix="/api/v1")
    app.include_router(wishlist_router, prefix="/api/v1")
    app.include_router(crawl_router, prefix="/api/v1")
    app.include_router(proxies_router, prefix="/api/v1")
    # proxypool 只读面：生产作业台账与出口账本
    app.include_router(proxypool_router, prefix="/api/v1")
    app.include_router(alerts_router, prefix="/api/v1")
    app.include_router(rates_router, prefix="/api/v1")
    app.include_router(regions_router, prefix="/api/v1")
    app.include_router(account_router, prefix="/api/v1")
    app.include_router(bundles_router, prefix="/api/v1")
    app.include_router(bills_router, prefix="/api/v1")
    app.include_router(family_router, prefix="/api/v1")
    app.include_router(redeem_router, prefix="/api/v1")
    app.include_router(steam_events_router, prefix="/api/v1")

    mcp_app = agent_mcp.McpAsgiApp()
    app.add_route(agent_mcp.MOUNT_PATH, mcp_app, methods=["GET", "POST", "DELETE"])
    app.mount(agent_mcp.MOUNT_PATH, mcp_app)

    dist = settings.web_dist_dir
    if (dist / "index.html").is_file():
        _mount_spa(app, dist)
    else:
        log_event(
            logger,
            "前端构建产物缺失（web/dist），当前仅提供 API；构建命令：cd web && npm run build",
            level=logging.WARNING,
        )
    return app


app = create_app()
