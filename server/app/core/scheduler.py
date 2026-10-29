"""APScheduler 常驻调度：账户同步 / 池价格爬取 / 汇率 / 代理体检。

价格网格锚=Steam 折扣刷新时刻（北京 01:00 夏令时 / 02:00 冬令时）+ 6h 步进，
每轮用外部时间重算下一格（DST 自动换轨）；三层串行 欠账补抓 → 监控层 →
目录层，每轮由一个 PriceCycle 统管（`cycle_run.run_price_cycle`）。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.external_time import local_next_grid, probe_next_grid
from app.core.database import WritePriority, write_gate, write_session
from app.core.logging import log_event

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai", job_defaults={"coalesce": True, "max_instances": 1})

_BOARD_LABELS = {
    "topsellers": "热销榜",
    "popularnew": "热门新品",
    "comingsoon": "即将推出",
    "specials": "特惠榜",
}
# job_id → 中文任务名（日志主句用；漏登记的新 job 原样露出 id）
_JOB_LABELS = {
    "wishlist_sync": "心愿单同步",
    "price_refresh": "价格刷新",
    "price_repair": "价格补抓",
    "mini_seed": "基础资料同步",
    "fx_refresh": "汇率刷新",
    "fx_history_repair": "汇率历史补抓",
    "proxy_health": "代理体检",
    "subscription_refresh": "代理订阅拉取",
    "wallet_sync": "钱包同步",
    "bills_sync": "账单同步",
    "achievements_sync": "成就同步",
    "board_topsellers": "热销榜抓取",
    "board_popularnew": "热门新品榜抓取",
    "board_comingsoon": "即将推出榜抓取",
    "comingsoon_retry": "即将推出重试",
    "free_promo_retry": "免费促销重试",
    "hb_choice": "慈善包精选",
    "epic_free": "Epic 免费游戏",
    "steam_events_sync": "Steam 活动同步",
    "bartervg_bundles": "捆绑包抓取",
    "wal_truncate": "WAL 截断",
    "auto_backup": "自动备份",
    "proxypool_cycle": "代理池轮换",
    "proxypool_retention": "代理池清理",
    "agent_ledger_prune": "运行账本清理",
}
# 钱包轮转失败标签。frozen 不在表内：冻结只是熔断终态，真因由 service 按
# 最后一次失败归因后自带（见 account.service._frozen_reason），写成「凭据
# 已失效」会把网络/风控故障顶到用户头上
_WALLET_REASON_LABELS = {
    "backoff": "已退避重试",
    "no_cookie": "未绑定 Cookie",
}
_TRIAGE_ACTION_LABELS = {
    "none": "无需处理",
    "bootstrapped": "已装配运行时",
    "unavailable": "运行时不可用",
    "rebuild_requested": "已请求重建",
}
_TIME_SOURCE_LABELS = {
    "timeapi.io": "在线时间源",
    "zoneinfo": "本地时区库",
}


def _board_label(board_key: str) -> str:
    return _BOARD_LABELS.get(board_key, board_key)


def _job_label(job_id: str) -> str:
    return _JOB_LABELS.get(job_id, job_id)


def _wallet_reason_label(reason: str) -> str:
    return _WALLET_REASON_LABELS.get(reason, reason)


def _triage_action_label(action: str) -> str:
    return _TRIAGE_ACTION_LABELS.get(action, action)


def _time_source_label(source: str) -> str:
    return _TIME_SOURCE_LABELS.get(source, source)


async def _job_achievements_sync() -> None:
    """成就快照日刷（cron 05:20，错开 03:00 汇率与 04:30 WAL 收缩）：
    已购时长 + 成就进度 + 明细增量。凭证缺失/Steam 拒绝只留日志——
    成就页的手动同步与首次进入自动同步是主通道，定时只做保鲜。"""
    from app.domains.achievements import service as achievements_service

    try:
        steamid, creds = await achievements_service.resolve_credentials()
        if not steamid or not creds:
            log_event(
                logger,
                "成就快照同步跳过：未绑定 Steam 账号或凭据不可用",
                tag="跳过",
                detail={"原因": "无可用账号或凭据"},
            )
            return
        snap = await achievements_service.start_sync()
        log_event(
            logger,
            "成就快照同步已发起",
            detail={"同步中": snap.get("running")},
        )
    except Exception:  # noqa: BLE001 —— 已在进行中/通道失败不反复打扰
        log_event(
            logger,
            "成就快照同步本轮未发起：已有同步在进行或数据通道不可用",
            tag="跳过",
            detail={"原因": "占用中或通道不可用"},
        )


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
                log_event(
                    logger,
                    "Steam 账号愿望单同步完成，但已购清单拉取失败",
                    level=logging.WARNING,
                    detail={
                        "账号": account["steamid"],
                        "新增": result["added"],
                        "活跃": result["active"],
                        "失败原因": result["ownedError"],
                    },
                )
            else:
                log_event(
                    logger,
                    "Steam 账号愿望单同步完成",
                    detail={
                        "账号": account["steamid"],
                        "新增": result["added"],
                        "活跃": result["active"],
                        "已购": result.get("ownedCount", 0),
                        "数据通道": result.get("ownedSource") or "未知",
                    },
                )
            # 占用漏爬的新增（sync_account crawlTriggered=False 且有 newAppids）
            if auto_crawl and result.get("crawlTriggered") is False and result.get("newAppids"):
                pending_new.extend(result["newAppids"])
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "Steam 账号愿望单同步失败",
                level=logging.ERROR,
                detail={"账号": account["steamid"]},
                exc_info=True,
            )

    # 收尾补爬：本轮同步中因任务占用未首爬的新增条目，定向小批补一次
    # （同批去重；仍在占用则 run_sequential 内部跳过，下轮 15min 同步兜底）
    if pending_new:
        from app.domains.crawl import service as crawl_service

        try:
            uniq = sorted(set(pending_new))
            results = await crawl_service.run_sequential(
                [{"scope": "appids", "appids": uniq, "kind": "wishlist_sync"}], trigger="scheduled",
            )
            if results:
                log_event(
                    logger,
                    "账户同步收尾，补抓本轮新增条目",
                    detail={"新增条目数": len(uniq)},
                )
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "账户同步收尾的新增条目补抓失败",
                level=logging.ERROR,
                exc_info=True,
            )

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
                    }], trigger="scheduled",
                )
                if results:
                    log_event(
                        logger,
                        "对从未取到价格的监控条目回补首抓",
                        detail={
                            "回补条目数": len(appids),
                            "单轮上限": _WISH_UNCRAWLED_BATCH,
                        },
                    )
                    # 爬完仍无 games 行的条目（全球不可见/预取全空）写一笔
                    # missing 尝试痕迹，防止下轮回补对同一批无限重扫——
                    # 之后由 missing 账本通道按自己的节奏重试。
                    await _stamp_uncrawled_missing(appids)
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "从未取到价格的监控条目回补失败",
                level=logging.ERROR,
                exc_info=True,
            )


# 未爬新增回补单轮配额帽：首绑大愿望单（几百款）一次全爬会长时间占住
# 单任务模型；分轮消化、可预期。
_WISH_UNCRAWLED_BATCH = 400


async def _uncrawled_active_appids() -> list[int]:
    """活跃监控条目中从未被爬过的 appid（games 无行**且**无任何价格状态行）。

    判据取「没有尝试痕迹」而非「没有价格」：locked/blocked/missing 都是
    首爬尝试过但拿不到价的**账本结论**（有各自的补抓/修复通道，重复
    再抓属于双通道重复烧配额）；games 行（含 COMING_SOON/非游戏打标行）也是
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
        # NOT EXISTS 反连接：短路价格表逐行扇出，
        # DISTINCT 保留（多账号对同一 appid 复写行）
        not_in_games = (
            _select(Game.appid).where(Game.appid == WishlistItem.appid).exists()
        )
        not_in_prices = (
            _select(GameCurrentPrice.appid)
            .where(GameCurrentPrice.appid == WishlistItem.appid)
            .exists()
        )
        rows = (
            await session.execute(
                _select(WishlistItem.appid)
                .where(
                    WishlistItem.active.is_(True),
                    ~not_in_games,
                    ~not_in_prices,
                )
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
    被回补对账重新选中重扫。missing 行让它们转入 missing 账本通道（4min
    修复轮 + 5 次穷尽 blocked），与本批其余欠账同节奏。写的是 GameCurrentPrice
    的状态行，不动 games 表——后续真抓到数据会正常覆盖。
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects.sqlite import insert as _insert

    from app.domains.games.models import Game, GameCurrentPrice
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    wrote = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        # 只挑仍然整行缺失的（回补成功建行的跳过）
        still_missing = (
            await session.execute(
                _select(Game.appid).where(Game.appid.in_([int(a) for a in appids]))
            )
        ).scalars().all()
        known = {int(a) for a in still_missing}
        todo = [int(a) for a in appids if int(a) not in known]
        if todo:
            stmt = _insert(GameCurrentPrice).on_conflict_do_nothing(
                index_elements=[
                    GameCurrentPrice.appid, GameCurrentPrice.region_code,
                ]
            )
            await session.execute(
                stmt,
                [
                    {
                        "appid": appid,
                        "region_code": "",
                        "currency": "",
                        "price": None,
                        "original_price": None,
                        "discount_percent": 0,
                        "sub_id": None,
                        "price_status": "missing",
                        "fail_count": 1,
                        "cny_fen": None,
                        "attempt_outcome": "failed",
                        "last_success_at": None,
                        "updated_at": now,
                    }
                    for appid in todo
                ],
            )
            await session.commit()
        wrote = len(todo)
    if wrote:
        log_event(
            logger,
            "回补后仍取不到价格，已标记为缺失数据并转入补抓通道",
            tag="未完成",
            detail={"条目数": wrote},
        )
    return wrote


async def price_refresh_interval_hours() -> int:
    """价格刷新网格步长（KV `crawl.price_interval_hours`，小时，默认 6）。

    用户在「自动抓取」页调整；非正数/超界一律夹取到 1..72，脏值回落默认。
    只改网格步长，锚点小时（Steam 每日刷新对齐）不变；保存后由设置路由
    触发一次重锚即生效。
    """
    from app.domains.settings.service import get_value

    try:
        hours = int(await get_value("crawl.price_interval_hours", 6))
    except (TypeError, ValueError):
        return 6
    return max(1, min(72, hours))


async def _reanchor_price_refresh(reason: str, *, external: bool = True) -> None:
    """用外部时间重算 price_refresh 下一格并重锚（自续约核心）。

    永不抛异常——重锚失败时保留现有 next_run_time，interval 兜底
    仍会把 job 拉起来，再走一次本函数即可恢复网格。
    """
    try:
        hours = await price_refresh_interval_hours()
        nxt, hour, is_dst, source = (await probe_next_grid(timedelta(hours=hours)) if external
            else local_next_grid(timedelta(hours=hours)))
        scheduler.modify_job("price_refresh", next_run_time=nxt)
        log_event(
            logger,
            "价格刷新已重新对齐网格，下一轮按新时刻触发",
            detail={
                "原因": reason,
                "下一轮": nxt.strftime("%m-%d %H:%M"),
                "锚点": f"{hour:02d}:00",
                "网格": "/".join(f"{(hour + hours * i) % 24:02d}" for i in range(4)),
                "步长": f"{hours}小时",
                "夏令时": "是" if is_dst else "否",
                "时间源": _time_source_label(source),
            },
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "价格刷新重新对齐网格失败，保留原排程",
            level=logging.ERROR,
            detail={"原因": reason},
            exc_info=True,
        )


# 主轮执行期间，普通补抓拍让路。
_price_cycle_busy = False


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


async def content_fetch_enabled(key: str) -> bool:
    """内容抓取源开关（KV `fetch.*`，默认开）。False = 该源定时任务到点即让路，
    已有数据保留展示，手动刷新不受影响。设置面在「自动抓取」页。"""
    from app.domains.settings.service import get_value

    return bool(await get_value(key, True))


# ─── 内容源自定义刷新节奏（自动抓取页：HB 月包 / Epic 喜加一）─────────────
# KV `fetch.<source>_schedule` = {"time": "HH:MM", "days": N}（1=每天，上限 30）。
# 注册用默认；启动链（apply_content_schedules_from_settings）与设置保存
# （settings 路由直调 apply_content_schedule）双生效点共用同一重排入口。
CONTENT_SCHEDULE_KEYS = {
    "hb_choice": "fetch.hb_choice_schedule",
    "epic_free": "fetch.epic_free_schedule",
}
CONTENT_SCHEDULE_DEFAULTS = {
    "hb_choice": {"time": "02:30", "days": 1},
    "epic_free": {"time": "23:05", "days": 1},
}


def parse_schedule_time(text: str) -> tuple[int, int]:
    """"HH:MM" → (小时, 分钟)；非法抛 ValueError。"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text).strip())
    if not m:
        raise ValueError(f"非法时刻: {text!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"时刻超出当日范围: {text!r}")
    return hour, minute


def normalize_schedule(raw: object, job_id: str) -> tuple[str, int]:
    """KV 原始值 → (HH:MM, 天数)；缺失/非法逐项回落该源默认。"""
    default = CONTENT_SCHEDULE_DEFAULTS[job_id]
    if not isinstance(raw, dict):
        return default["time"], int(default["days"])
    try:
        hour, minute = parse_schedule_time(str(raw.get("time") or ""))
    except ValueError:
        return default["time"], int(default["days"])
    try:
        days = max(1, min(30, int(raw.get("days") or 1)))
    except (TypeError, ValueError):
        days = int(default["days"])
    return f"{hour:02d}:{minute:02d}", days


def apply_content_schedule(job_id: str, time_text: str, days: int = 1) -> None:
    """内容源定时拍重排（设置保存即时生效 + 启动链应用自定义，同一入口）。

    reschedule 整体替换触发器：days>1 用 cron 日步进（day=*/N，按日历日
    计，月末偶有相邻两天）；days=1 即回到每日。
    """
    hour, minute = parse_schedule_time(time_text)
    kwargs: dict = {"hour": hour, "minute": minute}
    if days > 1:
        kwargs["day"] = f"*/{max(2, days)}"
    scheduler.reschedule_job(job_id, trigger="cron", **kwargs)


async def apply_content_schedules_from_settings() -> None:
    """启动链应用用户自定义内容源拍点（KV 缺失保持注册默认；失败不阻塞）。"""
    from app.domains.settings.service import get_value

    for job_id, key in CONTENT_SCHEDULE_KEYS.items():
        try:
            raw = await get_value(key, None)
            time_text, days = normalize_schedule(raw, job_id)
            apply_content_schedule(job_id, time_text, days)
        except Exception:  # noqa: BLE001 —— 作业缺失/未注册等，保持默认拍点
            continue
        if isinstance(raw, dict):
            label = _JOB_LABELS.get(job_id, job_id)
            rhythm = f"每天 {time_text}" if days == 1 else f"每 {days} 天的 {time_text}"
            log_event(logger, f"{label}自动刷新节奏已按自定义设置生效：{rhythm}")


def _crawler_idle() -> bool:
    """爬虫当前无任务在跑（主价格刷新以 _price_cycle_busy 判定）。

    探的是进程内任务表（crawl_service._active）与爬取占用面（occupancy，
    覆盖 bundles 直调 run_crawl 那条不经任务表的路径），非 6h 间隔——
    门禁语义是「价格刷新爬虫已结束工作」。
    """
    if _price_cycle_busy:
        return False
    from app.crawler.occupancy import crawler_busy

    if crawler_busy():
        return False
    from app.domains.crawl import service as crawl_service

    handle = crawl_service._active
    return handle is None or handle.task.done()


async def _job_price_repair() -> None:
    """维护派生视图、价格欠项与未完提醒，失败交给下一拍。"""
    from app.domains.crawl import service as crawl_service
    from app.domains.games.derived import maintain_derived
    from app.domains.notifications import service as notifications

    await maintain_derived()
    if _crawler_idle() and await price_auto_enabled():
        await _reanchor_price_refresh("正常周期校准", external=False)
        await _kick_stale_price_refresh()
        try:
            await crawl_service.start_job(kind="repair", missing_cooldown=4, trigger="scheduled")
        except (ValueError, RuntimeError) as exc:
            log_event(logger, "本次自动价格补抓未启动", tag="跳过", detail={"原因": str(exc)})
    await notifications.dispatch(None)


async def _job_price_refresh() -> None:
    """每拍执行一次价格周期，再刷新关注的捆绑包。"""
    global _price_cycle_busy
    if _price_cycle_busy:
        return
    if not await price_auto_enabled():
        log_event(logger, "自动价格更新已关闭，本轮未启动", tag="跳过")
        return
    _price_cycle_busy = True
    try:
        await _reanchor_price_refresh("轮转重锚")

        from app.domains.crawl import cycle_run, service as crawl_service

        await cycle_run.run_price_cycle(await crawl_service.default_queue_specs(), kind="scheduled")

        # 链尾段：捆绑包刷新（游戏侧跑完才轮到它，busy 窗口内修复轮
        # 继续让路；发现桩首抓并入同一次全量刷新，成败细节由
        # refresh_bundles 内部日志记录）
        try:
            from app.domains.bundles import refresh as bundles_refresh

            await bundles_refresh.refresh_bundles()
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "捆绑包刷新异常，不影响价格刷新主链结果",
                level=logging.ERROR,
                exc_info=True,
            )
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


async def _job_agent_ledger_prune() -> None:
    """运行账本保留：job 型 run（调度桥行）过窗级联清理，task/fake 不动；
    同拍顺带清爬取任务台账的过窗终态行（90 天窗，见 crawl service）。"""
    from app.domains.agent.runtime import scheduler_bridge
    from app.domains.crawl import service as crawl_service

    pruned = await scheduler_bridge.prune_job_runs()
    pruned_jobs = await crawl_service.prune_crawl_jobs()
    if pruned or pruned_jobs:
        log_event(
            logger,
            "过窗任务台账清理完成",
            detail={
                "清理条数": pruned,
                "清理抓取任务条数": pruned_jobs,
            },
        )


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
            log_event(
                logger,
                f"{label}邮件发送失败，不影响本轮结果",
                level=logging.ERROR,
                exc_info=True,
            )


async def _job_fx_refresh() -> None:
    """每日 03:00 定点刷新**当前汇率**（cron 而非 interval：interval 从启动
    起算，本地服务频繁重启时 24h 永远到不了点，自动刷新形同虚设）。

    错过定点（如整夜关机）由启动链的 rates refresh_if_stale 兜底补刷新。
    历史缺口修复是独立任务（_job_fx_history_repair），不挂在实时刷新里。
    """
    from app.domains.settings.service import get_value
    from app.domains.rates import service as rates_service

    if not await get_value("crawl.auto_refresh_rates", True):
        await scheduler_bridge_note("fx_refresh", {"skipped": "汇率自动更新已关闭"})
        return
    try:
        await rates_service.refresh_if_stale()
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率刷新失败", level=logging.ERROR, exc_info=True)


async def _job_fx_history_repair() -> None:
    """汇率历史修复（每日 04:00）：本地扫描 → Provider 拉取 → 写 derived。

    与 03:00 实时刷新彻底分离，不随每日自动刷新消耗。单源免 Key
    （frankfurter.v2），无任何配置即可运行。三重门禁，不满足即静默跳过
    （不产生失败重试风暴）：

    1. 「汇率历史补全」开关开启；
    2. 存在缺口（本地 scan，零网络；无缺口直接返回）；
    3. 爬虫空闲（历史修复与爬取共享出网预算）。

    单轮窗口上限（_FX_REPAIR_MAX_WINDOWS）限制单日出网规模，剩余缺口
    次轮续跑；失败的真实原因记日志，等下一轮自然恢复。
    """
    from app.domains.rates import history as rates_history

    if not await content_fetch_enabled("fetch.fx_history"):
        await scheduler_bridge_note("fx_history_repair", {"skipped": "汇率历史补全已关闭"})
        return  # 「汇率历史补全」开关关闭
    if not _crawler_idle():
        log_event(
            logger,
            "汇率历史修复跳过：爬虫正在占用，缺口留待下一轮",
            tag="跳过",
        )
        return
    try:
        result = await rates_history.repair_history_gaps(max_windows=_FX_REPAIR_MAX_WINDOWS)
        if result["status"] == "ok":
            log_event(
                logger,
                "汇率历史缺口修复完成",
                detail={
                    "请求次数": result["requests"],
                    "写入行数": result["written"],
                    "修复窗口数": len(result["windows"]),
                },
            )
        elif result["status"] == "no_gaps":
            await scheduler_bridge_note("fx_history_repair", {"skipped": "历史汇率没有缺漏，暂不补齐"})
        else:
            log_event(
                logger,
                "汇率历史修复中止",
                level=logging.WARNING,
                detail={"状态": result["status"], "原因": result.get("error", "")},
            )
    except Exception:  # noqa: BLE001
        log_event(logger, "汇率历史修复异常", level=logging.ERROR, exc_info=True)


# 历史修复单轮窗口上限：8 窗口/轮 ≈ 单日最多 8 次 Provider 请求
# （跨月重置自然续跑；缺口极大时多日消化，不挤占当月全部额度）
_FX_REPAIR_MAX_WINDOWS = 8


# 30 分钟重活的错峰位移：proxypool 周期固定在 5 分钟网格整点起跑，这两个 job 若与
# 它同秒起跑会一起抢 SQLite 写锁（真实生产已出现 `database is locked`）。
_BILLS_STAGGER = timedelta(minutes=2)

# 启动期错峰排程：首屏 0~5s 关键窗口期让路给前台会话与聚合读，后台定时重活在 5s~30s 错峰分批执行。
_STARTUP_STAGGER_PROXYPOOL_CYCLE = timedelta(seconds=5)
_STARTUP_STAGGER_PRICE_REPAIR = timedelta(seconds=10)
_STARTUP_STAGGER_SUBSCRIPTION_REFRESH = timedelta(seconds=15)
_STARTUP_STAGGER_FX_REFRESH = timedelta(seconds=20)
_STARTUP_STAGGER_MINI_SEED = timedelta(seconds=25)
_STARTUP_STAGGER_WISHLIST_SYNC = timedelta(seconds=30)


async def _job_proxypool_cycle() -> None:
    """先复用本地记录确保运行时，再执行正常L0/L1/L2维护。"""
    from datetime import datetime

    from app.core.config import get_settings
    from app.core.database import get_session_factory
    from app.domains.proxies import clash_manager as _cm
    from app.domains.proxies.env_check import USER_MESSAGE, tun_mode_suspected
    from app.domains.proxypool import scheduling as _sched
    from app.domains.proxypool.runtime import (
        controller_endpoint_of,
    )

    if tun_mode_suspected():
        # 环境不可信时探测结论必然是「全死」：节点地址已被换成假地址。继续探测
        # 只会把好节点按连续失败推进到退休，所以整轮（含 L0 与恢复探测）都停。
        log_event(
            logger,
            "代理体检跳过：本机网络被其他软件接管，节点探测结论不可信",
            tag="跳过",
            detail={"原因": USER_MESSAGE},
        )
        return
    data_dir = get_settings().data_dir
    try:
        from app.domains.proxypool.bootstrap import ensure_pool_runtime

        await asyncio.to_thread(_cm.ensure_kernel, data_dir)
        async with get_session_factory()() as session:
            prepared = await ensure_pool_runtime(
                session, data_dir=data_dir, runtime=_cm.pool_runtime,
                exe_path=str(_cm.kernel_exe(data_dir)), now=datetime.now(), skip_sync=True,
            )
        if not prepared.ready:
            log_event(logger, "代理池尚不可用，留待正常维护重试", tag="跳过", detail={"原因": prepared.detail})
            return
        base, secret = controller_endpoint_of(data_dir)
    except Exception as e:
        log_event(
            logger,
            "代理池周期跳过：池运行时尚未就绪",
            tag="跳过",
            detail={"原因": str(e)},
        )
        return
    try:
        # DEAD 恢复探测要打在**持有这些节点配置的内核**上：DEAD 不在池文件里，
        # 池内核不认识它们的名字；旧链路内核跑的就是整条订阅，认识全部节点。
        # 探测/重建/维护全程不持写闸：周期可达分钟级，长持闸会把交互写入
        # 饿死到 busy_timeout。周期内落库各自在 write_gate 短临界区内完成
        # （含 rebuild 段），此处会话只承载读取与兜底收尾提交。
        async with get_session_factory()() as session:
            result = await _sched.run_proxypool_cycle(
                session,
                data_dir=data_dir,
                controller_url=base,
                secret=secret,
                runtime=_cm.pool_runtime,
                exe_path=str(_cm.kernel_exe(data_dir)),
                now=datetime.now(),
            )
            async with write_gate(WritePriority.BACKGROUND):
                await session.commit()
        log_event(
            logger,
            "代理池周期完成",
            detail={
                "节点探测数": len(result.l0),
                "任务占用": "是" if result.busy else "否",
                "已重建": "是" if result.rebuilt else "否",
                "维护": "跳过" if result.maintenance is None else "已执行",
            },
        )
    except Exception:  # noqa: BLE001 —— 周期失败不拖垮调度器
        log_event(logger, "代理池周期执行异常", level=logging.ERROR, exc_info=True)


async def _job_proxypool_retention() -> None:
    """proxypool 遥测保留（每日 04:35，紧随 04:30 的 WAL 收缩）。

    删的是**观测**，不是身份：`proxy_job_runs` / `health_observations` /
    `subscription_snapshots` 按保留期分块清理；
    `proxy_nodes` / `pool_generations` 一行不碰。

    独立定时任务而非启动链一步：保留是**周期性**事务，不是启动一次性事务——本地软件
    不常驻，放启动链会在长会话里永远不跑。删除按
    5000 行一块、每块一个事务，防长事务持写锁跟爬取/调度抢锁；一轮最多 20 块，
    删不完留给下一轮。异常只记日志。
    """
    from datetime import datetime

    from app.core.database import get_session_factory
    from app.domains.proxypool import retention as _ret

    try:
        async with get_session_factory()() as session:
            result = await _ret.prune_telemetry(session, datetime.now())
        log_event(
            logger,
            "代理池遥测保留清理完成",
            detail={
                "作业记录": result.job_runs,
                "健康观测": result.health_observations,
                "订阅快照": result.snapshots,
                "已达单轮上限": "是" if result.truncated else "否",
            },
        )
    except Exception:  # noqa: BLE001 —— 清理失败不拖垮调度器
        log_event(logger, "代理池遥测保留清理异常", level=logging.ERROR, exc_info=True)


async def _job_price_cycle_retention() -> None:
    """每日清理过期价格事件、周期记录与已结束通知结果。"""
    from app.domains.crawl import retention as crawl_retention
    from app.domains.notifications import retention as notification_retention

    try:
        notification_result = await notification_retention.prune_notification_candidates()
        price_result = await crawl_retention.prune_price_cycles()
        if price_result["truncated"] or notification_result["truncated"]:
            log_event(
                logger,
                "价格与通知记录清理达到单轮上限，后续计划会继续",
                tag="延后",
            )
    except Exception:  # noqa: BLE001 —— 清理失败不拖垮调度器
        log_event(logger, "价格周期台账保留清理异常", level=logging.ERROR, exc_info=True)


async def _job_subscription_refresh() -> None:
    """空闲时刷新到期订阅，复用同次快照维护通用及池运行时。"""
    from app.domains.proxies import service as proxies_service

    if not _crawler_idle():
        log_event(
            logger,
            "代理订阅重拉跳过：爬虫正在占用",
            tag="跳过",
            detail={"原因": "爬虫占线"},
        )
        return
    sync_result = None
    try:
        result = await proxies_service.maybe_refresh_active_clash_subscription()
        sync_result = result.get("_syncResult")
    except Exception:  # noqa: BLE001
        log_event(logger, "代理订阅重拉异常", level=logging.ERROR, exc_info=True)
    else:
        if result.get("state") == "refreshed":
            log_event(
                logger,
                "代理订阅重拉完成",
                detail={"节点数": result.get("nodes")},
            )
            if result.get("restarted") and result.get("subscriptionId"):
                try:
                    checked = await proxies_service.test_clash_nodes(result["subscriptionId"])
                    log_event(
                        logger,
                        "订阅重拉后首次节点检测完成",
                        detail={
                            "节点总数": checked.get("total"),
                            "可用节点": checked.get("alive"),
                        },
                    )
                except Exception:  # noqa: BLE001 —— 首检失败不影响重拉事实
                    log_event(
                        logger,
                        "订阅重拉后首次节点检测失败，可稍后手动检测",
                        level=logging.ERROR,
                        exc_info=True,
                    )

    # 订阅刷新 → Snapshot/Registry → 池签名分流；**绝不在这里 stop/start 池 Runtime**
    try:
        from datetime import datetime

        from app.core.config import get_settings
        from app.core.database import get_session_factory
        from app.domains.proxies import clash_manager as _cm
        from app.domains.proxypool import bootstrap as _bs

        data_dir = get_settings().data_dir
        # 会话只承载读取：分流内写入各自进闸（订阅同步闸内提交、bootstrap 事件
        # 独立落库），runtime_ready 探测与内核操作不进闸
        async with get_session_factory()() as session:
            triage = await _bs.handle_subscription_refresh(
                session, data_dir=data_dir, runtime=_cm.pool_runtime,
                exe_path=str(_cm.kernel_exe(data_dir)), now=datetime.now(),
                sync_result=sync_result,
            )
        log_event(
            logger,
            "订阅刷新后代理池已分流处理",
            detail={
                "已同步": "是" if triage.synced else "否",
                "池配置变化": "是" if triage.pool_changed else "否",
                "处理动作": _triage_action_label(triage.action),
            },
        )
    except Exception:  # noqa: BLE001 —— 分流失败不影响订阅重拉事实
        log_event(
            logger,
            "订阅刷新后代理池分流失败，下一轮重试",
            level=logging.ERROR,
            exc_info=True,
        )

    try:
        await proxies_service.ensure_clash_running()
        await proxies_service.refresh_subscription_health_summary()
    except Exception:
        log_event(logger, "通用代理运行时维护失败，下一轮重试", level=logging.ERROR, exc_info=True)


async def _job_proxy_health() -> None:
    """代理体检：手动代理池全测 + 订阅健康摘要（按权威节点账本聚合，不做探测）。

    节点级探测归池维护（5min 拍内的 L1/L2）；本任务只把已落账事实聚合成
    订阅行口径并驱动降级判定，替代旧的 6h 全量重测。
    """
    if not await content_fetch_enabled("proxy.health_auto"):
        await scheduler_bridge_note("proxy_health", {"skipped": "自动节点体检已关闭"})
        return  # 「自动节点体检」开关关闭：定时全测与 Clash 节点检测一并停转
    from app.domains.proxies import service as proxies_service

    proxies = await proxies_service.list_proxies(enabled_only=True)
    if proxies:
        try:
            await proxies_service.test_all()
        except Exception:  # noqa: BLE001
            log_event(logger, "代理节点体检失败", level=logging.ERROR, exc_info=True)

    try:
        state = await proxies_service.refresh_subscription_health_summary()
        if state == "refreshed":
            log_event(logger, "订阅健康摘要已按节点账本刷新")
    except Exception:  # noqa: BLE001
        log_event(logger, "订阅健康摘要刷新失败", level=logging.ERROR, exc_info=True)

    # 体检后判定通道健康度：全灭 / 可用占比过低才发告警（12h 冷却，
    # 未配置任何通道视为用户选择，不告警）
    try:
        from app.domains.alerts import service as alerts_service

        await alerts_service.check_proxy_health()
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "代理通道健康告警检查失败",
            level=logging.ERROR,
            exc_info=True,
        )


# 钱包全员失败日志按「账号总数+冻结数」签名收敛：形态不变只报一次，之后每小时注意级提醒
_WALLET_FAIL_LOG_DIGEST_SECONDS = 3600.0
_wallet_fail_log_state: dict = {}


def _wallet_fail_signature(result: dict) -> tuple:
    return (result.get("total"), result.get("frozen"))


def _fmt_fail_duration(seconds: float) -> str:
    hours, minutes = divmod(int(seconds // 60), 60)
    if hours == 0:
        return f"{minutes} 分钟"
    if minutes == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {minutes} 分钟"


def _log_wallet_fail_converged(result: dict) -> None:
    reasons = result.get("reasons") or {}
    detail = {
        "账号总数": result.get("total"),
        "成功": result.get("ok_count"),
        "失败原因": (
            "；".join(f"{n}×{_wallet_reason_label(w)}" for w, n in reasons.items())
            if reasons else "可能在退避重试中"
        ),
    }
    signature = _wallet_fail_signature(result)
    now = time.monotonic()
    if signature != _wallet_fail_log_state.get("signature"):
        _wallet_fail_log_state.clear()
        _wallet_fail_log_state.update(signature=signature, first_at=now, last_at=now)
        log_event(
            logger,
            f"{result.get('total')} 个账号的 Steam 钱包余额本轮全部同步失败",
            level=logging.ERROR,
            detail=detail,
        )
        return
    if now - _wallet_fail_log_state.get("last_at", now) >= _WALLET_FAIL_LOG_DIGEST_SECONDS:
        _wallet_fail_log_state["last_at"] = now
        duration = _fmt_fail_duration(now - _wallet_fail_log_state["first_at"])
        total = result.get("total")
        if total and result.get("frozen") == total:
            headline = f"{total} 个账号的钱包余额同步仍在冻结，已持续 {duration}"
        else:
            headline = f"Steam 钱包余额同步仍未恢复，已持续 {duration}"
        log_event(logger, headline, level=logging.WARNING, detail=detail)


async def _job_wallet_sync() -> None:
    """钱包每分钟轮转：调度只是节拍器，真频率由 service 三层门禁决定
    （快照新鲜度活跃感知 / 失败递增退避 / 429 长冷却——无人看时单账号
    请求量自动降到 ~2 次/小时；App 打开 ≤60s 补上）。

    未绑 Cookie 静默跳过；多账号随机延时错峰不变。
    全员失败日志同形态只报一次，之后每小时注意级提醒。
    """
    from app.domains.account import service as account_service

    try:
        result = await account_service.sync_wallets_rotational()
        if result.get("ok"):
            _wallet_fail_log_state.clear()
            log_event(
                logger,
                "Steam 钱包余额轮转刷新完成",
                detail={
                    "成功": result.get("ok_count"),
                    "账号总数": result.get("total"),
                    "跳过": result.get("skipped"),
                },
            )
        elif result.get("status") != "no_cookie":
            _log_wallet_fail_converged(result)
        else:
            _wallet_fail_log_state.clear()
            await scheduler_bridge_note("wallet_sync", {"skipped": "未绑定 Steam 账号"})
    except Exception:  # noqa: BLE001
        log_event(logger, "Steam 钱包余额轮转异常", level=logging.ERROR, exc_info=True)


async def _job_bills_sync() -> None:
    """每半小时按绑定账号同步账单，单账号失败不阻断其余账号。"""
    from app.domains.account import service as account_service

    try:
        accounts = await account_service.list_accounts()
        if not accounts:
            await scheduler_bridge_note("bills_sync", {"skipped": "未绑定 Steam Cookie"})
        for account in accounts:
            await _sync_account_bills(account["steam_id"])
    except Exception:  # noqa: BLE001
        log_event(logger, "Steam 账单同步异常", level=logging.ERROR, exc_info=True)


async def _sync_account_bills(steam_id: str) -> None:
    from app.domains.bills import service as bills_service

    try:
        result = await bills_service.sync_bills(force=False, steam_id=steam_id)
        if result.get("ok"):
            log_event(
                logger,
                "Steam 账单同步完成",
                detail={
                    "账号": result.get("nickname"),
                    "游戏消费笔数": result.get("gameTxs"),
                    "许可记录行数": result.get("licenseRows"),
                },
            )
        elif result.get("status") == "no_cookie":
            await scheduler_bridge_note("bills_sync", {"skipped": "未绑定 Steam Cookie"})
        elif result.get("status") == "busy":
            await scheduler_bridge_note("bills_sync", {"skipped": "账单同步进行中"})
        elif result.get("status") not in ("no_cookie", "busy"):
            log_event(
                logger,
                "Steam 账单同步失败，等下一轮重试",
                level=logging.WARNING,
                detail={"原因": result.get("error")},
            )
    except Exception:  # noqa: BLE001
        log_event(logger, "Steam 账单同步异常", level=logging.ERROR, exc_info=True)


def _make_board_job(
    board_key: str, backfill_limit: int = 100, record_preset: bool = False
):
    """榜单发现源定时任务工厂：预热缓存 + 落监控池 + 反哺爬取队列。

    落池（boards.BOARDS[key].pool=True 的板：topsellers / popularnew /
    comingsoon）：本轮榜整批并入持久监控池（wishlist_service.
    ensure_board_pool）——榜单游戏成为随全池轮刷新的监控条目（直挂
    监控层 board 来源，不落账户名下、不要求绑定账户）。

    specials 不走本工厂：特惠+热门榜的去重爬取随价格轮尾段进行
    （`default_queue_specs` 的 specials 段），避免与独立 job 双爬。

    反哺限量（backfill_limit）：首跑反哺可达千级（热销榜封顶拉 500 条），
    按 Steam 返回的热度序每轮限量消化，避免单轮 run_sequential 跑几千个
    appid 挤占任务锁。

    record_preset=True（热销榜）：本轮榜整批登记进预设池清单
    （games/preset.py，随资产种子分发的初始游戏库来源之一）；登记只记档，
    不改变反哺/爬取语义，失败只记日志。

    **不受 crawl.auto_price 总开关管**：反哺是监控队列的发现源（把榜单新
    条目首爬入库），不是价格更新作业——关掉自动价格更新不应停掉发现。
    """

    async def _job() -> None:
        from app.domains.crawl import service as crawl_service
        from app.domains.games import boards as boards_mod

        if not await content_fetch_enabled("fetch.boards"):
            await scheduler_bridge_note(
                f"board_{board_key}", {"skipped": "Steam 排行榜自动更新已关闭"}
            )
            return  # 「Steam 榜单」开关关闭：发现源停转，落池与反哺一并跳过
        try:
            appids = await boards_mod.refresh_board(board_key)
            if appids:
                log_event(
                    logger,
                    f"{_board_label(board_key)}缓存预热完成",
                    detail={"收录条目数": len(appids)},
                )
            else:
                log_event(
                    logger,
                    f"{_board_label(board_key)}缓存预热未获取到数据",
                    level=logging.WARNING,
                )
                return
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                f"{_board_label(board_key)}缓存预热失败",
                level=logging.ERROR,
                exc_info=True,
            )
            return

        # 落持久监控池：board.pool=True 的板本轮整批并入监控池（反复上榜
        # 只补缺；已手动移除的条目不复活）。落池失败不阻断反哺。
        if boards_mod.BOARDS[board_key].pool:
            from app.domains.wishlist import service as wishlist_service

            try:
                landed = await wishlist_service.ensure_board_pool(appids)
                log_event(
                    logger,
                    f"{_board_label(board_key)}条目已并入监控池",
                    detail={
                        "新增": landed["added"],
                        "已在池": landed["exists"],
                        "已移除跳过": landed["skipped"],
                    },
                )
            except Exception:  # noqa: BLE001
                log_event(
                    logger,
                    f"{_board_label(board_key)}条目并入监控池失败，不阻断后续反哺",
                    level=logging.ERROR,
                    exc_info=True,
                )

        if record_preset:
            from app.domains.games import preset as preset_mod

            try:
                n = await preset_mod.record_board(appids)
                log_event(
                    logger,
                    f"{_board_label(board_key)}条目已登记进预设池",
                    detail={"登记款数": n},
                )
            except Exception:  # noqa: BLE001
                log_event(
                    logger,
                    f"{_board_label(board_key)}条目登记进预设池失败，不阻断后续反哺",
                    level=logging.ERROR,
                    exc_info=True,
                )

        try:
            specs = await boards_mod.backfill_specs(board_key, limit=backfill_limit)
            if specs:
                results = await crawl_service.run_sequential(specs, trigger="scheduled")
                log_event(
                    logger,
                    f"{_board_label(board_key)}条目反哺抓取完成",
                    detail={
                        "任务数": len(results),
                        "任务编号": "、".join(str(r["id"]) for r in results[:5]),
                    },
                )
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                f"{_board_label(board_key)}条目反哺抓取失败",
                level=logging.ERROR,
                exc_info=True,
            )

    return _job


BACKUP_STALE_HOURS = 24.0



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
        log_event(
            logger,
            "WAL 收缩跳过：爬虫正在占用",
            tag="跳过",
            detail={"原因": "爬虫占线"},
        )
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
    log_event(
        logger,
        "WAL 物理收缩完成",
        detail={
            "收缩前": f"{before / 1024 / 1024:.1f}MB",
            "收缩后": f"{after / 1024 / 1024:.1f}MB",
            "是否被占用": "是" if busy else "否",
            "检查点帧数": frames,
        },
    )


async def _job_backup() -> None:
    """自动备份只在最近快照超过保留间隔时创建。"""
    if not await content_fetch_enabled("backup.auto"):
        await scheduler_bridge_note("auto_backup", {"skipped": "自动备份已关闭"})
        return  # 「每日自动备份」开关关闭：定时与启动补备一并停转，手动备份不受影响
    from app.core import backup as core_backup

    try:
        items = core_backup.list_backups()
        if items:
            try:
                latest = datetime.fromisoformat(items[0].get("createdAt") or "")
                if (datetime.now() - latest).total_seconds() < BACKUP_STALE_HOURS * 3600:
                    return
            except (TypeError, ValueError):
                log_event(logger, "备份时间记录不可读取，继续创建安全副本", level=logging.WARNING)
        result = await core_backup.create_backup()
        log_event(
            logger,
            "自动备份完成",
            detail={
                "文件名": result["name"],
                "大小": f"{result['sizeBytes'] / 1024 / 1024:.1f}MB",
                "游戏数": result["games"],
            },
        )
    except Exception as e:  # noqa: BLE001
        log_event(logger, "自动备份失败", level=logging.ERROR, exc_info=True)
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
            log_event(
                logger,
                "备份失败告警发送失败",
                level=logging.ERROR,
                exc_info=True,
            )


async def _job_hb_choice() -> None:
    """当月 HB Choice 游戏侧标记（每日幂等：membership 页 machine_name
    未变即跳过，新月包出现才解析+打标；无登录态拿不到往期页，错过
    当月即漏，因此按日检查而非按月）+ 已标记未取价补首爬。"""
    if not await content_fetch_enabled("fetch.hb_choice"):
        await scheduler_bridge_note("hb_choice", {"skipped": "Humble Choice 每月合集自动更新已关闭"})
        return  # 「Humble Choice」开关关闭：当月包核对停转，已有标记保留
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_hb_choice()
        if not result.get("ok"):
            log_event(
                logger,
                "Humble Choice 当月包标记失败",
                level=logging.WARNING,
                detail={"原因": result.get("error")},
            )
        elif result.get("recorded") is False:
            log_event(
                logger,
                "Humble Choice 当月包标记未结案，本轮不记账，次日重试",
                tag="未完成",
                level=logging.WARNING,
                detail={"未解析条数": len(result.get("unresolved") or [])},
            )
        elif not result.get("skipped"):
            log_event(
                logger,
                "Humble Choice 当月包已标记",
                detail={"月包标识": result.get("machineName")},
            )
        # 新月包结案即发一封当月包清单（标签游标保证同月只发一封；
        # 失败/未结案/跳过都不发，次日重试）
        try:
            from app.domains.alerts import service as alerts_service

            await alerts_service.check_hb_choice(result)
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "Humble Choice 当月包清单邮件发送失败",
                level=logging.ERROR,
                exc_info=True,
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "Humble Choice 当月包标记异常，次日自动重试",
            level=logging.ERROR,
            exc_info=True,
        )
    # 已标记未取价的 HB 游戏补首爬（标记 ≠ 取价；打标失败不挡补价）。
    # 撞锁/池未就绪由链式层跳过留日志，次日重试；走爬取通道，auto_price
    # 关闭时与 comingsoon/free_promo 重试层一并停转。
    try:
        if await price_auto_enabled():
            backfill = await metadata_service.backfill_hb_prices()
            if backfill.get("pending"):
                log_event(
                    logger,
                    "Humble Choice 已标记但未取价的游戏已发起补抓",
                    detail={
                        "待补款数": backfill.get("pending"),
                        "已发起": "是" if backfill.get("started") else "否",
                        "任务号": backfill.get("jobId"),
                    },
                )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "Humble Choice 取价补抓异常，不影响标记结果",
            level=logging.ERROR,
            exc_info=True,
        )


async def _job_epic_free() -> None:
    """Epic 喜加一窗口标记（每日幂等：已标记 appid 直接跳过；窗口只含
    当期+预告，历史深度由静态档案/外部名单导入补足，漏跑一日由
    后续轮次自然补齐）。"""
    if not await content_fetch_enabled("fetch.epic_free"):
        await scheduler_bridge_note("epic_free", {"skipped": "Epic 免费游戏自动更新已关闭"})
        return  # 「Epic 免费游戏」开关关闭：喜加一标记停转，已有标记保留
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_epic_free()
        if not result.get("ok"):
            log_event(
                logger,
                "Epic 免费游戏标记失败",
                level=logging.WARNING,
                detail={"原因": result.get("error")},
            )
        elif result.get("unresolved"):
            log_event(
                logger,
                "Epic 免费游戏标记有未解析条目，次日重试",
                tag="未完成",
                level=logging.WARNING,
                detail={"未解析条数": len(result.get("unresolved") or [])},
            )
        else:
            log_event(
                logger,
                "Epic 免费游戏标记完成",
                detail={
                    "窗口款数": result.get("window"),
                    "新标款数": len(result.get("marked") or []),
                },
            )
        # 标记完成后发喜加一邮件（当期新条目才发；预告段只作展示，
        # 转正后才自己发一封）
        try:
            from app.domains.alerts import service as alerts_service

            await alerts_service.check_epic_free()
        except Exception:  # noqa: BLE001
            log_event(
                logger,
                "Epic 喜加一邮件发送失败",
                level=logging.ERROR,
                exc_info=True,
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "Epic 免费游戏标记异常，次日自动重试",
            level=logging.ERROR,
            exc_info=True,
        )


async def _job_steam_events_sync() -> None:
    """Steam 官方活动日历同步（每日一拍；校验门不过则保留旧数据）。
    活动窗口是价格观测行周期标签的来源，回贴随同步幂等执行。"""
    if not await content_fetch_enabled("fetch.steam_events"):
        await scheduler_bridge_note("steam_events_sync", {"skipped": "Steam 活动日历自动更新已关闭"})
        return  # 「Steam 活动日历」开关关闭：同步停转，已有数据保留展示
    from app.domains.steam_events import service as steam_events_service

    try:
        result = await steam_events_service.sync()
        log_event(
            logger,
            "Steam 活动日历同步完成",
            detail={
                "活动数": result.get("count", 0),
                "回贴价格观测行数": result.get("backfilled", 0),
            },
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "Steam 活动日历同步异常，次日自动重试",
            level=logging.ERROR,
            exc_info=True,
        )


async def _job_bartervg_bundles() -> None:
    """Barter.vg bundle 计数全量刷新（48h 新鲜度闸内跳过；计数只增，
    差量写入幂等，档案拉取失败保留库内旧值）。"""
    if not await content_fetch_enabled("fetch.bundle_counts"):
        await scheduler_bridge_note("bartervg_bundles", {"skipped": "捆绑包记录自动更新已关闭"})
        return  # 「捆绑包资料」开关关闭：进包计数刷新停转（启动补跑同闸）
    from app.domains.metadata import service as metadata_service

    try:
        result = await metadata_service.refresh_bundle_counts()
        if not result.get("ok"):
            log_event(
                logger,
                "捆绑包进包计数刷新失败",
                level=logging.WARNING,
                detail={"数据源": "Barter.vg", "原因": result.get("error")},
            )
        elif result.get("skipped"):
            log_event(
                logger,
                "捆绑包进包计数跳过：48 小时内已拉取过",
                tag="跳过",
                detail={"数据源": "Barter.vg"},
            )
        else:
            log_event(
                logger,
                "捆绑包进包计数刷新完成",
                detail={
                    "数据源": "Barter.vg",
                    "档案条数": result.get("records"),
                    "更新行数": result.get("updated"),
                },
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "捆绑包进包计数刷新异常，次日自动重试",
            level=logging.ERROR,
            exc_info=True,
        )


async def _job_coming_soon_retry() -> None:
    """COMING_SOON 重探层（每日 10:00，限量 20 个）：

    暂缓行 updated_at 冷却超 14 天的，重探一次元数据——已开放预购/
    上线的走正常入库覆盖转正（type/名称/价格全部补齐），仍未开放的
    mark_coming_soon 刷新时间戳重新冷却（每天最多 20 次低成本空转）。
    """
    from app.crawler.db_writer import DbWriter
    from app.domains.crawl import service as crawl_service

    if not await price_auto_enabled():
        await scheduler_bridge_note("comingsoon_retry", {"skipped": "自动价格更新已关闭"})
        return  # 自动价格更新关闭：重探也走爬取通道，一并停转
    try:
        pairs = await DbWriter().coming_soon_retry_pairs(
            cooldown_days=14, limit=20
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "即将推出游戏的重探候选查询失败",
            level=logging.ERROR,
            exc_info=True,
        )
        return
    if not pairs:
        await scheduler_bridge_note("comingsoon_retry", {"skipped": "暂无需要重新检查的游戏"})
        return
    try:
        # 走 kind=scheduled 正常 worker 通道（重探目标量小、多为元数据短路）
        results = await crawl_service.run_sequential(
            [{"scope": "appids", "appids": [a for a, _ in pairs],
              "kind": "comingsoon_retry"}], trigger="scheduled",
        )
        if results:
            log_event(
                logger,
                "即将推出游戏重探完成",
                detail={"候选数": len(pairs)},
            )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "即将推出游戏重探抓取失败",
            level=logging.ERROR,
            exc_info=True,
        )


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
        await scheduler_bridge_note("free_promo_retry", {"skipped": "自动价格更新已关闭"})
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
        log_event(
            logger,
            "限时赠送复查的候选查询失败",
            level=logging.ERROR,
            exc_info=True,
        )
        return
    appids = [int(r) for r in rows]
    if not appids:
        await scheduler_bridge_note("free_promo_retry", {"skipped": "暂无到期的赠送游戏"})
        return
    try:
        await crawl_service.run_sequential(
            [{"scope": "appids", "appids": appids, "kind": "free_promo_retry"}], trigger="scheduled",
        )
        log_event(
            logger,
            "限时赠送复查完成",
            detail={"候选数": len(appids)},
        )
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "限时赠送复查抓取失败",
            level=logging.ERROR,
            exc_info=True,
        )


# ── 启动新鲜度补偿 ──
# 启动只补一轮，后续恢复原网格；所有启用地区都近期成功才算对象新鲜。
_STARTUP_STALE_HOURS = 24
_STARTUP_STALE_SHARE = 0.5
_STARTUP_KICK_DELAY_SECONDS = 120


async def _pool_fresh_ratio() -> tuple[int, float] | None:
    """监控对象在全部启用地区近期成功观察的占比。"""
    from sqlalchemy import func, select

    from app.core.database import get_session_factory
    from app.domains.games.models import GameCurrentPrice
    from app.domains.monitoring.models import MonitorTarget
    from app.domains.regions.service import enabled_regions

    cutoff = datetime.now() - timedelta(hours=_STARTUP_STALE_HOURS)
    regions = [r.upper() for r in await enabled_regions()]
    if not regions:
        return None
    pool_where = (
        MonitorTarget.target_type == "game",
        MonitorTarget.state == "active",
    )
    async with get_session_factory()() as session:
        total = await session.scalar(
            select(func.count()).select_from(MonitorTarget).where(*pool_where)
        )
        if not total:
            return None
        fresh = await session.scalar(
            select(func.count()).select_from(MonitorTarget).where(
                *pool_where,
                MonitorTarget.target_id.in_(
                    select(GameCurrentPrice.appid).where(
                        GameCurrentPrice.region_code.in_(regions),
                        GameCurrentPrice.last_success_at >= cutoff,
                    )
                    .group_by(GameCurrentPrice.appid)
                    .having(func.count(func.distinct(GameCurrentPrice.region_code)) == len(regions))
                ),
            )
        )
    return int(total), int(fresh or 0) / int(total)


async def _kick_stale_price_refresh() -> None:
    """优先接续地区欠项；错过网格或监控价格过期时只提前一轮。"""
    if not await price_auto_enabled():
        return
    from app.domains.crawl import service as crawl_service

    if await crawl_service.has_pending_missing(cooldown_minutes=0):
        job = scheduler.get_job("price_repair")
        earliest = datetime.now().astimezone() + timedelta(seconds=_STARTUP_KICK_DELAY_SECONDS)
        if job is not None and (job.next_run_time is None or job.next_run_time > earliest):
            scheduler.modify_job("price_repair", next_run_time=earliest)
        return
    ratio = await _pool_fresh_ratio()
    from sqlalchemy import func, select
    from app.core.database import get_session_factory
    from app.domains.crawl.cycle import PriceCycle

    async with get_session_factory()() as session:
        last_cycle = await session.scalar(select(func.max(PriceCycle.started_at)).where(PriceCycle.kind == "scheduled"))
    interval = await price_refresh_interval_hours()
    previous_grid = local_next_grid(step=timedelta(hours=interval))[0] - timedelta(hours=interval)
    missed = last_cycle is not None and last_cycle < previous_grid.replace(tzinfo=None)
    if not missed and (ratio is None or ratio[1] >= _STARTUP_STALE_SHARE):
        return
    total, share = ratio or (0, 0.0)
    job = scheduler.get_job("price_refresh")
    if job is None:
        return
    earliest = datetime.now().astimezone() + timedelta(
        seconds=_STARTUP_KICK_DELAY_SECONDS
    )
    current = job.next_run_time
    if current is not None and current <= earliest:
        return
    scheduler.modify_job("price_refresh", next_run_time=earliest)
    log_event(
        logger,
        "正常维护发现价格刷新需要补执行，已提前安排",
        detail={
            "池内对象数": total,
            "24 小时内有更新的占比": f"{round(share * 100)}%",
            "触发门槛": f"{round(_STARTUP_STALE_SHARE * 100)}%",
            "提前至": earliest.strftime("%m-%d %H:%M"),
        },
    )


def _acct(job_id: str, fn):
    """为没有领域执行记录的调用保留运行事实。"""
    from app.domains.agent.runtime import scheduler_bridge

    @functools.wraps(fn)
    async def _wrapped():
        await scheduler_bridge.run_accounted(job_id, fn)

    return _wrapped


async def scheduler_bridge_note(step: str, summary: dict) -> None:
    """job 体向当前运行账 run 追加段位流水（跳过原因/链尾结果）；桥内 fail-soft。"""
    from app.domains.agent.runtime import scheduler_bridge

    await scheduler_bridge.note_step(step, summary)


# 调度跳拍事件处理的强引用集：监听器派发的异步投递任务防 GC
_EVENT_TASKS: set[asyncio.Task] = set()


def _hold_event_task(task: asyncio.Task) -> None:
    _EVENT_TASKS.add(task)
    task.add_done_callback(_EVENT_TASKS.discard)


async def _note_max_instances(event) -> None:
    from app.domains.agent.runtime import scheduler_bridge

    try:
        delivered = await scheduler_bridge.note_job_skipped(
            event.job_id,
            event.job_id,
            {
                "skipped": "上一轮还在进行，本拍让路",
                "原定时刻": event.scheduled_run_times[0].astimezone().strftime("%m-%d %H:%M"),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("让路流水投递失败", exc_info=True)
        return
    if not delivered:
        # 在飞 run 恰好刚收口（投递竞态）：降级出同词形日志，痕不丢
        log_event(
            logger,
            f"定时任务「{_job_label(event.job_id)}」上一轮还在进行，本拍让路",
            tag="跳过",
            level=logging.WARNING,
            detail={
                "原定时刻": event.scheduled_run_times[0].astimezone().strftime("%m-%d %H:%M")
            },
        )


def _on_job_skipped(event) -> None:
    try:
        if event.code == EVENT_JOB_MAX_INSTANCES:
            _hold_event_task(asyncio.create_task(_note_max_instances(event)))
        elif event.code == EVENT_JOB_MISSED:
            log_event(
                logger,
                f"定时任务「{_job_label(event.job_id)}」到点没能按时启动，那一拍已跳过，之后照常按拍执行",
                tag="跳过",
                level=logging.WARNING,
                detail={
                    "原定时刻": event.scheduled_run_time.astimezone().strftime("%m-%d %H:%M")
                },
            )
    except Exception:  # noqa: BLE001 —— 监听器异常绝不影响调度器
        logger.debug("调度跳拍事件处理失败", exc_info=True)


async def _job_mini_seed() -> None:
    from app.core.mini_seed import import_mini_seed

    await import_mini_seed()


def start_scheduler() -> None:
    if scheduler.running:
        return
    now = datetime.now()
    scheduler.add_job(
        _acct("wishlist_sync", _job_wishlist_sync),
        "interval",
        minutes=15,
        id="wishlist_sync",
        next_run_time=now + _STARTUP_STAGGER_WISHLIST_SYNC,
    )
    # 失败记录修复：5min 一轮，job 内部自判空闲（busy/任务表），占线即静默让路
    scheduler.add_job(
        _job_price_repair,
        "interval",
        minutes=5,
        id="price_repair",
        next_run_time=now + _STARTUP_STAGGER_PRICE_REPAIR,
    )
    # 池价格爬取：interval 6h 只做兜底（重锚链断裂也不脱轨；真实步长由
    # 设置 crawl.price_interval_hours 决定，初锚的 6h 网格点在启动 10s
    # 纠偏探针处按用户步长重算，注册期同步上下文读不到 KV）
    # 真实节奏由 _reanchor_price_refresh 手改 next_run_time
    # 主导（job 内 modify 的排程不受触发器覆盖）
    scheduler.add_job(
        _job_price_refresh_with_mails,
        "interval", hours=6, id="price_refresh",
        next_run_time=local_next_grid()[0],
        coalesce=True, misfire_grace_time=None,
    )
    scheduler.add_job(_acct("fx_refresh", _job_fx_refresh), "cron", hour=3, minute=0, id="fx_refresh", next_run_time=now + _STARTUP_STAGGER_FX_REFRESH)
    # 汇率历史修复：与实时刷新分离的独立任务（配额账本 + 缺口 + 爬虫空闲门禁）
    scheduler.add_job(
        _acct("fx_history_repair", _job_fx_history_repair), "cron", hour=4, minute=0, id="fx_history_repair"
    )
    scheduler.add_job(_acct("proxy_health", _job_proxy_health), "interval", hours=6, id="proxy_health")
    # 订阅重拉：拍子给密一点（30min），真间隔靠 service 的 6h KV 门槛 +
    # 爬虫空闲门禁——占线错过一拍不消费门槛，下一拍补上
    scheduler.add_job(
        _acct("subscription_refresh", _job_subscription_refresh), "interval", minutes=30, id="subscription_refresh",
        next_run_time=now + _STARTUP_STAGGER_SUBSCRIPTION_REFRESH,
    )
    scheduler.add_job(_acct("wallet_sync", _job_wallet_sync), "interval", minutes=1, id="wallet_sync")
    scheduler.add_job(
        _acct("bills_sync", _job_bills_sync), "interval", minutes=30, id="bills_sync",
        next_run_time=datetime.now() + _BILLS_STAGGER,
    )
    scheduler.add_job(_acct("achievements_sync", _job_achievements_sync), "cron", hour=5, minute=20, id="achievements_sync")
    # 热销榜：发现面 5 页（500 条，其中前 100 条仍作 TOP100 展示序）；
    # 首轮反哺放宽到 500 一次补满初始游戏库，并登记预设池清单（随种子分发）
    scheduler.add_job(
        _acct("board_topsellers", _make_board_job("topsellers", backfill_limit=500, record_preset=True)),
        "interval", hours=1, id="board_topsellers",
    )
    scheduler.add_job(_acct("board_popularnew", _make_board_job("popularnew")), "interval", hours=24, id="board_popularnew")
    # specials 不设独立 job：特惠+热门榜的去重爬取随价格轮尾段进行
    # （`default_queue_specs`），与内部队列重合的对象不重复爬
    scheduler.add_job(_acct("board_comingsoon", _make_board_job("comingsoon")), "interval", hours=24, id="board_comingsoon")
    scheduler.add_job(_acct("comingsoon_retry", _job_coming_soon_retry), "cron", hour=10, minute=0, id="comingsoon_retry")
    # 限时赠送复查：到期（或 1h 内到期）的 promo 重爬翻转状态；通常 0 候选
    scheduler.add_job(_acct("free_promo_retry", _job_free_promo_retry), "interval", hours=6, id="free_promo_retry")
    _hb_hour, _hb_minute = parse_schedule_time(CONTENT_SCHEDULE_DEFAULTS["hb_choice"]["time"])
    scheduler.add_job(_acct("hb_choice", _job_hb_choice), "cron", hour=_hb_hour, minute=_hb_minute, id="hb_choice")
    _ep_hour, _ep_minute = parse_schedule_time(CONTENT_SCHEDULE_DEFAULTS["epic_free"]["time"])
    scheduler.add_job(_acct("epic_free", _job_epic_free), "cron", hour=_ep_hour, minute=_ep_minute, id="epic_free")
    # Steam 活动日历：官方文档页低频变更，每日一拍足够；05:00 避开已占分钟
    scheduler.add_job(
        _acct("steam_events_sync", _job_steam_events_sync), "cron", hour=5, minute=0, id="steam_events_sync"
    )
    scheduler.add_job(_acct("bartervg_bundles", _job_bartervg_bundles), "cron", hour=5, minute=40, id="bartervg_bundles", next_run_time=now + timedelta(seconds=90))
    scheduler.add_job(_acct("wal_truncate", _job_wal_truncate), "cron", hour=4, minute=30, id="wal_truncate")
    # proxypool 遥测保留：每日 04:35（紧随 WAL 收缩，不与 04:30 的重活撞同一分钟）。
    # 分块删除 + 单轮块上限在函数内部；max_instances=1 防叠轮。
    scheduler.add_job(
        _acct("proxypool_retention", _job_proxypool_retention), "cron", hour=4, minute=35, id="proxypool_retention",
        max_instances=1, coalesce=True,
    )
    # 每日自动备份：3 GiB 库上单份是分钟级，且启动补备走的是直接 await 同一函数
    # ——max_instances=1 + coalesce 防「定时拍与补备拍」叠成两份并行快照。
    scheduler.add_job(
        _acct("auto_backup", _job_backup), "interval", hours=24, id="auto_backup",
        next_run_time=now + timedelta(minutes=5), max_instances=1, coalesce=True,
    )
    # proxypool 周期：**只注册这一个**（L0 / pending 重建 / L1-L2 都在它内部按序发生）。
    # 池 Runtime 未就绪时函数内部自行跳过；max_instances=1 防上一轮未跑完又叠一轮。
    scheduler.add_job(
        _acct("proxypool_cycle", _job_proxypool_cycle), "interval", minutes=5, id="proxypool_cycle",
        next_run_time=now + _STARTUP_STAGGER_PROXYPOOL_CYCLE, max_instances=1, coalesce=True,
    )
    # 运行账本保留：job 型 run 过窗清理（级联事件），每日 04:36
    scheduler.add_job(
        _acct("agent_ledger_prune", _job_agent_ledger_prune), "cron", hour=4, minute=36, id="agent_ledger_prune",
        max_instances=1, coalesce=True,
    )
    # 价格周期台账保留：每日 04:37（错峰序列 04:30 WAL→04:35 遥测→04:36 运行账→04:37 周期）
    scheduler.add_job(
        _acct("price_cycle_retention", _job_price_cycle_retention), "cron", hour=4, minute=37, id="price_cycle_retention",
        max_instances=1, coalesce=True,
    )
    # 跳拍事件监听（并发上限让路/到点错失）：remove 先行幂等，防 stop→start 重入双注册
    scheduler.remove_listener(_on_job_skipped)
    scheduler.add_listener(_on_job_skipped, EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED)
    scheduler.add_job(_acct("mini_seed", _job_mini_seed), "interval", minutes=30, id="mini_seed", next_run_time=now + _STARTUP_STAGGER_MINI_SEED)
    scheduler.start()
    log_event(
        logger,
        "调度器已启动，全部定时任务按预定节奏注册完成",
        detail={
            "任务节奏": "账户同步每 15 分钟；价格刷新按 Steam 折扣刷新锚点加 6 小时网格；"
            "失败记录修复每 5 分钟（空闲档）；汇率每日 03:00 刷新、历史缺口每日 04:00 修复；"
            "WAL 收缩每日 04:30；捆绑包进包计数每日 05:40；代理体检每 6 小时；"
            "订阅重拉每 30 分钟（6 小时门槛、爬虫空闲档）；钱包轮转每分钟；账单同步每 30 分钟；"
            "热销榜每小时；热门新品与即将推出每 24 小时；即将推出重探每日 10:00；自动备份每 24 小时",
        },
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
