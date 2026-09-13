"""alerts 域服务：规则 CRUD + 爬取后的触发检查 + 邮件通知 + 游戏搜索。"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlalchemy import func, or_, select

from app.core.app_info import APP_NAME
from app.core.database import get_session_factory
from app.crawler.config import APPDETAILS_URL
from app.crawler.utils import get_beijing_time_obj
from app.domains.account import service as account_service
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory
from app.domains.games.pricing import format_cny_fen, format_minor_units
from app.domains.regions import service as regions_service
from app.domains.settings import service as settings_service
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


def _alert_dict(a: PriceAlert) -> dict:
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
    """列出全部提醒规则，附带游戏中文名。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(PriceAlert).order_by(PriceAlert.id.desc()))
        ).scalars().all()
        # 批量取游戏名
        appids = {int(a.appid) for a in rows}
        games: dict[int, str] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): g[1] for g in game_rows}
    result = []
    for a in rows:
        d = _alert_dict(a)
        d["gameName"] = games.get(int(a.appid), "")
        result.append(d)
    return result


async def add_alert(
    appid: int, region: str, target_type: str, target_value: float | None
) -> dict:
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
    """更新提醒规则（支持编辑条件类型、目标值、区服、启停）。"""
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

    - price:        该区当前价 <= target_value
    - pct:          该区折扣率 >= target_value
    - historic_low: 该区当前价 <= 历史最低价（创新低）
    触发即写 AlertEvent 并置 last_triggered_at；合并成一封邮件通知。
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
                hit = int(current.price) <= int(alert.target_value)
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
            session.add(
                AlertEvent(
                    alert_id=alert.id,
                    appid=alert.appid,
                    region=alert.region,
                    price=int(current.price) if current.price else None,
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
                    "price": int(current.price) if current.price else None,
                    "discount": current.discount_percent or 0,
                    "alertType": alert.target_type,
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
                select(Game.appid, Game.name).where(
                    Game.appid.in_(appids), Game.hl_flag == 1
                )
            )
        ).all()
        if not fresh:
            return []

        sent_before = await settings_service.get_value(KEY_NEW_LOW_SENT, [])
        sent_set = {int(a) for a in sent_before} if isinstance(sent_before, list) else set()
        news = [(int(a), n) for a, n in fresh if int(a) not in sent_set]
        if not news:
            return []

        # 号主所在地区：主账号钱包快照 region_code（小写；钱包从未同步过则未知）
        owner_region = ""
        try:
            primary = await account_service.get_primary_account()
            wallet = primary.wallet_json if primary and isinstance(primary.wallet_json, dict) else None
            owner_region = (wallet or {}).get("region_code") or ""
        except Exception:  # noqa: BLE001 — 账号域异常不阻断新史低检查
            logger.warning("读取主账号地区失败，新史低按无号主区兜底")

        # 每款附最低价行（全区含国区，口径对齐仪表盘跑马灯）：号主区命中的
        # 直接用号主区价，否则取全区最低
        rows: list[tuple[int, str, str, str, int, bool]] = []  # appid, name, region_code, price_text, diff, owner_hit
        for appid, _name in news:
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


async def list_events(limit: int = 50) -> list[dict]:
    """列出触发历史，附带游戏中文名。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
        appids = {int(e.appid) for e in rows}
        games: dict[int, str] = {}
        if appids:
            game_rows = (
                await session.execute(
                    select(Game.appid, Game.name).where(Game.appid.in_(appids))
                )
            ).all()
            games = {int(g[0]): g[1] for g in game_rows}
    return [
        {
            "id": e.id,
            "alertId": e.alert_id,
            "appid": int(e.appid),
            "gameName": games.get(int(e.appid), ""),
            "region": e.region,
            "price": int(e.price) if e.price else None,
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
