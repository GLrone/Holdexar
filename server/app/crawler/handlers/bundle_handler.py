"""捆绑包 lane 任务处理器：爬取期间发现的 bundle-as-sub 即时整区抓价。

type="bundle" 任务由调度器单协程 lane 串行处理（与 app 任务并行，
全程 1 worker——42 区请求量级下节省 Steam 配额），语义复用 bundles
域刷新链：IStoreBrowseService 整区一批取价（bundleid/packageid 两种身份同接口，
先按库内身份问、未收录再换身份兜一次）→ 主档 + 区域价 upsert。
抓取失败不标记价格，touch 尝试时间戳后由播种层 24h 冷却重试。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def handle_bundle_task(context) -> None:
    """type=bundle：单包整区抓取落库（发现即爬，不等手动全量刷新）。"""
    bundle_id = int(context.task.get("id") or 0)
    if not bundle_id:
        return
    # 域层懒导入：bundles.refresh 懒引 crawler（runner），模块级互引会成环
    from app.domains.bundles.refresh import fetch_and_upsert_bundle
    from app.domains.games.service import get_rates

    rates = await get_rates()
    rate_map = dict(rates) if isinstance(rates, dict) else {}
    rate_map.setdefault("CNY", 1.0)
    regions = await fetch_and_upsert_bundle(
        context.session, bundle_id, rate_map, datetime.utcnow()
    )
    if not regions:
        # 失败不写价格、只记尝试时间戳（播种冷却判据）；死包不会每轮空转
        await context.db_writer.touch_bundle_attempt(bundle_id)
        logger.warning(
            "[捆绑包lane] bundle %s 抓取无数据（不存在/限速/网络），24h 后随播种重试",
            bundle_id,
        )
        return
    logger.info("[捆绑包lane] bundle %s 完成：%d 区价格", bundle_id, len(regions))
