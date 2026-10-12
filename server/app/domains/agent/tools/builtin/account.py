"""账户面工具：账号 / 愿望单 / 拥有库 / 成就 / 账单 / 家庭 / 额度 + 同步刷新。"""
from __future__ import annotations

from app.domains.account import service as account_service
from app.domains.achievements import challenges as achievements_challenges
from app.domains.achievements import level as achievements_level
from app.domains.achievements import service as achievements_service
from app.domains.achievements import social as achievements_social
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.bills import service as bills_service
from app.domains.family import service as family_service
from app.domains.games import service as games_service
from app.domains.rates import service as rates_service
from app.domains.redeem import service as redeem_service
from app.domains.wishlist import service as wishlist_service


async def list_accounts() -> dict:
    """绑定 Steam 账号清单（名称/好友码/主账号/拥有统计）——只读账号面，无任何凭据。"""
    accounts = await account_service.list_accounts()
    rows = [
        {
            "k": a.get("persona_name") or a.get("steam_id") or "—",
            "vKey": "acctPrimary" if a.get("is_primary") else "acctActive",
            "data": {
                "friend": a.get("friend_code") or "",
                "wish": a.get("wishlist_count", 0),
                "owned": a.get("game_count", 0),
            },
            "tone": "ok" if a.get("is_primary") else None,
        }
        for a in accounts
    ]
    return _shared.rows_result("accounts", rows, total=len(rows))


async def list_wishlist() -> dict:
    """账户愿望单游戏（监控池内 wishlisted 来源，库内快照）。"""
    items = await wishlist_service.list_items()
    ids = [int(it["appid"]) for it in items if it.get("wishlisted") and it.get("appid")]
    briefs = await games_service.briefs_for(ids)
    return _shared.games_card("wishlist", [_shared.brief_item(i, briefs.get(i)) for i in ids], total=len(ids))


async def list_owned() -> dict:
    """账户已拥有游戏（监控池内 owned 来源，带国区现价）。"""
    items = await wishlist_service.list_items()
    ids = [int(it["appid"]) for it in items if it.get("owned") and it.get("appid")]
    briefs = await games_service.briefs_for(ids)
    return _shared.games_card("owned", [_shared.brief_item(i, briefs.get(i)) for i in ids], total=len(ids))


async def achievements_summary() -> dict:
    """奖杯概览（本地快照）：KPI + 白金陈列 + 最近解锁（成就域同一份汇总）。"""
    s = await achievements_service.get_summary(None)
    return {
        "kind": "achievements",
        "hasCredential": bool(s.get("hasCredential")),
        "platinum": int(s.get("platinum") or 0),
        "unlocked": int(s.get("unlockedAchievements") or 0),
        "total": int(s.get("totalAchievements") or 0),
        "completionRate": s.get("completionRate"),
        "lastSyncedAt": s.get("lastSyncedAt"),
        "platinums": [
            {"appid": int(p["appid"]), "name": p.get("name") or None}
            for p in (s.get("platinums") or [])[:4] if p.get("appid")
        ],
        "recent": [
            {"appid": int(a["appid"]), "name": a.get("name") or "",
             "gameName": a.get("gameName") or None, "at": a.get("unlockTime")}
            for a in (s.get("recentUnlocks") or [])[:4] if a.get("appid")
        ],
    }


async def level_profile(steam_id: str | None = None) -> dict:
    """猎手等级读数（等级/段位/累计经验/下一级所需/连猎加成 + 最近解锁）。"""
    p = await achievements_level.level_profile(steam_id or None)
    bonus = (p.get("activeBonuses") or [{}])[0]
    unlocks = (await achievements_social.recent_unlocks(steam_id or None, 4)).get("items") or []
    rows = [
        {"k": "", "vKey": "levelNow",
         "data": {"level": p.get("level"), "tier": p.get("tier"), "totalXp": p.get("totalXp")},
         "tone": "ok"},
        {"k": "", "vKey": "levelNext",
         "data": {"into": p.get("xpIntoLevel"), "next": p.get("xpForNextLevel")}},
    ]
    if bonus.get("multiplier") and bonus["multiplier"] > 1.0:
        rows.append({"k": "", "vKey": "levelBonus",
                     "data": {"days": bonus.get("streakDays"),
                              "percent": round((bonus["multiplier"] - 1) * 100)},
                     "tone": "ok"})
    for u in unlocks:
        rows.append({"k": u.get("name") or "", "vKey": "levelUnlock",
                     "data": {"gameName": u.get("gameName") or ""}})
    out = _shared.rows_result("levelProfile", rows, total=len(rows))
    out.update({
        "level": p.get("level"),
        "tier": p.get("tier"),
        "totalXp": p.get("totalXp"),
        "xpForNextLevel": p.get("xpForNextLevel"),
        "streakDays": int(bonus.get("streakDays") or 0),
        "activeBonusMultiplier": float(bonus.get("multiplier") or 1.0),
        "recentUnlocks": [
            {"name": u.get("name") or "", "gameName": u.get("gameName") or "",
             "at": u.get("unlockTime")}
            for u in unlocks
        ],
    })
    return out


async def challenges_overview(steam_id: str | None = None) -> dict:
    """挑战进度读数（已完成/可领取/最接近完成的进行中项，挑战目录同源）。"""
    data = await achievements_challenges.challenges_overview(steam_id or None)
    summary = data.get("summary") or {}
    near = sorted(
        (c for c in (data.get("challenges") or [])
         if c.get("status") == "active" and int(c.get("target") or 0) > 0),
        key=lambda c: int(c.get("progress") or 0) / int(c.get("target") or 1),
        reverse=True,
    )[:3]
    near_complete = [
        {"challengeId": c.get("challengeId"), "progress": int(c.get("progress") or 0),
         "target": int(c.get("target") or 0), "rewardXp": int(c.get("rewardXp") or 0)}
        for c in near
    ]
    rows = [{"k": "", "vKey": "challengeSummary",
             "data": {"completed": int(summary.get("completed") or 0),
                      "claimable": int(summary.get("claimable") or 0),
                      "total": int(summary.get("total") or 0)},
             "tone": "ok" if summary.get("claimable") else None}]
    for c in near_complete:
        rows.append({"k": "", "vKey": "challengeNear",
                     "data": {"challengeId": c["challengeId"], "progress": c["progress"],
                              "target": c["target"], "rewardXp": c["rewardXp"]}})
    out = _shared.rows_result("challengesOverview", rows, total=len(rows))
    out.update({
        "completed": int(summary.get("completed") or 0),
        "claimable": int(summary.get("claimable") or 0),
        "nearComplete": near_complete,
    })
    return out


async def list_family_library() -> dict:
    """家庭共享库（family 域快照缓存，同一份前端家庭页数据源）。"""
    lib = await family_service.cached_family_library(None)
    games = lib.get("games") or []
    ids = [int(g["appid"]) for g in games if g.get("appid")][: _shared._READ_ROWS_LIMIT]
    briefs = await games_service.briefs_for(ids)
    return _shared.games_card("famLibrary", [_shared.brief_item(i, briefs.get(i)) for i in ids], total=len(games))


async def bills_summary() -> dict:
    """账单导入记录与消费合计（本地账本）。"""
    imports = await bills_service.list_imports()
    rows = [
        {
            "k": im.get("nickname") or im.get("sourceFile") or f"#{im.get('id')}",
            "vKey": "billSpend",
            "data": {"spendFen": im.get("gameSpendFen")},
            "v": f"{im.get('orders')}单" if im.get("orders") is not None else "",
            "at": im.get("importedAt"),
            "tone": None,
        }
        for im in imports
    ]
    return _shared.rows_result("bills", rows, total=len(rows))


async def family_status() -> dict:
    """家庭组状态（成员头像/角色/地区 + 钱包区，family 域同一 payload）。"""
    st = await family_service.get_status()
    members = []
    for g in st.get("groups") or []:
        for m in g.get("members") or []:
            sid = str(m.get("steamid") or "")
            if not sid or any(x["steamid"] == sid for x in members):
                continue
            members.append({
                "steamid": sid,
                "name": m.get("personaName") or None,
                "avatar": m.get("avatarUrl") or None,
                "role": "primary" if str(m.get("role") or "") == "primary" else "member",
                "region": m.get("region") or None,
            })
    if not st.get("bound") or not members:
        return {"kind": "family", "bound": False, "members": []}
    return {
        "kind": "family",
        "bound": True,
        "joined": bool(st.get("joined")),
        "walletRegion": st.get("walletRegion") or None,
        "members": members[:_shared._READ_ROWS_LIMIT],
        "total": len(members),
    }


async def redeem_quota() -> dict:
    """CDK 激活额度（账号维度 used/limit，只读前提状态）。"""
    q = await redeem_service.quota_status()
    if not q.get("hasCookie"):
        return _shared.rows_result("redeem", [{"k": "", "vKey": "redeemNoCookie", "tone": "warn"}])
    rows = [{"k": "", "vKey": "redeemQuota",
             "data": {"used": q.get("used", 0), "limit": q.get("limit", 0)},
             "tone": "ok" if q.get("used", 0) < q.get("limit", 0) else "warn"}]
    return _shared.rows_result("redeem", rows)


async def sync_wallet() -> dict:
    """立即抓取主账号钱包余额（账户域同一执行入口）。"""
    return _shared.sync_result("wallet", await account_service.sync_wallet(force=True),
                               done_key="walletSynced", fail_key="walletFailed")


async def sync_library(steam_id: str | None = None) -> dict:
    """同步账户愿望单 / 拥有库（wishlist 域账户同步同一执行入口）。"""
    return _shared.sync_result("library", await wishlist_service.sync_account(steam_id))


async def sync_achievements(steam_id: str | None = None) -> dict:
    """发起成就后台同步（进行中返回已在跑，不叠加）。"""
    try:
        result = await achievements_service.start_sync(steam_id or None)
    except ValueError:
        return _shared.rows_result("achievementsSync", [{"k": "", "vKey": "syncRunning", "tone": "warn"}])
    return _shared.sync_result("achievementsSync", result)


async def sync_bills(*, full: bool = False) -> dict:
    """同步 Steam 账单（默认增量探测；full=True 全量翻页，耗时数分钟）。"""
    result = await bills_service.sync_bills(force=full)
    if isinstance(result, dict) and result.get("status") == "no_cookie":
        return _shared.rows_result("billsSync", [{"k": "", "vKey": "billNoCookie", "tone": "warn"}])
    return _shared.sync_result("billsSync", result)


async def sync_family(steam_id: str | None = None) -> dict:
    """家庭组同步（默认遍历全部绑定账号，每账号独立成败）。"""
    return _shared.sync_result("familySync", await family_service.sync_family_group(steam_id))


async def refresh_rates() -> dict:
    """刷新汇率（带重试与缓存回落，零手工配置）。"""
    return _shared.sync_result("rates", await rates_service.refresh_rates())


async def _list_accounts(args: dict, sid: str | None = None) -> dict:
    return await list_accounts()


async def _list_wishlist(args: dict, sid: str | None = None) -> dict:
    return await list_wishlist()


async def _list_owned(args: dict, sid: str | None = None) -> dict:
    return await list_owned()


async def _achievements_summary(args: dict, sid: str | None = None) -> dict:
    return await achievements_summary()


async def _level_profile(args: dict, sid: str | None = None) -> dict:
    return await level_profile(args.get("steam_id") or None)


async def _challenges_overview(args: dict, sid: str | None = None) -> dict:
    return await challenges_overview(args.get("steam_id") or None)


async def _list_family_library(args: dict, sid: str | None = None) -> dict:
    return await list_family_library()


async def _bills_summary(args: dict, sid: str | None = None) -> dict:
    return await bills_summary()


async def _family_status(args: dict, sid: str | None = None) -> dict:
    return await family_status()


async def _redeem_quota(args: dict, sid: str | None = None) -> dict:
    return await redeem_quota()


async def _sync_wallet(args: dict, sid: str | None = None) -> dict:
    return await sync_wallet()


async def _sync_library(args: dict, sid: str | None = None) -> dict:
    return await sync_library(args.get("steam_id") or None)


async def _sync_achievements(args: dict, sid: str | None = None) -> dict:
    return await sync_achievements(args.get("steam_id") or None)


async def _sync_bills(args: dict, sid: str | None = None) -> dict:
    return await sync_bills(full=bool(args.get("full", False)))


async def _sync_family(args: dict, sid: str | None = None) -> dict:
    return await sync_family(args.get("steam_id") or None)


async def _refresh_rates(args: dict, sid: str | None = None) -> dict:
    return await refresh_rates()


SPECS = [
    ToolSpec(
        name="list_accounts", group="read", risk="low",
        description="列出绑定的 Steam 账号（名称、主账号、愿望单/拥有统计）。用户问『我绑定了哪些账号』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_accounts, step_label="accounts",
    ),
    ToolSpec(
        name="list_wishlist", group="read", risk="low",
        description="列出账户愿望单里的游戏。用户问『我愿望单里有什么』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_wishlist, step_label="wishlist",
    ),
    ToolSpec(
        name="list_owned", group="read", risk="low",
        description="列出账户已拥有的游戏（带国区现价）。用户问『我拥有哪些游戏』『我库里有什么』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_owned, step_label="owned",
    ),
    ToolSpec(
        name="list_family_library", group="read", risk="low",
        description="列出 Steam 家庭组的共享库游戏（家庭里所有成员共享的游戏，带国区现价）。"
                    "用户问『家庭库/家庭共享有哪些游戏』『家里能玩什么』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_list_family_library, step_label="famLibrary",
    ),
    ToolSpec(
        name="achievements_summary", group="read", risk="low",
        description="查看奖杯概览（白金数、解锁进度、白金陈列、最近解锁）。用户问『我的成就/奖杯进度』『最近解了什么成就』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_achievements_summary, step_label="achievements",
    ),
    ToolSpec(
        name="level_profile", group="read", risk="low",
        description="查看猎手等级与经验进度（等级、段位、累计经验、下一级还差多少、连猎加成、最近解锁）。用户问『我几级了』『经验/等级进度怎么样』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
        }},
        handler=_level_profile, step_label="levelProfile",
    ),
    ToolSpec(
        name="challenges_overview", group="read", risk="low",
        description="查看挑战进度（已完成数、可领取数、最接近完成的挑战）。用户问『挑战进度』『有没有能领的挑战奖励』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
        }},
        handler=_challenges_overview, step_label="challengesOverview",
    ),
    ToolSpec(
        name="bills_summary", group="read", risk="low",
        description="列出已导入的账单记录与消费合计。用户问『我花了多少钱/账单』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_bills_summary, step_label="bills",
    ),
    ToolSpec(
        name="family_status", group="read", risk="low",
        description="查看 Steam 家庭组状态（组、成员、钱包区）。用户问『我的家庭组』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_family_status, step_label="family",
    ),
    ToolSpec(
        name="redeem_quota", group="read", risk="low",
        description="查看 CDK 激活额度（已用/上限）。用户问『还能激活几次』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_redeem_quota, step_label="redeem",
    ),
    ToolSpec(
        name="sync_wallet", group="read", risk="medium",
        description="立即抓取主账号钱包余额。用户说『同步一下钱包』『看看余额对不对』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_sync_wallet, step_label="syncWallet",
    ),
    ToolSpec(
        name="sync_library", group="read", risk="medium",
        description="同步账户愿望单与拥有库（从 Steam 拉取最新）。用户说『同步我的愿望单/游戏库』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省同步全部绑定账号"}
        }},
        handler=_sync_library, step_label="syncLibrary",
    ),
    ToolSpec(
        name="sync_achievements", group="read", risk="medium",
        description="发起成就后台同步（进行中会提示已在跑）。用户说『同步成就』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
        }},
        handler=_sync_achievements, step_label="syncAchievements",
    ),
    ToolSpec(
        name="sync_bills", group="read", risk="medium",
        description="同步 Steam 账单（默认增量探测，很快；full=true 全量翻页需数分钟）。用户说『同步账单』时调用",
        parameters={"type": "object", "properties": {
            "full": {"type": "boolean", "description": "true=全量重拉，缺省增量"}
        }},
        handler=_sync_bills, step_label="syncBills",
    ),
    ToolSpec(
        name="sync_family", group="read", risk="medium",
        description="同步 Steam 家庭组。用户说『同步家庭组』时调用",
        parameters={"type": "object", "properties": {
            "steam_id": {"type": "string", "description": "账号 SteamID；缺省遍历全部绑定账号"}
        }},
        handler=_sync_family, step_label="syncFamily",
    ),
    ToolSpec(
        name="refresh_rates", group="read", risk="medium",
        description="刷新各币种汇率。用户说『刷新汇率』『汇率好像不对』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_refresh_rates, step_label="refreshRates",
    ),
]
