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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from ..core.database import init_db
from ..domains.proxypool import jobruns
from . import browse_store as bs
from .config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT
from .http_client import SteamHttpClient
from .occupancy import begin_crawl, end_crawl
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

    占用放在执行入口而不是调用方的 job 表上——bundles 链尾是**直调**本函数的，
    门禁落在入口两条路径才真正互斥。

    **生产作业台账也落在这里**（同一理由：唯一入口；写在上层 job 表上会漏掉 bundles
    直调与 CLI）。台账两笔写入（开始 `running` / 结束终态）全部 fail-soft：记录失败
    只留日志，不改变爬取行为。
    """
    begin_crawl("run_crawl")
    started_monotonic = time.monotonic()
    summary = jobruns.new_error_summary()
    run_id: int | None = None
    try:
        # 台账要写库：建表先于记录（init_db 幂等，是 lru 化的连接入口）
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
        )
        # browse 层重试耗尽的失败从不抛到 worker、只进 failure_ledger（已并入
        # stats["failed"]），分类上单独记一类，别混进 other
        jobruns.note_ledger(summary, len(bs.FAILED_TASKS))
        stopped = bool(stop_event is not None and stop_event.is_set())
        if stopped:
            # 「这次没跑完」是行级事实：手动停止与进程中断共用同一标记
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
            # `task_count` = 本次**计划**任务数（`total` = 初始任务量），不是已完成数：
            # 中断时计划 2、完成 1，台账记 2；完成量由 success_count + error_count 表达
            task_count=int(stats.get("total") or 0),
            success_count=int(stats.get("success") or 0),
            error_count=int(stats.get("failed") or 0),
            error_summary=summary,
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        )
        return stats
    except Exception as exc:  # noqa: BLE001 —— 台账 fail-soft，异常照旧抛出
        try:
            jobruns.note_error(summary, exc)
            await jobruns.record_finish(
                run_id,
                status=jobruns.STATUS_FAILED,
                now=datetime.now(),
                error_summary=summary,
                duration_ms=int((time.monotonic() - started_monotonic) * 1000),
            )
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("[台账] 失败记录写入异常（不影响爬取）")
        raise
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
