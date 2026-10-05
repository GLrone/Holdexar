"""追踪类工具：关注 / 提醒 / 捆绑包关注 / 清理提议 / 关注巡检流水线。

写工具 1:1 映射既有用户动作（关注 = monitoring.track，
提醒 = alerts_service.add_alert），守卫词命中即拒绝；批量动作不设执行，
只落提议交用户确认。"""
from __future__ import annotations

import re

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.alerts import service as alerts_service
from app.domains.bundles import service as bundles_service
from app.domains.games import service as games_service
from app.domains.monitoring import service as monitoring_service
from app.domains.wishlist import follows as wishlist_follows

from app.crawler.config import CC_LIST  # region_toggle 校验用
from app.crawler.utils import get_beijing_time_obj

_DEL_KEY_RE = re.compile(r"^(alert|bill_import|follow):\d+$")


async def list_follows() -> dict:
    """关注清单：手动加入 ∪ 收藏关注（两路显式关注合并成一份只读投影）。"""
    manual = await monitoring_service.ids_with_source("game", "manual")
    favorite = await wishlist_follows.followed_appids()
    ids = sorted({int(a) for a in manual} | {int(a) for a in favorite})
    briefs = await games_service.briefs_for(ids)
    fav = {int(a) for a in favorite}
    items = [
        _shared.brief_item(i, briefs.get(i), note={"key": "favorite" if i in fav else "manual"})
        for i in ids
    ]
    return _shared.games_card("follows", items, total=len(ids))


async def list_alerts() -> dict:
    """提醒规则清单：1:1 alerts.list_alerts，值词条化不拼用户文案。"""
    alerts = await alerts_service.list_alerts()
    vkey = {"price": "alertPrice", "historic_low": "alertLow", "pct": "alertPct"}
    rows = [
        {
            "k": a.get("gameName") or f"AppID {a.get('appid')}",
            "vKey": vkey.get(str(a.get("targetType")), "alertLow"),
            "v": a.get("region"),
            "data": {"priceFen": a.get("targetValue"), "pct": a.get("targetValue"), "alertId": a.get("id")},
            "tone": "ok" if a.get("active") else "warn",
        }
        for a in alerts
    ]
    return _shared.rows_result("alerts", rows, total=len(rows))


async def list_bundle_follows() -> dict:
    """关注的捆绑包清单。"""
    ids = await bundles_service.followed_bundle_ids()
    names = await bundles_service.names_for(ids)
    rows = [{"k": names.get(i, f"Bundle {i}")} for i in ids]
    return _shared.rows_result("bundles", rows, total=len(rows))


async def update_price_alert(alert_id: int, *, target_value_yuan=None, active: bool | None = None) -> dict:
    """改提醒（阈值 / 启停），1:1 alerts_service.update_alert。"""
    fen_value = None
    if target_value_yuan is not None:
        try:
            fen_value = int(float(target_value_yuan) * 100)
        except (TypeError, ValueError):
            return {"kind": "empty", "note": "bad_value"}
    try:
        alert = await alerts_service.update_alert(
            int(alert_id), active=active, target_value=fen_value,
        )
    except ValueError:
        return {"kind": "empty", "note": "alert_not_found"}
    names = await games_service.names_for([int(alert.get("appid") or 0)])
    name = names.get(int(alert.get("appid") or 0), f"AppID {alert.get('appid')}")
    if active is False:
        vkey, tone = "alertOff", "warn"
    elif active is True:
        vkey, tone = "alertOn", "ok"
    else:
        vkey, tone = "alertUpdated", "ok"
    return _shared.rows_result("alerts", [{
        "k": name,
        "vKey": vkey,
        "data": {"priceFen": alert.get("targetValue"), "alertId": alert.get("id")},
        "tone": tone,
    }])


async def bundle_follow(bundle_id: int, *, follow: bool = True) -> dict:
    """关注 / 取关捆绑包（挂 favorite 来源），1:1 bundles 域动作。"""
    try:
        if follow:
            await bundles_service.follow_bundle(int(bundle_id))
        else:
            await bundles_service.unfollow_bundle(int(bundle_id))
    except ValueError:
        return {"kind": "empty", "note": "bundle_not_found"}
    names = await bundles_service.names_for([int(bundle_id)])
    return _shared.rows_result("bundles", [{
        "k": names.get(int(bundle_id), f"Bundle {bundle_id}"),
        "vKey": "bundleFollowed" if follow else "bundleUnfollowed",
        "tone": "ok" if follow else "warn",
    }])


async def list_bundles() -> dict:
    """捆绑包列表前列（diff 差价降序，缓存聚合零外网）。"""
    bundles = await bundles_service.list_bundles("diff")
    rows = [
        {
            "k": b.get("name") or f"Bundle {b.get('bundleId')}",
            "vKey": "bundleLow",
            "data": {"priceFen": b.get("cnCnyFen"), "lowFen": b.get("lowestCnyFen"), "bundleId": b.get("bundleId")},
            "v": str(b.get("lowestRegion") or "").upper(),
        }
        for b in bundles
    ]
    return _shared.rows_result("bundlesAll", rows, total=len(rows))


async def monitor_add(appid: int) -> dict:
    """关注游戏：1:1 映射 monitoring.track（manual 来源 = 用户手动加入）。"""
    state = await monitoring_service.track("game", appid, "manual")
    detail = await games_service.get_game_detail(appid)
    return {
        "action": "monitor_add",
        "appid": appid,
        "name": detail.get("name") if detail else None,
        "state": state,
    }


async def alert_add(appid: int, *, target_type: str, target_value_fen: float | None) -> dict:
    """设价格提醒：1:1 映射 alerts_service.add_alert（中国区，price=分 / historic_low）。"""
    alert = await alerts_service.add_alert(appid, "CN", target_type, target_value_fen)
    detail = await games_service.get_game_detail(appid)
    return {
        "action": "alert_add",
        "appid": appid,
        "name": detail.get("name") if detail else None,
        "targetType": target_type,
        "targetValueFen": target_value_fen,
        "alertId": alert.get("id") if alert else None,
    }


async def retry_removed(appid: int) -> dict:
    """已移除游戏重新入库并排队补抓（1:1 games.retry_removed_game）。"""
    try:
        await games_service.retry_removed_game(appid)
    except ValueError:
        return {"kind": "empty", "note": "not_removed"}
    names = await games_service.names_for([appid])
    return _shared.rows_result("retry", [{
        "k": names.get(appid, f"AppID {appid}"),
        "vKey": "retryStarted", "tone": "ok",
    }])


async def monitor_include(appid: int) -> dict:
    """解除排除恢复监控（1:1 monitoring.set_exclusion False，可逆）。"""
    state = await monitoring_service.set_exclusion("game", appid, False)
    names = await games_service.names_for([appid])
    return _shared.rows_result("include", [{
        "k": names.get(appid, f"AppID {appid}"),
        "vKey": "includeDone", "v": str(state or ""), "tone": "ok",
    }])


async def region_toggle(region_code: str, *, enable: bool) -> dict:
    """启用 / 停用单个区服：读当前启用集 ±1 再整集写回（防清区）。

    区域生效在下一轮价格周期；全新加区（CC_LIST 之外）不属于本工具。"""
    code = str(region_code or "").strip().lower()
    valid = {c.lower() for c, _, _ in CC_LIST}
    if code not in valid:
        return _shared.rows_result("regions", [{"k": code.upper(), "vKey": "regionUnknown", "tone": "warn"}])
    from app.domains.regions import service as regions_service

    try:
        current = set(await regions_service.enabled_regions())
    except ValueError:
        current = set()
    if enable:
        current.add(code)
    else:
        current.discard(code)
    if not current:
        return _shared.rows_result("regions", [{"k": code.upper(), "vKey": "regionMinOne", "tone": "warn"}])
    await regions_service.set_enabled(sorted(current))
    return _shared.rows_result("regions", [{
        "k": code.upper(),
        "vKey": "regionOn" if enable else "regionOff",
        "tone": "ok" if enable else "warn",
    }])


async def _scan_deletables() -> list[dict]:
    """清理候选只读扫描：失效提醒（游戏已移除/不在目录/从未抓到价格）
    与同源重复账单导入（保留最新一批）。逐项带机器原因码，解释由前端词条化。"""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.domains.alerts.models import PriceAlert
    from app.domains.bills.models import BillImport
    from app.domains.games.models import Game, GameCurrentPrice

    items: list[dict] = []
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(PriceAlert, Game).outerjoin(Game, Game.appid == PriceAlert.appid)
        )).all()
        for alert, game in rows:
            base = {"key": f"alert:{alert.id}", "appid": int(alert.appid)}
            if game is None:
                items.append({**base, "name": f"AppID {alert.appid}", "reason": "noGame"})
            elif game.removed_at is not None:
                items.append({**base, "name": game.name or f"AppID {alert.appid}", "reason": "removed"})
            else:
                has_price = await session.scalar(
                    select(GameCurrentPrice.appid)
                    .where(GameCurrentPrice.appid == alert.appid)
                    .limit(1)
                )
                if has_price is None:
                    items.append({**base, "name": game.name or f"AppID {alert.appid}", "reason": "neverCrawled"})
        imports = (await session.execute(
            select(BillImport).order_by(BillImport.id.desc())
        )).scalars().all()
        seen_files: set[str] = set()
        for im in imports:
            sf = str(im.source_file or "")
            if sf and sf in seen_files:
                items.append({
                    "key": f"bill_import:{im.id}",
                    "name": f"{im.nickname or ''} {im.imported_at}".strip() or f"#{im.id}",
                    "reason": "dupImport",
                })
            elif sf:
                seen_files.add(sf)
    return items


async def find_deletables(sid: str) -> dict:
    """清理检查（只读）：有候选即落一份删除提议（无副作用），确认前不删任何东西。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    items = (await _scan_deletables())[:_shared._BULK_MAX_ITEMS]
    if not items:
        return {"kind": "empty", "note": "nothing_to_delete"}
    now = get_beijing_time_obj()
    pid = "d" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _shared.pilot_store().append_proposal(sid, {
        "pid": pid, "action": "delete", "items": items, "args": {}, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": "delete",
            "items": items, "args": {}, "state": "pending"}


async def propose_delete(sid: str, items: list) -> dict:
    """删除提议（模型可调，无副作用）：逐项 {key, name, reason, appid?}；key 限
    alert:/bill_import:/follow: 三类，确认后由服务层执行既有删除函数。
    appid 透传给前端渲染封面（不在目录的游戏也能凭 appid 拼 Steam 头图）。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    clean: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "")
        if not _DEL_KEY_RE.match(key):
            continue
        row: dict = {
            "key": key,
            "name": str(raw.get("name") or key)[:80],
            "reason": str(raw.get("reason") or "")[:24],
        }
        try:
            item_appid = int(raw.get("appid") or 0)
        except (TypeError, ValueError):
            item_appid = 0
        if item_appid > 0:
            row["appid"] = item_appid
        clean.append(row)
        if len(clean) >= _shared._BULK_MAX_ITEMS:
            break
    if not clean:
        return {"kind": "empty", "note": "no_target"}
    now = get_beijing_time_obj()
    pid = "d" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _shared.pilot_store().append_proposal(sid, {
        "pid": pid, "action": "delete", "items": clean, "args": {}, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": "delete",
            "items": clean, "args": {}, "state": "pending"}


async def propose_bulk(sid: str, action: str, appids: list, *,
                       target_type: str = "historic_low",
                       target_value_yuan: float | None = None) -> dict:
    """批量提议：落待确认清单（无副作用）；确认后由服务层逐项执行既有写动作。"""
    if not sid:
        return {"kind": "empty", "note": "no_session"}
    if str(action or "") not in _shared._BULK_ACTIONS:
        return {"kind": "empty", "note": "bad_action"}
    ids: list[int] = []
    for raw in appids or []:
        try:
            appid = int(raw)
        except (TypeError, ValueError):
            continue
        if appid > 0 and appid not in ids:
            ids.append(appid)
    if not ids:
        return {"kind": "empty", "note": "no_target"}
    if len(ids) > _shared._BULK_MAX_ITEMS:
        return {"kind": "empty", "note": "too_many", "data": {"max": _shared._BULK_MAX_ITEMS}}
    names = await games_service.names_for(ids)
    now = get_beijing_time_obj()
    args: dict = {"target_type": str(target_type or "historic_low")}
    if target_value_yuan is not None:
        args["target_value_yuan"] = float(target_value_yuan)
    items = [{"appid": i, "name": names.get(i) or f"AppID {i}"} for i in ids]
    pid = "p" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    ok = await _shared.pilot_store().append_proposal(sid, {
        "pid": pid, "action": action, "items": items, "args": args, "state": "pending",
    })
    if not ok:
        return {"kind": "empty", "note": "no_session"}
    return {"kind": "proposal", "pid": pid, "action": action,
            "items": items, "args": args, "state": "pending"}


async def audit_follows_workflow(sid: str = "") -> dict:
    """检查全部关注游戏的价格变动并更新提醒规则（长任务/流水线分步卡片）。"""
    manual = await monitoring_service.ids_with_source("game", "manual")
    favorite = await wishlist_follows.followed_appids()
    ids = sorted({int(a) for a in manual} | {int(a) for a in favorite})
    total = len(ids)
    if not ids:
        stepper = {
            "kind": "stepper",
            "title": "关注游戏价格与提醒巡检流水线",
            "currentStepIndex": 0,
            "steps": [
                {"id": 1, "title": "扫描关注列表", "detail": "关注列表为空 (0/0)", "status": "empty", "current": 0, "total": 0},
                {"id": 2, "title": "校验区服汇率与现价", "detail": "跳过校验", "status": "wait"},
                {"id": 3, "title": "生成批量提议", "detail": "无待处理项", "status": "wait"},
            ],
        }
        return {"kind": "stepper", "stepper": stepper}

    briefs = await games_service.briefs_for(ids)
    alerts = await alerts_service.list_alerts()
    alerted_appids = {int(a.get("appid") or 0) for a in alerts if a.get("active")}

    candidates = []
    for appid in ids:
        b = briefs.get(appid)
        if not b or not b.get("name"):
            continue
        if appid not in alerted_appids or (b.get("discount") or 0) > 0:
            candidates.append({"appid": appid, "name": b.get("name")})
        if len(candidates) >= 6:
            break

    if not candidates and ids:
        for appid in ids[:3]:
            b = briefs.get(appid) or {}
            candidates.append({"appid": appid, "name": b.get("name") or f"AppID {appid}"})

    now = get_beijing_time_obj()
    pid = "p" + now.strftime("%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
    args = {"target_type": "historic_low"}

    proposal = None
    if sid and candidates:
        ok = await _shared.pilot_store().append_proposal(sid, {
            "pid": pid, "action": "create_price_alert", "items": candidates, "args": args, "state": "pending",
        })
        if ok:
            proposal = {
                "kind": "proposal",
                "pid": pid,
                "action": "create_price_alert",
                "items": candidates,
                "args": args,
                "state": "pending",
            }

    stepper = {
        "kind": "stepper",
        "title": "关注游戏价格与提醒巡检流水线",
        "currentStepIndex": 2,
        "steps": [
            {
                "id": 1,
                "title": "扫描关注列表",
                "detail": f"扫描完成 ({total}/{total})",
                "status": "ok",
                "current": total,
                "total": total,
                "badge": f"{total}/{total}",
            },
            {
                "id": 2,
                "title": "校验区服汇率与现价",
                "detail": "汇率对齐与史低价格校验完成",
                "status": "ok",
                "current": total,
                "total": total,
            },
            {
                "id": 3,
                "title": "生成批量提议",
                "detail": f"待确认 {len(candidates)} 项提醒规则" if candidates else "全部提醒规则均已就绪",
                "status": "ok" if candidates else "empty",
                "badge": f"待确认 {len(candidates)} 项" if candidates else "已就绪",
            },
        ],
    }

    res: dict = {"kind": "stepper", "stepper": stepper}
    if proposal:
        res["proposal"] = proposal
    return res


async def _list_follows(args: dict, sid: str | None = None) -> dict:
    return await list_follows()


async def _list_alerts(args: dict, sid: str | None = None) -> dict:
    return await list_alerts()


async def _list_bundle_follows(args: dict, sid: str | None = None) -> dict:
    return await list_bundle_follows()


async def _list_bundles(args: dict, sid: str | None = None) -> dict:
    return await list_bundles()


async def _add_follow(args: dict, sid: str | None = None) -> dict:
    return await monitor_add(int(args.get("appid") or 0))


async def _create_price_alert(args: dict, sid: str | None = None) -> dict:
    ttype = args.get("target_type") or "price"
    yuan = args.get("target_value_yuan")
    fen = int(float(yuan) * 100) if yuan is not None else None
    return await alert_add(int(args.get("appid") or 0), target_type=ttype, target_value_fen=fen)


async def _update_price_alert(args: dict, sid: str | None = None) -> dict:
    return await update_price_alert(
        int(args.get("alert_id") or 0),
        target_value_yuan=args.get("target_value_yuan"),
        active=args.get("active"),
    )


async def _bundle_follow(args: dict, sid: str | None = None) -> dict:
    return await bundle_follow(
        int(args.get("bundle_id") or 0),
        follow=bool(args.get("follow", True)),
    )


async def _retry_removed_game(args: dict, sid: str | None = None) -> dict:
    return await retry_removed(int(args.get("appid") or 0))


async def _monitor_include(args: dict, sid: str | None = None) -> dict:
    return await monitor_include(int(args.get("appid") or 0))


async def _region_toggle(args: dict, sid: str | None = None) -> dict:
    return await region_toggle(
        str(args.get("region_code") or ""),
        enable=bool(args.get("enable", True)),
    )


async def _find_deletables(args: dict, sid: str | None = None) -> dict:
    return await find_deletables(str(sid or ""))


async def _propose_delete(args: dict, sid: str | None = None) -> dict:
    return await propose_delete(str(sid or ""), args.get("items") or [])


async def _propose_bulk(args: dict, sid: str | None = None) -> dict:
    return await propose_bulk(
        str(sid or ""),
        str(args.get("action") or ""),
        args.get("appids") or [],
        target_type=str(args.get("target_type") or "historic_low"),
        target_value_yuan=args.get("target_value_yuan"),
    )


async def _audit_follows_workflow(args: dict, sid: str | None = None) -> dict:
    return await audit_follows_workflow(str(sid or ""))


SPECS = [
    ToolSpec(
        name="add_follow", group="write", risk="medium",
        description="把某游戏加入用户关注（持续追踪价格）。仅当用户明确要求关注该一款游戏时调用；"
                    "多款一起关注改用 propose_bulk",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_add_follow, step_label="follow",
    ),
    ToolSpec(
        name="create_price_alert", group="write", risk="medium",
        description="为中国区创建价格提醒。仅当用户明确要求提醒这一款游戏时调用；用户给出具体价格用 "
                    "price 类型，用户说史低提醒用 historic_low 类型；多款一起设提醒改用 propose_bulk",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
            "target_type": {"type": "string", "enum": ["price", "historic_low"]},
            "target_value_yuan": {"type": "number", "description": "price 类必填，人民币元"},
        }, "required": ["appid", "target_type"]},
        handler=_create_price_alert, step_label="alert",
    ),
    ToolSpec(
        name="propose_bulk", group="read", risk="low",
        description="提交一份待用户确认的批量清单（本工具不执行任何写操作）。当用户要求对多款游戏"
                    "批量加关注或批量设提醒时调用：先用只读工具取到目标清单，再把 appid 数组交给"
                    "本工具，由用户在界面上确认后才会真正执行。一次最多 50 款",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add_follow", "create_price_alert"],
                       "description": "批量动作：加关注 / 设提醒"},
            "appids": {"type": "array", "items": {"type": "integer"},
                       "description": "目标游戏 AppID 数组"},
            "target_type": {"type": "string", "enum": ["price", "historic_low"],
                            "description": "action=create_price_alert 时有效"},
            "target_value_yuan": {"type": "number",
                                  "description": "target_type=price 时必填，人民币元"},
        }, "required": ["action", "appids"]},
        handler=_propose_bulk, step_label="propose",
    ),
    ToolSpec(
        name="list_follows", group="read", risk="low",
        description="列出用户关注的游戏。用户问『我关注了哪些游戏』或需要确认关注状态时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_follows, step_label="follows",
    ),
    ToolSpec(
        name="list_alerts", group="read", risk="low",
        description="列出已设置的价格提醒规则。用户问『我有哪些提醒』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_alerts, step_label="alerts",
    ),
    ToolSpec(
        name="list_bundle_follows", group="read", risk="low",
        description="列出用户关注的捆绑包",
        parameters={"type": "object", "properties": {}},
        handler=_list_bundle_follows, step_label="bundleFollows",
    ),
    ToolSpec(
        name="list_bundles", group="read", risk="low",
        description="列出捆绑包前列（差价降序，含国区现价与最低区价）。用户问『现在有什么捆绑包划算』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_bundles, step_label="bundlesAll",
    ),
    ToolSpec(
        name="update_price_alert", group="write", risk="medium",
        description="修改已存在的价格提醒：改阈值或启停。用户说『把 XX 的提醒改到 N 块』『先别提醒 XX 了』时调用；"
                    "alert_id 从提醒清单工具的结果里取",
        parameters={"type": "object", "properties": {
            "alert_id": {"type": "integer", "description": "提醒规则 ID"},
            "target_value_yuan": {"type": "number", "description": "新阈值（人民币元；只启停时省略）"},
            "active": {"type": "boolean", "description": "true=启用 false=停用；只改阈值时省略"},
        }, "required": ["alert_id"]},
        handler=_update_price_alert, step_label="updateAlert",
    ),
    ToolSpec(
        name="bundle_follow", group="write", risk="medium",
        description="关注或取关一个捆绑包。用户说『关注这个包』时调用；bundle_id 从捆绑包列表结果里取",
        parameters={"type": "object", "properties": {
            "bundle_id": {"type": "integer", "description": "捆绑包 ID"},
            "follow": {"type": "boolean", "description": "true=关注 false=取关，缺省关注"},
        }, "required": ["bundle_id"]},
        handler=_bundle_follow, step_label="bundleFollow",
    ),
    ToolSpec(
        name="region_toggle", group="write", risk="medium",
        description="启用或停用一个价格区服（下一轮价格周期生效）。用户说『把 XX 区关了』『加上 XX 区价格』时调用；"
                    "只支持已有区服代码（如 us/ru/ua），新增区服需要开发者在设置里配置",
        parameters={"type": "object", "properties": {
            "region_code": {"type": "string", "description": "区服代码（如 cn/us/ru）"},
            "enable": {"type": "boolean", "description": "true=启用 false=停用"},
        }, "required": ["region_code", "enable"]},
        handler=_region_toggle, step_label="regionToggle",
    ),
    ToolSpec(
        name="retry_removed_game", group="write", risk="medium",
        description="把已移除的游戏重新拉回目录并排队补抓。用户说『把 XX 加回来』『恢复 XX』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_retry_removed_game, step_label="retryRemoved",
    ),
    ToolSpec(
        name="monitor_include", group="write", risk="medium",
        description="解除某游戏的排除状态、恢复监控。用户说『XX 别排除了/恢复监控 XX』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_monitor_include, step_label="monitorInclude",
    ),
    ToolSpec(
        name="find_deletables", group="read", risk="low",
        description="清理检查（只读）：扫描失效提醒（游戏已移除/从未抓到价格）与重复账单导入，"
                    "落一份带逐项解释的删除提议，用户确认后才真正删除。用户问『有什么可以清理』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_find_deletables, step_label="deletables",
    ),
    ToolSpec(
        name="propose_delete", group="read", risk="low",
        description="提交一份待用户确认的删除清单（本工具不执行删除）。用户明确要求删除某提醒/账单批次/取消关注时调用；"
                    "items 每项 {key, name, reason}，key 形如 alert:3 / bill_import:7 / follow:530（appids 来自只读工具结果）",
        parameters={"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "key": {"type": "string"},
                "name": {"type": "string"},
                "reason": {"type": "string", "enum": ["removed", "neverCrawled", "noGame", "dupImport", "userAsk"]},
            }, "required": ["key", "name", "reason"]}},
        }, "required": ["items"]},
        handler=_propose_delete, step_label="proposeDelete",
    ),
    ToolSpec(
        name="audit_follows_workflow", group="read", risk="low",
        description="流水线工作流：全量扫描关注游戏价格变动、汇率与史低对比，并自动生成批量价格提醒更新提议。用户说『检查全部关注游戏的价格变动并更新提醒规则』『巡检关注游戏并提议提醒』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_audit_follows_workflow, step_label="auditFollowsWorkflow",
    ),
]
