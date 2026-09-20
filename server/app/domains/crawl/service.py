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

from sqlalchemy import or_, select

from app.core.database import get_session_factory
from app.core.events import bus
from app.crawler.config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT, WORKERS_MAX
from app.crawler.runner import CrawlRunConfig, run_crawl
from app.domains.alerts import service as alerts_service
from app.domains.crawl.models import CrawlJob
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.games import service as games_service
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


async def cleanup_orphan_jobs() -> None:
    """进程启动时把上一进程遗留的 running 任务标记为失败。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(CrawlJob).where(CrawlJob.status == "running")
            )
        ).scalars().all()
        for job in rows:
            job.status = "failed"
            job.error = "进程重启中断"
            job.finished_at = datetime.now()
        if rows:
            await session.commit()
            logger.warning("清理了 %d 个中断任务", len(rows))


async def import_appids(appids: list[int]) -> dict:
    """批量导入监控池（任务页批量导入 / 收藏列表导入共用通道）。

    导入 = 加入监控池 + 首爬入库：
    - 合法 appid 一律入池（wishlist_service.add_pool_items：无行新建
      manual_pool 条目、已脱池复活；真愿望单/已购/关注行原样保留来源标记；
      需已绑定账户，否则 ValueError → 400）；
    - 分类口径（对齐 boards.backfill_specs 的缺口判定）：
      ok=待首爬 / own=已在库 / fail=无效——供前端对「新导入」触发首爬
      （kind=import / fav_import）。

    手动入池条目属普通监控条目（必爬，第二优先级）；要升到第一优先级
    去游戏卡点星标关注，或在 Steam 愿望单里保留它。
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

    # 入池（manual_pool 条目）：首爬由前端对「新导入」触发，这里不自动开爬
    pool: dict = {"added": 0, "restored": 0, "exists": 0}
    if clean:
        from app.domains.wishlist import service as wishlist_service

        pool = await wishlist_service.add_pool_items(clean, auto_crawl=False)
    return {
        "results": results,
        "ok": ok,
        "own": own,
        "fail": fail,
        "poolAdded": pool.get("added", 0),
        "poolRestored": pool.get("restored", 0),
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


async def _active_wishlist_ids() -> tuple[list[int], set[int], set[int]]:
    """活跃监控条目去重 appid（下架脱池后，保序）+ 关注集 + 愿望单集。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    WishlistItem.appid, WishlistItem.manual, WishlistItem.wishlisted
                )
                .where(WishlistItem.active.is_(True))
                .distinct()
            )
        ).all()
    # 多账户同游戏多行：manual / wishlisted 按任一账户计；appid 去重保序
    # （distinct 对多列组合去不干净——SQLite DISTINCT 各列组合不同即保留）
    seen_ids: set[int] = set()
    ids: list[int] = []
    manual_ids: set[int] = set()
    wishlisted_ids: set[int] = set()
    for r in rows:
        appid = int(r.appid)
        if appid not in seen_ids:
            seen_ids.add(appid)
            ids.append(appid)
        if r.manual:
            manual_ids.add(appid)
        if r.wishlisted:
            wishlisted_ids.add(appid)
    # 下架脱池（宽限期外）：Steam 愿望单对下架游戏仍返回条目，
    # 不排除则每日价格刷新全 41 区空转打 404
    removed = await _excluded_removed_appids()
    return [a for a in ids if a not in removed], manual_ids, wishlisted_ids


async def _resolve_scope_appids(scope: str, appids: list[int] | None) -> list[tuple[int, str]]:
    """scope: appids（显式列表）| wishlist（全部活跃监控条目，含已购）
    | wishlist_only（活跃且非已购）| owned（活跃且已购）| pool（全池）。
    前四种按第一优先级（愿望单/关注）→ hot（打折/史低）→ appid 序排。

    wishlist 含已购是历史合并路径（跟随模式沿用，不为拆分多付一次预检）；
    自定义已购区域时用 wishlist_only + owned 两个 job 分道抓取。
    pool 为全池监控层：监控条目（愿望单/关注/已购/手动入池优先序）排前，
    其余 games 行垫后——主轮 6h 网格的爬取范围（appdetails 逐行时代全池
    不可行、browse 批量后 ~34 批/区/轮成本可忽略；存储代价由 db_writer
    历史差量门禁兜住）。
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
        # 不排除则每日价格刷新全 41 区空转打 404
        wl_ids = [a for a in wl_ids if a not in await _excluded_removed_appids()]
        return [
            (a, "")
            for a in await _wishlist_ordered(wl_ids, manual_ids, wishlisted_ids)
        ]

    if scope == "pool":
        # 全池 = 监控条目优先序（复用 wishlist 排序）在前 + 其余 games 行
        # （下架脱池）appid 稳定序垫后。单 job 一遍过，不做分层——分层会让
        # 愿望单同轮双爬，产出同价重复快照。
        async with get_session_factory()() as session:
            pool_rows = (
                await session.execute(
                    select(Game.appid)
                    .where(Game.removed_at.is_(None))
                    .order_by(Game.appid)
                )
            ).scalars().all()
        wl_ids, manual_ids, wishlisted_ids = await _active_wishlist_ids()
        head = await _wishlist_ordered(wl_ids, manual_ids, wishlisted_ids)
        head_set = set(head)
        rest = [int(a) for a in pool_rows if int(a) not in head_set]
        return [(a, "") for a in head] + [(a, "") for a in rest]

    raise ValueError(f"未知 scope: {scope}")


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


async def _load_job(job_id: int) -> CrawlJob | None:
    async with get_session_factory()() as session:
        return await session.get(CrawlJob, job_id)


async def _finish_job(job_id: int, status: str, stats: dict | None = None, error: str | None = None) -> None:
    async with get_session_factory()() as session:
        job = await session.get(CrawlJob, job_id)
        if job is None:
            return
        job.status = status
        job.stats_json = stats
        job.finished_at = datetime.now()
        job.error = error
        await session.commit()
    bus.publish("job.status", job_id=job_id, status=status, error=error)


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
            appid_pairs, config=config, stop_event=stop_event, pre_tasks=pre_tasks
        )
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        status = "stopped" if stop_event.is_set() else "done"
        await _finish_job(job_id, status, stats)
        logger.info("任务 %d 结束（%s）：%s", job_id, status, stats)

        if stop_event.is_set():
            # 手动停止：跳过提醒检查 / 史低刷新 / 排序缓存（对未爬完的数据无意义）
            return

        # 爬取落库后触发价格提醒检查 + 史低标记刷新（失败不影响任务状态）
        crawled = [aid for aid, _ in (appid_pairs or [])] + [
            t["id"] for t in (pre_tasks or [])
        ]
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
            # 系列归组：仅当库里有未识别行（series_id NULL）时才全库重算，
            # 已扫过的库这步是零成本探测
            if await games_series.has_unassigned():
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
) -> dict:
    """启动爬取任务。返回任务摘要；已有任务运行时抛 RuntimeError。

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
        pairs = await _resolve_scope_appids(scope, appids)
        if not pairs:
            raise ValueError("任务列表为空")
        effective = await effective_regions(regions)
    # 直连为标准形态：browse 按 country_code 返回各区数据，出口 IP 不参与
    # 判定；加速器（一般用户常态）在系统网络层透明生效。请求频率由全局
    # 限流闸（rate_limit.py 200 发/5 分钟）统一约束，不再有代理前置条件。
    # worker 数按可用出口 IP 节点数分类开启（见 _resolve_worker_count）。
    worker_count = await _resolve_worker_count()
    small_lane = kind in ("missing", "repair", "backfill")
    config = CrawlRunConfig(
        regions=effective,
        workers=min(worker_count, MISSING_RECOVERY_WORKERS) if small_lane else worker_count,
        timeout=HTTP_TIMEOUT,
    )
    async with get_session_factory()() as session:
        job = CrawlJob(
            kind=kind,
            status="running",
            mode="app",
            regions_json=effective,
            started_at=datetime.now(),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _execute(job_id, pairs, config, stop_event, pre_tasks=pre_tasks)
    )
    _active = JobHandle(id=job_id, task=task, stop_event=stop_event)

    # 口径按欠账行数计（appid × 区）：批量补抓一发装该区 ≤400 行，同一
    # appid 的多区欠账各算一行；兼容形状缺 appids 键的按 1 行计
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


async def stop_job(job_id: int | None = None) -> bool:
    """请求停止当前任务。返回是否找到可停止的任务。"""
    if _active is None:
        return False
    if job_id is not None and _active.id != job_id:
        return False
    _active.stop_event.set()
    logger.info("已请求停止任务 %d", _active.id)
    return True


async def run_sequential(
    specs: list[dict],
    *,
    missing_cooldown: int | None = None,
) -> list[dict]:
    """串行链式启动多个爬取任务（单任务模型下唯一的多 spec 方式）。

    specs: [{scope, appids?, regions?, kind?}, ...]，逐个 start_job 并
    await 其完成；已有任务运行（RuntimeError）或任务列表为空（ValueError）
    时跳过该 spec 继续下一个——链式触发的健壮性优先于严格性。
    missing_cooldown 显式传值时透传给 missing/repair 类 spec。
    """
    results: list[dict] = []
    for spec in specs:
        try:
            result = await start_job(
                scope=spec.get("scope", "appids"),
                appids=spec.get("appids"),
                regions=spec.get("regions"),
                kind=spec.get("kind", "scheduled"),
                missing_cooldown=missing_cooldown,
            )
        except (RuntimeError, ValueError) as e:
            logger.info("[链式] 跳过 %s：%s", spec.get("kind", spec.get("scope")), e)
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
            "regions": j.regions_json,
            "stats": j.stats_json,
            "startedAt": j.started_at.isoformat() if j.started_at else None,
            "finishedAt": j.finished_at.isoformat() if j.finished_at else None,
            "error": j.error,
        }
        for j in rows
    ]
