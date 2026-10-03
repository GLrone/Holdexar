"""可复用的爬取运行器：CLI（app.crawler.main）与服务端任务（domains/crawl）共用。

抓取层为 IStoreBrowseService（app/crawler/browse_store.py），并发调度沿用
CrawlerScheduler + SteamHttpClient 原装组件（请求频率由全局限流闸统一约束，
见 crawler/rate_limit.py）。

任务模型与旧 appdetails 链路的差异：
- 旧：1 任务 = 1 appid × 全区（appdetails 只能逐 appid 逐区请求）
- 新：1 任务 = 1 区 × ≤400 appid（browse 一发批量），补抓层同款——
  欠账账本按区分组凑批发（generate_missing_tasks），不再逐行一发。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from ..core.database import init_db
from ..domains.proxypool import jobruns
from . import browse_store as bs
from .config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT
from .occupancy import begin_crawl, end_crawl
from .http_client import SteamHttpClient
from .router import CrawlerRouter
from .scheduler import CrawlerScheduler

logger = logging.getLogger(__name__)


@dataclass
class CrawlRunConfig:
    regions: list[str] | None = None
    workers: int = DEFAULT_WORKER_COUNT
    # 单入口代理（调试通道 / 单入口形态）；生产多出口形态用 proxy_urls
    proxy_url: str | None = None
    # 多出口形态：每条 lane 一个本机代理地址，worker 按序号固定绑定
    proxy_urls: list[str] | None = None
    # 与 proxy_urls 同序的出口 IP：限流、熔断与统计都按它记账（同出口的 worker 共享一份）
    exit_keys: list[str] | None = None
    # 与 proxy_urls 同序的节点运行名：出口账本要能追到"当时是哪个节点在执行"
    exit_nodes: list[str] | None = None
    timeout: int = HTTP_TIMEOUT


def build_worker_clients(
    config: "CrawlRunConfig", http_client: SteamHttpClient, stats=None
) -> Callable[[int], SteamHttpClient] | None:
    """多出口形态下的 worker → 出口工厂：**一 worker 一条 lane，run 内固定**。

    限流预算、429 熔断器、统计账本全部**按出口 IP** 取（同出口的多个 worker 共享同一
    份）：某个出口撞风控或撞窗口只影响绑在它上面的 worker，其余出口继续跑；也保证
    「一个出口放 N 个 worker」不会放大成 N 份额度。

    `proxy_urls` 为空（单入口形态）返回 `None`——调用方沿用共享客户端，行为不变。
    """
    urls = [u for u in (config.proxy_urls or []) if u]
    if not urls:
        return None
    from .exit_stats import ExitStatsCollector  # noqa: F401 —— 类型提示用，保持同域可见
    from .http_client import ExitBreakers
    from .rate_limit import ExitRateLimits

    keys = list(config.exit_keys or [])
    exit_keys = [keys[i] if i < len(keys) else "" for i in range(len(urls))]
    limits = ExitRateLimits(exit_keys)
    breakers = ExitBreakers(exit_keys)

    def factory(worker_id: int) -> SteamHttpClient:
        idx = worker_id % len(urls)
        key = exit_keys[idx]
        return SteamHttpClient(
            timeout=config.timeout,
            max_retries=3,
            proxy_url=urls[idx],
            rate_limiter=limits.for_exit(key),
            breaker=breakers.for_exit(key),
            stats=stats,
            exit_key=key or None,
        )

    return factory


def build_router() -> CrawlerRouter:
    router = CrawlerRouter()
    router.handle("app")(bs.handle_browse_price_task)
    return router


def _build_app_tasks(
    appids: list[int], regions: list[str], extras: bool, workers: int = 0
) -> list[dict]:
    """appid 集 → 每区分批任务（1 任务 = 1 区 × ≤300 appid）。

    批大小生成顺序：本轮有效 worker 数 → 目标批 = appids × 区数 ÷ workers
    （任务总数 ≈ worker 数，每个 worker 都有任务在飞，削单请求长尾对整轮
    的拖累）→ 300 条上限收口 → URL 长度兜底再切（plan_batches）。各区共用
    同一 size，跨区批次的 appids 保持对齐。
    """
    n = len(appids)
    size = bs.DEFAULT_BATCH_SIZE
    if workers > 0 and regions and n:
        size = max(1, min(size, -(-n * len(regions) // workers)))
    tasks: list[dict] = []
    for cc in regions:
        for i, batch in enumerate(
            bs.StoreBrowseAPI.plan_batches(appids, cc, "english", extras, size)
        ):
            tasks.append(
                {"type": "app", "id": f"{cc}:{i + 1}", "region": cc, "appids": batch}
            )
    return tasks


async def run_crawl(
    appids: list[tuple[int, str]] | None,
    *,
    config: CrawlRunConfig,
    stop_event: asyncio.Event | None = None,
    pre_tasks: list[dict] | None = None,
) -> dict:
    """生产爬取入口：取得 crawler 占用后执行，结束（含异常）必定释放。

    占用放在这里而不是调用方的 job 表上——bundles 链尾是**直调**本函数的，
    只有把门禁落在执行入口，两条路径才会真正互斥。

    **生产作业台账也落在这里**（同一理由：这里是唯一入口，写在上层 job 表上会漏掉
    bundles 直调与 CLI）。台账两笔写入（开始 `running` / 结束终态）全部 fail-soft：
    记录失败只留日志，绝不改变爬取行为。
    """
    begin_crawl("run_crawl")
    started_monotonic = time.monotonic()
    summary = jobruns.new_error_summary()
    # 出口账本：内存累计，作业收尾一次性落库（不按请求写库）
    from .exit_stats import ExitStatsCollector

    exit_stats = ExitStatsCollector()
    node_by_exit = {
        str(k): str(v)
        for k, v in zip(config.exit_keys or [], config.exit_nodes or [])
        if k
    }
    run_id: int | None = None
    try:
        # 台账要写库，建表必须先于记录（init_db 幂等且是 lru 化的连接入口；
        # 在这里调用才能让"作业开始"这一笔真的在开始时刻落下）
        await init_db()
        from ..core.config import get_settings

        run_id = await jobruns.record_start(
            proxy_url=config.proxy_url,
            regions=config.regions,
            workers=config.workers,
            data_dir=Path(get_settings().data_dir),
            now=datetime.now(),
        )
        stats = await _run_crawl_locked(
            appids,
            config=config,
            stop_event=stop_event,
            pre_tasks=pre_tasks,
            error_sink=lambda exc: jobruns.note_error(summary, exc),
            exit_stats=exit_stats,
        )
        # browse 层重试耗尽的失败从不抛到 worker、只进 failure_ledger（已并入
        # stats["failed"]），分类上单独记一类，别混进 other
        jobruns.note_ledger(summary, len(bs.FAILED_TASKS))
        exit_stats.merge_into(summary)
        stopped = bool(stop_event is not None and stop_event.is_set())
        if stopped:
            # 「这次没跑完」是行级事实：手动停止与进程中断共用同一个标记，
            # 与启动清理写进去的那条保持同一语义（状态列仍是 interrupted）
            summary["interrupted"] = True
        status = jobruns.classify_outcome(
            int(stats.get("success") or 0),
            int(stats.get("failed") or 0),
            stopped=stopped,
        )
        await jobruns.record_finish(
            run_id,
            status=status,
            now=datetime.now(),
            # `task_count` = 本次**计划**任务数（`scheduler.total_target` = 初始任务量），
            # 不是已完成数：中断时计划 2、完成 1，台账必须记 2 —— 记 1 就等于篡改了
            # 「这次一共打算跑多少」。已完成数由 success_count + error_count 表达。
            task_count=int(stats.get("total") or 0),
            success_count=int(stats.get("success") or 0),
            error_count=int(stats.get("failed") or 0),
            error_summary=summary,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        )
        await _write_exit_ledger(run_id, exit_stats, node_by_exit)
        return stats
    except asyncio.CancelledError:
        summary["interrupted"] = True
        await jobruns.record_finish(
            run_id,
            status=jobruns.STATUS_INTERRUPTED,
            now=datetime.now(),
            error_summary=summary,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        )
        raise
    except Exception as exc:  # noqa: BLE001 —— 记完再抛，行为不变
        jobruns.note_error(summary, exc)
        exit_stats.merge_into(summary)
        await jobruns.record_finish(
            run_id,
            status=jobruns.STATUS_FAILED,
            now=datetime.now(),
            error_summary=summary,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        )
        await _write_exit_ledger(run_id, exit_stats, node_by_exit)
        raise
    finally:
        end_crawl()
        # 占用释放广播： bundles 链尾等直调路径不建 job 行、没有 job.status
        # 收尾事件，订阅端据此把「抓取进行中」的实时标志拉回空闲
        from ..core.events import bus

        bus.publish("crawl.idle")


async def _write_exit_ledger(run_id, exit_stats, node_by_exit) -> None:
    """出口账本落库（fail-soft：写不进去只记日志，不改作业结果）。"""
    from ..domains.proxypool import exitstats

    await exitstats.write_run_exits(
        run_id=run_id, collector=exit_stats, node_by_exit=node_by_exit
    )


async def _run_crawl_locked(
    appids: list[tuple[int, str]] | None,
    *,
    config: CrawlRunConfig,
    stop_event: asyncio.Event | None = None,
    pre_tasks: list[dict] | None = None,
    error_sink: Callable[[BaseException], None] | None = None,
    exit_stats=None,
) -> dict:
    """执行一批 app 任务，返回统计 dict。

    appids 走常规全量任务（每区分批，全区抓）；pre_tasks 为预构建任务
    （补抓层的按区批量 app 任务，只装该区欠账行），
    两者可同时给（关注层 + 补抓层合并一批）。
    """
    bs.reset_run_state()

    # ── 区服配置：严格按配置爬取，无效配置直接失败，绝不静默回退全区（防"乱爬"）──
    regions = [str(r).strip().lower() for r in (config.regions or []) if str(r).strip()]
    if not regions:
        raise ValueError("任务缺少区服配置，拒绝全区回退（请检查「我」页区服设置）")

    db = bs.BrowseDbWriter()
    await db.connect()

    # browse 拿不到的 games 列（chinese_support/is_visual_novel…）保留库内原值，
    # 否则 upsert 会把它们抹成 NULL
    from ..core.config import get_settings

    bs.PRESERVED.update(
        bs.load_preserved_rows(Path(get_settings().data_dir) / "holdexar.db")
    )

    http_client = SteamHttpClient(
        timeout=config.timeout, max_retries=3, proxy_url=config.proxy_url
    )
    client_factory = build_worker_clients(config, http_client, stats=exit_stats)
    router = build_router()

    target_ids = [int(a) for a, _ in (appids or [])]
    built_tasks = _build_app_tasks(
        target_ids, regions, bs.EXTRAS_ENABLED, config.workers
    )
    tasks: list[dict] = list(pre_tasks or []) + built_tasks

    logger.info(
        "任务就绪：常规 %d + 预构建 %d | appids=%d regions=%s workers=%d 入口=%s",
        len(built_tasks),
        len(pre_tasks or []),
        len(target_ids),
        ",".join(regions),
        config.workers,
        f"{len(config.proxy_urls)} 条 lane" if client_factory
        else (config.proxy_url or "直连"),
    )

    connector = aiohttp.TCPConnector(limit=config.workers * 2, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        # ── 预取：只补库内没有的对象（首爬新面孔）──
        # 已知对象（games 表有行，PRESERVED 已整表加载）不再走网络预取：
        # 价格轮对它们只需 appid + 库内名字核对，元数据由 handler 回退
        # PRESERVED（type 过滤 / name_en 版本后缀提取 / 建行原值保留）。
        # 整轮两语言全量预取会把 worker 启动挡在分钟级串行请求之后。
        if target_ids:
            unknown = [a for a in target_ids if a not in bs.PRESERVED]
            if unknown:
                zh = await bs.prefetch_lang(
                    session, http_client, unknown, "schinese", bs.EXTRAS_ENABLED,
                    bs.DEFAULT_BATCH_SIZE,
                )
                en = await bs.prefetch_lang(
                    session, http_client, unknown, "english", bs.EXTRAS_ENABLED,
                    bs.DEFAULT_BATCH_SIZE,
                )
                for aid in unknown:
                    if aid in zh or aid in en:
                        bs.META[aid] = bs.StoreBrowseAPI.build_meta(zh.get(aid), en.get(aid))
                logger.info(
                    "[预取] 元数据覆盖 %d/%d（网络预取 %d 款，库内原值 %d 款）",
                    len(bs.META), len(target_ids), len(unknown),
                    len(target_ids) - len(unknown),
                )
        # failure_ledger：browse 层重试耗尽的批次只记账本不抛异常，调度器
        # 对外口径（进度事件与这里的返回统计）必须把账本并入「失败」
        scheduler = CrawlerScheduler(
            router, http_client, db, worker_count=config.workers, stop_event=stop_event,
            failure_ledger=bs.FAILED_TASKS,
            error_sink=error_sink,
            client_factory=client_factory,
            # 入口总数即出口兵源数：大于 worker 数时多出的部分是待用出口池，
            # 被隔离的 lane 由它们顶替（worker 岗位数不掉）
            lane_count=len(config.proxy_urls or []) or None,
        )
        await scheduler.run(tasks, session)

        done, ok, failed = scheduler.counts()
        return {
            "total": scheduler.total_target,
            "processed": done,
            "success": ok,
            "failed": failed,
            "skipped_no_discount": 0,
            "discount_ended": 0,
            "elapsed_seconds": 0,  # scheduler.run 内部已日志，此处由调用方补充
        }
