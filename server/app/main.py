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
from app.domains.achievements.router import router as achievements_router
from app.domains.alerts.router import router as alerts_router
from app.domains.bills.router import router as bills_router
from app.domains.bundles.router import router as bundles_router
from app.domains.crawl.router import router as crawl_router
from app.domains.family.router import router as family_router
from app.domains.games.router import router as games_router
from app.domains.metadata.router import router as metadata_router
from app.domains.proxies.router import router as proxies_router
from app.domains.proxypool.router import router as proxypool_router
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


async def _post_startup_chain() -> None:
    """监听后的「先开门，再收拾」链（约束见 AGENTS.md 启动链红线）。

    每步独立 try/except，失败只留日志不拖垮后续步骤；**顺序有语义**
    （种子合并先于标记刷新——种子价格历史是史低/永降的输入；内核就位
    先于 Clash 自启——自启以内核存在为前提），与旧 lifespan 内联顺序
    逐一对齐，不做乱序调度。跑完才 `start_scheduler()`：种子合并单事务
    BEGIN IMMEDIATE 持锁可达十几秒，引擎连接的 busy_timeout 若先到期，
    定时任务的钱包/愿望单写入会撞锁失败——调度器排在链尾即无竞态。
    """
    from app.core.seed_assets import import_seed, merge_seed_incremental

    try:
        await import_seed()
    except Exception:  # noqa: BLE001
        logger.exception("资产种子导入失败（不阻塞启动）")
    try:
        await merge_seed_incremental()
    except Exception:  # noqa: BLE001
        logger.exception("增量种子合并失败（不阻塞启动）")

    # 家庭库后台预热：实时聚合要逐成员调 Steam HTTPS（代理、秒级起步），
    # 不预热的话每次启动后的首次打开都要干等。cached_family_library 自带
    # 「快照优先 + 后台拉新」语义，这里只是把它提前到启动时触发；失败
    # （无快照且实时不可用）只记日志，不影响启动。
    from app.domains.family import service as family_service

    try:
        await family_service.cached_family_library()
    except Exception:  # noqa: BLE001
        logger.info("[family] 启动预热失败（无快照且实时聚合不可用），跳过")

    from app.domains.crawl.service import cleanup_orphan_jobs

    try:
        await cleanup_orphan_jobs()
    except Exception:  # noqa: BLE001
        logger.exception("孤儿任务清理失败（不阻塞启动）")

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

    # 史低标记 + 永降标记 + 排序缓存预计算列 + 系列归组全库初始化
    # （秒级；爬取后另有增量刷新）
    from app.domains.games import service as games_service
    from app.domains.games import series as games_series

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
    try:
        refreshed = await games_series.refresh_series()
        logger.info("系列归组初始化完成：%d 款", refreshed)
    except Exception:  # noqa: BLE001
        logger.exception("系列归组初始化失败（不阻塞启动）")

    # 随包内核就位：mihomo 与 GeoIP 数据随发行包分发，复制进 data/clash/
    # （GeoIP 只补缺失；内核低版本时升级替换，见 clash_manager）。必须早于
    # 内核自启——自启与代理策略都以内核就位为前提。失败不阻断启动：前端
    # 「内核缺失」态与自动安装入口仍可兜底。-v 版本探测走子进程，放线程池
    # 不占事件循环。
    from app.domains.proxies import clash_manager

    try:
        installed = await asyncio.to_thread(
            clash_manager.ensure_kernel, get_settings().data_dir
        )
        if installed["copied"]:
            logger.info("随包 Clash 内核就位：%s", ", ".join(installed["copied"]))
        if installed.get("upgraded"):
            logger.info("随包内核已升级：%s", installed["upgraded"])
    except Exception:  # noqa: BLE001
        logger.exception("随包 Clash 内核就位失败（不阻塞启动）")

    # Clash 内核随服务自启（常驻后台语义）：有内核 + 有 clash 订阅即拉起。
    # 服务重启后 proxy_first 策略才不会降级直连（Steam 域直连基本不可用）。
    try:
        await _autostart_clash()
        # 启动体检（6h 门槛内跳过；本地软件不常驻，重启即检查点是设计语义）
        state = await proxies_service.maybe_run_clash_health_check()
        if state == "checked":
            logger.info("[启动体检] Clash 节点检测完成")
    except Exception:  # noqa: BLE001
        logger.exception("[启动体检] Clash 节点检测失败（不阻塞启动）")

    # 汇率启动兜底：错过每日 03:00 定点（关机/服务重启）时按快照龄补刷新，
    # 保证"每日自动抓取"承诺不因服务频繁重启落空（内含 >12h 阈值，幂等安全）
    try:
        await rates_service.refresh_if_stale()
    except Exception:  # noqa: BLE001
        logger.exception("[启动] 汇率过期检查失败（不阻塞启动）")
    # 历史缺口扫描（纯本地，零网络）：只记录待修复规模；外网修复归每日
    # 04:00 的 fx_history_repair——配置 Provider Key 后启动不烧任何配额
    try:
        from app.domains.rates import history as rates_history

        scan = await rates_history.scan_history_gaps()
        if scan["windows"]:
            logger.info(
                "[启动] 汇率历史缺口 %d 个窗口 / %d 个 (币种,日) 待修复（交每日 04:00 修复任务）",
                len(scan["windows"]), scan["totalPairs"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("[启动] 汇率历史缺口扫描失败（不阻塞启动）")

    # 捆绑包列表预热：全量聚合（25.8k 包 / 14.1 万行区域价）+ 12MB 预序列化
    # 是秒级重活，不预热则用户首次进捆绑包页要干等整段聚合。排在汇率兜底
    # 之后——列表缓存按汇率指纹失效，先刷汇率才不会预热出一份随即作废的
    # 缓存。失败只记日志：请求路径仍按需重算，功能不受影响。
    from app.domains.bundles import service as bundles_service

    # 排序快照全库重建（bundles.min_cny_fen/diff_fen/is_lowest/smart_score）：
    # 与 games 排序缓存同位——列表排序读快照列，不在请求期现算。必须早于列表
    # 预热（预热出的是含快照字段的完整载荷）。
    try:
        rebuilt = await bundles_service.refresh_bundle_sort_cache()
        logger.info("[启动] 捆绑包排序快照初始化完成：%d 个", rebuilt)
    except Exception:  # noqa: BLE001
        logger.exception("捆绑包排序快照初始化失败（不阻塞启动）")
    # 快照重建后强制失效聚合/序列化缓存：开门（health 200）到本步完成之间，
    # 早到的请求会用「重建前」的快照行建缓存，指纹不变就一直是旧行——预热
    # 拿到的是过期载荷。失效后预热必然以新快照重建。
    bundles_service.invalidate_bundles_cache()

    try:
        size = await bundles_service.warmup()
        logger.info("[启动] 捆绑包列表预热完成（%.1f MB）", size / 1e6)
    except Exception:  # noqa: BLE001
        logger.exception("捆绑包列表预热失败（不阻塞启动）")

    # Epic 喜加一快照预热：启动即后台拉新（不 await 网络轮），用户打开仪表盘
    # 时刷新多半已完成；快照新鲜（30 分钟内）时零开销；失败只记日志，
    # 请求路径仍有 stale-while-revalidate。
    from app.domains.metadata import service as metadata_service

    try:
        await metadata_service.preheat_epic_offers()
    except Exception:  # noqa: BLE001
        logger.exception("Epic 快照预热失败（不阻塞启动）")

    # 调度器在收拾链跑完后才启动（链首说明的写锁竞态；空窗几秒~十几秒
    # 对 15min/6h 拍完全无感）
    start_scheduler()
    logger.info("[启动] 后台收拾链完成，服务已就绪（调度器已启动）")


@asynccontextmanager
async def lifespan(_: FastAPI):
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
    await init_db()

    # 数据目录布局入日志：本机支持多布局（系统数据目录 / 便携 / 存量便携），
    # 「数据跑哪去了」是最高频的排查问题，启动即亮明。
    from app.core.paths import LAYOUT_LABEL, describe_layout

    layout = describe_layout(settings.data_dir)
    logger.info(
        "%s %s 启动：data=%s（%s）port=%d（后台收拾链进行中，服务已监听）",
        APP_NAME,
        settings.version,
        settings.data_dir,
        LAYOUT_LABEL.get(layout, layout),
        settings.port,
    )
    asyncio.create_task(_post_startup_chain())
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

    # 启动等待页已移交桌面壳内联 HTML（desktop/main.py _SPLASH_HTML）：
    # uvicorn 在 lifespan 完成前不监听端口，后端同源等待页在窗口期必然
    # 连接被拒，等待逻辑只有放在不依赖网络的进程内页面才能成立。

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
    app.include_router(achievements_router, prefix="/api/v1")
    app.include_router(metadata_router, prefix="/api/v1")
    app.include_router(wishlist_router, prefix="/api/v1")
    app.include_router(crawl_router, prefix="/api/v1")
    app.include_router(proxies_router, prefix="/api/v1")
    # proxypool 只读面：生产作业台账（/proxies 页「生产作业」分节的数据源）
    app.include_router(proxypool_router, prefix="/api/v1")
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
