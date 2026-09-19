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
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..core.database import init_db
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
    # 直连为标准形态（browse 按 country_code 返回各区数据）；proxy_url
    # 仅供调试通道显式指定（CLI --proxy / 环境变量），生产路径不传
    proxy_url: str | None = None
    timeout: int = HTTP_TIMEOUT


def build_router() -> CrawlerRouter:
    router = CrawlerRouter()
    router.handle("app")(bs.handle_browse_price_task)
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
    """生产爬取入口：取得 crawler 占用后执行，结束（含异常）必定释放。

    占用放在这里而不是调用方的 job 表上——bundles 链尾是**直调**本函数的，
    只有把门禁落在执行入口，两条路径才会真正互斥。
    """
    begin_crawl("run_crawl")
    try:
        return await _run_crawl_locked(
            appids, config=config, stop_event=stop_event, pre_tasks=pre_tasks
        )
    finally:
        end_crawl()


async def _run_crawl_locked(
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
        timeout=config.timeout, max_retries=3, proxy_url=config.proxy_url
    )
    router = build_router()

    target_ids = [int(a) for a, _ in (appids or [])]
    tasks: list[dict] = list(pre_tasks or []) + _build_app_tasks(
        target_ids, regions, bs.EXTRAS_ENABLED
    )

    logger.info(
        "任务就绪：常规 %d + 预构建 %d | appids=%d regions=%s workers=%d proxy=%s",
        len(_build_app_tasks(target_ids, regions, bs.EXTRAS_ENABLED)),
        len(pre_tasks or []),
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
