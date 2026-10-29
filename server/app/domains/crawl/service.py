"""crawl 域服务：任务启动 / 停止 / 记录。

本地工具同一时间只允许一个爬取任务（重复启动返回 409）。
任务完成后自动触发价格提醒检查（alerts.check_appids）。
"""
from __future__ import annotations

import asyncio
import logging
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import bindparam, or_, select, text

from app.core.database import WritePriority, get_session_factory, write_session
from app.core.events import bus
from app.core.logging import log_event
from app.crawler.config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT, WORKERS_MAX
from app.crawler.runner import CrawlRunConfig, run_crawl
from app.crawler.utils import get_beijing_time_obj
from app.domains.alerts import service as alerts_service
from app.domains.crawl import cycle as price_cycle
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.proxies import env_check
from app.domains.regions.service import effective_regions
from app.domains.wishlist.models import WishlistItem

logger = logging.getLogger(__name__)

# 模块级引用：本机网络环境判定，测试按此符号打桩
_tun_mode_suspected = env_check.tun_mode_suspected


@dataclass
class JobHandle:
    id: int
    task: asyncio.Task
    stop_event: asyncio.Event
    started_monotonic: float = field(default_factory=time.monotonic)
    cycle_id: int | None = None


# 进程内活动任务表（单任务模型）
_active: JobHandle | None = None

# 链级停止位：在跑的 run_price_cycle（自动轮/手动全队列同源）登记，stop 端点
# 置位后链不再推进后续段；单任务模型下同一时刻至多一条链在跑
_chain_stop: asyncio.Event | None = None
_chain_cycle_id: int | None = None


def register_chain_stop(ev: asyncio.Event, *, cycle_id: int | None = None) -> None:
    global _chain_stop, _chain_cycle_id
    if _chain_stop is not ev or cycle_id is not None:
        _chain_cycle_id = cycle_id
    _chain_stop = ev


def active_cycle_id() -> int | None:
    return _chain_cycle_id


def unregister_chain_stop(ev: asyncio.Event) -> None:
    global _chain_stop, _chain_cycle_id
    if _chain_stop is ev:
        _chain_stop = None
        _chain_cycle_id = None


def chain_stop_requested() -> bool:
    return _chain_stop is not None and _chain_stop.is_set()


# start_job 预取占用的 tag（经 config.occupancy_tag 传给 run_crawl 复用）
JOB_OCCUPANCY_TAG = "crawl_job"


def active_job_id() -> int | None:
    return _active.id if _active else None


async def active_snapshot() -> dict:
    """当前活动任务、爬取占用与系统侧活动（/crawl/active 与工具面的同一事实源）。

    busy 只表达「抓取在跑」；体检/重建由 maintenance 单独表达，混进 busy
    会让前端把维护期误亮成「抓取进行中」。maintenanceInfo 是体检账本的
    实时投影（开跑时刻 / 已探测数 / totals）。"""
    from app.crawler.occupancy import (
        MAINTENANCE_HEALTH,
        current_holder,
        maintenance_kind,
        maintenance_reason,
    )
    from app.crawler.rate_limit import steam_rate_limiter
    from app.domains.proxypool import health as pool_health

    job_id = active_job_id()
    kind_now = maintenance_kind()
    maintenance_info = None
    if kind_now is not None:
        maintenance_info: dict = {"kind": kind_now, "reason": maintenance_reason()}
        live = await pool_health.current_maintenance_run()
        if live is not None:
            maintenance_info["startedAt"] = live["startedAt"]
            maintenance_info["probed"] = live["probed"]
            totals = live.get("totals")
            if totals is not None:
                maintenance_info["totals"] = totals
    return {
        "activeJobId": job_id,
        "busy": job_id is not None or current_holder() is not None,
        "maintenance": kind_now == MAINTENANCE_HEALTH,
        "maintenanceInfo": maintenance_info,
        "throttled": steam_rate_limiter.waiting,
    }


# 进程启动时刻（模块导入即进程启动）：只用于识别「上一个进程遗留」的任务——
# 后台收拾链可能晚于首个用户请求跑完，本进程监听后启动的任务不得被判为中断
_PROCESS_STARTED_AT = get_beijing_time_obj().replace(tzinfo=None)


async def cleanup_orphan_jobs() -> None:
    """进程启动时把上一进程遗留的 running 任务标记为失败。"""
    async with write_session(WritePriority.BACKGROUND) as session:
        rows = (
            await session.execute(
                select(CrawlJob).where(
                    CrawlJob.status == "running",
                    or_(
                        CrawlJob.started_at.is_(None),
                        CrawlJob.started_at < _PROCESS_STARTED_AT,
                    ),
                )
            )
        ).scalars().all()
        for job in rows:
            job.status = "failed"
            job.error = "进程重启中断"
            job.finished_at = datetime.now()
        if rows:
            await session.commit()
            log_event(
                logger,
                f"进程启动清理了 {len(rows)} 个上一进程遗留的运行中任务，已标记为失败",
                tag="已修复",
                level=logging.WARNING,
                detail={"数量": len(rows)},
            )
    await price_cycle.cleanup_orphan_cycles(before=_PROCESS_STARTED_AT)
    await price_cycle.recover_pending_stages()


# 爬取任务台账保留窗：与 proxy_job_runs 同为 90 天——两层台账靠时序对应，
# 窗一致对应关系才在窗内完整；price_cycles 整行归 crawl/retention.py 的周期
# 保留窗，本函数一行不碰
CRAWL_JOB_RETENTION_DAYS = 90
# 分块删除（每块一个事务过写闸）：一次长 DELETE 会长时间持写锁；删不完留给下一轮
_CRAWL_PRUNE_CHUNK = 5000
_CRAWL_PRUNE_MAX_CHUNKS = 20


async def prune_crawl_jobs(
    days: int = CRAWL_JOB_RETENTION_DAYS,
    chunk: int = _CRAWL_PRUNE_CHUNK,
    max_chunks: int = _CRAWL_PRUNE_MAX_CHUNKS,
) -> int:
    """清理窗口外的终态爬取任务台账行；price_cycles 整行不碰（归周期保留窗）。

    账龄按 finished_at 算（长任务的 started_at 会低估账龄）；开放态行
    （pending/running）finished_at 恒空，加 status 防御双保险。分块删除：
    每块一个事务过写闸，块间写者位空出；单轮块上限内删不完留给下一日续删。
    """
    from sqlalchemy import text
    from app.domains.crawl.retention import unfinished_job_units_sql

    await _unobserved_job_targets(0, settle=True, limit_rows=0)
    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0
    for _ in range(max_chunks):
        async with write_session(WritePriority.BACKGROUND, "crawl_prune") as session:
            result = await session.execute(
                text(
                    "DELETE FROM crawl_jobs WHERE id IN ("
                    "SELECT id FROM crawl_jobs"
                    " WHERE finished_at IS NOT NULL AND finished_at < :cutoff"
                    " AND status NOT IN ('pending', 'running')"
                    " AND NOT EXISTS (SELECT 1 FROM price_events e WHERE e.cycle_id=-crawl_jobs.id)"
                    " AND NOT EXISTS (SELECT 1 FROM notification_candidates n WHERE n.cycle_id=-crawl_jobs.id)"
                    " AND NOT (" + unfinished_job_units_sql() + ")"
                    " ORDER BY id LIMIT :chunk)"
                ),
                {"cutoff": cutoff, "chunk": chunk},
            )
            await session.commit()
        affected = int(result.rowcount or 0)
        deleted += affected
        if affected < chunk:
            return deleted
    return deleted


async def import_appids(appids: list[int]) -> dict:
    """批量导入：加入本地游戏目录 + 分类，**不建立持续监控关系**。

    - 只分类，不落任何监控来源：ok=待首爬 / own=已在库 / fail=无效
      （分类口径对齐 boards.backfill_specs 的缺口判定），供前端对
      「新导入」触发一次首爬（kind=import / fav_import）；
    - 不要求绑定 Steam 账户、不写 wishlist_items——目录层与监控层分离：
      目录 = Holdexar 知道这个游戏存在；持续监控只由用户显式关注建立。
    """
    results: list[dict] = []
    ok = own = fail = 0
    clean: list[int] = []
    for a in appids:
        try:
            appid = int(a)
        except (TypeError, ValueError):
            results.append({"appid": a, "status": "fail", "detail": "AppID 无效"})
            fail += 1
            continue
        if appid <= 0:
            results.append({"appid": a, "status": "fail", "detail": "AppID 无效"})
            fail += 1
            continue
        clean.append(appid)
    if clean:
        async with get_session_factory()() as session:
            has_price = (
                select(GameCurrentPrice.appid)
                .where(GameCurrentPrice.appid == Game.appid)
                .exists()
            )
            rows = (
                await session.execute(
                    select(Game.appid, Game.updated_at, has_price).where(
                        Game.appid.in_(clean)
                    )
                )
            ).all()
        known = {int(a): (u is not None, bool(p)) for a, u, p in rows}
        for appid in clean:
            updated, priced = known.get(appid, (False, False))
            if updated or priced:
                results.append({"appid": appid, "status": "own", "detail": "已在库"})
                own += 1
            else:
                results.append({"appid": appid, "status": "ok", "detail": "待首爬入库"})
                ok += 1

    # 目录层：导入不建立监控来源（无 active Monitoring source 是合法状态），
    # 首爬由前端对「新导入」触发，这里不自动开爬
    return {
        "results": results,
        "ok": ok,
        "own": own,
        "fail": fail,
    }


async def _wishlist_ordered(
    wl_ids: list[int], manual_ids: set[int], wishlisted_ids: set[int]
) -> list[int]:
    """监控条目优先级排序（愿望单 + 关注 = 第一优先级）。

    档位：关注（manual）> 愿望单（wishlisted）> hot（打折中任一区
    discount>0 / 史低 hl_flag）> 其余 appid 稳定序——第一优先级组内部
    保持既有细分（星标关注最靠前，价格更新的时间价值最高），第二优先级
    （已购/手动入池等普通监控条目）内部按 hot 优先、appid 殿后。
    所有池内条目均为必爬对象，此处只决定入队先后。
    pairs 序 = 入队序 = worker 消费序（FIFO），排头即先爬。
    """
    if not wl_ids:
        return []
    async with get_session_factory()() as session:
        hot_rows = (
            await session.execute(
                select(Game.appid)
                .where(
                    Game.appid.in_(wl_ids),
                    or_(
                        Game.hl_flag > 0,
                        Game.appid.in_(
                            select(GameCurrentPrice.appid).where(
                                GameCurrentPrice.discount_percent > 0
                            )
                        ),
                    ),
                )
            )
        ).scalars().all()
    hot = set(int(a) for a in hot_rows)
    first = manual_ids | wishlisted_ids
    return sorted(
        wl_ids,
        key=lambda a: (a not in manual_ids, a not in first, a not in hot, a),
    )


async def resolve_scope_appids(scope: str, appids: list[int] | None) -> list[tuple[int, str]]:
    """对象范围解析唯一出口：scope → 有序 (appid, "") 对。

    所有爬取入口共用本函数——手动任务（start_job 的 scope/appids）、
    scheduler specs（run_sequential → start_job）、Cycle 期望集冻结
    （plan_scope_appids）都从这里拿对象集合；新增爬取入口禁止自带
    一套对象解析。监视各入口是否绕行：grep resolve_scope_appids。

    scope: appids（显式列表，最高优先级，原样直用）| wishlist（全部活跃
    监控条目，含已购）| wishlist_only（活跃且非已购）| owned（活跃且已购）
    | pool（监控层）| catalog（目录层）| specials（特惠榜尾段）。
    前四种按第一优先级（愿望单/关注）→ hot（打折/史低）→ appid 序排。

    wishlist 含已购是历史合并路径（跟随模式沿用，不为拆分多付一次预检）；
    自定义已购区域时用 wishlist_only + owned 两个 job 分道抓取。

    pool 与 catalog 是两个不同的层，不是一个集合的两段：
    - pool = Monitoring：monitor_targets 里 state=active 的对象，来源是
      家族愿望单 / 关注 / 手动入池 / 已购 / 榜单，排除门在 state 上；
    - catalog = Catalog：games 主档里未被业务状态（下架宽限期外、永久免费）
      摘除的行，减去 monitor_targets 全部在册对象（active 由 pool 段覆盖，
      released / excluded 按状态定义不进 crawl）——价格库维护轮。
    主轮 6h 网格两段都跑（先 pool 后 catalog），总覆盖与合并成一个 job 时
    相同，但「谁被监控」与「库里有什么」不再互相推导。
    """
    if scope == "appids":
        return [(int(a), "") for a in (appids or [])]

    if scope in ("wishlist", "wishlist_only", "owned"):
        owned_filter = {"wishlist": None, "wishlist_only": False, "owned": True}[scope]
        async with get_session_factory()() as session:
            stmt = (
                select(
                    WishlistItem.appid, WishlistItem.manual, WishlistItem.wishlisted
                )
                .where(WishlistItem.active.is_(True))
                .distinct()
            )
            if owned_filter is not None:
                stmt = stmt.where(WishlistItem.owned.is_(owned_filter))
            rows = (await session.execute(stmt)).all()
        wl_ids = [int(r.appid) for r in rows]
        # 多账户同游戏多行：manual / wishlisted 按任一账户计；appid 去重保序
        # （distinct 对多列组合去不干净——SQLite DISTINCT 各列组合不同即保留）
        seen_ids: set[int] = set()
        deduped_ids: list[int] = []
        manual_ids = {int(r.appid) for r in rows if r.manual}
        wishlisted_ids = {int(r.appid) for r in rows if r.wishlisted}
        for a in wl_ids:
            if a in seen_ids:
                continue
            seen_ids.add(a)
            deduped_ids.append(a)
        wl_ids = deduped_ids
        # 下架脱池（宽限期外）：Steam 愿望单对下架游戏仍返回条目，
        # 留着只会让每日价格刷新全 41 区空转打 404；永久免费同理
        excluded = await _excluded_removed_appids() | await _excluded_free_appids()
        wl_ids = [a for a in wl_ids if a not in excluded]
        return [
            (a, "")
            for a in await _wishlist_ordered(wl_ids, manual_ids, wishlisted_ids)
        ]

    if scope == "pool":
        # 监控池：只取 Monitoring 层（有有效来源且未被排除）。下架宽限期外
        # 与永久免费是业务状态，在 crawl 侧再过滤一层——它们不写监控排除，
        # 仍留在 monitor_targets 里。
        from app.domains.monitoring import service as monitoring_service

        ids = await monitoring_service.crawl_order("game")
        if not ids:
            return []
        excluded = await _excluded_removed_appids() | await _excluded_free_appids()
        return [(a, "") for a in ids if a not in excluded]

    if scope == "catalog":
        # 目录层：games 主档里未被业务状态摘除的行，减去 monitor_targets
        # 全部在册对象——active 由 pool 段覆盖（同轮不重复爬，避免同价
        # 重复快照）；released / excluded 按状态定义不进任何 crawl 段
        # （排除门复用 monitoring.blocked_ids，不另立第二套排除账）。
        # appid 稳定序。
        from app.domains.monitoring import service as monitoring_service

        async with get_session_factory()() as session:
            pool_rows = (
                await session.execute(
                    select(Game.appid)
                    .where(
                        Game.removed_at.is_(None), Game.free_kind.is_(None)
                    )
                    .order_by(Game.appid)
                )
            ).scalars().all()
        monitored = set(await monitoring_service.active_ids("game"))
        monitored |= await monitoring_service.blocked_ids("game")
        return [(int(a), "") for a in pool_rows if int(a) not in monitored]

    if scope == "specials":
        # 特惠榜尾段：Steam 特惠+热门榜（翻页 5000 封顶）去重后垫在价格
        # 轮最后——与 pool/catalog 两段重合的对象不重复爬，尾段只剩榜单
        # 独有的差集（多为不在库的新面孔，爬取落库即完成目录发现）。
        # 榜单内容走三级缓存（热 1h → miss 实时拉取 → stale 兜底）。
        from app.domains.games import boards as games_boards
        from app.domains.monitoring import service as monitoring_service
        from app.domains.settings.service import get_value as _kv

        if not await _kv("fetch.boards", True):
            return []
        board_ids = await games_boards.get_board("specials")
        excluded = await _excluded_removed_appids() | await _excluded_free_appids()
        excluded |= await monitoring_service.blocked_ids("game")
        known = set(await monitoring_service.active_ids("game"))
        async with get_session_factory()() as session:
            catalog_rows = (
                await session.execute(
                    select(Game.appid).where(
                        Game.removed_at.is_(None), Game.free_kind.is_(None)
                    )
                )
            ).scalars().all()
        known.update(int(a) for a in catalog_rows)
        seen: set[int] = set()
        out: list[tuple[int, str]] = []
        for a in board_ids:
            appid = int(a)
            if appid in seen or appid in known or appid in excluded:
                continue
            seen.add(appid)
            out.append((appid, ""))
        return out

    if scope == "discounts":
        # 直连折扣队列：特惠榜（search specials 接口）**全量**作爬取对象——
        # 不减目录集（目录段直连默认关，榜内在库游戏由本段刷新价格），只减
        # 监控层（active 由 pool 段覆盖，同轮不重复爬）与业务排除（下架/
        # 永久免费）。新面孔爬取落库即完成目录发现。受 fetch.boards 门控。
        from app.domains.games import boards as games_boards
        from app.domains.monitoring import service as monitoring_service
        from app.domains.settings.service import get_value as _kv

        if not await _kv("fetch.boards", True):
            return []
        board_ids = await games_boards.get_board("specials")
        excluded = await _excluded_removed_appids() | await _excluded_free_appids()
        excluded |= await monitoring_service.blocked_ids("game")
        monitored = set(await monitoring_service.active_ids("game"))
        return [
            (int(a), "")
            for a in dict.fromkeys(board_ids)
            if int(a) not in monitored and int(a) not in excluded
        ]

    raise ValueError(f"未知 scope: {scope}")


async def plan_scope_appids(scope: str, appids: list[int] | None = None) -> list[int]:
    """本轮范围解析（Cycle planning 冻结期望集用）：只取对象 id 序列。

    与 `resolve_scope_appids` 同一出口，保证「本轮该刷谁」在冻结时刻与
    执行时刻口径一致；空列表 / 未知 scope 照旧抛 ValueError 由调用方跳过。
    """
    return [int(a) for a, _ in await resolve_scope_appids(scope, appids)]


# 欠账补抓冷却（分钟）。池价格爬取 6h 一轮 → 冷却是重试节奏的主闸：
# 5 次重试上限（MISSING_MAX_RETRIES）× 24h ≈ 5 天烧尽转 blocked——
# 持续多天的 429/锁区风暴也不会快速烧成终态，恢复后冷却期外自然续补。
MISSING_RETRY_COOLDOWN_MINUTES = 24 * 60

# 失败记录修复冷却（分钟）。修复线程 5min 一轮（空闲门禁），冷却须
# 低于轮转间隔——上一轮标记失败的区下一轮即可复访，不等主轮 24h；
# 不设 0 是防同轮内重复拾取。
REPAIR_RETRY_COOLDOWN_MINUTES = 4

# 单轮补抓行数上限（按账本行计，非请求数）。补抓按区分组批量后一发可装
# ≤300 行，请求量天然被压住；这个上限挡的是「积压账本一轮吃满」挤占
# 关注层——老账先补，余量留给下一轮。8000 行 ≈ 单区 20 发，写入量与
# 主轮常态持平。
MISSING_BATCH_ROW_LIMIT = 8000


async def _missing_tasks(
    cooldown_minutes: int = MISSING_RETRY_COOLDOWN_MINUTES,
    limit_rows: int = MISSING_BATCH_ROW_LIMIT,
) -> list[dict]:
    """欠账账本 → 定向补抓任务（按区分组批量，每发只装该区的欠账行）。

    locked/blocked 不进补抓（终态）；missing 连续 MISSING_MAX_RETRIES 次失败
    已在 mark_region_status 里转 blocked。行数上限防积压账本一轮吃满挤占
    关注层（老账先补）；任务形状与主轮一致（type=app），批量 handler 直接
    消费，失败退避与 missing 记账语义同一条路径。
    """
    from app.crawler.db_writer import DbWriter

    from app.crawler.browse_store import DEFAULT_BATCH_SIZE

    await price_cycle.recover_pending_stages()
    excluded = await _recovery_excluded_appids()
    if excluded:
        async with write_session(WritePriority.BACKGROUND) as session:
            jobs = (await session.execute(select(CrawlJob).where(
                CrawlJob.status.in_(("failed", "interrupted")),
            ))).scalars().all()
            for job in jobs:
                stats = job.stats_json or {}
                targets = stats.get("targets") or {}
                skipped = set(stats.get("skipUnits") or [])
                tasks = targets.get("tasks") or [{"appids": targets.get("appids", []), "region": cc}
                                                 for cc in targets.get("regions", [])]
                skipped.update(f"{aid}:{str(task['region']).upper()}" for task in tasks
                               for aid in task.get("appids", []) if int(aid) in excluded)
                if skipped != set(stats.get("skipUnits") or []):
                    job.stats_json = {**stats, "skipUnits": sorted(skipped)}
            await session.commit()
    db = DbWriter()
    tasks = await db.generate_missing_tasks(
        cooldown_minutes=cooldown_minutes, limit_rows=limit_rows, excluded_appids=excluded,
    )
    pairs = {(int(aid), task["region"].upper()) for task in tasks for aid in task["appids"]}
    pending = await _unobserved_job_targets(
        cooldown_minutes, settle=True,
        limit_rows=max(0, limit_rows - len(pairs)) if limit_rows > 0 else -1,
        excluded_appids=excluded,
    )
    for pair in sorted(pending):
        if limit_rows > 0 and len(pairs) >= limit_rows:
            break
        pairs.add(pair)
    pairs = {(aid, cc) for aid, cc in pairs if aid not in excluded}
    by_region: dict[str, list[int]] = {}
    for aid, cc in sorted(pairs):
        by_region.setdefault(cc.lower(), []).append(aid)
    return [{"type": "app", "id": f"{cc}:补{i // DEFAULT_BATCH_SIZE + 1}",
             "region": cc, "appids": appids[i:i + DEFAULT_BATCH_SIZE]}
            for cc, appids in by_region.items()
            for i in range(0, len(appids), DEFAULT_BATCH_SIZE)]


async def _recovery_excluded_appids() -> set[int]:
    from app.domains.monitoring import service as monitoring_service

    return (await monitoring_service.blocked_ids("game")
            | await _excluded_removed_appids() | await _excluded_free_appids())


_JOB_TARGET_UNITS_SQL = """
WITH target_jobs AS MATERIALIZED (
    SELECT id, json_extract(stats_json, '$.targets.appids') AS appids,
           json_extract(stats_json, '$.targets.regions') AS regions,
           json_extract(stats_json, '$.targets.tasks') AS tasks,
           replace(json_extract(stats_json, '$.targetStartedAt'), 'T', ' ') AS threshold
    FROM crawl_jobs
    WHERE status IN ('failed','interrupted')
      AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id = crawl_jobs.cycle_id AND c.status = 'cancelled')
      AND json_type(stats_json, '$.targets') = 'object'
      AND json_extract(stats_json, '$.targetStartedAt') IS NOT NULL
      AND (:job_id IS NULL OR id = :job_id)
), units AS (
    SELECT j.id AS job_id, CAST(a.value AS INTEGER) AS appid,
           upper(r.value) AS region, j.threshold
    FROM target_jobs j, json_each(j.appids) a, json_each(j.regions) r
    UNION ALL
    SELECT j.id, CAST(a.value AS INTEGER),
           upper(json_extract(t.value, '$.region')), j.threshold
    FROM target_jobs j, json_each(j.tasks) t,
         json_each(t.value, '$.appids') a
), skipped AS (
    SELECT s.value AS unit,
           MAX(replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')) AS threshold
    FROM crawl_jobs j, json_each(j.stats_json, '$.skipUnits') s
    WHERE replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')
          >= (SELECT MIN(threshold) FROM target_jobs)
    GROUP BY s.value
), unit_results AS (
    SELECT u.*, CASE WHEN (
        p.price_status IN ('ok','locked','missing','blocked')
        AND (p.updated_at >= u.threshold OR p.last_success_at >= u.threshold)
    ) OR s.threshold >= u.threshold THEN 1 ELSE 0 END AS settled
    FROM units u
    LEFT JOIN game_current_prices p ON p.appid = u.appid AND p.region_code = u.region
    LEFT JOIN skipped s ON s.unit = CAST(u.appid AS TEXT) || ':' || u.region
), unresolved AS (
    SELECT DISTINCT appid, region, threshold FROM unit_results WHERE settled = 0
)
"""


async def _unobserved_job_targets(
    cooldown_minutes: int, *, settle: bool = False, limit_rows: int = MISSING_BATCH_ROW_LIMIT,
    appids: list[int] | None = None,
    excluded_appids: set[int] | None = None,
) -> set[tuple[int, str]]:
    cutoff = get_beijing_time_obj().replace(tzinfo=None) - timedelta(minutes=cooldown_minutes)
    pending = set()
    async with get_session_factory()() as session:
        if limit_rows != 0 and appids != []:
            statement = text(_JOB_TARGET_UNITS_SQL + """
                SELECT DISTINCT appid, region FROM unresolved
                WHERE threshold < :cutoff
            """ + (" AND appid IN :appids" if appids is not None else "")
                + (" AND appid NOT IN (SELECT CAST(value AS INTEGER) FROM json_each(:excluded_appids))" if excluded_appids else "")
                + " LIMIT :limit_rows")
            parameters = {"job_id": None, "cutoff": cutoff, "limit_rows": limit_rows}
            if excluded_appids:
                parameters["excluded_appids"] = json.dumps(sorted(excluded_appids))
            if appids is not None:
                statement = statement.bindparams(bindparam("appids", expanding=True))
                parameters["appids"] = appids
            rows = await session.execute(statement, parameters)
            pending.update((int(aid), cc) for aid, cc in rows)
        changed = list((await session.execute(text(_JOB_TARGET_UNITS_SQL +
            "SELECT DISTINCT job_id FROM unit_results WHERE settled = 1"), {"job_id": None})).scalars()) if settle else []
    if changed:
        from app.crawler.browse_store import DEFAULT_BATCH_SIZE

        for start in range(0, len(changed), 50):
            async with write_session(WritePriority.BACKGROUND, label="crawl_targets_resolved") as session:
                updates = []
                for job_id in changed[start:start + 50]:
                    targets = (await session.execute(text(_JOB_TARGET_UNITS_SQL + """
                        SELECT CASE WHEN COUNT(*) = 0 THEN NULL
                        WHEN COUNT(*) = COUNT(DISTINCT appid) * COUNT(DISTINCT region) THEN
                            json_object('appids', json_group_array(DISTINCT appid),
                                        'regions', json_group_array(DISTINCT region))
                        ELSE json_object('tasks', json((
                            SELECT json_group_array(json_object('region', region, 'appids', json(appids)))
                            FROM (
                                SELECT region, bucket, json_group_array(appid) AS appids
                                FROM (
                                    SELECT appid, region,
                                           (ROW_NUMBER() OVER (PARTITION BY region ORDER BY appid)-1) / :batch_size AS bucket
                                    FROM unresolved
                                ) GROUP BY region, bucket ORDER BY region, bucket
                            )
                        ))) END FROM unresolved
                    """), {"job_id": job_id, "batch_size": DEFAULT_BATCH_SIZE})).scalar_one()
                    updates.append({"job_id": job_id, "targets": targets})
                await session.execute(text(
                    "UPDATE crawl_jobs SET stats_json = CASE WHEN :targets IS NULL THEN "
                    "json_remove(stats_json, '$.targets') ELSE "
                    "json_set(stats_json, '$.targets', json(:targets)) END "
                    "WHERE id = :job_id AND status IN ('failed','interrupted')"
                ), updates)
                await session.commit()
    return pending


async def has_pending_missing(
    cooldown_minutes: int = REPAIR_RETRY_COOLDOWN_MINUTES,
) -> bool:
    """接续未启动计划后检查可重试的地区欠账。

    空区行（region_code=''）是即拾即清的尝试痕迹，不算待补欠账——与
    generate_missing_tasks 清账后的实际可抓集合同口径。"""
    cutoff = get_beijing_time_obj().replace(tzinfo=None) - timedelta(
        minutes=cooldown_minutes
    )
    await price_cycle.recover_pending_stages()
    excluded = await _recovery_excluded_appids()
    async with get_session_factory()() as session:
        row = await session.execute(
            select(GameCurrentPrice.appid)
            .where(
                GameCurrentPrice.price_status == "missing",
                GameCurrentPrice.region_code != "",
                GameCurrentPrice.updated_at < cutoff,
                text("game_current_prices.appid NOT IN (SELECT CAST(value AS INTEGER) FROM json_each(:excluded))")
                .bindparams(excluded=json.dumps(sorted(excluded))),
            )
            .limit(1)
        )
    return row.first() is not None or bool(await _unobserved_job_targets(
        cooldown_minutes, limit_rows=1, excluded_appids=excluded,
    ))


async def _load_job(job_id: int) -> CrawlJob | None:
    async with get_session_factory()() as session:
        return await session.get(CrawlJob, job_id)


async def _finish_job(job_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
    async with write_session(WritePriority.BACKGROUND) as session:
        job = await session.get(CrawlJob, job_id)
        if job is None:
            return
        changed = job.status not in ("done", "failed", "stopped", "interrupted")
        if changed:
            job.status = status
            job.finished_at = datetime.now()
            job.error = error
        # stats=None（异常收尾）保留启动时落的初始账本——分母不能丢
        job.stats_json = {**(job.stats_json or {}), **stats} if stats is not None else job.stats_json
        if job.cycle_id is not None:
            # 轮批次完成账：挂轮各 job 的已处理量之和，收尾一拍重算
            # （不增量累加——停止/失败后的重跑会把同一批计两次）。
            # 先 flush 本 job 的统计再聚合，原生 SQL 才能看到本笔。
            from app.domains.crawl.cycle import PriceCycle

            await session.flush()
            row = await session.execute(
                text(
                    "SELECT COALESCE(SUM(json_extract(stats_json, '$.processed')), 0) "
                    "FROM crawl_jobs WHERE cycle_id = :cid"
                ),
                {"cid": job.cycle_id},
            )
            cycle = await session.get(PriceCycle, job.cycle_id)
            if cycle is not None:
                cycle.batches_done = int(row.scalar_one())
        await session.commit()
    if changed:
        bus.publish("job.status", job_id=job_id, status=status, error=error)


# 后台标记链的强引用集：create_task 产物只有被持有时才不会被 GC


def _crawled_appids(
    appid_pairs: list[tuple[int, str]] | None, pre_tasks: list[dict] | None
) -> list[int]:
    """本轮实际爬到的对象 id（收尾下游链的输入，全部是 int appid）。

    常规段来自 (appid, region) 对；补抓段（missing/repair）的任务 id 是
    「区:补n」批次标签不是 appid，真实对象在每发的 appids 列表里——
    收尾链（提醒/史低/永降/排序/脱池）按 appid 消费，喂标签等于整段空转。
    """
    return [aid for aid, _ in (appid_pairs or [])] + [
        a for t in (pre_tasks or []) for a in t.get("appids", [])
    ]


async def _finish_price_outputs(crawled: list[int], job_id: int) -> None:
    from app.domains.crawl import events as crawl_events
    from app.domains.wishlist import service as wishlist_service

    job = await _load_job(job_id)
    if job is not None and job.cycle_id is None and crawled:
        try:
            await crawl_events.detect_window(crawled, job.regions_json or [], job.started_at,
                                            job.finished_at or datetime.now(), cycle_id=-job_id)
        except Exception:
            log_event(logger, "价格已保存，变化事件检测未完成", level=logging.ERROR, exc_info=True)
    for consume in (alerts_service.check_appids, alerts_service.check_new_lows,
                    wishlist_service.release_free_games):
        try:
            await consume(crawled)
        except Exception:  # noqa: BLE001
            log_event(logger, "价格已保存，后续通知处理未完成", level=logging.ERROR, exc_info=True)


async def _execute(
    job_id: int,
    appid_pairs: list[tuple[int, str]] | None,
    config: CrawlRunConfig,
    stop_event: asyncio.Event,
    pre_tasks: list[dict] | None = None,
) -> None:
    started = time.monotonic()
    try:
        stats = await run_crawl(
            appid_pairs,
            config=config,
            stop_event=stop_event,
            pre_tasks=pre_tasks,
            crawl_job_id=job_id,
        )
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        status = "stopped" if stop_event.is_set() else "failed" if stats.get("unpersistedUnits") else "done"
        error = "部分地区数据未能保存，将自动补全" if stats.get("unpersistedUnits") else None
        await _finish_job(job_id, status, stats, error)
        log_event(
            logger,
            f"爬取任务 {job_id} 已结束，共处理 {stats.get('processed')}/{stats.get('total')} 项",
            detail={
                "任务": job_id,
                "状态": {"done": "完成", "stopped": "已停止"}.get(status, status),
                "总数": stats.get("total"),
                "已处理": stats.get("processed"),
                "成功": stats.get("success"),
                "失败": stats.get("failed"),
                "无折扣跳过": stats.get("skipped_no_discount"),
                "折扣已结束": stats.get("discount_ended"),
                "耗时": f"{stats.get('elapsed_seconds')}秒",
            },
        )

        if stop_event.is_set():
            # 手动停止：跳过提醒检查 / 史低刷新 / 排序缓存（对未爬完的数据无意义）
            return

        crawled = _crawled_appids(appid_pairs, pre_tasks)
        await _finish_price_outputs(crawled, job_id)
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            f"爬取任务 {job_id} 执行失败",
            level=logging.ERROR,
            exc_info=True,
            detail={"任务": job_id},
        )
        await _finish_job(job_id, "failed", None, str(e))
        # 系统异常告警：任务级失败（含定时价格网格轮）带 12h 冷却发一封，
        # 连续失败不刷屏；告警自身异常不得影响任务状态收敛
        try:
            await alerts_service.send_system_alert(
                kind="crawl",
                title="爬取任务失败",
                summary="本轮爬取任务异常终止，涉及的条目价格本轮不会更新。",
                rows=[
                    ("任务 ID", str(job_id)),
                    ("失败原因", str(e)[:180] or e.__class__.__name__),
                    ("发生时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ],
                level="danger",
                hint="可在「任务」页手动重跑一次；连续失败请检查代理通道与网络。",
            )
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "爬取失败告警发送失败（不影响任务状态）",
                level=logging.ERROR,
                exc_info=True,
            )
    finally:
        global _active
        if _active and _active.id == job_id:
            _active = None
        if config is not None and config.occupancy_tag:
            # 占用是 start_job 前移预取的：释放权在任务收尾（run_crawl 只复用不取还）
            from app.crawler.occupancy import end_crawl

            end_crawl()
            bus.publish("crawl.idle")


# 补抓层专用：定向小流量通道，低 worker 防 429 风暴（STT"稀缺配额单独通道"）
MISSING_RECOVERY_WORKERS = 6

# 直连形态的 worker 上限：单出口（用户本机网络环境），请求速率由全局滑动窗口闸
# （200 发/5 分钟）统一约束，多 worker 只是在闸前排队，不提高吞吐
DIRECT_MODE_WORKERS = 4


# 下架监控：removed_at 非空的游戏脱池停爬——已无商店页，
# 每 41 区一次的 404 空转纯烧配额。36h 宽限期兜底误判（两轮价格刷新
# 周期内可经复探通道自愈）；复活走「重新上榜反哺」或手动复探端点。
_REMOVED_GRACE_HOURS = 36


async def _excluded_removed_appids() -> set[int]:
    """下架脱池名单：removed_at 落值且已过宽限期的 appid 集。

    removed_at 刚落的 36h 内不排除（误判保险期，照常参与爬取——
    真下架多烧一轮、误判自愈，代价可控）。
    """
    from datetime import timedelta

    from app.crawler.utils import get_beijing_time_obj

    cutoff = get_beijing_time_obj().replace(tzinfo=None) - timedelta(
        hours=_REMOVED_GRACE_HOURS
    )
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid).where(
                    Game.removed_at.is_not(None), Game.removed_at < cutoff
                )
            )
        ).scalars().all()
    return {int(r) for r in rows}


async def _excluded_free_appids() -> set[int]:
    """永久免费脱池名单：free_kind='f2p' 的 appid 集。

    永久免费的价格事实已定（免费态由写库层每轮维护），任何自动通道
    再爬都是空转配额；promo（限时赠送）**不在**此列——赠送会结束，
    必须持续爬到价格翻回正价为止。scope=appids 直传不过滤（手动补爬
    仍可显式指定）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid).where(Game.free_kind == "f2p")
            )
        ).scalars().all()
    return {int(r) for r in rows}


async def _resolve_worker_count() -> int:
    """主轮 worker 数：按可用出口 IP 节点数开启（一个出口一个 worker）。

    数据源 = proxies 域 pool_stats() 的 available（手动池可用 + Clash 在跑
    订阅的存活出口 IP 去重数，「一个出口 IP 算一个」与仪表盘同口径）。
    规则：workers = available，上限 WORKERS_MAX——出口少并发小（不把请求
    全压在同几个出口上），出口多并发跟着开。

    分支：
    - 显式配置 crawl.workers（正整数）→ 尊重显式值，按出口数开不介入；
    - available > 0 → 一个出口一个 worker，上限 WORKERS_MAX；
    - available == 0 / 统计不可用 → DEFAULT_WORKER_COUNT（未配代理 / 内核
      没跑的直连形态，行为与既有版本一致）。
    """
    from app.domains.settings.service import get_value

    explicit = await get_value("crawl.workers")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and int(explicit) > 0:
        log_event(
            logger,
            f"已显式配置并发 worker 数为 {int(explicit)}，不再按可用出口数推导",
            detail={"worker": int(explicit), "来源": "显式配置"},
        )
        return int(explicit)

    try:
        from app.domains.proxies import service as proxies_service

        available = int((await proxies_service.pool_stats()).get("available") or 0)
    except Exception as e:  # noqa: BLE001 —— 统计失败不阻断爬取
        log_event(
            logger,
            f"统计可用出口 IP 失败，回退使用默认并发 worker 数 {DEFAULT_WORKER_COUNT}",
            tag="降级",
            level=logging.WARNING,
            detail={"原因": e, "worker": DEFAULT_WORKER_COUNT},
        )
        return DEFAULT_WORKER_COUNT

    if available <= 0:
        log_event(
            logger,
            f"当前没有可用出口 IP，按直连形态使用默认并发 worker 数 {DEFAULT_WORKER_COUNT}",
            detail={"可用出口": 0, "worker": DEFAULT_WORKER_COUNT},
        )
        return DEFAULT_WORKER_COUNT

    workers = min(WORKERS_MAX, available)
    log_event(
        logger,
        f"按可用出口 IP 数开启 {workers} 个并发 worker（一个出口一个 worker，上限 {WORKERS_MAX}）",
        detail={"可用出口": available, "worker": workers, "上限": WORKERS_MAX},
    )
    return workers


async def start_job(
    scope: str = "appids",
    appids: list[int] | None = None,
    regions: list[str] | None = None,
    kind: str = "manual",
    missing_cooldown: int | None = None,
    cycle_id: int | None = None,
    trigger: str = "unknown",
) -> dict:
    """启动爬取任务。返回任务摘要；已有任务运行时抛 RuntimeError。

    cycle_id 非空时本 job 归属该价格刷新周期（PriceCycle 1:N CrawlJob）；
    不传即为不挂周期的任务（手动任务、暂不归属的修复轮）。

    kind="missing" 为补抓层：忽略 scope/appids，从欠账账本生成
    按区分组的批量补抓任务（每发只装该区欠账行），低 worker，
    冷却默认 24h（MISSING_RETRY_COOLDOWN_MINUTES）。
    kind="repair" 为失败记录修复线程：与 missing 同通道
    （按区批量定向补抓、低 worker），仅冷却默认 4min（REPAIR_RETRY_
    COOLDOWN_MINUTES）——供 5min 空闲档修复轮高频复访失败区；
    仍失败的照常走 missing 计账（fail_count 递增，穷尽 5 次转 blocked），
    成功清账，与主爬虫逻辑完全一致。
    missing_cooldown 显式传值时覆盖 missing/repair 两类冷却。
    """
    global _active
    # 统一占用语义：bundles 链尾是直调 run_crawl 的（不登记 _active），只看
    # _active 会漏掉它。检查后立即预取占用（tag 经 config.occupancy_tag 传给
    # run_crawl 复用，释放权在 _execute 收尾）——检查与取得占用之间无 await，
    # 并发启动窗口闭合，注定失败的任务行不再产生。
    from app.crawler.occupancy import (
        CrawlerBusyError,
        begin_crawl,
        crawler_busy,
        end_crawl,
        maintenance_reason,
    )

    if crawler_busy():
        raise RuntimeError(maintenance_reason() or "已有爬取任务在运行")
    if _chain_stop is not None and cycle_id is None:
        raise RuntimeError("价格更新正在进行")
    if _active is not None and not _active.task.done():
        raise RuntimeError(maintenance_reason() or "已有爬取任务在运行")
    try:
        begin_crawl(JOB_OCCUPANCY_TAG)
    except CrawlerBusyError:
        raise RuntimeError("已有爬取任务在运行")

    try:
        pre_tasks: list[dict] | None = None
        pairs: list[tuple[int, str]] = []
        if kind in ("missing", "repair"):
            effective = await effective_regions(regions)
            cooldown = (
                missing_cooldown
                if missing_cooldown is not None
                else (
                    REPAIR_RETRY_COOLDOWN_MINUTES
                    if kind == "repair"
                    else MISSING_RETRY_COOLDOWN_MINUTES
                )
            )
            pre_tasks = await _missing_tasks(cooldown_minutes=cooldown)
            if not pre_tasks:
                raise ValueError("没有待补抓的欠账（missing）数据")
        else:
            pairs = await resolve_scope_appids(scope, appids)
            if not pairs:
                raise ValueError("任务列表为空")
            effective = await effective_regions(regions)
        # 受管爬取：**每次 run 只取一次**当前 Runtime 的入口集合，整个 run 固定用它
        # （重建会换端口并打断在途请求，所以 run 内不换）。拿不到就拒绝启动——
        # 绝不静默退回直连或旧订阅代理（那会把"池坏了"伪装成"爬取成功"）。
        #
        # 容量单位是**独立出口 IP**：入口集合由 `crawl_lane_plan` 用**当前出口槽快照**
        # 校验后给出（池里有出口 + 有 lane + 两者逐位一致），任一不满足即拒绝启动——
        # **绝不**回退 GLOBAL / 直连 / 旧订阅代理。收敛结果即本次 run 的**快照**：
        # run 内不再变，后台维护改出口集只影响下一次 run。
        from app.core.config import get_settings as _get_settings
        from app.domains.proxypool.exits import MAX_CRAWL_WORKERS
        from app.domains.proxypool.runtime import crawl_lane_plan
        worker_count = await _resolve_worker_count()
        small_lane = kind in ("missing", "repair")
        planned_workers = (
            min(worker_count, MISSING_RECOVERY_WORKERS) if small_lane else worker_count
        )
        data_dir = _get_settings().data_dir
        from app.domains.settings.service import get_value
        # 默认 direct_only（初次打开即直连；与 proxies.get_strategy 的默认
        # 同源，改默认须两处同步）
        if (await get_value("proxy.strategy", "direct_only")) == "direct_only":
            # 直连策略：价格作业**托管到用户本机网络环境**——TUN 型加速器裸连
            # 即走；系统代理型（Clash Verge 等）由系统代理解析找到并显式挂上
            # （aiohttp 不读注册表，不挂就等于绕开用户的本地代理）。池子状态与
            # 此形态无关（空池也能作业）。频率由 crawler 的全局滑动窗口闸
            # （200 发/5 分钟）统一约束，多 worker 只是在闸前排队，不提高请求
            # 速率，因此 worker 数收在小额。
            from app.domains.proxies import service as proxy_service

            system_proxy = await proxy_service.resolve_system_proxy_url()
            effective_workers = min(planned_workers, DIRECT_MODE_WORKERS)
            if system_proxy:
                log_event(
                    logger,
                    f"直连形态：价格抓取托管到本机网络环境，已探活系统代理，使用 {effective_workers} 个 worker，频率闸 200 发/5 分钟",
                    detail={
                        "系统代理": system_proxy,
                        "worker": effective_workers,
                        "频率闸": "200 发/5 分钟",
                    },
                )
            else:
                log_event(
                    logger,
                    f"直连形态：价格抓取托管到本机网络环境，未检测到可用系统代理、裸直连出网，使用 {effective_workers} 个 worker，频率闸 200 发/5 分钟",
                    detail={"worker": effective_workers, "频率闸": "200 发/5 分钟"},
                )
            config = CrawlRunConfig(
                regions=effective,
                workers=effective_workers,
                occupancy_tag=JOB_OCCUPANCY_TAG,
                timeout=HTTP_TIMEOUT,
                proxy_url=system_proxy,
            )
        else:
            from app.domains.proxies import clash_manager as _cm
            async with get_session_factory()() as session:
                # max_lanes 不传：run 拿走全部 active lane（≤MAX_LANES）——多出的
                # 部分即待用出口池；worker 数由下面的 effective_workers 单独钳在 60
                run_plan = await crawl_lane_plan(
                    session, data_dir, runtime=_cm.pool_runtime,
                )
            proxy_urls = run_plan["urls"]
            effective_workers = max(1, min(planned_workers, len(proxy_urls), MAX_CRAWL_WORKERS))
            log_event(
                logger,
                f"出口槽已就绪，本轮启用 {effective_workers} 个 worker 抓取",
                detail={
                    "已知出口": run_plan.get("known_exits", 0) or 0,
                    "活动通道": len(proxy_urls),
                    "内核监听": run_plan.get("runtime_lanes", len(proxy_urls)) or 0,
                    "待用通道": max(0, len(proxy_urls) - effective_workers),
                    "worker": effective_workers,
                    "期望worker": planned_workers,
                    "上限": MAX_CRAWL_WORKERS,
                },
            )
            config = CrawlRunConfig(
                regions=effective,
                workers=effective_workers,
                occupancy_tag=JOB_OCCUPANCY_TAG,
                timeout=HTTP_TIMEOUT,
                proxy_url=proxy_urls[0],
                proxy_urls=proxy_urls,
                exit_keys=run_plan["exit_keys"],
                exit_nodes=run_plan["nodes"],
            )
        # 任务行落库过写调度器：定时写者（体检台账/钱包轮转）密集时不过闸的
        # commit 会在 SQLite 写锁上排队到超时——主轮建不出任务行，用户手动启动
        # 的任务也迟迟建不出来。
        # 轮账本的初始批次：与 run_crawl 收尾统计同口径（total_target = 初始任务数）
        # —— browse 批量段是「区数 × 款数÷单发容量」的桶数，补抓段是 pre_tasks
        # 的发数（行数只是日志口径），口径错位会让进度条永远跑不满。
        from app.crawler.browse_store import DEFAULT_BATCH_SIZE
        if pre_tasks:
            initial_total = len(pre_tasks)
        else:
            initial_total = -(-len(pairs) // DEFAULT_BATCH_SIZE) * len(effective)
        async with write_session(WritePriority.BACKGROUND) as session:
            job = CrawlJob(
                kind=kind,
                status="running",
                mode="app",
                regions_json=effective,
                started_at=datetime.now(),
                cycle_id=cycle_id,
                stats_json={
                    "trigger": trigger,
                    "total": initial_total, "processed": 0, "success": 0, "failed": 0,
                    "targetStartedAt": get_beijing_time_obj().replace(tzinfo=None).isoformat(),
                    "targets": {"tasks": [
                        {"region": str(t["region"]).upper(), "appids": list(dict.fromkeys(t["appids"]))}
                        for t in pre_tasks
                    ]} if pre_tasks else {
                        "appids": list(dict.fromkeys(int(aid) for aid, _ in pairs)),
                        "regions": list(dict.fromkeys(str(cc).upper() for cc in effective)),
                    },
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            _execute(job_id, pairs, config, stop_event, pre_tasks=pre_tasks)
        )
        _active = JobHandle(id=job_id, task=task, stop_event=stop_event, cycle_id=cycle_id)
    except BaseException:  # noqa: BLE001 — 清理后原样重抛
        # 启动半途失败（无欠账/范围空/lane 规划拒绝等）：释放预取的占用
        end_crawl()
        raise


    # 事件与日志沿用「项数」口径（款数/行数），初始账本的桶口径见上
    pre_rows = sum(
        len(t["appids"]) if "appids" in t else 1 for t in (pre_tasks or [])
    )
    count = len(pairs) + pre_rows
    bus.publish("job.started", job_id=job_id, scope=scope, count=count, regions=effective)
    log_event(
        logger,
        f"爬取任务 {job_id} 已启动，共 {count} 项待抓",
        detail={
            "任务": job_id,
            "类型": kind,
            "范围": scope,
            "项数": count,
            "补抓批次": len(pre_tasks or []),
            "补抓行数": pre_rows,
        },
    )
    return {"id": job_id, "scope": scope, "count": count, "regions": effective}


async def default_queue_specs(*, full_library: bool = False) -> list[dict]:
    """自动维护按策略选范围；手动全部始终包含符合条件的目录。"""
    from app.domains.settings.service import get_value

    specs: list[dict] = [
        {"kind": "missing"},
        {"scope": "pool"},
    ]
    # 默认 direct_only（与 proxies.get_strategy 同源）
    direct_mode = (
        (await get_value("proxy.strategy", "direct_only")) == "direct_only"
    )
    catalog_default = False if direct_mode else True
    if direct_mode and not full_library:
        specs.append({"scope": "discounts", "kind": "discounts"})
    catalog_refresh = await get_value("crawl.catalog_refresh", catalog_default)
    if full_library or catalog_refresh:
        specs.append({"scope": "catalog"})
    if catalog_refresh:
        specs.append({"scope": "specials", "kind": "specials_backfill"})
    from app.domains.crawl import cycle as price_cycle

    return await price_cycle.prioritize_specs(specs)


# 手动全队列链句柄：段间隙占用窗口（_active 已清、下一段未建）的防重入闸
_queue_chain_task: asyncio.Task | None = None
# 受理段占位：检查通过后、首个 await 前同步置位，并发受理不得都过闸
_queue_accepting = False


async def start_full_queue() -> dict:
    """手动全库更新，复用价格周期编排与范围解析。"""
    global _queue_chain_task, _queue_accepting
    from app.crawler.occupancy import crawler_busy, maintenance_reason

    if _chain_stop is not None or crawler_busy() or (_active is not None and not _active.task.done()):
        raise RuntimeError(maintenance_reason() or "已有爬取任务在运行")
    if _queue_accepting or (
        _queue_chain_task is not None and not _queue_chain_task.done()
    ):
        raise RuntimeError("全量队列已在启动中")
    # 检查与置位之间无 await（同 start_job 预取占用判例）
    _queue_accepting = True
    chain_stop = asyncio.Event()
    register_chain_stop(chain_stop)
    handed_off = False
    try:
        composed = await default_queue_specs(full_library=True)

        missing_pending = await has_pending_missing()
        segment_plan: list[dict] = [{"name": "missing", "count": 1 if missing_pending else 0}]
        for spec in composed:
            scope = spec.get("scope")
            if not scope:
                continue
            spec["resolved_appids"] = await plan_scope_appids(scope, spec.get("appids"))
            segment_plan.append(
                {"name": scope, "count": len(spec["resolved_appids"])}
            )
        if not any(p["count"] for p in segment_plan):
            raise ValueError(
                "没有可抓取的对象：关注与游戏库都是空的，先在游戏商店页添加游戏"
            )

        # 无显式 kind 的 scope 段手动触发记 manual；missing / specials_backfill
        # 是通道/段身份标签（任务页按此显示），保留
        specs = [
            {**spec, "kind": "manual"}
            if spec.get("scope") and "kind" not in spec
            else spec
            for spec in composed
        ]
        cycle_id = await price_cycle.create("manual", "all", stop_event=chain_stop)
        register_chain_stop(chain_stop, cycle_id=cycle_id)
        regions, expected = await price_cycle.freeze_expected(cycle_id, specs)

        async def _chain() -> None:
            # 编排本体在 crawl 域内，与自动轮共用一份；链级停止位由 run_price_cycle
            # 自行登记（stop 端点经它停整轮）。本函数只负责「后台跑 + 链级异常兜底」
            from app.domains.crawl import cycle_run

            try:
                await cycle_run.run_price_cycle(
                    specs, kind="manual", prepared=(cycle_id, regions, expected), stop_event=chain_stop,
                )
                log_event(
                    logger,
                    "手动全队列执行已结束",
                )
            except Exception:  # noqa: BLE001 —— 链级异常只记日志，段内已各自兜底
                log_event(
                    logger,
                    "手动全队列执行异常，段内已各自兜底",
                    level=logging.ERROR,
                    exc_info=True,
                )

        _queue_chain_task = asyncio.create_task(_chain())
        handed_off = True
    finally:
        _queue_accepting = False
        if not handed_off:
            unregister_chain_stop(chain_stop)
    segment_names = [p["name"] for p in segment_plan]
    log_event(
        logger,
        "手动全队列已受理，正在后台按段串行执行",
        detail={
            "段序": "→".join(segment_names),
            "各段款数": "/".join(str(p["count"]) for p in segment_plan),
        },
    )
    return {
        "queued": True,
        "scope": "all",
        "cycleId": cycle_id,
        "total": sum(p["count"] for p in segment_plan if p["name"] != "missing"),
        "segments": segment_plan,
    }


async def stop_job(
    job_id: int | None = None, *, whole_chain: bool = True, cycle_id: int | None = None,
) -> bool:
    """按活动身份请求协作停止；重复请求不重复写入。"""
    handle = _active
    if cycle_id is not None and (_chain_stop is None or _chain_cycle_id != cycle_id):
        return False
    if job_id is not None and (handle is None or handle.id != job_id):
        return False
    if cycle_id is not None and handle is not None and handle.cycle_id != cycle_id:
        handle = None
    chain_hit = whole_chain and _chain_stop is not None and not _chain_stop.is_set()
    job_hit = handle is not None and not handle.stop_event.is_set()
    if chain_hit:
        _chain_stop.set()
    if job_hit:
        handle.stop_event.set()
    if chain_hit:
        await price_cycle.cancel_open_stages(_PROCESS_STARTED_AT, cycle_id=_chain_cycle_id)
    if job_hit:
        await _finish_job(handle.id, "stopped", error="用户已停止")
        log_event(
            logger, f"已请求停止爬取任务 {handle.id}",
            detail={"任务": handle.id, "整轮": "是" if chain_hit else "否"},
        )
    return bool(chain_hit or job_hit)


def full_queue_running() -> bool:
    """手动全队列链是否在跑（含段间隙——链句柄未完成即算在跑）。"""
    return _queue_accepting or (_queue_chain_task is not None and not _queue_chain_task.done())


async def stop_full_queue() -> bool:
    """停止手动全队列链：置链级停止位 + 停当前段（协作式，见 stop_job）。

    不 cancel 链任务：链内 await 的是 `run_price_cycle`，取消会穿透它的
    try 边界（CancelledError 不是 Exception），本轮 Cycle 将永久停在
    running 直到下次进程重启收尸。"""
    stopped = await stop_job(whole_chain=True)
    if stopped:
        log_event(logger, "已请求停止手动全队列")
    return stopped


async def run_sequential(
    specs: list[dict],
    *,
    missing_cooldown: int | None = None,
    cycle_id: int | None = None,
    skip_reasons: list[str] | None = None,
    stop_event: asyncio.Event | None = None,
    trigger: str = "unknown",
) -> list[dict]:
    """串行链式启动多个爬取任务（单任务模型下唯一的多 spec 方式）。

    specs: [{scope, appids?, regions?, kind?}, ...]，逐个 start_job 并
    await 其完成；已有任务运行（RuntimeError）或任务列表为空（ValueError）
    时跳过该 spec 继续下一个——链式触发的健壮性优先于严格性。
    missing_cooldown 显式传值时透传给 missing/repair 类 spec；cycle_id
    非空时本轮启动的 job 全部挂到该价格刷新周期。
    skip_reasons 传列表时逐段收集跳过原因（"段标识：原因"），供编排层在
    「整轮零启动」终态判定时把真实拒因写进 Cycle 账本，而不是只进日志。
    stop_event 置位时段间隙即退出（当前段由 stop_job 收敛为 stopped），
    链不再推进下一段——取消只能是协作式的，链任务本身不可 cancel。
    """
    from app.domains.crawl import cycle as price_cycle

    results: list[dict] = []
    for index, spec in enumerate(specs):
        if stop_event is not None and stop_event.is_set():
            for remaining in specs[index:]:
                await price_cycle.record_stage(cycle_id, remaining, "cancelled", "用户已停止")
            if skip_reasons is not None:
                skip_reasons.append(f"{spec.get('kind', spec.get('scope'))}：用户已停止")
            break
        # 本机网络被接管时抓取只会堆失败记录与限流：当前段跑完（已爬数据照常
        # 落库）后不再推进下一段，链按协作式停止收敛
        if _tun_mode_suspected():
            for remaining in specs[index:]:
                await price_cycle.record_stage(cycle_id, remaining, "deferred", env_check.USER_MESSAGE)
            if stop_event is not None:
                stop_event.set()
            if skip_reasons is not None:
                skip_reasons.append(
                    f"{spec.get('kind', spec.get('scope'))}：{env_check.USER_MESSAGE}"
                )
            log_event(
                logger,
                "本机网络被其他软件接管，抓取链在当前段结束后停止",
                tag="跳过",
                detail={"原因": env_check.USER_MESSAGE},
            )
            break
        await price_cycle.record_stage(cycle_id, spec, "pending")
        try:
            result = await start_job(
                scope="appids" if "resolved_appids" in spec else spec.get("scope", "appids"),
                appids=spec.get("resolved_appids", spec.get("appids")),
                regions=spec.get("resolved_regions", spec.get("regions")),
                kind=spec.get("kind", "scheduled"),
                missing_cooldown=missing_cooldown,
                cycle_id=cycle_id,
                trigger=trigger,
            )
        except (RuntimeError, ValueError) as e:
            await price_cycle.record_stage(
                cycle_id, spec, "empty" if isinstance(e, ValueError) else "deferred", str(e),
            )
            log_event(
                logger,
                "本轮串行链跳过了一个抓取段",
                tag="跳过",
                detail={
                    "段": spec.get("kind", spec.get("scope")) or "未标注",
                    "原因": e,
                },
            )
            if skip_reasons is not None:
                skip_reasons.append(f"{spec.get('kind', spec.get('scope'))}：{e}")
            continue
        results.append(result)
        await price_cycle.record_stage(cycle_id, spec, "pending", job_id=result["id"])
        active = _active
        if active is not None:
            await active.task
        state = "cancelled" if stop_event is not None and stop_event.is_set() else "done"
        job = await _load_job(result["id"]) if cycle_id is not None else None
        stats = job.stats_json if job is not None else result.get("stats")
        reason = None
        if state != "cancelled" and (
            (job is not None and job.status == "failed")
            or (stats and stats.get("failed", 0) > 0 and not stats.get("success", 0))
        ):
            state = "deferred"
            reason = "本段未能取得价格，稍后自动补跑"
        await price_cycle.record_stage(cycle_id, spec, state, reason)
    return results


async def list_jobs(limit: int = 20) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": j.id,
            "kind": j.kind,
            "status": j.status,
            "mode": j.mode,
            "cycleId": j.cycle_id,
            "regions": j.regions_json,
            "stats": j.stats_json,
            "startedAt": j.started_at.isoformat() if j.started_at else None,
            "finishedAt": j.finished_at.isoformat() if j.finished_at else None,
            "error": j.error,
        }
        for j in rows
    ]
