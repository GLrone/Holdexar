"""FastAPI 应用工厂：API 路由 + 前端静态托管（SPA fallback）。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


async def _autostart_clash(*, snapshot_only: bool = False) -> None:
    """Clash 内核随服务自启（后台任务，不阻塞 lifespan）。

    有内置内核 + 已存 clash 订阅才拉起；取配置按候选遍历：某条订阅下载
    失败且无本地缓存时降级到下一条可用订阅（本地缓存照常可用）——开机
    即有代理，proxy_first 不降级直连。
    """
    try:
        from app.domains.proxies import clash_manager, service as proxies_service
        from app.domains.settings.service import get_value

        if not await get_value("proxy.autostart", True):
            log_event(logger, "内核自启开关已关闭，跳过 Clash 内核自启", tag="跳过")
            return

        settings = get_settings()
        detect = clash_manager.detect_kernel(settings.data_dir)
        if not detect["found"]:
            log_event(logger, "本机没有 Clash 内核，跳过内核自启", tag="跳过")
            return
        subs = await proxies_service.list_subscriptions("clash", reveal=True)
        usable = [s for s in subs if not s.get("deprecated")]
        if not usable:
            if subs:
                log_event(
                    logger,
                    "Clash 订阅当前全部不可用，等待订阅或节点恢复",
                    tag="跳过",
                    level=logging.WARNING,
                )
            else:
                log_event(logger, "还没有 Clash 订阅，跳过内核自启", tag="跳过")
            return
        # 取配置按候选遍历（新→旧）：某条订阅链接失效且无本地缓存时降级到
        # 下一条，内核起不来不允许是「下载失败」一个原因——节点好不好交给
        # 启动后的首检与定时体检
        try:
            picked = await proxies_service.resolve_startable_clash(
                settings.data_dir, snapshot_only=snapshot_only
            )
        except ValueError as e:
            log_event(
                logger,
                "内核自启放弃，没有能选出的可用订阅",
                tag="跳过",
                level=logging.WARNING,
                detail={"原因": str(e)},
            )
            return
        sub = picked["subscription"]
        config_path = picked["configPath"]
        port_options = await proxies_service.kernel_port_options()
        status = await clash_manager.runtime.ensure_running(
            detect["path"], config_path, subscription_url=sub["url"],
            **port_options,
        )
        # 账本收敛：启动用的是上面这份配置内容，已下线/改名节点的旧行随启动清理
        # （订阅名不再自动回填——手动改名，见 proxies 域）
        try:
            from pathlib import Path

            names = clash_manager.parse_node_names(
                Path(config_path).read_text(encoding="utf-8", errors="ignore")
            )
            if names:
                pruned = await proxies_service.prune_clash_node_ledger(
                    sub["id"], set(names)
                )
                if pruned:
                    log_event(
                        logger,
                        "Clash 内核自启后清理节点账本，删除已下线节点",
                        detail={"删除节点数": pruned},
                    )
        except Exception:  # noqa: BLE001 —— 收敛失败不影响启动
            log_event(logger, "Clash 内核自启后清理节点账本跳过", tag="跳过", level=logging.WARNING)
        log_event(logger, "Clash 内核已随服务自启", detail={"端口": status.get("port")})
    except Exception:  # noqa: BLE001 —— 自启失败不阻塞服务
        log_event(logger, "Clash 内核自启失败，不影响服务运行", level=logging.ERROR, exc_info=True)


def _kick_startup_health_check() -> None:
    """订阅健康摘要后台点火（不阻塞收拾链）。

    只做账本聚合（存活统计/降级判定），不做探测——池 Runtime 装配与 lane
    一致性闸（fail-closed）不依赖它；异常由摘要函数自身吞掉。
    """
    from app.domains.proxies import service as proxies_service

    asyncio.get_running_loop().create_task(
        proxies_service.refresh_subscription_health_summary()
    )


async def _startup_proxy_maintenance() -> None:
    """Warm both proxy runtimes from one subscription sync in the background."""
    from app.core import scheduler as core_scheduler

    sync = await core_scheduler._startup_subscription_sync()
    try:
        await _autostart_clash(snapshot_only=True)
        _kick_startup_health_check()
    except Exception:  # noqa: BLE001
        log_event(logger, "通用代理运行时启动失败，不影响服务运行", level=logging.ERROR, exc_info=True)
    try:
        await core_scheduler._startup_pool_runtime(skip_sync=True)
    except Exception:  # noqa: BLE001
        log_event(logger, "代理池运行时启动装配失败，生产代理继续保持不可用", level=logging.ERROR, exc_info=True)
    if sync is None:
        log_event(logger, "代理启动同步未取得结果，继续检查可恢复的本地状态")


def _kick_startup_proxy_maintenance() -> None:
    asyncio.get_running_loop().create_task(
        _startup_proxy_maintenance(), name="startup-proxy-maintenance"
    )


@dataclass(frozen=True)
class _StartupStep:
    """收拾链一步：fail_log 为空表示该步自兜异常或属必经动作（异常照常上抛）。"""

    body: Callable[[], Awaitable[None]]
    fail_log: str | None = None
    level: int = logging.ERROR
    tag: str | None = None
    log_exc: bool = True


async def _run_startup_steps(steps: list[_StartupStep]) -> None:
    for step in steps:
        if step.fail_log is None:
            await step.body()
            continue
        try:
            await step.body()
        except Exception:  # noqa: BLE001 —— 收拾链各步互不拖垮，只留日志
            log_event(
                logger,
                step.fail_log,
                level=step.level,
                exc_info=step.log_exc,
                tag=step.tag,
            )


def _startup_steps() -> list[_StartupStep]:
    async def recover_delivery() -> None:
        from app.domains.notifications.service import recover_delivery_state

        await recover_delivery_state()

    async def resume_scans() -> None:
        from app.domains.notifications.service import resume_event_scans

        await resume_event_scans()

    async def merge_seeds() -> None:
        from app.core.seed_assets import merge_seed_incremental

        await merge_seed_incremental()

    async def warm_family_library() -> None:
        from app.domains.family import service as family_service

        await family_service.cached_family_library()

    async def cleanup_orphans() -> None:
        from app.domains.crawl.service import cleanup_orphan_jobs

        await cleanup_orphan_jobs()

    async def reap_jobruns() -> None:
        from app.domains.proxypool import jobruns as proxypool_jobruns

        await proxypool_jobruns.record_interrupted()

    async def cleanup_rates() -> None:
        from app.domains.rates import service as rates_service

        await rates_service.cleanup_disallowed()

    async def migrate_legacy_subscription() -> None:
        from app.domains.proxies import service as proxies_service

        await proxies_service.migrate_legacy_subscription()

    async def revert_to_direct_if_abandoned() -> None:
        from app.domains.proxies import service as proxies_service

        await proxies_service.maybe_revert_to_direct()

    async def migrate_legacy_kv() -> None:
        from app.domains.account import service as account_service

        migrated = await account_service.migrate_legacy_kv()
        if migrated:
            log_event(
                logger,
                "旧单账号 Cookie 已迁移至 Steam 多账号表",
                detail={"迁移账号数": migrated},
            )

    async def seal_account_credentials() -> None:
        from app.domains.account import service as account_service

        await account_service.seal_credentials_at_rest()

    async def seal_subscription_urls() -> None:
        from app.domains.proxies import service as proxies_service

        await proxies_service.seal_subscription_urls()

    async def seal_proxy_credentials() -> None:
        from app.domains.proxies import service as proxies_service

        await proxies_service.seal_proxy_credentials()

    async def tighten_node_secret_files() -> None:
        from app.domains.proxies import node_secret_files

        node_secret_files.tighten_all(get_settings().data_dir)
        node_secret_files.sweep_transient(get_settings().data_dir)

    async def refresh_hl_flags() -> None:
        from app.domains.games import service as games_service

        refreshed = await games_service.refresh_hl_flags()
        log_event(logger, "史低标记初始化完成", detail={"款数": refreshed})

    async def refresh_pp_flags() -> None:
        from app.domains.games import service as games_service

        refreshed = await games_service.refresh_pp_flags()
        log_event(logger, "永降标记初始化完成", detail={"款数": refreshed})

    async def refresh_sort_cache() -> None:
        from app.domains.games import service as games_service

        refreshed = await games_service.refresh_sort_cache()
        log_event(logger, "排序缓存初始化完成", detail={"款数": refreshed})

    async def refresh_series() -> None:
        from app.domains.games import series as games_series

        refreshed = await games_series.refresh_series()
        log_event(logger, "系列归组初始化完成", detail={"款数": refreshed})

    async def ensure_kernel() -> None:
        from app.domains.proxies import clash_manager

        installed = await asyncio.to_thread(
            clash_manager.ensure_kernel, get_settings().data_dir
        )
        if installed["copied"]:
            log_event(
                logger,
                "随包 Clash 内核已复制就位",
                detail={"文件数": len(installed["copied"]), "文件": ", ".join(installed["copied"])},
            )
        if installed.get("upgraded"):
            log_event(logger, "随包 Clash 内核已升级", detail={"版本": installed["upgraded"]})

    async def kick_proxy_maintenance() -> None:
        _kick_startup_proxy_maintenance()

    async def refresh_rates_if_stale() -> None:
        from app.domains.rates import service as rates_service

        await rates_service.refresh_if_stale()

    async def scan_rates_history_gaps() -> None:
        from app.domains.rates import history as rates_history

        scan = await rates_history.scan_history_gaps()
        if scan["windows"]:
            log_event(
                logger,
                "汇率历史发现缺口，留待每日 04:00 修复任务处理",
                detail={"缺口窗口数": len(scan["windows"]), "待修复对数": scan["totalPairs"]},
            )

    async def rebuild_bundle_sort_cache() -> None:
        from app.domains.bundles import service as bundles_service

        rebuilt = await bundles_service.refresh_bundle_sort_cache()
        log_event(logger, "捆绑包排序快照初始化完成", detail={"包数": rebuilt})

    async def invalidate_bundles_cache() -> None:
        from app.domains.bundles import service as bundles_service

        bundles_service.invalidate_bundles_cache()

    async def warmup_bundles() -> None:
        from app.domains.bundles import service as bundles_service

        size = await bundles_service.warmup()
        log_event(logger, "捆绑包列表预热完成", detail={"体积MB": round(size / 1e6, 1)})

    async def preheat_epic() -> None:
        from app.domains.metadata import service as metadata_service

        await metadata_service.preheat_epic_offers()

    async def refresh_steam_events() -> None:
        from app.domains.steam_events import service as steam_events_service

        await steam_events_service.refresh_if_stale()

    async def reap_agent_runs() -> None:
        from app.domains.agent import service as agent_service

        await agent_service.reconcile_orphan_runs()

    async def start_core_scheduler() -> None:
        start_scheduler()

    async def apply_content_schedules() -> None:
        from app.core.scheduler import apply_content_schedules_from_settings

        await apply_content_schedules_from_settings()

    async def announce_ready() -> None:
        log_event(logger, "后台收拾链完成，服务已就绪（调度器已启动）")

    async def ensure_seed_online() -> None:
        from app.core import seed_fetch

        await seed_fetch.ensure_seed_online()

    async def bootstrap_rates_history() -> None:
        from app.domains.rates import history as rates_history

        await rates_history.bootstrap_full_history()

    return [
        _StartupStep(recover_delivery, "提醒投递状态恢复失败，不阻塞启动"),
        _StartupStep(resume_scans, "未完成的提醒生成仍保留，后续继续恢复"),
        # 种子现价历史是史低/永降标记的输入，必须先合并再刷新标记
        _StartupStep(merge_seeds, "增量种子合并失败，不阻塞启动"),
        _StartupStep(
            warm_family_library,
            "家庭库启动预热失败（无快照且实时聚合不可用），跳过",
            level=logging.INFO,
            tag="跳过",
            log_exc=False,
        ),
        _StartupStep(cleanup_orphans, "孤儿爬取任务清理失败，不阻塞启动"),
        _StartupStep(reap_jobruns),  # 台账收尸函数内自兜失败
        _StartupStep(cleanup_rates, "汇率白名单清洗失败，不阻塞启动"),
        _StartupStep(migrate_legacy_subscription, "旧版 Clash 订阅迁移失败，不阻塞启动"),
        # 无出口自愈：订阅删光仍停代理形态会让作业空等不存在的出口
        _StartupStep(revert_to_direct_if_abandoned, "无代理出口策略自愈失败，不阻塞启动"),
        _StartupStep(migrate_legacy_kv, "旧单账号 Cookie 迁移失败，不阻塞启动"),
        # 三段静态加密均幂等且读取侧兼容明文，失败不阻塞启动
        _StartupStep(seal_account_credentials, "存量凭据静态加密失败，不阻塞启动"),
        _StartupStep(seal_subscription_urls, "订阅链接静态加密失败，不阻塞启动"),
        _StartupStep(seal_proxy_credentials, "代理凭据静态加密失败，不阻塞启动"),
        _StartupStep(tighten_node_secret_files, "节点配置文件权限收紧失败", level=logging.WARNING),
        _StartupStep(refresh_hl_flags, "史低标记初始化失败，不阻塞启动"),
        _StartupStep(refresh_pp_flags, "永降标记初始化失败，不阻塞启动"),
        _StartupStep(refresh_sort_cache, "排序缓存初始化失败，不阻塞启动"),
        _StartupStep(refresh_series, "系列归组初始化失败，不阻塞启动"),
        # 内核就位必须早于 Clash 自启（自启以内核存在为前提），失败由前端安装入口兜底
        _StartupStep(ensure_kernel, "随包 Clash 内核就位失败，不阻塞启动"),
        # 代理刷新与运行时初始化是后台任务，不阻塞调度器可用性
        _StartupStep(kick_proxy_maintenance),
        _StartupStep(refresh_rates_if_stale, "汇率过期体检失败，不阻塞启动"),
        # 只做本地缺口扫描，外网修复归每日 04:00 任务——启动不烧配额
        _StartupStep(scan_rates_history_gaps, "汇率历史缺口扫描失败，不阻塞启动"),
        # 汇率兜底在前防列表缓存按旧汇率指纹预热；排序快照必须早于列表预热
        _StartupStep(rebuild_bundle_sort_cache, "捆绑包排序快照初始化失败，不阻塞启动"),
        # 快照重建后强制失效聚合缓存，防预热拿到重建前的旧行
        _StartupStep(invalidate_bundles_cache),
        _StartupStep(warmup_bundles, "捆绑包列表预热失败，不阻塞启动"),
        _StartupStep(preheat_epic, "Epic 喜加一快照预热失败，不阻塞启动"),
        _StartupStep(refresh_steam_events, "Steam 活动日历兜底同步失败，不阻塞启动"),
        _StartupStep(reap_agent_runs, "Agent 孤儿运行收尸失败，不阻塞启动"),
        # 调度器最后启动：种子合并持锁可达十几秒，定时任务先跑会撞锁
        _StartupStep(start_core_scheduler),
        _StartupStep(apply_content_schedules, "内容源自定义刷新节奏应用失败，保持默认拍点"),
        _StartupStep(announce_ready),
        # 按需补齐是网络步骤，排调度器后不占开门链，失败下次启动重来
        _StartupStep(ensure_seed_online, "种子按需获取失败，不阻塞启动"),
        _StartupStep(bootstrap_rates_history, "汇率全史初始化失败，不阻塞启动"),
    ]


async def _post_startup_chain() -> None:
    """监听后的「先开门，再收拾」链（约束见 AGENTS.md 启动链红线）。

    步骤按 `_startup_steps()` 声明顺序执行，顺序不变量随表内注释标注；
    `fail_log` 为空的步骤不兜异常（自兜或必经动作）。
    """
    await _run_startup_steps(_startup_steps())


@asynccontextmanager
async def lifespan(application: FastAPI):
    """监听前只做「不收拾完就不能开门」的最小集，其余全部移交后台链。

    监听前（阻塞端口监听，即用户等待时间）：
    - `setup_logging`：任何后续报错都要有日志承接；
    - `init_db`：建表 + 零登记层补列补索引 + 结构性迁移链——迁移改的是
      表结构，监听后的请求会立刻读到这些表，新旧 schema 混存会让请求
      踩到不存在的列；且迁移失败要留全栈日志并拦下启动，不能半途开门。

    其余（种子并库/标记预计算/清洗/一次性迁移/内核/自启/调度器）全部
    是幂等后台工作，一律走 `_post_startup_chain`，见其 docstring。
    """
    settings = get_settings()
    setup_logging(settings.data_dir)
    update_attempt = os.environ.get(f"{ENV_PREFIX}UPDATE_ATTEMPT_ID")
    from app.core import layout as update_layout, update_data

    with update_data.startup_authorization(settings.data_dir) as update_token:
        await init_db(maintenance_token=update_token)
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
        f"{APP_NAME} {settings.version} 已启动并监听端口，后台收拾链进行中",
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
        await _post_startup_chain()

    startup_task = asyncio.create_task(start_after_acceptance() if update_token else _post_startup_chain())
    try:
        yield
    finally:
        startup_task.cancel()
        await asyncio.gather(startup_task, return_exceptions=True)
        stop_scheduler()
        log_event(logger, f"{APP_NAME} 已停止")


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
