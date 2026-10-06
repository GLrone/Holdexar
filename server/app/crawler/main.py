"""Holdexar 爬虫 CLI 入口（调试与手动任务）。

用法（在 server/ 目录下）:
    python -m app.crawler --appids 620 --regions cn,ua
    python -m app.crawler --appids 570,620 --workers 10
    python -m app.crawler --queue games.json            # 任务队列文件

抓取层为 IStoreBrowseService（app/crawler/browse_store.py），任务编排委托
runner.run_crawl，与生产服务端（domains/crawl）同一条链路。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from ..core.app_info import APP_NAME, ENV_PREFIX
from ..core.logging import log_event
from .config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT
from .runner import CrawlRunConfig, run_crawl

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.crawler", description=f"{APP_NAME} 爬虫（IStoreBrowseService 版）"
    )
    parser.add_argument("--appids", default="", help="逗号分隔的 appid 列表，如 620,570")
    parser.add_argument(
        "--regions", default="",
        help="逗号分隔的区服代码（小写 cc），如 cn,ua；缺省为启用区集",
    )
    parser.add_argument("--queue", default="", help="任务队列 JSON 文件路径")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKER_COUNT)
    parser.add_argument(
        "--proxy", default=None,
        help=f"代理 URL（如 http://127.0.0.1:7897 或 socks5://...）；缺省直连——"
             f"browse 接口按 country_code 返回各区数据，仅显式指定或环境变量 "
             f"{ENV_PREFIX}PROXY_URL 时走代理（调试用）",
    )
    parser.add_argument(
        "--timeout", type=int, default=HTTP_TIMEOUT, help="单请求总超时秒数（默认 20）"
    )
    parser.add_argument("--debug", action="store_true", help="DEBUG 日志")
    return parser


def _load_appids(args) -> list[tuple[int, str]]:
    appids: list[tuple[int, str]] = []
    if args.appids:
        for part in str(args.appids).split(","):
            part = part.strip()
            if part.isdigit():
                appids.append((int(part), ""))
        return appids

    if args.queue:
        path = Path(args.queue)
        if not path.exists():
            log_event(
                logger,
                "任务队列文件不存在，无法读取任务",
                level=logging.ERROR,
                detail={"文件": str(path)},
            )
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "任务队列文件解析失败，无法读取任务",
                level=logging.ERROR,
                detail={"文件": str(path), "原因": str(e)},
            )
            return []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, (int, str)) and str(item).isdigit():
                    appids.append((int(item), ""))
                elif isinstance(item, dict):
                    aid = item.get("appid") or item.get("id") or item.get("app_id")
                    if aid:
                        appids.append((int(aid), item.get("name", "")))
    return appids


async def async_main(args) -> int:
    # 直连为标准形态：browse 按 country_code 返回各区数据、出口 IP 不参与
    # 判定，加速器在系统网络层透明生效。代理仅显式指定时启用（调试通道）。
    proxy_url = args.proxy or os.environ.get(f"{ENV_PREFIX}PROXY_URL")
    if proxy_url:
        log_event(logger, "已启用显式指定的代理抓取", detail={"代理": proxy_url})
    else:
        log_event(logger, "以直连模式抓取，未配置代理")

    from app.domains.regions.service import effective_regions

    explicit = [r.strip().lower() for r in str(args.regions).split(",") if r.strip()]
    regions = await effective_regions(explicit or None)
    log_event(
        logger,
        f"本次抓取覆盖 {len(regions)} 个区服",
        detail={"区服": ", ".join(regions)},
    )

    appids = _load_appids(args)
    if not appids:
        log_event(
            logger,
            "没有可执行的任务，请用 --appids 或 --queue 指定",
            level=logging.ERROR,
        )
        return 1
    log_event(
        logger,
        f"任务就绪：{len(appids)} 款游戏、{args.workers} 个 worker",
        detail={
            "游戏数": len(appids),
            "游戏ID": ", ".join(str(a) for a, _ in appids),
            "worker 数": args.workers,
        },
    )

    # 与生产同口径入运行账（CLI 一次调用 = agent_runs 一行；账本失败不拖垮抓取）
    from app.domains.agent.runtime import scheduler_bridge

    async def _crawl_body():
        body_stats = await run_crawl(
            appids,
            config=CrawlRunConfig(
                regions=regions, workers=args.workers, proxy_url=proxy_url, timeout=args.timeout
            ),
        )
        await scheduler_bridge.note_step("crawl", {
            "total": body_stats.get("total"),
            "processed": body_stats.get("processed"),
            "success": body_stats.get("success"),
            "failed": body_stats.get("failed"),
        })
        return body_stats

    stats = await scheduler_bridge.run_accounted(
        "crawl_cli", _crawl_body, trigger="manual",
        ref={"appids": [a for a, _ in appids], "regions": regions,
             "workers": args.workers, "entry": "cli"},
    )
    log_event(
        logger,
        f"命令行抓取完成：共处理 {stats['processed']} 款，成功 {stats['success']} 款，失败 {stats['failed']} 款",
        detail={
            "处理": stats["processed"],
            "成功": stats["success"],
            "失败": stats["failed"],
        },
    )
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        log_event(logger, "用户中断，抓取提前结束", tag="未完成", level=logging.WARNING)
        return 130


if __name__ == "__main__":
    sys.exit(main())
