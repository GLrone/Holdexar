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
        help=f"代理 URL（如 http://127.0.0.1:7897 或 socks5://...）；缺省读环境变量 "
             f"{ENV_PREFIX}PROXY_URL，未设置则走策略引擎",
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
            logger.error("队列文件不存在: %s", path)
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.error("队列文件解析失败: %s", e)
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
    proxy_url = args.proxy or os.environ.get(f"{ENV_PREFIX}PROXY_URL")
    if proxy_url is None:
        from app.domains.proxies.service import resolve_proxy_url

        proxy_url = await resolve_proxy_url()
    if proxy_url:
        logger.info("代理已启用: %s", proxy_url)
    else:
        logger.info("代理未配置，直连模式（网络慢时可用 --proxy http://127.0.0.1:7897）")

    from app.domains.regions.service import effective_regions

    explicit = [r.strip().lower() for r in str(args.regions).split(",") if r.strip()]
    regions = await effective_regions(explicit or None)
    logger.info("生效区服: %s", ", ".join(regions))

    appids = _load_appids(args)
    if not appids:
        logger.error("没有任务：用 --appids 或 --queue 指定")
        return 1
    logger.info("任务就绪：appids=%s workers=%d", [a for a, _ in appids], args.workers)

    stats = await run_crawl(
        appids,
        config=CrawlRunConfig(
            regions=regions, workers=args.workers, proxy_url=proxy_url, timeout=args.timeout
        ),
    )
    logger.info(
        "完成：处理 %d | 成功 %d / 失败 %d",
        stats["processed"], stats["success"], stats["failed"],
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
        logger.warning("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
