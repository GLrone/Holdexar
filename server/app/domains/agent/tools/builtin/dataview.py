"""数据查看类工具：游戏详情 / 历史价格 / 版本全区价 / 捆绑包 / 汇率 / 区服配置。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.agent.tools.builtin.prices import _trend_series
from app.domains.bundles import service as bundles_service
from app.domains.games import service as games_service
from app.domains.monitoring import service as monitoring_service
from app.domains.rates import service as rates_service
from app.domains.regions import service as regions_service

_BUNDLE_MEMBERS_LIMIT = 30   # bundle_detail 成员截断线（超出只给 total）
_RATES_MAX_POINTS = 72       # 汇率走势抽样上限（与价格走势同量级）


async def game_detail(appid: int) -> dict:
    """游戏完整详情（瘦身版）：名称/评测/标签/价格摘要/史低旗标/关注状态。

    get_game_detail 的大数组（价格矩阵/版本列表/关联捆绑包）压成计数与
    摘要；逐区现价走 get_region_prices，版本与捆绑包各有专工具。"""
    detail = await games_service.get_game_detail(appid)
    if detail is None:
        return {"kind": "empty", "note": "no_game"}
    tracked = appid in set(await monitoring_service.active_ids("game"))
    tags = [t.get("name") or t.get("nameEn") for t in (detail.get("tags") or [])]
    return {
        "kind": "game_detail",
        "appid": appid,
        "name": detail.get("name"),
        "nameEn": detail.get("nameEn"),
        "releaseDate": detail.get("releaseDate"),
        "reviewCount": detail.get("reviewCount"),
        "positiveRate": detail.get("positiveRate"),
        "chineseSupport": detail.get("chineseSupport"),
        "tags": [t for t in tags if t][:10],
        "cn": {
            "cnyFen": detail.get("cnCnyFen"),
            "discount": detail.get("cnDiscount") or 0,
            "discountEndsAt": detail.get("cnDiscountEndsAt"),
        },
        "priceRegionCount": len(detail.get("priceMatrix") or {}),
        "lowest": {
            "region": detail.get("lowestRegionCode") or None,
            "cnyFen": detail.get("lowestCnyFen"),
            "savingsFen": detail.get("savingsFen") or 0,
        },
        "hlFlag": detail.get("hlFlag") or 0,
        "freeKind": detail.get("freeKind"),
        "tracked": tracked,
        "versionCount": len(detail.get("versions") or []),
        "linkedBundleCount": len(detail.get("linkedBundles") or []),
        "removedAt": detail.get("removedAt"),
    }


async def game_price_history(
    appid: int, region: str = "cn", days: int = 365, sub_id: int | None = None
) -> dict:
    """历史价格走势：get_game_history 全量点位 → _trend_series 抽样（同日取末值、
    保变化点/最低点/末点，上限 72 点）；窗口内极值与版本清单原样透传。"""
    hist = await games_service.get_game_history(appid, region=region, days=days, sub_id=sub_id)
    points = hist.get("points") or []
    if not points:
        return {"kind": "empty", "note": "no_data", "region": region}
    return {
        "kind": "price_history",
        "appid": appid,
        "region": hist.get("region") or region,
        "subId": sub_id,
        "days": days,
        "points": _trend_series(points),
        "lowest": hist.get("lowest"),
        "highest": hist.get("highest"),
        "count": hist.get("count") or len(points),
        "versions": hist.get("versions") or [],
    }


async def game_versions(appid: int) -> dict:
    """全版本 × 全区最新价：get_game_versions 透传，逐区压成 原币价/折扣/折合。"""
    data = await games_service.get_game_versions(appid)
    raw = data.get("versions") or []
    if not raw:
        return {"kind": "empty", "note": "no_data"}
    versions = [
        {
            "subId": v.get("subId"),
            "suffix": v.get("suffix") or "",
            "isGold": bool(v.get("isGold")),
            "regions": {
                code: {
                    "price": r.get("formatted"),
                    "discount": r.get("discount") or 0,
                    "cnyFen": r.get("cnyFen"),
                }
                for code, r in (v.get("regions") or {}).items()
            },
        }
        for v in raw
    ]
    return {"kind": "game_versions", "appid": appid, "versions": versions}


async def linked_bundles(appid: int) -> dict:
    """游戏关联捆绑包：games_service.linked_bundles 直调，剥离封面图。"""
    items = await games_service.linked_bundles(appid)
    if not items:
        return {"kind": "empty", "note": "no_data"}
    slim = [
        {k: it.get(k) for k in (
            "bundleId", "name", "url", "itemKind",
            "priceCny", "lowestRegion", "lowestPriceFen", "diffFen",
        )}
        for it in items
    ]
    return {
        "kind": "bundles",
        "appid": appid,
        "items": slim[:_shared._READ_ROWS_LIMIT],
        "total": len(slim),
    }


async def bundle_detail(bundle_id: int) -> dict:
    """捆绑包详情：各区包价 + 包内成员（各区现价）。成员超上限截断给 total；
    区域价表剥离逐区 appIds（成员清单已单独给全量计数）。"""
    d = await bundles_service.get_bundle_detail(bundle_id)
    if d is None:
        return {"kind": "empty", "note": "no_bundle"}
    region_prices = {
        code: {k: v for k, v in rp.items() if k != "appIds"}
        for code, rp in (d.get("regionPrices") or {}).items()
    }
    members_all = d.get("games") or []
    members = [
        {"appid": m.get("appid"), "name": m.get("name"), "prices": m.get("prices") or {}}
        for m in members_all[:_BUNDLE_MEMBERS_LIMIT]
    ]
    return {
        "kind": "bundle_detail",
        "bundleId": d.get("bundleId"),
        "name": d.get("name"),
        "url": d.get("url"),
        "itemKind": d.get("itemKind"),
        "mustPurchaseAsSet": d.get("mustPurchaseAsSet"),
        "regionPrices": region_prices,
        "cnCnyFen": d.get("cnCnyFen"),
        "lowest": {"region": d.get("lowestRegion") or None, "cnyFen": d.get("lowestCnyFen")},
        "diffFen": d.get("diffFen") or 0,
        "members": members,
        "memberTotal": len(members_all),
    }


async def rates_history(currency: str = "USD", days: int = 90) -> dict:
    """汇率走势：rate_history 取尾部 days 天日线，等距抽样到上限（保末点），
    极值/末值按抽样前全量行计算。"""
    cur = str(currency or "USD").strip().upper() or "USD"
    window = max(int(days or 0), 1)
    rows = await rates_service.rate_history(cur, limit=window)
    seq = [
        [r["date"], r["rateToCny"]]
        for r in rows
        if isinstance(r.get("rateToCny"), (int, float))
    ]
    if not seq:
        return {"kind": "empty", "note": "no_data", "currency": cur}
    values = [p[1] for p in seq]
    if len(seq) > _RATES_MAX_POINTS:
        stride = len(seq) / _RATES_MAX_POINTS
        keep = {int(i * stride) for i in range(_RATES_MAX_POINTS)}
        keep.add(len(seq) - 1)
        seq = [seq[i] for i in sorted(keep)]
    return {
        "kind": "rates_history",
        "currency": cur,
        "days": window,
        "points": seq,
        "min": min(values),
        "max": max(values),
        "last": values[-1],
        "count": len(rows),
    }


async def regions_config() -> dict:
    """区服配置：全量区服（含启用标记）+ 已购抓取区。剥离排序等展示字段。"""
    rows = await regions_service.list_regions()
    regions = [
        {
            "code": r.get("code"),
            "name": r.get("name"),
            "currency": r.get("currency"),
            "enabled": bool(r.get("enabled")),
        }
        for r in rows
    ]
    return {
        "kind": "regions_config",
        "regions": regions,
        "enabledCount": sum(1 for r in regions if r["enabled"]),
        # None = 已购抓取跟随启用集（未单独配置）
        "ownedRegions": await regions_service.owned_regions(),
    }


async def _game_detail(args: dict, sid: str | None = None) -> dict:
    return await game_detail(int(args.get("appid") or 0))


async def _game_price_history(args: dict, sid: str | None = None) -> dict:
    sub_id = args.get("sub_id")
    return await game_price_history(
        int(args.get("appid") or 0),
        region=str(args.get("region") or "cn").strip().lower(),
        days=int(args.get("days") or 365),
        sub_id=int(sub_id) if sub_id else None,
    )


async def _game_versions(args: dict, sid: str | None = None) -> dict:
    return await game_versions(int(args.get("appid") or 0))


async def _linked_bundles(args: dict, sid: str | None = None) -> dict:
    return await linked_bundles(int(args.get("appid") or 0))


async def _bundle_detail(args: dict, sid: str | None = None) -> dict:
    return await bundle_detail(int(args.get("bundle_id") or 0))


async def _rates_history(args: dict, sid: str | None = None) -> dict:
    return await rates_history(
        currency=str(args.get("currency") or "USD"),
        days=int(args.get("days") or 90),
    )


async def _regions_config(args: dict, sid: str | None = None) -> dict:
    return await regions_config()


SPECS = [
    ToolSpec(
        name="game_detail", group="read", risk="low",
        description="取某游戏的完整档案：名称、评测与好评率、标签、国区现价与折扣、"
                    "最低价区、史低旗标、是否在关注中。用户问『这游戏什么情况/什么类型/"
                    "值得买吗』时调用；逐区现价明细用 get_region_prices",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_game_detail, step_label="gameDetail",
    ),
    ToolSpec(
        name="game_price_history", group="read", risk="low",
        description="取某游戏的历史价格走势（默认国区近一年，可换区、换时间窗、指定版本），"
                    "点位已抽样并附区间最高最低。用户问『以前卖多少钱/什么时候史低过/"
                    "现在算便宜吗』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
            "region": {"type": "string", "description": "区域代码（如 cn/us/ru），默认国区 cn"},
            "days": {"type": "integer", "description": "回看天数，默认 365"},
            "sub_id": {"type": "integer", "description": "版本 sub 包 ID，缺省为标准版序列"},
        }, "required": ["appid"]},
        handler=_game_price_history, step_label="priceHistory",
    ),
    ToolSpec(
        name="game_versions", group="read", risk="low",
        description="取某游戏全部版本（标准版/豪华版/Gold 等）在各区的最新价格。"
                    "用户问『豪华版多少钱/黄金版和标准版差多少』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_game_versions, step_label="gameVersions",
    ),
    ToolSpec(
        name="linked_bundles", group="read", risk="low",
        description="取某游戏出现在哪些捆绑包里，及各包国区价、最低区与差价。"
                    "用户问『有没有捆绑包更划算/这游戏和谁打包卖』时调用",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_linked_bundles, step_label="linkedBundles",
    ),
    ToolSpec(
        name="bundle_detail", group="read", risk="low",
        description="取捆绑包详情：包内游戏清单与各区现价、各区包价折合人民币、最低区与差价。"
                    "用户问『这个捆绑包值不值/里面有什么』时调用；成员超过 30 个只列前 30 个",
        parameters={"type": "object", "properties": {
            "bundle_id": {"type": "integer", "description": "捆绑包 ID"},
        }, "required": ["bundle_id"]},
        handler=_bundle_detail, step_label="bundleDetail",
    ),
    ToolSpec(
        name="rates_history", group="read", risk="low",
        description="取某币种对人民币的汇率走势（默认美元近 90 天），点位已抽样。"
                    "用户问『为什么外区折算价变了/汇率现在多少』时调用",
        parameters={"type": "object", "properties": {
            "currency": {"type": "string", "description": "币种代码（如 USD/JPY/RUB），默认 USD"},
            "days": {"type": "integer", "description": "回看天数，默认 90"},
        }},
        handler=_rates_history, step_label="ratesHistory",
    ),
    ToolSpec(
        name="regions_config", group="read", risk="low",
        description="读取区服配置：全部区服、哪些已启用抓取、已购游戏抓取区设置。"
                    "用户问『现在抓哪几个区/能看哪里的价』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_regions_config, step_label="regionsConfig",
    ),
]
