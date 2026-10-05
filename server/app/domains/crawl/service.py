"""crawl 域服务：任务启动 / 停止 / 记录。

本地工具同一时间只允许一个爬取任务（重复启动返回 409）。
任务完成后自动触发价格提醒检查（alerts.check_appids）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select, text

from app.core.database import WritePriority, get_session_factory, write_gate
from app.core.events import bus
from app.crawler.config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT, WORKERS_MAX
from app.crawler.runner import CrawlRunConfig, run_crawl
from app.crawler.utils import get_beijing_time_obj
from app.domains.alerts import service as alerts_service
from app.domains.crawl import cycle as price_cycle
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.games import service as games_service
from app.domains.games import series as games_series
from app.domains.regions.service import effective_regions
from app.domains.wishlist.models import WishlistItem

logger = logging.getLogger(__name__)


@dataclass
class JobHandle:
    id: int
    task: asyncio.Task
    stop_event: asyncio.Event
    started_monotonic: float = field(default_factory=time.monotonic)


# 进程内活动任务表（单任务模型）
_active: JobHandle | None = None


def active_job_id() -> int | None:
    return _active.id if _active else None


# 进程启动时刻（模块导入即进程启动）：只用于识别「上一个进程遗留」的任务——
# 后台收拾链可能晚于首个用户请求跑完，本进程监听后启动的任务不得被判为中断
_PROCESS_STARTED_AT = get_beijing_time_obj().replace(tzinfo=None)


async def cleanup_orphan_jobs() -> None:
    """进程启动时把上一进程遗留的 running 任务标记为失败。"""
    async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
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
            logger.warning("清理了 %d 个中断任务", len(rows))
    await price_cycle.cleanup_orphan_cycles()


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
# ≤400 行，请求量天然被压住；这个上限挡的是「积压账本一轮吃满」挤占
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

    db = DbWriter()
    return await db.generate_missing_tasks(
        cooldown_minutes=cooldown_minutes, limit_rows=limit_rows
    )


async def has_pending_missing(
    cooldown_minutes: int = REPAIR_RETRY_COOLDOWN_MINUTES,
) -> bool:
    """是否存在待补抓欠账（Cycle 判定本轮是否遗留未覆盖单元的出口）。"""
    return bool(await _missing_tasks(cooldown_minutes=cooldown_minutes, limit_rows=1))


async def _load_job(job_id: int) -> CrawlJob | None:
    async with get_session_factory()() as session:
        return await session.get(CrawlJob, job_id)


async def _finish_job(job_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
    async with write_gate(WritePriority.BACKGROUND):
        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            job = await session.get(CrawlJob, job_id)
            if job is None:
                return
            job.status = status
            # stats=None（异常收尾）保留启动时落的初始账本——分母不能丢
            job.stats_json = stats if stats is not None else job.stats_json
            job.finished_at = datetime.now()
            job.error = error
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
    bus.publish("job.status", job_id=job_id, status=status, error=error)


# 后台标记链的强引用集：create_task 产物只有被持有时才不会被 GC
_post_crawl_tasks: set[asyncio.Task] = set()


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


def _spawn_post_crawl_chain(crawled: list[int], job_id: int) -> None:
    """任务收尾下游链转后台：事件检测 → 提醒 → 史低 → 新史低邮件 → 永降
    → 排序缓存 → 系列归组 → 免费脱池。串行顺序与前台版一致，逐段兜异常
    不影响任务状态。
    """
    async def _chain() -> None:
        try:
            # 非周期执行（job 无 Cycle：手动单发 / 补抓拍）的价格变化同样进
            # 事件账本——检测逻辑与挂轮共用一份（events.detect_window），
            # 归属键取 -job_id，窗口=本 job 起止时刻
            job = await _load_job(job_id)
            if job is not None and job.cycle_id is None and crawled:
                from app.domains.crawl import events as crawl_events

                written = await crawl_events.detect_window(
                    crawled,
                    job.regions_json or [],
                    job.started_at,
                    job.finished_at or datetime.now(),
                    cycle_id=-job_id,
                )
                if written:
                    logger.info(
                        "任务 %d 价格事件 %d 条：%s",
                        job_id, len(written),
                        sorted({e["event_type"] for e in written}),
                    )
        except Exception:  # noqa: BLE001
            logger.exception("任务 %d 价格事件检测失败（不影响任务）", job_id)
        try:
            await alerts_service.check_appids(crawled)
        except Exception:  # noqa: BLE001
            logger.exception("提醒检查失败（不影响任务）")
        try:
            refreshed = await games_service.refresh_hl_flags(crawled)
            logger.info("史低标记已刷新 %d 款", refreshed)
        except Exception:  # noqa: BLE001
            logger.exception("史低标记刷新失败（不影响任务）")
        try:
            # 新史低邮件：基于刚落库的 hl_flag 增量（差集对历史游标），
            # 在 refresh_hl_flags 之后才有数据可查
            await alerts_service.check_new_lows(crawled)
        except Exception:  # noqa: BLE001
            logger.exception("新史低邮件检查失败（不影响任务）")
        try:
            refreshed = await games_service.refresh_pp_flags(crawled)
            logger.info("永降标记已刷新 %d 款", refreshed)
        except Exception:  # noqa: BLE001
            logger.exception("永降标记刷新失败（不影响任务）")
        try:
            refreshed = await games_service.refresh_sort_cache(crawled)
            logger.info("排序缓存已增量刷新 %d 款", refreshed)
        except Exception:  # noqa: BLE001
            logger.exception("排序缓存刷新失败（不影响任务）")
        try:
            # 系列归组：库里有未识别行（series_id NULL）或系列覆盖文件被
            # 改过时才全库重算，否则是零成本探测
            if await games_series.has_unassigned() or games_series.overrides_changed():
                refreshed = await games_series.refresh_series()
                logger.info("系列归组已刷新 %d 款", refreshed)
        except Exception:  # noqa: BLE001
            logger.exception("系列归组刷新失败（不影响任务）")
        try:
            # 永久免费自动脱池：本轮爬到 free_kind='f2p' 的游戏移出监控池
            # （价格事实已定，留在池里只会每轮空转配额）；promo 不脱，赠送
            # 结束前要持续跟踪
            from app.domains.wishlist import service as wishlist_service

            released = await wishlist_service.release_free_games(crawled)
            if released:
                logger.info("永久免费自动脱池 %d 款", released)
        except Exception:  # noqa: BLE001
            logger.exception("免费游戏自动脱池失败（不影响任务）")

    task = asyncio.create_task(_chain())
    _post_crawl_tasks.add(task)
    task.add_done_callback(_post_crawl_tasks.discard)


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
        status = "stopped" if stop_event.is_set() else "done"
        await _finish_job(job_id, status, stats)
        logger.info("任务 %d 结束（%s）：%s", job_id, status, stats)

        if stop_event.is_set():
            # 手动停止：跳过提醒检查 / 史低刷新 / 排序缓存（对未爬完的数据无意义）
            return

        # 爬取落库后的下游链整段转后台：任务状态不等它。史低/永降/排序都是
        # 派生数据（晚几秒可见；进程退出丢一轮由下一轮重算自愈），提前释放
        # 的是抓取占用与任务收尾时长
        crawled = _crawled_appids(appid_pairs, pre_tasks)
        _spawn_post_crawl_chain(crawled, job_id=job_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("任务 %d 失败", job_id)
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
            logger.exception("爬取失败告警发送失败（不影响任务状态）")
    finally:
        global _active
        if _active and _active.id == job_id:
            _active = None


# 补抓层专用：定向小流量通道，低 worker 防 429 风暴（STT"稀缺配额单独通道"）
MISSING_RECOVERY_WORKERS = 6

# 直连形态的 worker 上限：单出口（用户本机网络环境），请求速率由全局滑动窗口闸
# （200 发/5 分钟）统一约束，多 worker 只是在闸前排队，不提高吞吐
DIRECT_MODE_WORKERS = 4


# 孤儿回补层：每日价格刷新第三层消化挂名孤儿（updated_at IS NULL），
# 当日限量防挤占——779 级欠账按此配额多日自然消化，不阻塞关注层
BACKFILL_DAILY_LIMIT = 60

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


async def _backfill_pairs(limit: int = BACKFILL_DAILY_LIMIT) -> list[tuple[int, str]]:
    """挂名孤儿 appid → 爬取对（updated_at IS NULL 且无任何价格行的 games 行）。

    家庭组/愿望单展示层兜底落库只写 appid+name（无元数据无价格行），
    这些行由本层在每日价格刷新里逐步回补。按 appid 升序稳定分批，
    多日跑量可预期。

    排除已有价格行的孤儿（meta 失败后 Phase 2 已写 locked/missing
    状态行的）——它们进了 missing 账本通道由补抓层负责，回补层再
    抓属于双通道重复吃配额。下架行（removed_at 非空）不回补。
    """
    from sqlalchemy import asc as _asc

    async with get_session_factory()() as session:
        has_price = (
            select(GameCurrentPrice.appid)
            .where(GameCurrentPrice.appid == Game.appid)
            .exists()
        )
        rows = (
            await session.execute(
                select(Game.appid, Game.name)
                .where(
                    Game.updated_at.is_(None),
                    ~has_price,
                    Game.removed_at.is_(None),
                )
                .order_by(_asc(Game.appid))
                .limit(limit)
            )
        ).all()
    return [(int(a), n or "") for a, n in rows]


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
        logger.info("[worker 分类] 显式配置 crawl.workers=%d，不按出口数分类", int(explicit))
        return int(explicit)

    try:
        from app.domains.proxies import service as proxies_service

        available = int((await proxies_service.pool_stats()).get("available") or 0)
    except Exception as e:  # noqa: BLE001 —— 统计失败不阻断爬取
        logger.warning(
            "[worker 分类] 出口 IP 统计失败（%s），回退 workers=%d", e, DEFAULT_WORKER_COUNT
        )
        return DEFAULT_WORKER_COUNT

    if available <= 0:
        logger.info(
            "[worker 分类] 无可用出口 IP（直连形态）→ workers=%d（默认）",
            DEFAULT_WORKER_COUNT,
        )
        return DEFAULT_WORKER_COUNT

    workers = min(WORKERS_MAX, available)
    logger.info(
        "[worker 分类] 可用出口 IP %d → workers=%d（1 出口 1 worker，上限 %d）",
        available,
        workers,
        WORKERS_MAX,
    )
    return workers


async def start_job(
    scope: str = "appids",
    appids: list[int] | None = None,
    regions: list[str] | None = None,
    kind: str = "manual",
    missing_cooldown: int | None = None,
    cycle_id: int | None = None,
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
    kind="backfill" 为孤儿回补层：忽略 scope/appids，取挂名孤儿行
    （updated_at IS NULL）首爬，低 worker、跳过预检、当日限量。
    missing_cooldown 显式传值时覆盖 missing/repair 两类冷却。
    """
    global _active
    # 统一占用语义：bundles 链尾是直调 run_crawl 的（不登记 _active），只看
    # _active 会漏掉它。在创建 job 之前就挡，避免留下一条"注定失败"的任务行。
    from app.crawler.occupancy import crawler_busy

    if crawler_busy():
        raise RuntimeError("已有爬取任务在运行")
    if _active is not None and not _active.task.done():
        raise RuntimeError("已有爬取任务在运行")

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
    elif kind == "backfill":
        pairs = await _backfill_pairs()
        if not pairs:
            raise ValueError("没有待回补的挂名孤儿游戏")
        effective = await effective_regions(regions)
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
    small_lane = kind in ("missing", "repair", "backfill")
    planned_workers = (
        min(worker_count, MISSING_RECOVERY_WORKERS) if small_lane else worker_count
    )
    data_dir = _get_settings().data_dir

    from app.domains.settings.service import get_value

    if (await get_value("proxy.strategy", "proxy_first")) == "direct_only":
        # 直连策略：价格作业**托管到用户本机网络环境**——加速器 / Clash Verge
        # 等本地代理的通道即实际出口，池子状态与此形态无关（空池也能作业）。
        # 频率由 crawler 的全局滑动窗口闸（200 发/5 分钟）统一约束，多 worker
        # 只是在闸前排队，不提高请求速率，因此 worker 数收在小额。
        effective_workers = min(planned_workers, DIRECT_MODE_WORKERS)
        logger.info(
            "[容量] 直连形态：作业托管到本机网络环境（加速器 / 本地代理的通道即实际出口）"
            "| worker %d | 频率闸 200 发/5 分钟",
            effective_workers,
        )
        config = CrawlRunConfig(
            regions=effective,
            workers=effective_workers,
            timeout=HTTP_TIMEOUT,
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
        logger.info(
            "[容量] 出口槽：已知出口 %d | active lane %d（内核 listener %d，"
            "其中待用 %d）| worker %d（期望 %d，上限 %d）",
            run_plan.get("known_exits", 0), len(proxy_urls),
            run_plan.get("runtime_lanes", len(proxy_urls)),
            max(0, len(proxy_urls) - effective_workers),
            effective_workers, planned_workers, MAX_CRAWL_WORKERS,
        )
        config = CrawlRunConfig(
            regions=effective,
            workers=effective_workers,
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
    async with write_gate(WritePriority.BACKGROUND):
        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            job = CrawlJob(
                kind=kind,
                status="running",
                mode="app",
                regions_json=effective,
                started_at=datetime.now(),
                cycle_id=cycle_id,
                stats_json={
                    "total": initial_total, "processed": 0, "success": 0, "failed": 0,
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _execute(job_id, pairs, config, stop_event, pre_tasks=pre_tasks)
    )
    _active = JobHandle(id=job_id, task=task, stop_event=stop_event)

    # 事件与日志沿用「项数」口径（款数/行数），初始账本的桶口径见上
    pre_rows = sum(
        len(t["appids"]) if "appids" in t else 1 for t in (pre_tasks or [])
    )
    count = len(pairs) + pre_rows
    bus.publish("job.started", job_id=job_id, scope=scope, count=count, regions=effective)
    logger.info(
        "任务 %d 已启动：kind=%s scope=%s 共 %d 项（预构建补抓 %d 发 / %d 行）",
        job_id, kind, scope, count, len(pre_tasks or []), pre_rows,
    )
    return {"id": job_id, "scope": scope, "count": count, "regions": effective}


async def default_queue_specs() -> list[dict]:
    """默认爬取队列组成（唯一来源：自动价格轮与任务页「全部」档共用）。

    - 常驻两段：欠账补抓（missing）→ 监控层（pool：有来源且未排除的对象，
      愿望单/关注按来源优先级排前）；
    - 目录层（catalog：games 主档减监控层）与特惠榜差值段（specials：榜单
      翻页队列去重后的差集）随 KV `crawl.catalog_refresh`（默认开）决定是否
      带上——关闭后只抓监控层，差值段是目录发现通道随之一并停；榜单源另受
      KV `fetch.boards` 门控（在 specials scope 内判定）。

    调度器 `_price_refresh_specs` 与 `start_full_queue` 都从这里取组成；
    新增/调整队列段只改本函数。
    """
    from app.domains.settings.service import get_value

    specs: list[dict] = [
        {"kind": "missing"},
        {"scope": "pool"},
    ]
    if await get_value("crawl.catalog_refresh", True):
        specs.append({"scope": "catalog"})
        specs.append({"scope": "specials", "kind": "specials_backfill"})
    return specs


# 手动全队列链句柄：段间隙占用窗口（_active 已清、下一段未建）的防重入闸
_queue_chain_task: asyncio.Task | None = None

# 链级停止位：段间隙也要能喊停（链句柄只看 done 判不出「该不该继续下一段」）
_queue_stop: asyncio.Event | None = None


async def start_full_queue() -> dict:
    """任务页「全部」档：按默认队列组成后台串行启动整条链。

    与自动价格轮同一编排（`cycle_run.run_price_cycle`，kind='manual'），范围/
    冻结/覆盖/事件/通知全部一致，手动轮同样挂 PriceCycle——手动跑一次与等
    6h 自动跑一次是同一件事。受理前按同出口预解析各段款数：全空 ValueError
    （路由 400），占用 RuntimeError（409），链句柄防重入；无显式 kind 的
    scope 段 job 行记 manual（missing/specials_backfill 是段身份标签，保留）。
    """
    global _queue_chain_task, _queue_stop
    from app.crawler.occupancy import crawler_busy

    if crawler_busy() or (_active is not None and not _active.task.done()):
        raise RuntimeError("已有爬取任务在运行")
    if _queue_chain_task is not None and not _queue_chain_task.done():
        raise RuntimeError("全量队列已在启动中")
    composed = await default_queue_specs()

    missing_pending = await has_pending_missing()
    segment_plan: list[dict] = [{"name": "missing", "count": 1 if missing_pending else 0}]
    for spec in composed:
        scope = spec.get("scope")
        if not scope:
            continue
        segment_plan.append(
            {"name": scope, "count": len(await plan_scope_appids(scope, None))}
        )
    if not any(p["count"] for p in segment_plan):
        raise ValueError(
            "没有可抓取的对象：关注与游戏库都是空的，先在找游戏页添加游戏"
        )

    # 无显式 kind 的 scope 段手动触发记 manual；missing / specials_backfill
    # 是通道/段身份标签（任务页按此显示），保留
    specs = [
        {**spec, "kind": "manual"}
        if spec.get("scope") and "kind" not in spec
        else spec
        for spec in composed
    ]

    async def _chain() -> None:
        # 编排本体在 crawl 域内，与自动轮共用一份；本函数只负责「后台跑 +
        # 链级异常兜底」（段内异常各自兜底，不外抛）
        global _queue_stop
        from app.domains.crawl import cycle_run

        try:
            started = await cycle_run.run_price_cycle(
                specs, kind="manual", stop_event=_queue_stop
            )
            logger.info(
                "[队列] 手动全队列完成：启动 %s",
                "部分/全部段" if started else "无可抓段（全部为空或被占用）",
            )
        except Exception:  # noqa: BLE001 —— 链级异常只记日志，段内已各自兜底
            logger.exception("[队列] 手动全队列异常")
        finally:
            _queue_stop = None

    _queue_chain_task = asyncio.create_task(_chain())
    segment_names = [p["name"] for p in segment_plan]
    logger.info(
        "[队列] 手动全队列受理：段序 %s（款数 %s）",
        segment_names, [p["count"] for p in segment_plan],
    )
    return {
        "queued": True,
        "scope": "all",
        "total": sum(p["count"] for p in segment_plan if p["name"] != "missing"),
        "segments": segment_plan,
    }


async def stop_job(job_id: int | None = None) -> bool:
    """请求停止当前任务。返回是否找到可停止的任务。"""
    if _active is None:
        return False
    if job_id is not None and _active.id != job_id:
        return False
    _active.stop_event.set()
    logger.info("已请求停止任务 %d", _active.id)
    return True


def full_queue_running() -> bool:
    """手动全队列链是否在跑（含段间隙——链句柄未完成即算在跑）。"""
    return _queue_chain_task is not None and not _queue_chain_task.done()


async def stop_full_queue() -> bool:
    """停止手动全队列链：置段间停止位 + 停当前段，返回是否找到在跑的链。

    不 cancel 链任务：链内 await 的是 `run_price_cycle`，取消会穿透它的
    try 边界（CancelledError 不是 Exception），本轮 Cycle 将永久停在
    running 直到下次进程重启收尸。协作式停止让链在段间隙自行退出，
    Cycle 照常收尾（当前段 stopped → 终态 cancelled）。
    """
    global _queue_stop
    if not full_queue_running():
        _queue_stop = None
        return False
    if _queue_stop is not None:
        _queue_stop.set()
    await stop_job()
    logger.info("[队列] 手动全队列已请求停止")
    return True


async def run_sequential(
    specs: list[dict],
    *,
    missing_cooldown: int | None = None,
    cycle_id: int | None = None,
    skip_reasons: list[str] | None = None,
    stop_event: asyncio.Event | None = None,
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
    results: list[dict] = []
    for spec in specs:
        if stop_event is not None and stop_event.is_set():
            if skip_reasons is not None:
                skip_reasons.append(f"{spec.get('kind', spec.get('scope'))}：用户已停止")
            break
        try:
            result = await start_job(
                scope=spec.get("scope", "appids"),
                appids=spec.get("appids"),
                regions=spec.get("regions"),
                kind=spec.get("kind", "scheduled"),
                missing_cooldown=missing_cooldown,
                cycle_id=cycle_id,
            )
        except (RuntimeError, ValueError) as e:
            logger.info("[链式] 跳过 %s：%s", spec.get("kind", spec.get("scope")), e)
            if skip_reasons is not None:
                skip_reasons.append(f"{spec.get('kind', spec.get('scope'))}：{e}")
            continue
        results.append(result)
        active = _active
        if active is not None:
            await active.task
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
