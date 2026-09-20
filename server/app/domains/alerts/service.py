"""alerts 域服务：规则 CRUD + 爬取后的触发检查 + 邮件通知 + 游戏搜索。"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx
from sqlalchemy import func, or_, select

from app.core.app_info import APP_NAME
from app.core.database import get_session_factory
from app.crawler.config import APPDETAILS_URL
from app.crawler.utils import get_beijing_time_obj
from app.domains.account import service as account_service
from app.domains.games.models import (
    Bundle,
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
    GamePriceHistory,
)
from app.domains.games.pricing import (
    REGION_TO_CURRENCY,
    build_steam_header_url,
    format_cny_fen,
    format_minor_units,
)
from app.domains.regions import service as regions_service
from app.domains.settings import service as settings_service
from app.domains.wishlist.models import WishlistItem
from .models import AlertEvent, PriceAlert
from . import notify

logger = logging.getLogger(__name__)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def region_display_name(code: str) -> str:
    """区服中文名（邮件正文用）；未知 code 回退 code 本身。"""
    try:
        regions = await regions_service.list_regions()
        return next((r["name"] for r in regions if r["code"] == code.lower()), code)
    except Exception:  # noqa: BLE001 — 区服表异常时展示 code 即可
        return code


async def _owner_region_code() -> str:
    """号主所在地区：主账号钱包快照 region_code（小写；钱包从未同步过则未知）。

    新史低速报与监控池折扣速报共用「可直接入手的地区优先」口径，两处必须
    同源，所以出口只有一个。
    """
    try:
        primary = await account_service.get_primary_account()
        wallet = primary.wallet_json if primary and isinstance(primary.wallet_json, dict) else None
        return str((wallet or {}).get("region_code") or "")
    except Exception:  # noqa: BLE001 — 账号域异常不阻断价格检查
        logger.warning("读取主账号地区失败，按无号主区兜底")
        return ""


def _alert_dict(a: PriceAlert) -> dict:
    # targetValue 口径：price 类 = 人民币分（与 cny_fen 同口径）；
    # pct 类 = 百分数
    return {
        "id": a.id,
        "appid": int(a.appid),
        "region": a.region,
        "targetType": a.target_type,
        "targetValue": a.target_value,
        "active": a.active,
        "createdAt": a.created_at.isoformat() if a.created_at else None,
        "lastTriggeredAt": a.last_triggered_at.isoformat() if a.last_triggered_at else None,
    }


async def list_alerts() -> list[dict]:
    """列出全部提醒规则，附带游戏中文名与封面（缺档回退 Steam CDN 拼图）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(PriceAlert).order_by(PriceAlert.id.desc()))
        ).scalars().all()
        # 批量取游戏名 + 封面
        appids = {int(a.appid) for a in rows}
        games: dict[int, tuple[str, str | None]] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name, Game.header_image).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): (g[1], g[2]) for g in game_rows}
    result = []
    for a in rows:
        d = _alert_dict(a)
        name, header = games.get(int(a.appid), ("", None))
        d["gameName"] = name
        d["gameHeader"] = header or build_steam_header_url(int(a.appid))
        result.append(d)
    return result


async def add_alert(
    appid: int, region: str, target_type: str, target_value: float | None
) -> dict:
    """新增规则。target_value：price 类 = 人民币分（前端按元输入 ×100），pct 类 = 百分数。"""
    if target_type not in ("price", "pct", "historic_low"):
        raise ValueError(f"未知提醒类型: {target_type}")
    if target_type in ("price", "pct") and target_value is None:
        raise ValueError("该类型需要目标值")
    async with get_session_factory()() as session:
        alert = PriceAlert(
            appid=appid,
            region=region.upper(),
            target_type=target_type,
            target_value=target_value,
            active=True,
            created_at=_naive(get_beijing_time_obj()),
        )
        session.add(alert)
        await session.commit()
        return _alert_dict(alert)


async def update_alert(
    alert_id: int,
    *,
    active: bool | None = None,
    target_value: float | None = None,
    target_type: str | None = None,
    region: str | None = None,
) -> dict:
    """更新提醒规则（支持编辑条件类型、目标值、区服、启停）。

    target_value 口径同 add_alert：price 类 = 人民币分，pct 类 = 百分数。
    改区不改值——阈值即人民币目标位，换区后继续按同一人民币口径比较。
    """
    async with get_session_factory()() as session:
        alert = await session.get(PriceAlert, alert_id)
        if alert is None:
            raise ValueError("提醒不存在")
        if active is not None:
            alert.active = active
        if target_type is not None:
            if target_type not in ("price", "pct", "historic_low"):
                raise ValueError(f"未知提醒类型: {target_type}")
            alert.target_type = target_type
        if target_value is not None:
            alert.target_value = target_value
        if region is not None:
            alert.region = region.upper()
        await session.commit()
        return _alert_dict(alert)


async def delete_alert(alert_id: int) -> bool:
    async with get_session_factory()() as session:
        alert = await session.get(PriceAlert, alert_id)
        if alert is None:
            return False
        await session.delete(alert)
        await session.commit()
        return True


async def check_appids(appids: list[int]) -> list[dict]:
    """爬取落库后调用：对每个 appid 的活跃规则做触发检查。

    - price:        该区当前价折算人民币分（cny_fen）<= target_value（人民币分）
    - pct:          该区折扣率 >= target_value
    - historic_low: 该区当前价 <= 历史最低价（创新低）
    触发即写 AlertEvent（本币价 + 人民币分快照）并置 last_triggered_at；
    合并成一封邮件通知。
    """
    if not appids:
        return []

    triggered: list[dict] = []
    now = _naive(get_beijing_time_obj())

    async with get_session_factory()() as session:
        alerts = (
            await session.execute(
                select(PriceAlert).where(
                    PriceAlert.active.is_(True), PriceAlert.appid.in_(appids)
                )
            )
        ).scalars().all()

        for alert in alerts:
            price_row = await session.execute(
                select(GameCurrentPrice).where(
                    GameCurrentPrice.appid == alert.appid,
                    GameCurrentPrice.region_code == alert.region,
                    GameCurrentPrice.price_status == "ok",
                )
            )
            current = price_row.scalars().first()
            if current is None or current.price is None:
                continue

            hit = False
            if alert.target_type == "price" and alert.target_value is not None:
                # 阈值与比较同用人民币分口径；cny_fen 缺档（汇率缺失轮）跳过不判
                hit = current.cny_fen is not None and int(current.cny_fen) <= int(alert.target_value)
            elif alert.target_type == "pct" and alert.target_value is not None:
                hit = (current.discount_percent or 0) >= int(alert.target_value)
            elif alert.target_type == "historic_low":
                hist_min = await session.execute(
                    select(func.min(GamePriceHistory.price)).where(
                        GamePriceHistory.appid == alert.appid,
                        GamePriceHistory.region_code == alert.region,
                        GamePriceHistory.price.is_not(None),
                    )
                )
                lowest = hist_min.scalar()
                hit = lowest is not None and int(current.price) <= int(lowest)

            if not hit:
                continue

            game = await session.get(Game, alert.appid)
            price = int(current.price)
            price_cny = int(current.cny_fen) if current.cny_fen else None
            currency = REGION_TO_CURRENCY.get(alert.region, "CNY")
            session.add(
                AlertEvent(
                    alert_id=alert.id,
                    appid=alert.appid,
                    region=alert.region,
                    price=price,
                    price_cny=price_cny,
                    triggered_at=now,
                    notified=False,
                )
            )
            alert.last_triggered_at = now
            triggered.append(
                {
                    "appid": int(alert.appid),
                    "name": game.name if game else str(alert.appid),
                    "region": alert.region,
                    "region_name": await region_display_name(alert.region),
                    "price": price,
                    "price_cny": price_cny,
                    # 邮件价格列：本币现价为主、人民币参照为辅（国区行不重复）
                    "price_text": format_minor_units(price, currency),
                    "cn_price_text": (
                        format_cny_fen(price_cny) if price_cny and alert.region != "CN" else ""
                    ),
                    "discount": current.discount_percent or 0,
                    "alertType": alert.target_type,
                    "targetValue": alert.target_value,
                    # 邮件卡片的辨识度来源：封面 + 该区商店直链（notify 侧拼装）
                    "headerImage": game.header_image if game else None,
                }
            )

        await session.commit()

    if triggered:
        sent = await notify.send_mail(
            f"{APP_NAME} 降价提醒：{len(triggered)} 条触发",
            notify.alert_mail_html(triggered),
        )
        async with get_session_factory()() as session:
            await session.execute(
                AlertEvent.__table__.update().where(
                    AlertEvent.triggered_at == now
                ).values(notified=sent)  # SMTP 未配置/发送失败时诚实标 False
            )
            await session.commit()

    if triggered:
        logger.info("价格提醒触发 %d 条", len(triggered))
    return triggered


# ─── 新史低邮件（hl_flag=1 落库后自动通知）─────────────────────────

# 每封邮件最多展示的新史低条数：全量清单无浏览价值，只递样本（用户语义）
NEW_LOW_PICKS = 2

# 历史新史低 appid 游标（app_settings KV，list[int]）。本轮差集非空才发信，
# 游标随每轮全量刷新（不随 picks 增长，避免漏报）
KEY_NEW_LOW_SENT = "alerts.new_low_sent"


async def check_new_lows(appids: list[int]) -> list[dict]:
    """新史低检查：refresh_hl_flags 落库后调用。

    语义：本轮新史低 = 刚标为 hl_flag=1 的 appids ∩ 全库新史低集 −
    历史已通报集（游标存 app_settings）。命中非空才发信，且**不展示全部**：
    只挑 2 款，优先挑**最低价地区 = 号主所在地区**（主账号钱包区，即用户
    可以直接入手的地区）的，按价差从大到小排；号主区无新史低时按价差
    降序兜底，保证「有新史低就一定通知」。

    返回挑中的 picks（空列表 = 无新增，未发信）。发信失败/未配置不重发，
    游标仍前移（与降价提醒 notified 语义一致：诚实记录，不刷屏重试）。
    """
    if not appids:
        return []
    async with get_session_factory()() as session:
        fresh = (
            await session.execute(
                select(Game.appid, Game.name, Game.header_image).where(
                    Game.appid.in_(appids), Game.hl_flag == 1
                )
            )
        ).all()
        if not fresh:
            return []

        sent_before = await settings_service.get_value(KEY_NEW_LOW_SENT, [])
        sent_set = {int(a) for a in sent_before} if isinstance(sent_before, list) else set()
        news = [(int(a), n, h) for a, n, h in fresh if int(a) not in sent_set]
        if not news:
            return []

        # 号主所在地区：主账号钱包快照 region_code（小写；钱包从未同步过则未知）
        owner_region = await _owner_region_code()

        # 每款附最低价行（全区含国区，口径对齐仪表盘跑马灯）：号主区命中的
        # 直接用号主区价，否则取全区最低
        rows: list[tuple[int, str, str, str, int, bool]] = []  # appid, name, region_code, price_text, diff, owner_hit
        header_map: dict[int, str | None] = {}
        for appid, _name, _header in news:
            header_map[appid] = _header
            prices = (
                await session.execute(
                    select(GameCurrentPrice).where(
                        GameCurrentPrice.appid == appid,
                        GameCurrentPrice.price_status == "ok",
                        GameCurrentPrice.cny_fen.is_not(None),
                        GameCurrentPrice.cny_fen > 0,
                    )
                )
            ).scalars().all()
            best = min(prices, key=lambda p: int(p.cny_fen)) if prices else None
            if best is None:
                continue
            owner_row = next(
                (p for p in prices if p.region_code == owner_region.upper()), None
            ) if owner_region else None
            chosen = owner_row or best
            rows.append(
                (
                    appid,
                    _name,
                    chosen.region_code,
                    format_minor_units(chosen.price, chosen.currency or "CNY"),
                    int(chosen.cny_fen),
                    owner_row is not None,
                )
            )

        # 排序：号主区命中优先，价差大优先（国区价 - 该区价，省得多 = 降幅醒目）
        cn_price_map = (
            await session.execute(
                select(GameCurrentPrice.appid, GameCurrentPrice.cny_fen, GameCurrentPrice.discount_percent)
                .where(
                    GameCurrentPrice.region_code == "CN",
                    GameCurrentPrice.price_status == "ok",
                    GameCurrentPrice.appid.in_([r[0] for r in rows]),
                )
            )
        ).all()
        cn_map = {int(a): (int(f) if f else 0, d or 0) for a, f, d in cn_price_map}
        rows.sort(
            key=lambda r: (not r[5], -(cn_map.get(r[0], (0, 0))[0] - r[4]))
        )

        picks = [
            {
                "appid": appid,
                "name": name,
                "region_code": region,
                "region_name": await region_display_name(region),
                "price_text": price_text,
                "cn_price_text": format_cny_fen(cn_map.get(appid, (None, 0))[0]),
                "discount": cn_map.get(appid, (0, 0))[1],
                "header_image": header_map.get(appid),
            }
            for appid, name, region, price_text, _diff, _hit in rows[:NEW_LOW_PICKS]
        ]
        new_sent = sorted(sent_set | {r[0] for r in rows})

    # 先写游标再发信（同一轮并发检查不会双发；发信失败游标已前移 = 不重试）
    await settings_service.set_value(KEY_NEW_LOW_SENT, new_sent)

    if picks:
        sent = await notify.send_mail(
            f"{APP_NAME} 新史低：{len(picks)} 款精选",
            notify.new_low_mail_html(picks),
        )
        logger.info("新史低邮件已发 %s：%d 款（新增 %d，号主区 %s）",
                    "成功" if sent else "失败（未配置 SMTP 或发送异常，本轮不重试）",
                    len(picks), len(news), owner_region or "未知")
    return picks


# ─── 监控范围扩展邮件：折扣速报 / 捆绑包 / Epic / HB / 通道告警 ──────
#
# 统一语义（与降价提醒、新史低一致）：
# - 「先记账再发信」——游标与时间闸在 send_mail 之前写；SMTP 未配置或发送
#   失败不重试、不刷屏，下一轮按新数据继续（诚实记录）；
# - 每条监控面各自持有游标，互不干扰；
# - 频次闸挡在噪音前面：折扣类 20h 上限（价格网格 6h 一轮 → 每天最多一封），
#   告警类按 kind 冷却（SYS_ALERT_COOLDOWN_HOURS）。

DEAL_MIN_DISCOUNT = 50          # 折扣深度门槛（%）
DEAL_PICKS = 6                  # 监控池速报单封最多展示条数
BUNDLE_DEAL_PICKS = 4           # 捆绑包精选单封最多展示条数
EPIC_FREE_PICKS = 4             # Epic 喜加一单封最多展示条数
DEAL_MIN_INTERVAL_HOURS = 20.0  # 同类速报最小间隔
BUNDLE_MIN_CNY_FEN = 500        # 捆绑包价格下限（¥5，滤掉拆分/占位包）
PROXY_ALERT_RATIO = 0.2         # 可用通道占比低于此值即告警

KEY_WISHLIST_DEAL_SENT = "alerts.wishlist_deal_sent"   # {appid: 已通报折扣}
KEY_WISHLIST_DEAL_AT = "alerts.wishlist_deal_at"       # epoch 秒
KEY_BUNDLE_DEAL_SENT = "alerts.bundle_deal_sent"       # {bundle_id: 已通报折扣}
KEY_BUNDLE_DEAL_AT = "alerts.bundle_deal_at"           # epoch 秒
KEY_EPIC_FREE_SENT = "alerts.epic_free_sent"           # [当期条目键]
KEY_HB_CHOICE_SENT = "alerts.hb_choice_sent"           # 已通报的当月包标签
KEY_SYS_ALERT_AT = "alerts.sys_alert_at"               # {kind: epoch 秒}

SYS_ALERT_COOLDOWN_HOURS: dict[str, float] = {
    "backup": 24.0,   # 备份失败
    "proxy": 12.0,    # 代理通道告警
    "crawl": 12.0,    # 价格网格连续失败
}
SYS_ALERT_DEFAULT_COOLDOWN = 24.0


def _hours_since_ts(ts) -> float:
    """距记账时间戳的小时数；无值/坏值回一个大数（= 从未记过账）。"""
    try:
        return (time.time() - float(ts)) / 3600
    except (TypeError, ValueError):
        return 1e9


def _bundle_cover(header_image: str | None, bundle_id: int, item_kind: int) -> str:
    """包封面第一优先：库内 header_image；Sub 形态再补一条可用的 CDN 路径。

    **bundle 形态的裸 CDN 路径不能当兜底**：`/steam/bundles/{id}/header.jpg`
    恒 404（真实资产名带哈希，只有 browse 的 asset_url_format 才拿得到），
    所以这里不做猜测——缺失时由调用方回落到包内第一款游戏的封面
    （见 check_bundle_deals），那是稳定可加载且辨识度更高的图。
    """
    if header_image:
        return header_image
    if int(item_kind or -1) == 1:
        return (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/"
            f"subs/{int(bundle_id)}/header_ratio.jpg"
        )
    return ""


def _bundle_url(url: str | None, bundle_id: int, item_kind: int, region: str) -> str:
    """包商店直链 + 该区 cc（形态与购买语义解耦：item_kind=1 是 /sub/）。"""
    kind = "sub" if int(item_kind or -1) == 1 else "bundle"
    base = str(url) if url else f"https://store.steampowered.com/{kind}/{int(bundle_id)}/"
    cc = (region or "").strip().lower()
    if not cc:
        return base
    return f"{base}{'&' if '?' in base else '?'}cc={cc}"


def _pick_region_row(prices: list[dict], owner_region: str) -> dict:
    """该区选择口径（与新史低速报同源）：号主区可买优先，否则折扣最深、价最低。"""
    owner_row = next(
        (p for p in prices if owner_region and p["region"] == owner_region.upper()), None
    )
    return owner_row or max(prices, key=lambda p: (p["discount"], -p["cny"]))


async def check_wishlist_deals() -> list[dict]:
    """监控池折扣速报：池内（愿望单 / 星标 / 手动入池）出现新折扣即通报。

    监控范围＝用户主动关注的条目；榜单发现入池（board_pool）是发现面、
    已拥有的条目是推荐无用面，两者都不参与。命中非空才发信，且只挑折扣
    最深的 DEAL_PICKS 款。

    去重是双重闸：同类速报 20h 上限（价格网格 6h 一轮 → 每天最多一封）＋
    逐款折扣游标（同一款只有折扣进一步加深才再次上榜）。游标随本轮候选
    收敛，折扣消失的条目会被剪掉——下次重新打折仍能通报。
    """
    if _hours_since_ts(await settings_service.get_value(KEY_WISHLIST_DEAL_AT, 0)) < DEAL_MIN_INTERVAL_HOURS:
        return []
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    WishlistItem.appid,
                    Game.name,
                    Game.header_image,
                    GameCurrentPrice.region_code,
                    GameCurrentPrice.currency,
                    GameCurrentPrice.price,
                    GameCurrentPrice.cny_fen,
                    GameCurrentPrice.discount_percent,
                )
                .join(Game, Game.appid == WishlistItem.appid)
                .join(GameCurrentPrice, GameCurrentPrice.appid == WishlistItem.appid)
                .where(
                    WishlistItem.active.is_(True),
                    WishlistItem.owned.is_(False),
                    or_(
                        WishlistItem.wishlisted.is_(True),
                        WishlistItem.manual.is_(True),
                        WishlistItem.manual_pool.is_(True),
                    ),
                    GameCurrentPrice.price_status == "ok",
                    GameCurrentPrice.discount_percent >= DEAL_MIN_DISCOUNT,
                    GameCurrentPrice.cny_fen.is_not(None),
                    GameCurrentPrice.cny_fen > 0,
                )
            )
        ).all()

        sent_before = await settings_service.get_value(KEY_WISHLIST_DEAL_SENT, {})
        sent = (
            {int(k): int(v) for k, v in sent_before.items()}
            if isinstance(sent_before, dict)
            else {}
        )
        owner_region = await _owner_region_code()

        grouped: dict[int, dict] = {}
        for appid, name, header, region, currency, price, cny, discount in rows:
            entry = grouped.setdefault(
                int(appid), {"name": name, "header": header, "prices": []}
            )
            entry["prices"].append({
                "region": (region or "").upper(),
                "currency": currency or "CNY",
                "price": int(price) if price is not None else None,
                "cny": int(cny) if cny is not None else 0,
                "discount": int(discount or 0),
            })

        picks: list[dict] = []
        for appid, entry in grouped.items():
            best_discount = max(p["discount"] for p in entry["prices"])
            if best_discount <= sent.get(appid, 0):
                continue  # 折扣没有进一步加深：已通报过
            chosen = _pick_region_row(entry["prices"], owner_region)
            cn_row = next((p for p in entry["prices"] if p["region"] == "CN"), None)
            picks.append({
                "appid": appid,
                "name": entry["name"],
                "header_image": entry["header"],
                "region_code": chosen["region"],
                "region_name": await region_display_name(chosen["region"]),
                "price_text": format_minor_units(chosen["price"], chosen["currency"]),
                "cn_price_text": format_cny_fen(cn_row["cny"]) if cn_row else "—",
                "discount": chosen["discount"],
                # 游标用「池内最深折扣」而非展示区折扣：展示区可能是号主区，
                # 用展示折扣记账会让同一条每 20h 反复上榜
                "max_discount": best_discount,
            })
        picks.sort(key=lambda p: (-p["max_discount"], p["appid"]))
        picks = picks[:DEAL_PICKS]
        new_sent = {str(a): sent[a] for a in grouped if a in sent}
        for p in picks:
            new_sent[str(p["appid"])] = p["max_discount"]

    if not picks:
        return []
    # 先记账再发信（发信失败不重试、不刷屏）
    await settings_service.set_value(KEY_WISHLIST_DEAL_SENT, new_sent)
    await settings_service.set_value(KEY_WISHLIST_DEAL_AT, time.time())
    sent_ok = await notify.send_mail(
        f"{APP_NAME} 监控池折扣速报：{len(picks)} 款新折扣",
        notify.wishlist_deal_mail_html(picks),
    )
    logger.info(
        "监控池折扣速报已发 %s：%d 款（池内折扣条目 %d）",
        "成功" if sent_ok else "失败（未配置 SMTP 或发送异常，本轮不重试）",
        len(picks), len(grouped),
    )
    return picks


async def check_bundle_deals() -> list[dict]:
    """捆绑包精选：全量刷包链尾调用，挑国区最低 / 高折扣的包通报。

    与监控池速报同款双重闸（20h 上限 + 逐包折扣游标）。低价垃圾包
    （<BUNDLE_MIN_CNY_FEN）与无 CNY 快照的包直接排除。
    """
    if _hours_since_ts(await settings_service.get_value(KEY_BUNDLE_DEAL_AT, 0)) < DEAL_MIN_INTERVAL_HOURS:
        return []
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    Bundle.bundle_id,
                    Bundle.name,
                    Bundle.header_image,
                    Bundle.item_kind,
                    Bundle.url,
                    BundleRegionPrice.region_code,
                    BundleRegionPrice.currency,
                    BundleRegionPrice.price,
                    BundleRegionPrice.cny_fen,
                    BundleRegionPrice.discount_percent,
                    BundleRegionPrice.app_ids,
                )
                .join(BundleRegionPrice, BundleRegionPrice.bundle_id == Bundle.bundle_id)
                .where(
                    BundleRegionPrice.price_status == "ok",
                    BundleRegionPrice.discount_percent >= DEAL_MIN_DISCOUNT,
                    BundleRegionPrice.cny_fen.is_not(None),
                    BundleRegionPrice.cny_fen >= BUNDLE_MIN_CNY_FEN,
                )
            )
        ).all()

        sent_before = await settings_service.get_value(KEY_BUNDLE_DEAL_SENT, {})
        sent = (
            {int(k): int(v) for k, v in sent_before.items()}
            if isinstance(sent_before, dict)
            else {}
        )
        owner_region = await _owner_region_code()

        grouped: dict[int, dict] = {}
        for bid, name, header, kind, url, region, currency, price, cny, discount, app_ids in rows:
            entry = grouped.setdefault(int(bid), {
                "name": name, "header": header, "kind": int(kind or -1),
                "url": url, "prices": [],
            })
            entry["prices"].append({
                "region": (region or "").upper(),
                "currency": currency or "CNY",
                "price": int(price) if price is not None else None,
                "cny": int(cny) if cny is not None else 0,
                "discount": int(discount or 0),
                "count": len(app_ids or []),
                # 封面兜底的取样点：包内第一款游戏（bundle 资产路径带哈希，
                # 拼不出可用的包封面，游戏封面才是稳定可加载的那张）
                "first_appid": int(app_ids[0]) if app_ids else None,
            })

        picks: list[dict] = []
        for bid, entry in grouped.items():
            best_discount = max(p["discount"] for p in entry["prices"])
            if best_discount <= sent.get(bid, 0):
                continue
            chosen = _pick_region_row(entry["prices"], owner_region)
            cn_row = next((p for p in entry["prices"] if p["region"] == "CN"), None)
            picks.append({
                "bundle_id": bid,
                "name": entry["name"],
                "header_image": _bundle_cover(entry["header"], bid, entry["kind"]),
                "url": _bundle_url(entry["url"], bid, entry["kind"], chosen["region"]),
                "region_code": chosen["region"],
                "region_name": await region_display_name(chosen["region"]),
                "price_text": format_minor_units(chosen["price"], chosen["currency"]),
                "cn_price_text": format_cny_fen(cn_row["cny"]) if cn_row else "—",
                "discount": chosen["discount"],
                "max_discount": best_discount,
                "first_appid": chosen["first_appid"],
                "condition": f"包内 {chosen['count']} 项 · 深度 {best_discount}%" if chosen["count"] else "",
            })
        picks.sort(key=lambda p: (-p["max_discount"], p["bundle_id"]))
        picks = picks[:BUNDLE_DEAL_PICKS]

        # 封面兜底只对入选的 ≤BUNDLE_DEAL_PICKS 个包做一次查询（不随候选集
        # 膨胀），包内首款游戏封面优先，查不到再拼 Steam header（真实 appid
        # 恒有图）
        need = sorted({p["first_appid"] for p in picks
                       if not p["header_image"] and p["first_appid"]})
        cover_map: dict[int, str] = {}
        if need:
            for appid, header in (
                await session.execute(
                    select(Game.appid, Game.header_image).where(Game.appid.in_(need))
                )
            ).all():
                cover_map[int(appid)] = header or build_steam_header_url(int(appid))
        for p in picks:
            if not p["header_image"]:
                appid = p.pop("first_appid", None)
                p["header_image"] = cover_map.get(appid) or (
                    build_steam_header_url(appid) if appid else ""
                )
            else:
                p.pop("first_appid", None)

        new_sent = {str(b): sent[b] for b in grouped if b in sent}
        for p in picks:
            new_sent[str(p["bundle_id"])] = p["max_discount"]

    if not picks:
        return []
    await settings_service.set_value(KEY_BUNDLE_DEAL_SENT, new_sent)
    await settings_service.set_value(KEY_BUNDLE_DEAL_AT, time.time())
    sent_ok = await notify.send_mail(
        f"{APP_NAME} 捆绑包精选：{len(picks)} 个新折扣",
        notify.bundle_deal_mail_html(picks),
    )
    logger.info(
        "捆绑包精选已发 %s：%d 个（池内折扣包 %d）",
        "成功" if sent_ok else "失败（未配置 SMTP 或发送异常，本轮不重试）",
        len(picks), len(grouped),
    )
    return picks


def _epic_key(offer: dict) -> str:
    """当期条目标识：appid 优先，无 appid 用标题小写（游乐条目常无 appid）。"""
    appid = offer.get("appid")
    if appid:
        return f"appid:{int(appid)}"
    return "title:" + str(offer.get("title") or "").strip().lower()


def _epic_item(offer: dict, *, upcoming: bool) -> dict:
    """Epic 条目 → 邮件卡片字段（封面走 Epic keyImage，缺失回落 Steam header）。"""
    appid = offer.get("appid")
    end, start = offer.get("end"), offer.get("start")
    worth = offer.get("priceOriginal")
    if upcoming:
        condition = f"{start} 起免费" if start else "即将免费"
    else:
        condition = f"免费领取至 {end}" if end else "限时免费"
    if worth:
        condition += f" · 原价 {worth}"
    return {
        "appid": int(appid) if appid else None,
        "name": offer.get("titleCn") or offer.get("title") or "",
        "header_image": offer.get("image") or "",
        "url": offer.get("url") or "",
        "price_text": "即将免费" if upcoming else "免费",
        "cn_price_text": "",
        "discount": 0,
        "condition": condition,
    }


async def check_epic_free() -> list[dict]:
    """Epic 喜加一：当期免费窗口出现新条目即通报（预告段只作展示不记账）。

    取数走展示链（`metadata.epic_free_offers`，含封面/商店页/原价），与
    标记链 `refresh_epic_free` 分离——邮件要的是展示字段，标记链只落
    is_epic/epic_date。记账只含当期条目：预告条目转正后才会自己发一封。
    """
    from app.domains.metadata import service as metadata_service

    payload = await metadata_service.epic_free_offers()
    if not payload.get("ok"):
        logger.info("Epic 喜加一邮件跳过：展示链本轮无数据")
        return []
    offers = [o for o in (payload.get("offers") or []) if o.get("title")]
    current = [o for o in offers if not o.get("upcoming")]
    upcoming = [o for o in offers if o.get("upcoming")]
    if not current:
        return []

    sent_before = await settings_service.get_value(KEY_EPIC_FREE_SENT, [])
    sent = {str(x) for x in sent_before} if isinstance(sent_before, list) else set()
    fresh = [o for o in current if _epic_key(o) not in sent]
    if not fresh:
        return []

    picks = [_epic_item(o, upcoming=False) for o in fresh[:EPIC_FREE_PICKS]]
    preview = [_epic_item(o, upcoming=True) for o in upcoming[:EPIC_FREE_PICKS]]
    await settings_service.set_value(
        KEY_EPIC_FREE_SENT, sorted(sent | {_epic_key(o) for o in current})
    )
    sent_ok = await notify.send_mail(
        f"{APP_NAME} Epic 喜加一：{len(picks)} 款限免",
        notify.epic_free_mail_html(picks, preview),
    )
    logger.info(
        "Epic 喜加一邮件已发 %s：当期新 %d 款（窗口 %d，预告 %d）",
        "成功" if sent_ok else "失败（未配置 SMTP 或发送异常，本轮不重试）",
        len(picks), len(current), len(upcoming),
    )
    return picks


async def check_hb_choice(result: dict) -> list[dict]:
    """Humble Choice 当月包：`refresh_hb_choice` 结案后调用（每天幂等）。

    只在「新月包 + 已记账」时发一封；标签游标保证同一个月不重复。
    marked 里的 appid 都已在 games 侧落行（占位行也算），补封面/中文名。
    """
    if not result or not result.get("recorded"):
        return []
    label = str(result.get("label") or result.get("machineName") or "")
    marked = result.get("marked") or []
    appids = [int(m["appid"]) for m in marked if m.get("appid")]
    if not label or not appids:
        return []
    if await settings_service.get_value(KEY_HB_CHOICE_SENT, "") == label:
        return []

    names = {int(m["appid"]): m.get("title") or "" for m in marked if m.get("appid")}
    headers: dict[int, str | None] = {}
    async with get_session_factory()() as session:
        for appid, name, header in (
            await session.execute(
                select(Game.appid, Game.name, Game.header_image).where(Game.appid.in_(appids))
            )
        ).all():
            headers[int(appid)] = header
            names[int(appid)] = name or names.get(int(appid), "")

    games = [
        {
            "appid": appid,
            "name": names.get(appid) or str(appid),
            "header_image": headers.get(appid),
            "condition": "Steam 可兑换",
        }
        for appid in appids
    ]
    await settings_service.set_value(KEY_HB_CHOICE_SENT, label)
    sent_ok = await notify.send_mail(
        f"{APP_NAME} Humble Choice 当月包：{label}",
        notify.hb_choice_mail_html(
            month_label=label,
            product_name=result.get("productName") or "",
            games=games,
        ),
    )
    logger.info(
        "Humble Choice 邮件已发 %s：%s（%d 款）",
        "成功" if sent_ok else "失败（未配置 SMTP 或发送异常，本轮不重试）",
        label, len(games),
    )
    return games


async def _subscription_digest(proxies_service) -> list[dict]:
    """订阅摘要（只取展示字段）：订阅 URL 是凭据，绝不进邮件正文。"""
    try:
        subs = await proxies_service.list_subscriptions()
    except Exception:  # noqa: BLE001 — 摘要失败不影响告警主体
        return []
    out = []
    for s in subs[:5]:
        stats = s.get("lastStats")
        traffic = stats.get("traffic") if isinstance(stats, dict) else None
        out.append({
            "id": s.get("id"),
            "label": s.get("label") or f"订阅 {s.get('id')}",
            "kind": s.get("kind"),
            "traffic": traffic if isinstance(traffic, dict) else None,
        })
    return out


async def check_proxy_health() -> bool:
    """代理通道 / Clash 订阅告警：全灭或可用占比过低时发一封（12h 冷却）。

    未配置任何通道（total=0）是用户选择而不是异常，静默跳过。
    """
    from app.domains.proxies import service as proxies_service

    try:
        stats = await proxies_service.pool_stats()
    except Exception:  # noqa: BLE001 — 统计失败无法判定，不误报
        logger.exception("代理池统计失败，跳过通道告警")
        return False
    total = int(stats.get("total") or 0)
    available = int(stats.get("available") or 0)
    if total <= 0:
        return False
    if available <= 0:
        reason = "全部代理通道不可用"
    elif available / total < PROXY_ALERT_RATIO:
        reason = f"可用通道不足 {int(PROXY_ALERT_RATIO * 100)}%（{available}/{total}）"
    else:
        return False
    if not await _alert_cooldown_ok("proxy"):
        return False

    clash = stats.get("clash") or {}
    pool = stats.get("pool") or {}
    snapshot = {
        "reason": reason,
        "available": available,
        "total": total,
        "pool_total": pool.get("total", 0),
        "pool_ok": pool.get("ok", 0),
        "clash_running": bool(clash.get("running")),
        "clash_nodes": clash.get("nodes", 0),
        "clash_ok_nodes": clash.get("okNodes", 0),
        "clash_exit_ips": clash.get("exitIps", 0),
        "clash_ok_exit_ips": clash.get("okExitIps", 0),
        "subscriptions": await _subscription_digest(proxies_service),
    }
    await _mark_alert("proxy")
    sent_ok = await notify.send_mail(
        f"{APP_NAME} 代理通道告警：{reason}",
        notify.proxy_health_mail_html(snapshot),
    )
    logger.info("代理通道告警已发 %s：%s", "成功" if sent_ok else "失败（未配置 SMTP）", reason)
    return sent_ok


async def _alert_cooldown_ok(kind: str) -> bool:
    store = await settings_service.get_value(KEY_SYS_ALERT_AT, {})
    last = store.get(kind) if isinstance(store, dict) else None
    cooldown = SYS_ALERT_COOLDOWN_HOURS.get(kind, SYS_ALERT_DEFAULT_COOLDOWN)
    return _hours_since_ts(last) >= cooldown


async def _mark_alert(kind: str) -> None:
    store = await settings_service.get_value(KEY_SYS_ALERT_AT, {})
    store = dict(store) if isinstance(store, dict) else {}
    store[kind] = time.time()
    if len(store) > 20:  # 只留最近 20 类，防 KV 无界增长
        for key, _ in sorted(store.items(), key=lambda kv: kv[1])[: len(store) - 20]:
            store.pop(key, None)
    await settings_service.set_value(KEY_SYS_ALERT_AT, store)


async def send_system_alert(
    *,
    kind: str,
    title: str,
    summary: str,
    rows: list[tuple[str, str]] | None = None,
    level: str = "warning",
    hint: str = "",
) -> bool:
    """系统异常告警（备份失败 / 价格网格连续失败…）：按 kind 冷却后发一封。"""
    if not await _alert_cooldown_ok(kind):
        return False
    await _mark_alert(kind)
    sent_ok = await notify.send_mail(
        f"{APP_NAME} 系统告警：{title}",
        notify.system_alert_mail_html(
            title=title, summary=summary, rows=rows or [], level=level, hint=hint
        ),
    )
    logger.info("系统告警已发 %s：%s", "成功" if sent_ok else "失败（未配置 SMTP）", title)
    return sent_ok


async def delete_event(event_id: int) -> bool:
    """删除单条触发历史。"""
    async with get_session_factory()() as session:
        event = await session.get(AlertEvent, event_id)
        if event is None:
            return False
        await session.delete(event)
        await session.commit()
        return True


async def clear_events() -> int:
    """清空全部触发历史，返回删除条数。"""
    async with get_session_factory()() as session:
        result = await session.execute(AlertEvent.__table__.delete())
        await session.commit()
        return int(result.rowcount or 0)


async def list_events(limit: int = 50) -> list[dict]:
    """列出触发历史，附带游戏中文名、封面与本币价文本。

    priceCny 取触发时刻落库的人民币分快照；迁移前的旧事件无快照，
    前端回退展示本币价文本（priceText）。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
        appids = {int(e.appid) for e in rows}
        games: dict[int, tuple[str, str | None]] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name, Game.header_image).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): (g[1], g[2]) for g in game_rows}
    return [
        {
            "id": e.id,
            "alertId": e.alert_id,
            "appid": int(e.appid),
            "gameName": games.get(int(e.appid), ("", None))[0],
            "gameHeader": games.get(int(e.appid), ("", None))[1]
            or build_steam_header_url(int(e.appid)),
            "region": e.region,
            "price": int(e.price) if e.price else None,
            "priceCny": int(e.price_cny) if e.price_cny else None,
            "priceText": (
                format_minor_units(int(e.price), REGION_TO_CURRENCY.get(e.region, "CNY"))
                if e.price
                else ""
            ),
            "triggeredAt": e.triggered_at.isoformat() if e.triggered_at else None,
            "notified": e.notified,
        }
        for e in rows
    ]


async def search_games(q: str, region: str = "CN") -> dict:
    """搜索游戏：优先本地数据库（中文名/英文名/appid），本地无结果且输入为纯数字 appid 时
    尝试在线查询 Steam appdetails 验证该游戏存在且为非免费游戏。

    返回 { items: [{appid, name, nameEn, isFree}] }
    """
    q = q.strip()
    if not q:
        return {"items": []}

    region_cc = region.lower()
    # 1. 本地搜索（中文名 LIKE / 英文名 LIKE / appid 精确）
    async with get_session_factory()() as session:
        # 纯数字 → 也按 appid 精确匹配
        is_numeric = q.isdigit()
        if is_numeric:
            appid_val = int(q)
            rows = (
                await session.execute(
                    select(Game).where(or_(
                        Game.name.like(f"%{q}%"),
                        Game.name_en.like(f"%{q}%"),
                        Game.appid == appid_val,
                    )).limit(20)
                )
            ).scalars().all()
        else:
            rows = (
                await session.execute(
                    select(Game).where(or_(
                        Game.name.like(f"%{q}%"),
                        Game.name_en.like(f"%{q}%"),
                    )).limit(20)
                )
            ).scalars().all()

    if rows:
        return {
            "items": [
                {
                    "appid": int(g.appid),
                    "name": g.name or "",
                    "nameEn": g.name_en or "",
                    "isFree": False,  # 本地库存的游戏默认非免费
                }
                for g in rows
            ]
        }

    # 2. 本地无结果，如果输入不是纯数字 appid → 返回无效
    if not is_numeric:
        return {"items": [], "error": "输入不是有效的 AppID，且本地无匹配游戏记录"}

    # 3. 输入是 appid → 在线查询 Steam appdetails 验证（代理优先：Steam 域直连基本不通）
    appid_val = int(q)
    try:
        from app.domains.proxies import service as proxies_service

        try:
            proxy_url = await proxies_service.resolve_proxy_url()
        except Exception:  # noqa: BLE001
            proxy_url = None
        async with httpx.AsyncClient(timeout=15, proxy=proxy_url) as client:
            resp = await client.get(
                APPDETAILS_URL,
                params={"appids": appid_val, "cc": region_cc, "l": "schinese"},
            )
        data = resp.json()
        app_data = data.get(str(appid_val), {}).get("data", {})
        if not app_data:
            return {"items": [], "error": f"AppID {appid_val} 在 Steam 不存在或已下架"}
        is_free = app_data.get("is_free", False)
        name = app_data.get("name", "")
        if is_free:
            return {"items": [], "error": f"《{name}》是免费游戏，无需价格提醒"}
        return {
            "items": [{
                "appid": appid_val,
                "name": name,
                "nameEn": "",
                "isFree": False,
            }]
        }
    except Exception as e:
        logger.warning("在线查询 AppID %s 失败: %s", appid_val, e)
        return {"items": [], "error": f"在线查询失败: {e}"}


# ─── SMTP 邮件设置（转发到 notify 模块）───

async def get_smtp_config() -> dict:
    """返回 SMTP 配置（密码掩码），供路由层调用。"""
    return await notify.get_smtp_config()


async def update_smtp_config(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    to_addr: str,
    use_ssl: bool,
) -> dict:
    """更新 SMTP 配置；password 为空则保持原密码不变。"""
    return await notify.update_smtp_config(
        host=host,
        port=port,
        user=user,
        password=password,
        to_addr=to_addr,
        use_ssl=use_ssl,
    )


async def test_smtp_config(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    to_addr: str,
    use_ssl: bool,
) -> dict:
    """发送连通性测试邮件（不落库，按表单当前值试连）。"""
    return await notify.send_test_mail(
        host=host,
        port=port,
        user=user,
        password=password,
        to_addr=to_addr,
        use_ssl=use_ssl,
    )
