"""games 域路由：列表 / 详情 / 历史价格 / CDK 第三方平台。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.crawler import cdk_fetcher

from . import service

router = APIRouter(prefix="/games", tags=["games"])


class PriceContextPair(BaseModel):
    appid: int
    date: str


class PriceContextBatchRequest(BaseModel):
    items: list[PriceContextPair]


@router.get("")
async def list_games(
    sort: str = Query("default"),
    limit: int = Query(40, ge=1, le=100),
    after: str | None = None,
    q: str | None = Query(None, max_length=200),
    region: str = Query("", max_length=5),
    filterMode: str = Query("global"),
    onlyDiscounted: bool = False,
    isLowest: bool = False,
    flag: str = Query("", pattern="^(|hl|pp|any)$"),
    minRating: int = Query(0, ge=0, le=100),
    maxRating: int | None = Query(None, ge=0, le=100),
    minReviews: int = Query(0, ge=0),
    maxReviews: int | None = Query(None, ge=0),
    minPrice: int | None = None,
    maxPrice: int | None = None,
    onlyHb: bool = False,
    onlyEpic: bool = False,
    onlyXgp: bool = False,
    hideOwned: bool = False,
    diffMin: int | None = None,
    diffMax: int | None = None,
    diffType: str = Query("absolute", pattern="^(absolute|percent)$"),
    toleranceFen: int | None = Query(None, ge=0),
    strictLowest: bool = False,
    excludeDlc: bool = Query(False),
):
    return await service.list_games(
        sort=sort,
        limit=limit,
        after=after,
        q=q,
        region=region,
        filter_mode=filterMode,
        only_discounted=onlyDiscounted,
        is_lowest=isLowest,
        flag=flag,
        min_rating=minRating,
        max_rating=maxRating,
        min_reviews=minReviews,
        max_reviews=maxReviews,
        min_price=minPrice,
        max_price=maxPrice,
        only_hb=onlyHb,
        only_epic=onlyEpic,
        only_xgp=onlyXgp,
        hide_owned=hideOwned,
        diff_min_fen=diffMin,
        diff_max_fen=diffMax,
        diff_type=diffType,
        tolerance_fen=toleranceFen,
        strict_lowest=strictLowest,
        exclude_dlc=excludeDlc,
    )


@router.get("/top100")
async def games_top100():
    """Steam 热销 TOP100 appid 列表。

    拉取完全失败（无缓存无 stale）返回 502（FETCH_FAILED 语义）。
    """
    from app.domains.games import boards as boards_mod

    appids = await boards_mod.get_board("topsellers")
    if appids:
        return {"appIds": [str(a) for a in appids], "cached": True}
    raise HTTPException(status_code=502, detail="FETCH_FAILED")


@router.get("/boards/{key}")
async def games_board(key: str):
    """榜单发现源原始 appid 清单（topsellers / popularnew / specials）。

    调试/观测入口：看各板拉到多少条、缓存命中情况；未知 key 404。
    """
    from app.domains.games import boards as boards_mod

    if key not in boards_mod.BOARDS:
        raise HTTPException(status_code=404, detail=f"未知榜单源: {key}")
    appids = await boards_mod.get_board(key)
    if appids:
        return {"board": key, "appIds": [str(a) for a in appids], "count": len(appids)}
    raise HTTPException(status_code=502, detail="FETCH_FAILED")


@router.get("/{appid}/history")
async def game_history(
    appid: int,
    region: str = Query("cn", max_length=5),
    days: int = Query(0, ge=0, le=3650),
    subId: int | None = Query(None, ge=1),
):
    """历史价格走势。subId 缺省=标准版；days=0=全部时间。"""
    return await service.get_game_history(appid, region=region, days=days, sub_id=subId)


@router.get("/{appid}/price-context")
async def game_price_context(
    appid: int,
    date: str = Query(..., min_length=10, max_length=10),
    region: str = Query("cn", max_length=5),
):
    """截至 date 的价格上下文（账单许可证命中条）：当时价 + 历史最低。"""
    return await service.get_price_context(appid, date=date, region=region)


@router.post("/price-context-batch")
async def game_price_context_batch(req: PriceContextBatchRequest):
    """批量截至日价格上下文（账单消费明细命中条，展开行时补拉）。"""
    return await service.get_price_context_batch([(i.appid, i.date) for i in req.items])


@router.get("/{appid}/versions")
async def game_versions(appid: int):
    """全版本 × 全区最新价（走势抽屉「全部版本」区块）。"""
    return await service.get_game_versions(appid)


@router.get("/{appid}/bundles")
async def game_bundles(appid: int):
    """游戏关联捆绑包（GPW「关联捆绑包」区块数据源）。"""
    return {"bundles": await service.linked_bundles(appid)}


@router.get("/{appid}/cdk")
async def game_cdk(appid: int):
    """第三方平台 (SteamPY / SteamCICI) CDK 状态——跨域查价的终端化承接。"""
    return await cdk_fetcher.fetch_cdk(appid)


@router.get("/{appid}")
async def game_detail(appid: int):
    detail = await service.get_game_detail(appid)
    if detail is None:
        raise HTTPException(status_code=404, detail="游戏不存在")
    return detail


@router.post("/{appid}/retry-removed")
async def retry_removed(appid: int):
    """下架游戏手动复探：清标 + 后台重爬（误判自愈 / 重新上架复活通道）。"""
    try:
        return await service.retry_removed_game(appid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
