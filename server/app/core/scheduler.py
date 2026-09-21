"""APScheduler 常驻调度：账户同步 / 池价格爬取 / 汇率 / 代理体检。

监控池两层节奏——「池成员资格」与「价格爬取」分开：
- 账户同步 wishlist_sync 15min：拉账户愿望单/已购、差异入库（决定监控
  队列里有什么）+ 新增条目即时首爬（占用时收尾定向补爬；crawl.auto_price
  关闭时只入库不爬，与全部调度链一并停转）
- 池价格爬取 price_refresh 锚点网格：外部时间判
  太平洋夏令时 → Steam 折扣刷新时刻为锚（北京 01:00 夏令时 / 02:00
  冬令时）+ 6h 步进网格（1/7/13/19 或 2/8/14/20）；每轮触发时用
  外部时间重算下一格（DST 切换日网格自动换轨重算）。启动时本地
  zoneinfo 初锚 + 异步外部时间纠偏探针；interval 6h 兜底（与网格
  间距同宽——重锚链断裂也不脱轨）。三层串行 欠账补抓 → 关注层 → 孤儿回补
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.external_time import local_next_grid, probe_next_grid

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def _job_achievements_sync() -> None:
    """成就快照日刷（cron 05:20，错开 03:00 汇率与 04:30 WAL 收缩）：
    已购时长 + 成就进度 + 明细增量。凭证缺失/Steam 拒绝只留日志——
    成就页的手动同步与首次进入自动同步是主通道，定时只做保鲜。"""
    from app.domains.achievements import service as achievements_service

    try:
        steamid, creds = await achievements_service.resolve_credentials()
        if not steamid or not creds:
            logger.info("[定时] 成就同步跳过：未绑定账号或无可用凭证")
            return
        snap = await achievements_service.start_sync()
        logger.info("[定时] 成就同步已发起（running=%s）", snap.get("running"))
    except Exception:  # noqa: BLE001 —— 已在进行中/通道失败不反复打扰
        logger.info("[定时] 成就同步未发起（占用或通道不可用）")


async def _job_wishlist_sync() -> None:
    """账户同步（15min）：池成员资格层——拉账户愿望单/已购、差异入库。
    新增条目的即时首爬受 crawl.auto_price 管辖：开 = 即时首爬（占用时
    sync_account 返回未爬标记，收尾定向补爬）；关 = 只入库不爬。
    不做池内价格刷新——那是 6h 一轮的 _job_price_refresh 职责。
    """
    from app.domains.wishlist import service as wishlist_service

    accounts = await wishlist_service.list_accounts()
    auto_crawl = await price_auto_enabled()
    pending_new: list[int] = []
    for account in accounts:
        try:
            result = await wishlist_service.sync_account(
                account["steamid"], auto_crawl=auto_crawl
            )
            if result.get("ownedError"):
                logger.warning(
                    "[定时] 账户同步 %s：新增 %d / 活跃 %d / 已购拉取失败（%s）",
                    account["steamid"], result["added"], result["active"],
                    result["ownedError"],
                )
            else:
                logger.info(
                    "[定时] 账户同步 %s：新增 %d / 活跃 %d / 已购 %d（通道 %s）",
                    account["steamid"], result["added"], result["active"],
                    result.get("ownedCount", 0), result.get("ownedSource") or "-",
                )
            # 占用漏爬的新增（sync_account crawlTriggered=False 且有 newAppids）
            if auto_crawl and result.get("crawlTriggered") is False and result.get("newAppids"):
                pending_new.extend(result["newAppids"])
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 账户同步失败: %s", account["steamid"])

    # 收尾补爬：本轮同步中因任务占用未首爬的新增条目，定向小批补一次
    # （同批去重；仍在占用则 run_sequential 内部跳过，下轮 15min 同步兜底）
    if pending_new:
        from app.domains.crawl import service as crawl_service

        try:
            uniq = sorted(set(pending_new))
            results = await crawl_service.run_sequential(
                [{"scope": "appids", "appids": uniq, "kind": "wishlist_sync"}],
            )
            if results:
                logger.info("[定时] 账户同步收尾补爬 %d 个新增", len(uniq))
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 账户同步收尾补爬失败")

    # 未爬新增回补：同步入库了但从未获得过价格数据（绑定后进程重启中断、
    # 任务占用漏爬后 15min 内被再次跳过等），会留下
    # 「active=1 且 games 无行」的条目——这些是监控池第一优先级，用户绑定
    # 后看不到价格即体验为「首爬没跑」。每次同步收尾做一次存量对账（配额
    # 帽 _WISH_UNCRAWLED_BATCH 限批），漏多少补多少，直到清零。
    if auto_crawl:
        try:
            appids = await _uncrawled_active_appids()
            if appids:
                from app.domains.crawl import service as crawl_service

                results = await crawl_service.run_sequential(
                    [{
                        "scope": "appids",
                        "appids": appids,
                        "kind": "wishlist_sync",
                    }],
                )
                if results:
                    logger.info("[定时] 未爬新增回补 %d 个（配额帽 %d）",
                                len(appids), _WISH_UNCRAWLED_BATCH)
                    # 爬完仍无 games 行的条目（全球不可见/预取全空）写一笔
                    # missing 尝试痕迹，防止下轮回补对同一批无限重扫——
                    # 之后由 missing 账本通道按自己的节奏重试。
                    await _stamp_uncrawled_missing(appids)
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 未爬新增回补失败")


# 未爬新增回补单轮配额帽：首绑大愿望单（几百款）一次全爬会长时间占住
# 单任务模型；与孤儿回补层的日限量同思路，分轮消化、可预期。
_WISH_UNCRAWLED_BATCH = 400


async def _uncrawled_active_appids() -> list[int]:
    """活跃监控条目中从未被爬过的 appid（games 无行**且**无任何价格状态行）。

    判据取「没有尝试痕迹」而非「没有价格」：locked/blocked/missing 都是
    首爬尝试过但拿不到价的**账本结论**（有各自的补抓/修复通道，回补层再抓
    属于双通道重复烧配额）；games 行（含 COMING_SOON/非游戏打标行）也是
    尝试痕迹。games 无行但有价格状态行的组合（browse 写价成功但元数据
    双缺跳过建行）同样算尝试过——只有**两处全空**（首爬从未抵达写入阶段）
    的条目才是真欠账。排除下架行。
    """
    from sqlalchemy import select as _select

    from app.core.database import get_session_factory
    from app.domains.crawl.service import _excluded_removed_appids
    from app.domains.games.models import Game, GameCurrentPrice
    from app.domains.wishlist.models import WishlistItem

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                _select(WishlistItem.appid)
                .where(WishlistItem.active.is_(True))
                .outerjoin(Game, Game.appid == WishlistItem.appid)
                .outerjoin(GameCurrentPrice, GameCurrentPrice.appid == WishlistItem.appid)
                .where(Game.appid.is_(None), GameCurrentPrice.appid.is_(None))
                .distinct()
            )
        ).scalars().all()
    appids = sorted({int(a) for a in rows})
    excluded = await _excluded_removed_appids()
    return [a for a in appids[:_WISH_UNCRAWLED_BATCH] if a not in excluded]


async def _stamp_uncrawled_missing(appids: list[int]) -> int:
    """回补轮跑完后，对仍无 games 行的 appid 写 missing 状态行（尝试痕迹）。

    只对「回补任务真跑完」的批次执行（调用方在 results 非空时触发）：
    全区 browse 仍不可见的条目没有 games 行，不落痕迹的话每 15min 都会
    被回补层重新选中重扫。missing 行让它们转入 missing 账本通道（4min
    修复轮 + 5 次穷尽 blocked），与本批其余欠账同节奏。写的是 GameCurrentPrice
    的状态行，不动 games 表——后续真抓到数据会正常覆盖。
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects.sqlite import insert as _insert

    from app.core.database import get_session_factory
    from app.domains.games.models import Game, GameCurrentPrice
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    wrote = 0
    async with get_session_factory()() as session:
        # 只挑仍然整行缺失的（回补成功建行的跳过）
        still_missing = (
            await session.execute(
                _select(Game.appid).where(Game.appid.in_([int(a) for a in appids]))
            )
        ).scalars().all()
        known = {int(a) for a in still_missing}
        todo = [int(a) for a in appids if int(a) not in known]
        for appid in todo:
            stmt = _insert(GameCurrentPrice).values(
                appid=appid,
                region_code="",
                currency="",
                price=None,
                original_price=None,
                discount_percent=0,
                sub_id=None,
                price_status="missing",
                fail_count=1,
                cny_fen=None,
                updated_at=now,
            )
            await session.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=[
                        GameCurrentPrice.appid, GameCurrentPrice.region_code,
                    ]
                )
            )
            wrote += 1
        if todo:
            await session.commit()
    if wrote:
        logger.info("[定时] 未爬回补后仍无数据：%d 个记 missing（转补抓通道）", wrote)
    return wrote


async def _reanchor_price_refresh(reason: str) -> None:
    """用外部时间重算 price_refresh 下一格并重锚（自续约核心）。

    永不抛异常——重锚失败时保留现有 next_run_time，interval 兜底
    仍会把 job 拉起来，再走一次本函数即可恢复网格。
    """
    try:
        nxt, hour, is_dst, source = await probe_next_grid()
        scheduler.modify_job("price_refresh", next_run_time=nxt)
        logger.info(
            "[调度] price_refresh 重锚（%s）→ %s 锚点 %02d:00 %s 网格（DST=%s，源=%s）",
            reason, nxt.strftime("%m-%d %H:%M"), hour,
            "/".join(f"{(hour + 6 * i) % 24:02d}" for i in range(4)),
            is_dst, source,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[调度] price_refresh 重锚失败（%s）——保留现有排程", reason)


# ── 5min 失败记录修复线程 ──
# 主价格刷新轮结束工作后到下一轮之间的空窗，每 5 分钟扫全库失败记录
# （price_status='missing'）定向重抓；空闲门禁看任务表不看 6h 间隔——
# 任何爬取任务（含用户手动）在跑 = 爬虫未结束工作，本轮让路。
# 撞锁保护：主价格刷新轮开头置 _price_cycle_busy（修复轮让路）+ 排干
# 等待在跑任务结束后再开三层链（run_sequential 撞锁 spec 整条丢弃）。
_price_cycle_busy = False

# 排干等待参数：修复轮/用户任务占锁时主轮等它结束再开三层链
_DRAIN_POLL_SECONDS = 10
_DRAIN_MAX_POLLS = 60  # 60 × 10s = 10min 上限，超时放行（走撞锁跳过语义）


async def price_auto_enabled() -> bool:
    """自动价格链总开关（KV `crawl.auto_price`，默认开）。

    关掉 = 定时价格更新停转：主轮 price_refresh（含链尾捆绑包刷新）、
    修复轮 price_repair 到点即让路；账户同步只同步池成员资格（新增条目
    不再即时首爬、收尾补爬跳过）；COMING_SOON 重探同样停转。榜单反哺
    （监控队列发现源）与手动发起的爬取（「启动任务」/导入首爬/愿望单页
    手动同步触发的首爬）不受影响——想手动抓的用户关这里即可，全自动
    用户无感。
    """
    from app.domains.settings.service import get_value

    return bool(await get_value("crawl.auto_price", True))


def _crawler_idle() -> bool:
    """爬虫当前无任务在跑（主价格刷新以 _price_cycle_busy 判定）。

    探的是进程内任务表（crawl_service._active），非 6h 间隔——门禁语义是
    「价格刷新爬虫已结束工作」。
    """
    if _price_cycle_busy:
        return False
    from app.domains.crawl import service as crawl_service

    handle = crawl_service._active
    return handle is None or handle.task.done()


async def _job_price_repair() -> None:
    """失败记录修复（5min 一轮）：扫全库 missing 失败记录定向重抓。

    复用 kind='repair'（与主轮 missing 层同通道：按区批量定向补抓、
    低 worker、无打折预检）——抓取逻辑与主爬虫一致；
    仍失败由 handler 照常 mark_region_status('missing') 计账（失败
    标志保留，fail_count 递增，连续 5 次转 blocked 终态停烧配额，
    主轮 watch 层全区重爬仍是恢复通道），成功则清账自愈闭环。

    冷却 4min（< 轮转 5min）：上一轮刚标失败的区下一轮修复即可复访，
    不必等主轮 24h 冷却；不设 0 是防同轮内重复拾取。

    归属边界：本轮启动的 job 不挂 Price Cycle（`cycle_id` 为 NULL）——修复
    候选是全库 missing 账本，与某一轮的期望集没有确定性关联，不做时间
    猜测式归属；归属留给按缺口单元承接的阶段。

    门禁三层：主价格刷新轮占线（busy）让路；任何爬取任务在跑让路
    （用户手动/愿望单同步/榜单反哺都算「爬虫未结束工作」）；无欠账
    （ValueError）静默跳过。撞锁（RuntimeError）静默——上轮修复还在
    收尾，下轮再来。
    """
    global _price_cycle_busy
    if not _crawler_idle():
        return
    if not await price_auto_enabled():
        return
    from app.domains.crawl import service as crawl_service

    try:
        result = await crawl_service.run_sequential(
            [{"kind": "repair"}],
            missing_cooldown=4,
        )
        if result:
            logger.info("[修复] 失败记录修复完成：任务 %s", [r["id"] for r in result])
    except Exception:  # noqa: BLE001
        logger.exception("[修复] 失败记录修复轮异常")


async def _run_price_cycle(specs: list[dict]) -> None:
    """价格网格主轮的 Cycle 驱动段：一个 Cycle 统管本轮全部 job。

    三层 spec 语义不变——欠账补抓 → 监控层（pool：有来源且未排除的对象，
    来源优先级排前）→ 目录层（catalog：games 主档减去监控层，价格库维护轮）。
    两段分开是为了让「被监控的对象」与「库里存在的对象」不再互相推导；
    catalog 排除监控层，同轮不会重复爬。空的 spec 视为正常跳过。

    本轮每个 job 挂同一个 PriceCycle：planning 冻结期望集 → running 跑链 →
    （结束时仍有待补欠账）repairing → finalizing → 终态。Cycle 记账失败
    不阻断抓取——记账失败不该让用户少一轮价格；无 Cycle 时本轮照常跑完，
    只是这一轮没有归属可查。
    """
    from app.domains.crawl import cycle as price_cycle
    from app.domains.crawl import service as crawl_service

    cycle_id: int | None = None
    regions: list[str] | None = None
    expected_units = 0
    try:
        cycle_id = await price_cycle.create("scheduled", "pool")
    except Exception:  # noqa: BLE001
        logger.exception("[周期] 本轮 Cycle 创建失败：抓取照常进行（本轮不挂 Cycle）")
        cycle_id = None
    if cycle_id is not None:
        try:
            regions, expected_units = await price_cycle.freeze_expected(cycle_id, specs)
        except Exception:  # noqa: BLE001
            # 冻结失败不影响抓取：本轮照跑，终态按「无区可爬 / 无期望集」收 failed
            logger.exception("[周期] 本轮期望集冻结失败：抓取照常进行（本轮记 failed）")
            regions = None

    entered_repairing = False
    try:
        await price_cycle.advance(cycle_id, price_cycle.RUNNING)
        results = await crawl_service.run_sequential(specs, cycle_id=cycle_id)
        if not results:
            logger.info("[定时] 池价格爬取：本轮无任务启动（占用/空列表）")
        else:
            logger.info("[定时] 池价格爬取链完成：%s", [r["id"] for r in results])
        # 本轮结束时仍有待补欠账 → 本轮进入 repairing。补抓由既有 5min
        # repair 通道承担（它扫全库账本，与本轮期望集没有确定性关联，job
        # 暂不挂 Cycle），Cycle 只把「本轮确实遗留了未覆盖单元」记下来。
        if cycle_id is not None and await crawl_service.has_pending_missing():
            entered_repairing = await price_cycle.advance(
                cycle_id, price_cycle.REPAIRING
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("[周期] 本轮价格链异常")
        await price_cycle.advance(cycle_id, price_cycle.FAILED, error=str(e)[:200])
        await _record_cycle_stats(cycle_id)
        return

    if cycle_id is None:
        return
    jobs = await price_cycle.jobs_of(cycle_id)
    terminal = (
        price_cycle.FAILED
        if regions is None
        else price_cycle.decide_terminal(
            [j["status"] for j in jobs],
            expected_units=expected_units,
            entered_repairing=entered_repairing,
        )
    )
    if not await price_cycle.advance(cycle_id, price_cycle.FINALIZING):
        return
    # 事件检测排在本轮最终有效结果之上（finalizing 内、终态之前）：job 自己产生
    # 事件会让「暂时失败→随后补抓成功」的单元先报不可用再报恢复
    await _detect_cycle_events(cycle_id)
    await price_cycle.advance(cycle_id, terminal)
    logger.info(
        "[周期] 价格刷新 Cycle %d → %s（job %d 个，期望 %d 单元）",
        cycle_id, terminal, len(jobs), expected_units,
    )
    await _record_cycle_stats(cycle_id)


async def _detect_cycle_events(cycle_id: int | None) -> None:
    """Cycle finalizing：把本轮观察相对历史的变化写成 price_events。

    检测失败只记日志：本轮抓取结果与统计已经落库，事件缺失不该影响它们。
    """
    if cycle_id is None:
        return
    try:
        from app.domains.crawl import events as price_events

        written = await price_events.detect(cycle_id)
        if written:
            logger.info(
                "[周期] Cycle %d 价格事件 %d 条：%s",
                cycle_id, len(written),
                sorted({e["event_type"] for e in written}),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 价格事件检测失败（不影响本轮结果）", cycle_id)


async def _record_cycle_stats(cycle_id: int | None) -> None:
    """Cycle 收敛后留下本轮生产统计（观测结果，不参与任何控制）。

    统计失败只记日志：本轮抓取结果已经落库，统计缺失不该影响它。
    """
    if cycle_id is None:
        return
    try:
        from app.domains.crawl import stats as price_stats

        recorded = await price_stats.record_stats(cycle_id)
        if recorded is not None:
            logger.info(
                "[周期] Cycle %d 统计：对象 %s/%s，单元 %s（ok %s / locked %s / 失败 %s / "
                "未观察 %s），覆盖 %s（确认 %s），stale %s，耗时 %ss",
                cycle_id,
                recorded["targetsDone"], recorded["targetsTotal"],
                recorded["unitsExpected"], recorded["unitsOk"],
                recorded["unitsLocked"], recorded["unitsFailed"],
                recorded["unitsUnobserved"], recorded["coverage"],
                recorded["coverageConfirmed"], recorded["staleCount"],
                recorded["durationSeconds"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("[周期] Cycle %d 统计落库失败（不影响本轮抓取结果）", cycle_id)


async def _job_price_refresh() -> None:
    """池价格爬取（锚点网格：北京 01/07/13/19 夏令时 · 02/08/14/20 冬令时，
    6h 步进）：两层串行 欠账补抓 → 全池（愿望单+已购优先序排前）。

    开头先重锚（下一格算好排队）再干活——长任务跑完后触发器不会覆盖
    手改的 next_run_time（APScheduler 3.11 行为）；DST 切换日下一轮
    探针自动把网格换到新锚点。

    修复线程让路：busy 标志在排干等待**之前**置位——等待窗口内修复
    轮同样让路（否则修复可能抢在主链前夺锁，spec 撞锁整条丢弃，
    该轮全区刷新丢失）。若上一轮修复/用户任务仍占锁，排干等待其结束
    再开链（每 10s 探一次，上限 10min 超时放行）。

    全池层单 job 一遍过：愿望单+已购（manual→打折/史低→appid 优先序）
    排头先爬，其余 games 行（含名单/导入行）垫后——全部池内条目获得与
    愿望单同频的现价刷新。旧关注层/回补层随全池退役（分层会让愿望单
    同轮双爬产生重复快照；孤儿行本就是 games 行，限量随分层失去意义）。
    存储代价由 db_writer 历史差量门禁兜住：价格未变不写快照。

    链尾捆绑包刷新：两层链逐个 await 跑完后，紧跟
    bundles.refresh_bundles() 全量刷包——发现的捆绑包（游戏条目
    purchase_options 落下的无价桩）随同一批请求完成首抓，与存量包的
    各区价/折扣一起跟着这张 6h 网格轮换（锚点即 Steam 折扣刷新时刻，
    折扣轮换后捆包/单买比较不失真）。捆绑包抓取只在链尾发生，不随
    其他爬取运行触发；出网直连 + 与主链共享全局限流预算，异常只记
    日志，不拖垮主链结果。
    """
    global _price_cycle_busy
    if not await price_auto_enabled():
        return
    _price_cycle_busy = True
    try:
        await _reanchor_price_refresh("轮转重锚")

        from app.domains.crawl import service as crawl_service

        # 排干等待：锁被上一轮修复/用户任务占着时等它结束再开链
        for _ in range(_DRAIN_MAX_POLLS):
            if crawl_service._active is None or crawl_service._active.task.done():
                break
            await asyncio.sleep(_DRAIN_POLL_SECONDS)
        specs: list[dict] = [
            {"kind": "missing"},
            {"scope": "pool"},
            {"scope": "catalog"},
        ]
        await _run_price_cycle(specs)

        # 链尾段：捆绑包刷新（游戏侧跑完才轮到它，busy 窗口内修复轮
        # 继续让路；发现桩首抓并入同一次全量刷新，成败细节由
        # refresh_bundles 内部日志记录）
        try:
            from app.domains.bundles import refresh as bundles_refresh

            await bundles_refresh.refresh_bundles()
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 捆绑包刷新异常（不影响主链结果）")
    finally:
        _price_cycle_busy = False


async def _job_price_refresh_with_mails() -> None:
    """调度器注册入口：价格网格主轮 + 链尾邮件段。

    邮件段刻意放在主轮 busy 窗口**之外**——SMTP 出网最长 20s/封，不该占住
    `_price_cycle_busy` 让 5min 修复轮空等；邮件只读库 + 发信，不与爬取争锁。
    包装层同时把「爬取链」与「出网邮件」解耦：任何直接调用主轮函数的地方
    （测试、诊断脚本）都只跑爬取，不会因为在跑主轮而顺手发一封真邮件。
    """
    await _job_price_refresh()
    await _send_price_cycle_mails()


async def _send_price_cycle_mails() -> None:
    """价格网格链尾邮件段：监控池折扣速报 → 捆绑包精选（串行、逐个兜异常）。

    两封各自带游标 + 20h 频率闸（同一条只有折扣进一步加深才再发），
    邮件异常只记日志，绝不影响轮次结果。
    """
    from app.domains.alerts import service as alerts_service

    for label, send in (
        ("监控池折扣速报", alerts_service.check_wishlist_deals),
        ("捆绑包精选", alerts_service.check_bundle_deals),
    ):
        try:
            await send()
        except Exception:  # noqa: BLE001
            logger.exception("[定时] %s 发送失败（不影响轮次结果）", label)


async def _job_fx_refresh() -> None:
    """每日 03:00 定点刷新**当前汇率**（cron 而非 interval：interval 从启动
    起算，本地服务频繁重启时 24h 永远到不了点，自动刷新形同虚设）。

    错过定点（如整夜关机）由启动链的 rates refresh_if_stale 兜底补刷新。
    历史缺口修复是独立任务（_job_fx_history_repair），不挂在实时刷新里。
    """
    from app.domains.settings.service import get_value
    from app.domains.rates import service as rates_service

    if not await get_value("crawl.auto_refresh_rates", True):
        return
    try:
        await rates_service.refresh_rates()
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 汇率刷新失败")


async def _job_fx_history_repair() -> None:
    """汇率历史修复（每日 04:00）：本地扫描 → Provider timeframe → 写 observed。

    与 03:00 实时刷新彻底分离——修复走 Provider 配额（月度上限），不随
    每日自动刷新消耗。四重门禁，不满足即静默跳过（不产生失败重试风暴）：

    1. 存在缺口（本地 scan，零网络；无缺口直接返回）；
    2. 爬虫空闲（历史修复与爬取共享出网预算）；
    3. 已配置 Provider Key（缺失 = 功能未启用）；
    4. 账期配额足够（repair 内部逐窗 quota guard 硬拦，剩余 0 绝不触网）。

    单轮窗口上限（_FX_REPAIR_MAX_WINDOWS）限制单日额度消耗，剩余缺口
    次轮续跑；失败/额度耗尽的真实原因记日志，等下一轮或下月自然恢复。
    """
    from app.domains.rates import history as rates_history
    from app.domains.rates.providers.exchangerate_host import resolve_api_key

    try:
        scan = await rates_history.scan_history_gaps()
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 汇率历史缺口扫描失败")
        return
    if not scan["windows"]:
        return
    if not _crawler_idle():
        logger.info("[定时] 汇率历史修复跳过：爬虫占线（缺口 %d 个窗口待次轮）",
                    len(scan["windows"]))
        return
    if not resolve_api_key():
        return  # 未配置 Key：功能未启用，静默
    try:
        result = await rates_history.repair_history_gaps(max_windows=_FX_REPAIR_MAX_WINDOWS)
        if result["status"] == "ok":
            logger.info(
                "[定时] 汇率历史修复：请求 %d 次 / 写入 %d 行 / 窗口 %d",
                result["requests"], result["written"], len(result["windows"]),
            )
        elif result["status"] != "no_gaps":
            logger.warning(
                "[定时] 汇率历史修复中止：%s（%s）",
                result["status"], result.get("error", ""),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 汇率历史修复异常")


# 历史修复单轮窗口上限：8 窗口/轮 ≈ 单日最多 8 次 Provider 请求
# （跨月重置自然续跑；缺口极大时多日消化，不挤占当月全部额度）
_FX_REPAIR_MAX_WINDOWS = 8


async def _job_subscription_refresh() -> None:
    """Clash 订阅重拉（30min 一拍；真间隔由 service 侧 6h 门槛决定）。

    门槛落在库里（KV），跨重启有效——本地软件不常驻，APScheduler 的间隔
    只是兜底频率，短会话靠启动自启那次下载。只拉「内核正在跑的那条」，
    内核没跑或认不出在跑哪条就跳过（见 service.maybe_refresh_active_...）。

    爬虫占线让路：配置有变化会重启内核，在跑的爬取连接会被切断——门槛
    不消费，等到空闲的那一刻照拉。

    重拉带新配置（内核已重启）时接一次节点检测：新节点在账本里是空行，
    「存活 x/y」与仪表盘可用数否则会停在账本口径等下个 6h 体检窗口。
    """
    from app.domains.proxies import service as proxies_service

    if not _crawler_idle():
        logger.info("[定时] Clash 订阅重拉跳过：爬虫占线")
        return
    try:
        result = await proxies_service.maybe_refresh_active_clash_subscription()
    except Exception:  # noqa: BLE001
        logger.exception("[定时] Clash 订阅重拉异常")
        return
    if result.get("state") != "refreshed":
        return
    logger.info("[定时] Clash 订阅重拉完成：%s 节点", result.get("nodes"))
    if result.get("restarted") and result.get("subscriptionId"):
        try:
            checked = await proxies_service.test_clash_nodes(result["subscriptionId"])
            logger.info(
                "[定时] 订阅重拉后首检：共 %s 节点，可用 %s",
                checked.get("total"), checked.get("alive"),
            )
        except Exception:  # noqa: BLE001 —— 首检失败不影响重拉事实
            logger.exception("[定时] 订阅重拉后首检失败（可稍后手动检测）")


async def _job_proxy_health() -> None:
    """代理体检：手动代理池全测 + Clash 节点状态机检测。

    Clash 侧真正的节流靠 clash_nodes 账本 last_checked_at 的 6h 门槛
    （跨重启有效——本地软件不常驻）；APScheduler 间隔只是兜底频率。
    """
    from app.domains.proxies import service as proxies_service

    proxies = await proxies_service.list_proxies(enabled_only=True)
    if proxies:
        try:
            await proxies_service.test_all()
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 代理体检失败")

    try:
        state = await proxies_service.maybe_run_clash_health_check()
        if state == "checked":
            logger.info("[定时] Clash 节点体检完成")
    except Exception:  # noqa: BLE001
        logger.exception("[定时] Clash 节点体检失败")

    # 体检后判定通道健康度：全灭 / 可用占比过低才发告警（12h 冷却，
    # 未配置任何通道视为用户选择，不告警）
    try:
        from app.domains.alerts import service as alerts_service

        await alerts_service.check_proxy_health()
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 代理通道告警检查失败")


async def _job_wallet_sync() -> None:
    """钱包每分钟轮转：调度只是节拍器，真频率由 service 三层门禁决定
    （快照新鲜度活跃感知 / 失败递增退避 / 429 长冷却——无人看时单账号
    请求量自动降到 ~2 次/小时；App 打开 ≤60s 补上）。

    未绑 Cookie 静默跳过；多账号随机延时错峰不变。
    """
    from app.domains.account import service as account_service

    try:
        result = await account_service.sync_wallets_rotational()
        if result.get("ok"):
            logger.info(
                "[定时] 钱包轮转完成：%s/%s 个账号刷新成功（跳过 %s）",
                result.get("ok_count"), result.get("total"), result.get("skipped"),
            )
        elif result.get("status") != "no_cookie":
            reasons = result.get("reasons") or {}
            logger.info(
                "[定时] 钱包轮转无成功账号：%s/%s 成功%s",
                result.get("ok_count"),
                result.get("total"),
                "；" + "；".join(f"{n}× {why}" for why, n in reasons.items())
                if reasons
                else "（可能全部退避中）",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 钱包轮转异常")


async def _job_bills_sync() -> None:
    """账单/许可常驻同步（Cookie 绑定后自动拉全量消费历史 + 入库记录）。

    半小时一轮；上一轮失败不重试，等下一轮到点再拉。单次失败
    不再同步的语义由 sync_bills 的陈旧锁判定兜底（见其 docstring）。
    """
    from app.domains.bills import service as bills_service

    try:
        result = await bills_service.sync_bills(force=False)
        if result.get("ok"):
            logger.info(
                "[定时] 账单同步完成：%s 游戏 %s 笔 / licenses %s 行",
                result.get("nickname"), result.get("gameTxs"), result.get("licenseRows"),
            )
        elif result.get("status") not in ("no_cookie", "busy"):
            logger.warning("[定时] 账单同步失败（等下一轮）：%s", result.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 账单同步异常")


def _make_board_job(
    board_key: str, backfill_limit: int = 100, record_preset: bool = False
):
    """榜单发现源定时任务工厂：预热缓存 + 落监控池 + 反哺爬取队列。

    落池（boards.BOARDS[key].pool=True 的板：topsellers / popularnew /
    comingsoon）：本轮榜整批并入持久监控池（wishlist_service.
    ensure_board_pool）——榜单游戏成为随全池轮刷新的监控条目；无绑定
    账户（ValueError）静默跳过，反哺照常。specials 属临时队列
    （pool=False）：只补游戏商店差集，不落监控池。

    反哺限量（backfill_limit）：首跑特惠差集可达千级（封顶拉榜 5000 条），
    按 Steam 返回的热度序每轮限量消化（100 → specials 每 6h 一轮 = 400/天），
    避免单轮 run_sequential 跑几千个 appid 挤占任务锁。

    record_preset=True（热销榜）：本轮榜整批登记进预设池清单
    （games/preset.py，随资产种子分发的初始游戏库来源之一）；登记只记档，
    不改变反哺/爬取语义，失败只记日志。

    **不受 crawl.auto_price 总开关管**：反哺是监控队列的发现源（把榜单新
    条目首爬入库），不是价格更新作业——关掉自动价格更新不应停掉发现。
    """

    async def _job() -> None:
        from app.domains.crawl import service as crawl_service
        from app.domains.games import boards as boards_mod

        try:
            appids = await boards_mod.refresh_board(board_key)
            if appids:
                logger.info("[定时] %s 预热完成：%d 个 appid", board_key, len(appids))
            else:
                logger.warning("[定时] %s 预热未获取到数据", board_key)
                return
        except Exception:  # noqa: BLE001
            logger.exception("[定时] %s 预热失败", board_key)
            return

        # 落持久监控池：board.pool=True 的板本轮整批并入监控池（反复上榜
        # 只补缺；已手动移除的条目不复活）。无账户/落池失败不阻断反哺。
        if boards_mod.BOARDS[board_key].pool:
            from app.domains.wishlist import service as wishlist_service

            try:
                landed = await wishlist_service.ensure_board_pool(appids)
                logger.info(
                    "[定时] %s 落监控池：新增 %d / 已在池 %d / 已移除跳过 %d",
                    board_key, landed["added"], landed["exists"], landed["skipped"],
                )
            except ValueError as e:
                logger.info("[定时] %s 未落监控池（%s）——反哺照常", board_key, e)
            except Exception:  # noqa: BLE001
                logger.exception("[定时] %s 落监控池失败（不阻断反哺）", board_key)

        if record_preset:
            from app.domains.games import preset as preset_mod

            try:
                n = await preset_mod.record_board(appids)
                logger.info("[定时] %s 预设池登记：%d 款", board_key, n)
            except Exception:  # noqa: BLE001
                logger.exception("[定时] %s 预设池登记失败（不阻断反哺）", board_key)

        try:
            specs = await boards_mod.backfill_specs(board_key, limit=backfill_limit)
            if specs:
                results = await crawl_service.run_sequential(specs)
                logger.info("[定时] %s 反哺爬取完成：%s", board_key, [r["id"] for r in results])
        except Exception:  # noqa: BLE001
            logger.exception("[定时] %s 反哺爬取失败", board_key)

    return _job


BACKUP_STALE_HOURS = 24.0
"""启动补备的判据：最新备份龄超过这个值才补一份。

与 `auto_backup` 的 24h interval 同宽——interval 从启动起算，常驻才准；
本判据负责「不常驻」的那一半（见 `_job_backup_catchup`）。
"""


async def _job_wal_truncate() -> None:
    """WAL 物理收缩兜底（每日 04:30 低峰）：TRUNCATE checkpoint + 大小留痕。

    journal_size_limit（database.py 连接钩子）只对设置之后新开库的连接生效，
    且历史峰值文件要等下一次 checkpoint 才截断；这里定期显式截断一次，
    保证「WAL 物理文件长到与主库同量级」这件事可观测、可自愈。爬虫占线
    即静默让路（TRUNCATE 撞写入高峰只会白跑一轮）。
    """
    from pathlib import Path

    from sqlalchemy import text

    from app.core.database import get_engine

    if not _crawler_idle():
        logger.info("[定时] WAL 收缩跳过：爬虫占线")
        return
    db = get_engine().url.database
    if not db:
        return
    wal = Path(str(db) + "-wal")
    before = wal.stat().st_size if wal.exists() else 0
    async with get_engine().connect() as conn:
        busy, frames, _checkpointed = (
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        ).one()
    after = wal.stat().st_size if wal.exists() else 0
    logger.info(
        "[定时] WAL 收缩完成：%.1f MB → %.1f MB（busy=%d, frames=%d）",
        before / 1024 / 1024, after / 1024 / 1024, busy, frames,
    )


async def _job_backup() -> None:
    """每日自动备份（VACUUM INTO 在线快照：不打断写入、含 WAL 已提交事务）。

    **interval 从启动起算**：APScheduler 的 interval 触发器首跑在
    `now + 24h`，所以只跑 8 小时就关机的用法永远等不到它——那一半由
    `_job_backup_catchup` 在启动后补。
    """
    from app.core import backup as core_backup

    try:
        result = await core_backup.create_backup()
        logger.info(
            "[定时] 自动备份完成：%s（%.1f MB，games=%d）",
            result["name"], result["sizeBytes"] / 1024 / 1024, result["games"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[定时] 自动备份失败")
        # 系统告警：备份失败是「数据没有第二份」的信号，24h 冷却内只提醒一次
        try:
            from app.domains.alerts import service as alerts_service

            await alerts_service.send_system_alert(
                kind="backup",
                title="自动备份失败",
                summary="本轮自动备份未能完成，数据库在线快照没有生成。",
                rows=[
                    ("失败原因", str(e)[:180] or e.__class__.__name__),
                    ("发生时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ],
                level="danger",
                hint="可在「设置 → 数据」页检查备份目录剩余空间与写入权限。",
            )
        except Exception:  # noqa: BLE001
            logger.exception("[定时] 备份失败告警发送失败")


async def _job_backup_catchup() -> None:
    """启动补备：最新备份已过期（或压根没有）才补一份。

    判据是「备份龄」而不是「是否启动过」：`BACKUP_KEEP=5` 是「保留最近 5 份」
    而不是「每天一份」，不加判据会让一天重启五次把 5 个位子全占满、把真正
    有历史价值的日备挤掉。

    延迟 5 分钟：启动期 init_db / 首轮爬取正在写库，此刻开 VACUUM INTO
    既抢 IO 又让快照落在最没代表性的时刻（半空的库）。
    """
    from app.core import backup as core_backup

    try:
        await asyncio.sleep(300)
        items = core_backup.list_backups()  # 新→旧；只查元数据不开库
        if items:
            newest = items[0].get("createdAt") or ""
            age_h = (
                datetime.now() - datetime.fromisoformat(newest)
            ).total_seconds() / 3600
            if age_h < BACKUP_STALE_HOURS:
                logger.info("[调度] 启动补备跳过：最新备份 %.1f 小时前", age_h)
                return
            logger.info("[调度] 启动补备触发：最新备份已 %.1f 小时前", age_h)
        else:
            logger.info("[调度] 启动补备触发：尚无任何备份")
        await _job_backup()
    except Exception:  # noqa: BLE001
        logger.exception("[调度] 启动补备异常（不阻塞启动）")


async def _job_hb_choice() -> None:
    """当月 HB Choice 游戏侧标记（每日幂等：membership 页 machine_name
    未变即跳过，新月包出现才解析+打标；无登录态拿不到往期页，错过
    当月即漏，因此按日检查而非按月）。"""
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_hb_choice()
        if not result.get("ok"):
            logger.warning("[调度] HB 当月包标记失败：%s", result)
        elif result.get("recorded") is False:
            logger.warning(
                "[调度] HB 当月包标记未结案（不记账，次日重试）：%d 条未解析",
                len(result.get("unresolved") or []),
            )
        elif not result.get("skipped"):
            logger.info("[调度] HB 当月包标记：%s", result.get("machineName"))
        # 新月包结案即发一封当月包清单（标签游标保证同月只发一封；
        # 失败/未结案/跳过都不发，次日重试）
        try:
            from app.domains.alerts import service as alerts_service

            await alerts_service.check_hb_choice(result)
        except Exception:  # noqa: BLE001
            logger.exception("[调度] HB 当月包邮件发送失败")
    except Exception:  # noqa: BLE001
        logger.exception("[调度] HB 当月包标记异常（次日自动重试）")


async def _job_epic_free() -> None:
    """Epic 喜加一窗口标记（每日幂等：已标记 appid 直接跳过；窗口只含
    当期+预告，历史深度由静态档案/外部名单导入补足，漏跑一日由
    后续轮次自然补齐）。"""
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_epic_free()
        if not result.get("ok"):
            logger.warning("[调度] Epic 免费标记失败：%s", result.get("error"))
        elif result.get("unresolved"):
            logger.warning(
                "[调度] Epic 免费标记有 %d 条未解析（次日重试）",
                len(result.get("unresolved") or []),
            )
        else:
            logger.info(
                "[调度] Epic 免费标记：窗口 %s 款，新标 %d 款",
                result.get("window"), len(result.get("marked") or []),
            )
        # 标记完成后发喜加一邮件（当期新条目才发；预告段只作展示，
        # 转正后才自己发一封）
        try:
            from app.domains.alerts import service as alerts_service

            await alerts_service.check_epic_free()
        except Exception:  # noqa: BLE001
            logger.exception("[调度] Epic 喜加一邮件发送失败")
    except Exception:  # noqa: BLE001
        logger.exception("[调度] Epic 免费标记异常（次日自动重试）")


async def _job_bartervg_bundles() -> None:
    """Barter.vg bundle 计数全量刷新（48h 新鲜度闸内跳过；计数只增，
    差量写入幂等，档案拉取失败保留库内旧值）。"""
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_bundle_counts()
        if not result.get("ok"):
            logger.warning("[调度] Barter.vg bundle 计数刷新失败：%s", result.get("error"))
        elif result.get("skipped"):
            logger.info("[调度] Barter.vg bundle 计数：48h 内已拉取，跳过")
        else:
            logger.info(
                "[调度] Barter.vg bundle 计数：档案 %s 条，更新 %s 行",
                result.get("records"), result.get("updated"),
            )
    except Exception:  # noqa: BLE001
        logger.exception("[调度] Barter.vg bundle 计数异常（次日自动重试）")


async def _job_bartervg_catchup() -> None:
    """启动补跑：首次部署/长期停机等不到每日定点，启动窗口 90s 后补跑
    一轮（48h 新鲜度闸内静默跳过，重启风暴无代价）。"""
    try:
        await asyncio.sleep(90)
        await _job_bartervg_bundles()
    except Exception:  # noqa: BLE001
        logger.exception("[调度] Barter.vg 启动补跑异常（不阻塞启动）")


async def _job_coming_soon_retry() -> None:
    """COMING_SOON 重探层（每日 10:00，限量 20 个）：

    暂缓行 updated_at 冷却超 14 天的，重探一次元数据——已开放预购/
    上线的走正常入库覆盖转正（type/名称/价格全部补齐），仍未开放的
    mark_coming_soon 刷新时间戳重新冷却（每天最多 20 次低成本空转）。
    """
    from app.crawler.db_writer import DbWriter
    from app.domains.crawl import service as crawl_service

    if not await price_auto_enabled():
        return  # 自动价格更新关闭：重探也走爬取通道，一并停转
    try:
        pairs = await DbWriter().coming_soon_retry_pairs(
            cooldown_days=14, limit=20
        )
    except Exception:  # noqa: BLE001
        logger.exception("[定时] COMING_SOON 重探候选查询失败")
        return
    if not pairs:
        return
    try:
        # 走 kind=scheduled 正常 worker 通道（重探目标量小、多为元数据短路）
        results = await crawl_service.run_sequential(
            [{"scope": "appids", "appids": [a for a, _ in pairs],
              "kind": "comingsoon_retry"}],
        )
        if results:
            logger.info("[定时] COMING_SOON 重探完成：%d 个候选", len(pairs))
    except Exception:  # noqa: BLE001
        logger.exception("[定时] COMING_SOON 重探爬取失败")


async def _job_free_promo_retry() -> None:
    """限时赠送复查层（每 6h，限量 30 个）：

    free_kind='promo' 且结束时刻已过（或 1h 内到期）的游戏重爬一次——
    赠送结束 → 价格翻回正价、free_kind 由写库层清除，仪表盘喜加一模块
    随之消失；Steam 延期 → promo_end_at 就地刷新。赠送游戏多数不在
    监控池（特惠反哺临时通道入库），没有这层就永远停在赠送态。
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.crawl import service as crawl_service
    from app.domains.games.models import Game

    if not await price_auto_enabled():
        return  # 自动价格更新关闭：复查也走爬取通道，一并停转
    try:
        now_ts = int(get_beijing_time_obj().timestamp())
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(Game.appid)
                    .where(
                        Game.free_kind == "promo",
                        Game.promo_end_at.is_not(None),
                        Game.promo_end_at <= now_ts + 3600,
                    )
                    .order_by(Game.promo_end_at)
                    .limit(30)
                )
            ).scalars().all()
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 限时赠送复查候选查询失败")
        return
    appids = [int(r) for r in rows]
    if not appids:
        return
    try:
        await crawl_service.run_sequential(
            [{"scope": "appids", "appids": appids, "kind": "free_promo_retry"}],
        )
        logger.info("[定时] 限时赠送复查完成：%d 个候选", len(appids))
    except Exception:  # noqa: BLE001
        logger.exception("[定时] 限时赠送复查爬取失败")


async def _anchor_probe_after_start() -> None:
    """启动后外部时间纠偏探针：初锚是本地 zoneinfo 推算（同步上下文
    无法请求外网），异步请求外部时间权威源核对——非切换日两者恒一致，
    切换日/时钟偏差场景由本探针保证网格跟权威源走。

    服务频繁重启是常态，10s 延时避免启动风暴撞网；启动后第一格
    网格最远 6h，第一轮 price_refresh 触发时还会再重锚一次（双保险）。
    """
    try:
        await asyncio.sleep(10)
        await _reanchor_price_refresh("启动纠偏探针")
    except Exception:  # noqa: BLE001
        logger.exception("[调度] 启动纠偏探针异常（不阻塞启动）")


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(_job_wishlist_sync, "interval", minutes=15, id="wishlist_sync")
    # 失败记录修复：5min 一轮，job 内部自判空闲（busy/任务表），占线即静默让路
    scheduler.add_job(_job_price_repair, "interval", minutes=5, id="price_repair")
    # 池价格爬取：interval 6h 只做兜底（与网格间距同宽——重锚链断裂
    # 也不脱轨），真实节奏由 _reanchor_price_refresh 手改 next_run_time
    # 主导（job 内 modify 的排程不受触发器覆盖）
    scheduler.add_job(
        _job_price_refresh_with_mails, "interval", hours=6, id="price_refresh",
        next_run_time=local_next_grid()[0],
        coalesce=True, misfire_grace_time=None,
    )
    scheduler.add_job(_job_fx_refresh, "cron", hour=3, minute=0, id="fx_refresh")
    # 汇率历史修复：与实时刷新分离的独立任务（配额账本 + 缺口 + 爬虫空闲门禁）
    scheduler.add_job(
        _job_fx_history_repair, "cron", hour=4, minute=0, id="fx_history_repair"
    )
    scheduler.add_job(_job_proxy_health, "interval", hours=6, id="proxy_health")
    # 订阅重拉：拍子给密一点（30min），真间隔靠 service 的 6h KV 门槛 +
    # 爬虫空闲门禁——占线错过一拍不消费门槛，下一拍补上
    scheduler.add_job(
        _job_subscription_refresh, "interval", minutes=30, id="subscription_refresh"
    )
    scheduler.add_job(_job_wallet_sync, "interval", minutes=1, id="wallet_sync")
    scheduler.add_job(_job_bills_sync, "interval", minutes=30, id="bills_sync")
    scheduler.add_job(_job_achievements_sync, "cron", hour=5, minute=20, id="achievements_sync")
    # 热销榜：发现面 5 页（500 条，其中前 100 条仍作 TOP100 展示序）；
    # 首轮反哺放宽到 500 一次补满初始游戏库，并登记预设池清单（随种子分发）
    scheduler.add_job(
        _make_board_job("topsellers", backfill_limit=500, record_preset=True),
        "interval", hours=1, id="board_topsellers",
    )
    scheduler.add_job(_make_board_job("popularnew"), "interval", hours=24, id="board_popularnew")
    scheduler.add_job(_make_board_job("specials"), "interval", hours=6, id="board_specials")
    scheduler.add_job(_make_board_job("comingsoon"), "interval", hours=24, id="board_comingsoon")
    scheduler.add_job(_job_coming_soon_retry, "cron", hour=10, minute=0, id="comingsoon_retry")
    # 限时赠送复查：到期（或 1h 内到期）的 promo 重爬翻转状态；通常 0 候选
    scheduler.add_job(_job_free_promo_retry, "interval", hours=6, id="free_promo_retry")
    scheduler.add_job(_job_hb_choice, "cron", hour=6, minute=40, id="hb_choice")
    scheduler.add_job(_job_epic_free, "cron", hour=7, minute=10, id="epic_free")
    scheduler.add_job(_job_bartervg_bundles, "cron", hour=5, minute=40, id="bartervg_bundles")
    scheduler.add_job(_job_wal_truncate, "cron", hour=4, minute=30, id="wal_truncate")
    scheduler.add_job(_job_backup, "interval", hours=24, id="auto_backup")
    scheduler.start()
    # 外部时间纠偏探针（异步，10s 延时错开启动风暴）；无事件循环的
    # 同步上下文静默跳过——初锚已可用，首轮触发时重锚会再核对一次
    try:
        asyncio.create_task(_anchor_probe_after_start())
    except RuntimeError:
        logger.warning("[调度] 无运行中事件循环，跳过外部时间纠偏探针（初锚生效）")
    # 备份补备（异步，5min 延时）：interval 从启动起算，不常驻的用法等不到
    try:
        asyncio.create_task(_job_backup_catchup())
    except RuntimeError:
        logger.warning("[调度] 无运行中事件循环，跳过启动补备")
    # Barter.vg bundle 计数启动补跑（异步，90s 延时；48h 闸内静默跳过）
    try:
        asyncio.create_task(_job_bartervg_catchup())
    except RuntimeError:
        logger.warning("[调度] 无运行中事件循环，跳过 Barter.vg 启动补跑")
    logger.info("调度器已启动（账户同步 15min / 池价格爬取锚点网格：Steam 折扣刷新锚 北京 01:00[夏令时]/02:00[冬令时] + 6h 步进，外部时间判定 DST / 捆绑包存量刷新随价格链 / 失败记录修复 5min 空闲档 / 汇率每日 03:00 + 历史修复每日 04:00[缺口·空闲·Key·配额四重门禁] / WAL 收缩每日 04:30 / Barter.vg bundle 计数每日 05:40 / 代理体检 6h / Clash 订阅重拉 30min 拍[6h 门槛·爬虫空闲档] / 钱包每分钟轮转 / 账单 30min / 热销榜 1h / 热门新品 24h / 特惠差集 6h / 即将推出 24h / CS 重探每日 10:00 / 自动备份 24h）")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
