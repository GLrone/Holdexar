"""可复用的爬取运行器：CLI（app.crawler.main）与服务端任务（domains/crawl）共用。

抓取层为 IStoreBrowseService（app/crawler/browse_store.py），并发与 IP 调度沿用
CrawlerScheduler + SteamHttpClient 原装组件。

任务模型与旧 appdetails 链路的差异：
- 旧：1 任务 = 1 appid × 全区（appdetails 只能逐 appid 逐区请求）
- 新：1 任务 = 1 区 × ≤400 appid（browse 一发批量），补抓层同款——
  欠账账本按区分组凑批发（generate_missing_tasks），不再逐行一发。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..core.database import init_db
from . import browse_store as bs
from .config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT
from .http_client import SteamHttpClient
from .router import CrawlerRouter
from .scheduler import CrawlerScheduler
from .handlers.bundle_handler import handle_bundle_task

logger = logging.getLogger(__name__)


@dataclass
class CrawlRunConfig:
    regions: list[str] | None = None
    workers: int = DEFAULT_WORKER_COUNT
    proxy_url: str | None = None
    timeout: int = HTTP_TIMEOUT
    # direct_first 失败换代理：直连传输失败/429 时调此取代理出口。
    # 由调用方按策略注入（crawl service）：direct_first/proxy_first/proxy_only
    # 给 resolver，direct_only 严格语义给 None。返回 None = 无可用
    # 代理，沿用退避重试
    failover_proxy_resolver: Callable[[], Awaitable[str | None]] | None = None


def build_router() -> CrawlerRouter:
    router = CrawlerRouter()
    router.handle("app")(bs.handle_browse_price_task)
    # 捆绑包 lane 任务：单协程并行抓价（发现即爬 + 播种无价包）
    router.handle("bundle")(handle_bundle_task)
    return router


def _build_app_tasks(
    appids: list[int], regions: list[str], extras: bool
) -> list[dict]:
    """appid 集 → 每区分批任务（1 任务 = 1 区 × ≤400 appid）。"""
    tasks: list[dict] = []
    for cc in regions:
        for i, batch in enumerate(
            bs.StoreBrowseAPI.plan_batches(
                appids, cc, "english", extras, bs.DEFAULT_BATCH_SIZE
            )
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
    """执行一批 app 任务，返回统计 dict。

    appids 走常规全量任务（每区分批，全区抓）；pre_tasks 为预构建任务
    （补抓层的按区批量 app 任务，只装该区欠账行），
    两者可同时给（关注层 + 补抓层合并一批）。
    """
    await init_db()
    bs.reset_run_state()

    # ── 区服配置：严格按配置爬取，无效配置直接失败，绝不静默回退全区（防"乱爬"）──
    regions = [str(r).strip().lower() for r in (config.regions or []) if str(r).strip()]
    if not regions:
        raise ValueError("任务缺少区服配置，拒绝全区回退（请检查「我」页区服设置）")

    db = bs.BrowseDbWriter()
    await db.connect()

    # browse 拿不到的 games 列（chinese_support/genres/is_visual_novel…）保留库内原值，
    # 否则 upsert 会把它们抹成 NULL
    from ..core.config import get_settings

    bs.PRESERVED.update(
        bs.load_preserved_rows(Path(get_settings().data_dir) / "holdexar.db")
    )

    http_client = SteamHttpClient(
        timeout=config.timeout, max_retries=3, proxy_url=config.proxy_url,
        failover_proxy_resolver=config.failover_proxy_resolver,
    )
    router = build_router()

    target_ids = [int(a) for a, _ in (appids or [])]
    tasks: list[dict] = list(pre_tasks or []) + _build_app_tasks(
        target_ids, regions, bs.EXTRAS_ENABLED
    )

    # ── 捆绑包 lane 播种：无区域价的捆绑包（发现桩/上次失败 24h 冷却后
    # 重试），与 app 任务并行、全程单协程、排干自动收尾 ──
    from datetime import datetime, timedelta

    try:
        pending_bundles = await db.get_pending_bundle_ids(
            datetime.utcnow() - timedelta(hours=24)
        )
    except Exception:  # noqa: BLE001
        pending_bundles = []
    lane_tasks = [{"type": "bundle", "id": bid} for bid in pending_bundles]
    if lane_tasks:
        logger.info("[捆绑包lane] 播种 %d 个无价捆绑包（单协程并行）", len(lane_tasks))

    logger.info(
        "任务就绪：常规 %d + 预构建 %d + 捆绑包lane %d | appids=%d regions=%s workers=%d proxy=%s",
        len(_build_app_tasks(target_ids, regions, bs.EXTRAS_ENABLED)),
        len(pre_tasks or []),
        len(lane_tasks),
        len(target_ids),
        ",".join(regions),
        config.workers,
        config.proxy_url or "直连",
    )

    connector = aiohttp.TCPConnector(limit=config.workers * 2, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        # ── 预取：元数据（schinese/english 两轮，跨回退区补齐）+ RU CIS 基准 ──
        if target_ids:
            zh = await bs.prefetch_lang(
                session, http_client, target_ids, "schinese", bs.EXTRAS_ENABLED,
                bs.DEFAULT_BATCH_SIZE,
            )
            en = await bs.prefetch_lang(
                session, http_client, target_ids, "english", bs.EXTRAS_ENABLED,
                bs.DEFAULT_BATCH_SIZE,
            )
            for aid in target_ids:
                if aid in zh or aid in en:
                    bs.META[aid] = bs.StoreBrowseAPI.build_meta(zh.get(aid), en.get(aid))
            logger.info("[预取] 元数据覆盖 %d/%d", len(bs.META), len(target_ids))
            if "ru" in regions:
                await bs.prefetch_cis(
                    session, http_client, target_ids, bs.EXTRAS_ENABLED,
                    bs.DEFAULT_BATCH_SIZE,
                )

        # failure_ledger：browse 层重试耗尽的批次只记账本不抛异常，调度器
        # 对外口径（进度事件与这里的返回统计）必须把账本并入「失败」
        scheduler = CrawlerScheduler(
            router, http_client, db, worker_count=config.workers, stop_event=stop_event,
            failure_ledger=bs.FAILED_TASKS,
        )
        await scheduler.run(tasks + lane_tasks, session)

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
