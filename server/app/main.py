"""FastAPI 应用工厂：API 路由 + 前端静态托管（SPA fallback）。"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.app_info import APP_NAME
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import log_event, setup_logging
from app.core.scheduler import start_scheduler, stop_scheduler
from app.domains.account.router import router as account_router
from app.domains.agent.router import router as agent_router
from app.domains.achievements.router import router as achievements_router
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
from app.domains.wishlist.router import router as wishlist_router

logger = logging.getLogger(__name__)


async def _autostart_clash() -> None:
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
        subs = await proxies_service.list_subscriptions("clash")
        usable = [s for s in subs if not s.get("deprecated")]
        if not usable:
            if subs:
                log_event(
                    logger,
                    "Clash 订阅全部已废弃（不可用节点超过 95%），跳过内核自启",
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
            picked = await proxies_service.resolve_startable_clash(settings.data_dir)
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
        status = await clash_manager.runtime.ensure_running(
            detect["path"], config_path, subscription_url=sub["url"]
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
    """启动体检后台点火（不阻塞收拾链）。

    全量节点探测分钟级，在链上 await 曾是「开机到能用 ~230s」的绝对大头；
    体检只刷新节点新鲜度，池 Runtime 装配与 lane 一致性闸（fail-closed）
    不依赖它。6h 门槛节流不变；后台任务异常由体检函数自身吞掉。
    """
    from app.domains.proxies import service as proxies_service

    asyncio.get_running_loop().create_task(proxies_service.maybe_run_clash_health_check())


async def _post_startup_chain() -> None:
    """监听后的「先开门，再收拾」链（约束见 AGENTS.md 启动链红线）。

    每步独立 try/except，失败只留日志不拖垮后续步骤；**顺序有语义**
    （种子合并先于标记刷新——种子价格历史是史低/永降的输入；内核就位
    先于 Clash 自启——自启以内核存在为前提），与旧 lifespan 内联顺序
    逐一对齐，不做乱序调度。跑完才 `start_scheduler()`：种子合并单事务
    BEGIN IMMEDIATE 持锁可达十几秒，引擎连接的 busy_timeout 若先到期，
    定时任务的钱包/愿望单写入会撞锁失败——调度器排在链尾即无竞态。
    """
    from app.core.seed_assets import merge_seed_incremental

    try:
        await merge_seed_incremental()
    except Exception:  # noqa: BLE001
        log_event(logger, "增量种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 家庭库后台预热：实时聚合要逐成员调 Steam HTTPS（代理、秒级起步），
    # 不预热的话每次启动后的首次打开都要干等。cached_family_library 自带
    # 「快照优先 + 后台拉新」语义，这里只是把它提前到启动时触发；失败
    # （无快照且实时不可用）只记日志，不影响启动。
    from app.domains.family import service as family_service

    try:
        await family_service.cached_family_library()
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "家庭库启动预热失败（无快照且实时聚合不可用），跳过",
            tag="跳过",
        )

    from app.domains.crawl.service import cleanup_orphan_jobs

    try:
        await cleanup_orphan_jobs()
    except Exception:  # noqa: BLE001
        log_event(logger, "孤儿爬取任务清理失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 汇率白名单清洗：41 区货币集（+ TRY/ARS 预留）之外的历史币种数据清除
    from app.domains.rates import service as rates_service

    try:
        await rates_service.cleanup_disallowed()
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率白名单清洗失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 旧 Clash 订阅设置 → 订阅表（一次性）
    from app.domains.proxies import service as proxies_service

    try:
        await proxies_service.migrate_legacy_subscription()
    except Exception:  # noqa: BLE001
        log_event(logger, "旧版 Clash 订阅迁移失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 旧单账号 Cookie KV → steam_accounts 多账号表（一次性）
    from app.domains.account import service as account_service

    try:
        migrated = await account_service.migrate_legacy_kv()
        if migrated:
            log_event(
                logger,
                "旧单账号 Cookie 已迁移至 Steam 多账号表",
                detail={"迁移账号数": migrated},
            )
    except Exception:  # noqa: BLE001
        log_event(logger, "旧单账号 Cookie 迁移失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 存量明文凭据静态加密（幂等）：账号 Cookie 行与凭据类设置键封装为
    # AES-256-GCM 密文，读取侧使用时解密；已带密文前缀的原样跳过
    try:
        await account_service.seal_credentials_at_rest()
    except Exception:  # noqa: BLE001 —— 读取侧兼容明文，本步失败不阻塞启动
        log_event(logger, "存量凭据静态加密失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 存量明文订阅链接静态加密（幂等）：订阅表 + 快照 provenance 列封装为
    # AES-256-GCM 密文，读取侧使用时解密；已带密文前缀的原样跳过
    try:
        await proxies_service.seal_subscription_urls()
    except Exception:  # noqa: BLE001 —— 读取侧兼容明文，本步失败不阻塞启动
        log_event(logger, "订阅链接静态加密失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 史低标记 + 永降标记 + 排序缓存预计算列 + 系列归组全库初始化
    # （秒级；爬取后另有增量刷新）
    from app.domains.games import service as games_service
    from app.domains.games import series as games_series

    try:
        refreshed = await games_service.refresh_hl_flags()
        log_event(logger, "史低标记初始化完成", detail={"款数": refreshed})
    except Exception:  # noqa: BLE001
        log_event(logger, "史低标记初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        refreshed = await games_service.refresh_pp_flags()
        log_event(logger, "永降标记初始化完成", detail={"款数": refreshed})
    except Exception:  # noqa: BLE001
        log_event(logger, "永降标记初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        refreshed = await games_service.refresh_sort_cache()
        log_event(logger, "排序缓存初始化完成", detail={"款数": refreshed})
    except Exception:  # noqa: BLE001
        log_event(logger, "排序缓存初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        refreshed = await games_series.refresh_series()
        log_event(logger, "系列归组初始化完成", detail={"款数": refreshed})
    except Exception:  # noqa: BLE001
        log_event(logger, "系列归组初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)

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
            log_event(
                logger,
                "随包 Clash 内核已复制就位",
                detail={"文件数": len(installed["copied"]), "文件": ", ".join(installed["copied"])},
            )
        if installed.get("upgraded"):
            log_event(logger, "随包 Clash 内核已升级", detail={"版本": installed["upgraded"]})
    except Exception:  # noqa: BLE001
        log_event(logger, "随包 Clash 内核就位失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # Clash 内核随服务自启（常驻后台语义）：有内核 + 有 clash 订阅即拉起。
    # 服务重启后 proxy_first 策略才不会降级直连（Steam 域直连基本不可用）。
    try:
        await _autostart_clash()
        # 启动体检后台点火（6h 门槛节流不变）：全量探测分钟级，链上 await
        # 曾是「开机到能用 ~230s」的绝对大头；lane 一致性闸不依赖体检结果
        _kick_startup_health_check()
    except Exception:  # noqa: BLE001
        log_event(logger, "Clash 节点启动体检失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 池 Runtime bootstrap：首次把「订阅 → Snapshot → Registry → Pool → Runtime」建起来。
    # 幂等（已有可用 Runtime 直接返回）；**失败不阻塞启动**——crawler 保持 fail-closed，
    # 30min 后的订阅刷新就是下一次机会。放在内核就位之后：bootstrap 需要内核可执行文件。
    from app.core import scheduler as core_scheduler

    try:
        await core_scheduler._startup_pool_runtime()
    except Exception:  # noqa: BLE001 —— 与链内其它步骤同约定：本步异常只留日志
        log_event(logger, "代理池运行时启动装配失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 订阅同步（下载后置）：内核就位不等订阅下载——冷启动直接吃持久化 Registry
    # 起核，本步在 Runtime 就位后全量拉一遍订阅；池签名变化就地消费一次重建
    # （热重载优先）。失败不阻塞启动，30min 刷新是下一次机会。
    try:
        await core_scheduler._startup_subscription_sync()
    except Exception:  # noqa: BLE001 —— 与链内其它步骤同约定：本步异常只留日志
        log_event(logger, "代理订阅启动同步失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 汇率启动兜底：错过每日 03:00 定点（关机/服务重启）时按快照龄补刷新，
    # 保证"每日自动抓取"承诺不因服务频繁重启落空（内含 >12h 阈值，幂等安全）
    try:
        await rates_service.refresh_if_stale()
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率过期体检失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    # 历史缺口扫描（纯本地，零网络）：只记录待修复规模；外网修复归每日
    # 04:00 的 fx_history_repair——配置 Provider Key 后启动不烧任何配额
    try:
        from app.domains.rates import history as rates_history

        scan = await rates_history.scan_history_gaps()
        if scan["windows"]:
            log_event(
                logger,
                "汇率历史发现缺口，留待每日 04:00 修复任务处理",
                detail={"缺口窗口数": len(scan["windows"]), "待修复对数": scan["totalPairs"]},
            )
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率历史缺口扫描失败，不阻塞启动", level=logging.ERROR, exc_info=True)

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
        log_event(logger, "捆绑包排序快照初始化完成", detail={"包数": rebuilt})
    except Exception:  # noqa: BLE001
        log_event(logger, "捆绑包排序快照初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    # 快照重建后强制失效聚合/序列化缓存：开门（health 200）到本步完成之间，
    # 早到的请求会用「重建前」的快照行建缓存，指纹不变就一直是旧行——预热
    # 拿到的是过期载荷。失效后预热必然以新快照重建。
    bundles_service.invalidate_bundles_cache()

    try:
        size = await bundles_service.warmup()
        log_event(logger, "捆绑包列表预热完成", detail={"体积MB": round(size / 1e6, 1)})
    except Exception:  # noqa: BLE001
        log_event(logger, "捆绑包列表预热失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # Epic 喜加一快照预热：启动即后台拉新（不 await 网络轮），用户打开仪表盘
    # 时刷新多半已完成；快照新鲜（30 分钟内）时零开销；失败只记日志，
    # 请求路径仍有 stale-while-revalidate。
    from app.domains.metadata import service as metadata_service

    try:
        await metadata_service.preheat_epic_offers()
    except Exception:  # noqa: BLE001
        log_event(logger, "Epic 喜加一快照预热失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # Steam 活动日历兜底同步：错过每日 05:00 定点时按快照龄补（>72h 才真正
    # 抓取，幂等安全）；活动窗口是价格观测行周期标签的来源，排在调度器启动
    # 前可让首轮观测带上标签。失败只记日志，旧数据继续展示。
    try:
        from app.domains.steam_events import service as steam_events_service

        await steam_events_service.refresh_if_stale()
    except Exception:  # noqa: BLE001
        log_event(logger, "Steam 活动日历兜底同步失败，不阻塞启动", level=logging.ERROR, exc_info=True)

    # 调度器在收拾链跑完后才启动（链首说明的写锁竞态；空窗几秒~十几秒
    # 对 15min/6h 拍完全无感）
    # Agent 孤儿 run 收尸：执行者随上轮进程消失，未终态 run 不可能再推进
    try:
        from app.domains.agent import service as agent_service

        await agent_service.reconcile_orphan_runs()
    except Exception:  # noqa: BLE001
        log_event(logger, "Agent 孤儿运行收尸失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    start_scheduler()
    log_event(logger, "后台收拾链完成，服务已就绪（调度器已启动）")

    # 按需补齐（调度器启动后，网络步骤不占开门链）：种子缺失时后台下载并
    # 并入；全新安装的汇率全史 bootstrap。两者失败只留状态/日志，下次启动重来。
    try:
        from app.core import seed_fetch

        await seed_fetch.ensure_seed_online()
    except Exception:  # noqa: BLE001
        log_event(logger, "种子按需获取失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        from app.domains.rates import history as rates_history

        await rates_history.bootstrap_full_history()
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率全史初始化失败，不阻塞启动", level=logging.ERROR, exc_info=True)


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
    log_event(
        logger,
        f"{APP_NAME} {settings.version} 已启动并监听端口，后台收拾链进行中",
        detail={
            "数据目录": settings.data_dir,
            "目录布局": LAYOUT_LABEL.get(layout, layout),
            "端口": settings.port,
        },
    )
    asyncio.create_task(_post_startup_chain())
    yield
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

    @app.middleware("http")
    async def _add_static_cache_headers(request, call_next):
        response = await call_next(request)
        cache_control = _cache_control_for(request.url.path.lstrip("/"))
        if cache_control:
            response.headers.setdefault("Cache-Control", cache_control)
        return response

    app.include_router(system_router, prefix="/api/v1")
    app.include_router(agent_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(games_router, prefix="/api/v1")
    app.include_router(achievements_router, prefix="/api/v1")
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
