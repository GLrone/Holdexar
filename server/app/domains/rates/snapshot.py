"""CNY 价格快照重算 + 排序快照刷新的**事务编排**（汇率变更的原子刷新边界）。

一致性铁律（本模块存在的唯一理由）：
- 汇率是派生数据的根：cny_fen = 本币价 × rate_to_cny，games/bundles 的排序
  快照（min_cny_fen/diff_fen/…）又由 cny_fen 聚合而来；
- 若「汇率已提交、cny_fen 还是旧汇率」或「cny_fen 已重算、排序快照还是旧值」，
  GET 就会读到半刷新状态（rates=新 / cny_fen=旧 / diff_fen=旧）。
  因此三件事必须在**同一事务**内一次提交：

      BEGIN
        fx_rates                     ← 调用方（rates.service.refresh_rates）写入
        recompute_cny_fen_all        → game_current_prices / bundle_region_prices
        games.refresh_sort_cache     → games.min_cny_fen / diff_fen
        bundles.refresh_bundle_sort_cache → bundles.min_cny_fen / diff_fen / is_lowest
      COMMIT

- 数据源是**本事务内的 fx_rates 行**，不是进程内 get_rates() 缓存（300s TTL）
  ——「DB 新 / cache 旧」会把刚解决的一致性问题重新引回来；
- 库为 WAL + busy_timeout：重建期间并发 GET 不阻塞（读到上一次提交的完整
  快照），提交瞬间原子切到新快照。规模参考：game_current_prices ≈ 123 万行、
  bundle_region_prices ≈ 14 万行，单事务秒级。

历史价格快照（game_price_history.cny_fen）**故意不重算**：它是当日快照的
账本，记的是「当时的 CNY」，用今天的汇率改写过去等于篡改历史。
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# cny_fen = ROUND(price(分) × rate_to_cny)：与写库侧
# （crawler/db_writer._compute_cny_fen、games/pricing.convert_minor_to_cny_fen）
# 同一口径。UPPER() 兜脏行大小写（写库侧按原样匹配，旧行可能有小写币种）。
# 不命中的行（无对应币种汇率/单价缺失）保留原值，不回写 NULL。
_RECOMPUTE_GAMES_SQL = text(
    """
    UPDATE game_current_prices AS p
       SET cny_fen = CAST(ROUND(p.price * r.rate_to_cny) AS INTEGER)
      FROM fx_rates AS r
     WHERE r.currency_code = UPPER(p.currency)
       AND p.price IS NOT NULL AND p.price > 0
    """
)

_RECOMPUTE_BUNDLES_SQL = text(
    """
    UPDATE bundle_region_prices AS p
       SET cny_fen = CAST(ROUND(p.price * r.rate_to_cny) AS INTEGER)
      FROM fx_rates AS r
     WHERE r.currency_code = UPPER(p.currency)
       AND p.price IS NOT NULL AND p.price > 0
    """
)


async def recompute_cny_fen_all(session: AsyncSession) -> dict[str, int]:
    """用**本事务内**的 fx_rates 快照重算全部 cny_fen（games + bundles 两表）。

    调用方负责提交（本函数不 commit）：保证「汇率 → cny_fen → 排序快照」
    落在同一事务边界内。返回各表写入行数 {"games": n, "bundles": m}。
    """
    games = await session.execute(_RECOMPUTE_GAMES_SQL)
    bundles = await session.execute(_RECOMPUTE_BUNDLES_SQL)
    counts = {"games": games.rowcount or 0, "bundles": bundles.rowcount or 0}
    logger.info(
        "CNY 快照重算（同事务）：games %d 行 / bundles %d 行",
        counts["games"], counts["bundles"],
    )
    return counts


async def rebuild_sort_snapshots(session: AsyncSession) -> dict[str, int]:
    """重建两域排序快照（同事务调用，读到的 cny_fen 一定是本事务刚算的）。

    games：min_cny_fen / diff_fen（refresh_sort_cache）
    bundles：min_cny_fen / diff_fen / is_lowest（refresh_bundle_sort_cache）
    不提交。返回各域写入行数 {"games": n, "bundles": m}。
    """
    from app.domains.bundles import service as bundles_service
    from app.domains.games import service as games_service

    games = await games_service.refresh_sort_cache(session=session)
    bundles = await bundles_service.refresh_bundle_sort_cache(session=session)
    logger.info(
        "排序快照重建（同事务）：games %d 款 / bundles %d 个", games, bundles
    )
    return {"games": games, "bundles": bundles}
